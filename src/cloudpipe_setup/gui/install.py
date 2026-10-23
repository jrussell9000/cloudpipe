"""The setup page's install step when the runner is on (`cloudpipe-gui --allow-install`).

Layout only: what may run, and how, is `runner.py`. A tab polls the server's
runner twice a second rather than subscribing to it, so a tab that closes leaves
nothing behind to unsubscribe, and a tab opened mid-run catches up from the
runner's buffer on its first tick (design D3 of
`openspec/changes/archive/2026-10-22-run-installer-phases-from-the-setup-page/`).
"""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any

from nicegui import ui

from . import model, runner

#: What each outcome means to the deployer, keyed by `runner.outcome`'s values.
OUTCOME_TEXT = {
    "complete": "Finished. Every phase in this run completed.",
    "blocked": (
        "Waiting on you. The installer stopped because something only you can do is still "
        "outstanding; its message in the output below says what. Do it, then continue."
    ),
    "check_failed": (
        "A check failed. The installer's message in the output below says which one and why. Fix it, then "
        "continue."
    ),
    "invalid": (
        "The installer refused its input before changing anything. Its message in the output below says why."
    ),
    "error": (
        "The run failed. The installer's message in the output below shows the error, and names the log "
        "with Terraform's full output. If your AWS sign-in expired, run `aws sso login` in any "
        "terminal: this page picks up the new session without being restarted."
    ),
    "cancelled": (
        "Cancelled. The installer was interrupted once, as Ctrl-C would, so Terraform could "
        "finish the change it was making."
    ),
}

OUTCOME_COLOURS = {
    "complete": "positive",
    "blocked": "info",
    "check_failed": "warning",
    "invalid": "warning",
    "error": "negative",
    "cancelled": "grey",
}


def exit_description(code: int) -> str:
    """An exit code as a deployer reads it. Python reports death by a signal as minus its number."""
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        return f"interrupted by {name}"
    return f"exit code {code}"


class InstallPanel:
    """One tab's view of the server's runner for this root."""

    def __init__(self, root: Path, install_runner: runner.Runner) -> None:
        self.root = root
        self.runner = install_runner
        self.listed: list[dict[str, Any]] = install_runner.listed()
        self.titles = {int(p["id"]): p["title"] for p in self.listed}
        #: The phases after which the public endpoint is closed: the ones that
        #: need the WARP acknowledgement (D5). Read from the installer's own list.
        self.closing = {int(p["id"]) for p in self.listed if not p["endpoint_public_access"]}
        self.run_buttons: list[ui.button] = []
        self.seen = 0
        self.shown_run: runner.Run | None = None
        self.shown_state: tuple | None = None

    def build(self) -> None:
        ui.markdown(
            "This page can run the installer for you. Each run asks you to confirm first, and "
            "each phase checks its own preconditions and stops if one is not met. A first "
            "install is best run one phase at a time. You can close this tab during a run: the "
            "run continues, and reopening the page shows it. Stop the page itself with Ctrl-C in "
            "its terminal, which interrupts a run safely."
        )
        for phase in self.listed:
            pid = int(phase["id"])
            with ui.card().classes("w-full"):
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.badge(f"Phase {pid}")
                    ui.label(phase["title"]).classes("font-medium")
                    ui.space()
                    self.run_buttons.append(
                        ui.button(
                            f"Run phase {pid}", on_click=lambda p=pid: self.confirm(phase=p)
                        ).props("outline")
                    )
                if pid in self.closing:
                    ui.label(
                        "After this phase, the cluster can be reached only through WARP."
                    ).classes("text-xs text-grey-7")
                ui.code(model.phase_command(self.root, pid), language="bash").classes("w-full")
        with ui.row().classes("gap-2 mt-2"):
            self.run_buttons.append(ui.button("Run every phase", on_click=lambda: self.confirm()))

        self.status = ui.column().classes("w-full mt-2")
        self.output = ui.log(max_lines=runner.BUFFER_LINES).classes("w-full h-96 text-xs")
        ui.timer(0.5, self.refresh)

    # --- starting a run ----------------------------------------------------------

    def confirm(self, *, phase: int | None = None, from_phase: int | None = None) -> None:
        """The dialog every run goes through (D5). Nothing starts unless Start is pressed."""
        phases = self.runner.phases_for(phase=phase, from_phase=from_phase)
        closes = bool(self.closing & set(phases))
        # Persistent, so only its own two buttons close it, and both delete it: a
        # closed dialog left in the page keeps a Start button for an old choice.
        with ui.dialog().props("persistent") as dialog, ui.card().classes("max-w-xl"):
            heading = (
                f"Run phase {phases[0]}?"
                if len(phases) == 1
                else f"Run phases {phases[0]} to {phases[-1]}?"
            )
            ui.label(heading).classes("text-lg font-medium")
            for pid in phases:
                ui.label(f"Phase {pid}: {self.titles[pid]}").classes("text-sm")
            ui.label(
                "This creates or changes resources in your AWS and Cloudflare accounts."
            ).classes("text-sm mt-2")
            if any(pid >= 4 for pid in phases):
                token = "is set" if os.environ.get("CLOUDFLARE_API_TOKEN") else "is not set"
                ui.label(
                    f"CLOUDFLARE_API_TOKEN {token} in the terminal that started this page. Phase 4 "
                    "and later need it."
                ).classes("text-sm")
            if closes:
                ui.label(
                    "This run closes the public Kubernetes endpoint. Afterwards, the cluster can be "
                    "reached only through WARP. The installer checks that a sign-in account "
                    "exists; it cannot check that someone has used it."
                ).classes("text-sm text-orange-10 mt-2")
                # `start` is looked up when the box is ticked, after it exists below.
                ui.checkbox(
                    "At least one operator has signed in over WARP and can reach the cluster that "
                    "way.",
                    on_change=lambda e: start.set_enabled(bool(e.value)),
                ).mark("warp-ack")
            with ui.row().classes("gap-2 mt-2"):
                start = ui.button(
                    "Start", on_click=lambda: self.start(dialog, phase, from_phase)
                ).mark("start-run")
                ui.button("Cancel", on_click=lambda: self.dismiss(dialog)).props("flat").mark(
                    "cancel-dialog"
                )
            if closes:
                start.disable()
        dialog.open()

    @staticmethod
    def dismiss(dialog: ui.dialog) -> None:
        dialog.close()
        dialog.delete()

    async def start(self, dialog: ui.dialog, phase: int | None, from_phase: int | None) -> None:
        self.dismiss(dialog)
        try:
            await self.runner.start(phase=phase, from_phase=from_phase)
        except (runner.Busy, ValueError) as err:
            ui.notify(str(err), type="warning")
        self.refresh()

    # --- following a run ------------------------------------------------------

    def refresh(self) -> None:
        run = self.runner.run
        if run is None:
            return
        if run is not self.shown_run:
            # A new run, or this tab's first look at one: start its output afresh.
            self.output.clear()
            self.seen = 0
            self.shown_run = run
        for line in run.unseen(self.seen):
            self.output.push(line)
        self.seen = run.emitted

        busy = not run.finished
        for button in self.run_buttons:
            button.set_enabled(not busy)
        state = (run.state, run.current_phase, run.exit_code)
        if state != self.shown_state:
            self.shown_state = state
            self.status.clear()
            with self.status:
                self.render_status(run)

    def render_status(self, run: runner.Run) -> None:
        where = f"phase {run.current_phase}" if run.current_phase else "before the first phase"
        if run.state == "running":
            with ui.row().classes("items-center gap-2"):
                ui.spinner()
                ui.label(f"Running, now in {where}.").classes("font-medium")
                ui.button("Cancel", on_click=self.runner.cancel).props("outline color=negative")
            return
        if run.state == "stopping":
            with ui.row().classes("items-center gap-2"):
                ui.spinner()
                ui.label(
                    f"Stopping in {where}. Terraform is finishing the change in progress; this "
                    "can take a few minutes."
                ).classes("font-medium")
            return
        ui.badge(run.state.replace("_", " "), color=OUTCOME_COLOURS[run.state])
        ui.markdown(OUTCOME_TEXT[run.state]).classes("text-sm")
        if run.exit_code not in (None, 0):
            ui.label(f"Stopped in {where}, {exit_description(run.exit_code)}.").classes("text-xs")
        # Shown only once the run has finished, and redrawn away when another tab
        # starts one, so it never needs disabling with the other run buttons.
        if run.state != "complete" and run.current_phase is not None:
            ui.button(
                f"Continue from phase {run.current_phase}",
                on_click=lambda p=run.current_phase: self.confirm(from_phase=p),
            )
