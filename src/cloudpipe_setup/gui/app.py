"""The setup form as a local web page. A prototype.

One page, one stepper: a step per schema section, then three that drive the CLI —
write the Terraform files, run preflight, and list the installer's phases. The
page is served on 127.0.0.1 only and asks for no credential. Preflight reads the
AWS SSO session and `CLOUDFLARE_API_TOKEN` from the environment the page was
started in, exactly as it does from a terminal.

By default it runs nothing that changes AWS or Cloudflare: the last step prints
the command for each phase and the deployer runs it in a terminal, which is where
design D8 of publish-phased-installer left the installer. Started with
`--allow-install`, that step can also run the phases itself (`install.py`,
`runner.py`; spec `openspec/specs/setup-page-install-runner/`).

Answers are saved after every accepted change, as the prompt saves after every
answer, so reloading the page resumes from the answers document alone.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nicegui import app, run, ui

from .. import config, prompt, schema
from ..commands.setup import STEP_ORDER, apply_defaults, display_command
from . import model, runner, wording

#: Quasar colours for a record's state. Never the only signal: every badge also
#: carries the same word the terminal report prints.
COLOURS = {"pass": "positive", "fail": "negative", "skipped": "warning", "blocked": "info"}

#: The page's words for a record's state. Not the terminal's four-character
#: marks: "WAIT" reads as "hold on", where the state means "still to do".
BADGES = {"pass": "Passed", "fail": "Failed", "skipped": "Skipped", "blocked": "To do"}


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def field_title(identifier: str) -> str:
    """A field's question, for an error about it; the identifier for one the schema lacks."""
    try:
        return schema.field(identifier).title
    except KeyError:
        return identifier


def page(
    root: Path, answers_path: Path, *, install_runner: runner.Runner | None = None
) -> Callable[[], None]:
    """The page as NiceGUI's root function, which `ui.run` and its test harness both take.

    `install_runner` is None unless the page was started with `--allow-install`,
    and then it is the server's one runner for this root, shared by every tab.
    """

    def index() -> None:
        Form(root, answers_path, install_runner=install_runner).build()

    return index


def serve(root: Path, answers_path: Path, *, port: int, show: bool, allow_install: bool) -> None:
    install_runner = runner.for_root(root) if allow_install else None
    if install_runner is not None:
        # Ctrl-C on the page's terminal stops the server; a run in progress gets
        # the same single interrupt Cancel sends, and the server waits for it.
        app.on_shutdown(install_runner.shutdown)
    # 127.0.0.1, not NiceGUI's default: this page writes files on the deployer's
    # machine and must not be reachable from anywhere else on their network.
    ui.run(
        page(root, answers_path, install_runner=install_runner),
        host="127.0.0.1",
        port=port,
        title="CloudPipe setup",
        reload=False,
        show=show,
    )


class Form:
    """One browser tab's state. Built per page load, so a reload re-reads the answers file."""

    def __init__(
        self, root: Path, answers_path: Path, *, install_runner: runner.Runner | None = None
    ) -> None:
        self.root = root
        self.install_runner = install_runner
        self.answers_path = answers_path
        loaded, _ = config.load(answers_path)
        self.answers: dict[str, Any] = config.derive(apply_defaults(loaded))
        self.with_backend = True
        self.inputs: dict[str, ui.input] = {}
        self.syncing = False

    # --- layout -------------------------------------------------------------

    def build(self) -> None:
        with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-2"):
            ui.label("Set up your CloudPipe deployment").classes("text-2xl font-semibold")
            ui.markdown(
                "Answering these questions creates nothing in AWS or Cloudflare, and no password "
                "or key is asked for. The optional Globus data transfer is set up separately, "
                "with `globus init`."
            )
            with ui.row().classes("text-sm text-grey-8 gap-6"):
                ui.label(f"Terraform root: {self.root}")
                ui.label(f"Answers saved to: {self.answers_path}")

            with ui.stepper().props("vertical header-nav").classes("w-full") as stepper:
                for section in schema.sections():
                    with ui.step(section.identifier, title=section.title):
                        self.section(section.identifier)
                        with ui.stepper_navigation():
                            ui.button("Next", on_click=stepper.next)
                            if section != schema.sections()[0]:
                                ui.button("Back", on_click=stepper.previous).props("flat")
                with ui.step("write", title="Write the Terraform files"):
                    self.write_step()
                    self.nav(stepper)
                with ui.step("preflight", title="Check your answers against AWS and Cloudflare"):
                    self.preflight_step()
                    self.nav(stepper)
                with ui.step("install", title="Install"):
                    self.install_step()
                    with ui.stepper_navigation():
                        ui.button("Back", on_click=stepper.previous).props("flat")

    @staticmethod
    def nav(stepper: ui.stepper) -> None:
        with ui.stepper_navigation():
            ui.button("Next", on_click=stepper.next)
            ui.button("Back", on_click=stepper.previous).props("flat")

    # --- the questions ------------------------------------------------------

    def section(self, identifier: str) -> None:
        if identifier == "terraform-state":
            ui.switch(
                "Keep the install's records in a storage bucket (writes backend.tf)",
                value=self.with_backend,
                on_change=lambda e: setattr(self, "with_backend", e.value),
            ).tooltip("Turn this off only if you already keep Terraform's records somewhere else.")
        for field in model.shown(identifier, with_backend=True):
            with ui.card().classes("w-full"):
                if field.not_asked:
                    self.by_hand(field)
                else:
                    self.question(field)

    def help_text(self, field: schema.Field) -> None:
        ui.label(field.title).classes("text-base font-medium")
        ui.label(prompt.SOURCE_LABELS[field.source]).classes("text-xs text-grey-7")
        first, *rest = field.description.split("\n\n")
        ui.markdown(model.literal_markdown(first)).classes("text-sm")
        if rest:
            with ui.expansion("More about this").classes("w-full text-sm"):
                for paragraph in rest:
                    ui.markdown(model.literal_markdown(paragraph))

    def question(self, field: schema.Field) -> None:
        self.help_text(field)
        if field.is_derived:
            ui.label(
                "You can type a different value. Clear the box to go back to the one worked "
                "out for you."
            ).classes("text-xs text-grey-7")
        elif field.is_suggested:
            ui.label(
                "Filled in from an earlier answer. Keep it, or type the value you need."
            ).classes("text-xs text-grey-7")

        box = (
            ui.input(
                label="Your answer",
                value=model.initial_text(field, self.answers),
                placeholder="Separate entries with commas" if field.is_list else None,
                validation=lambda text, f=field: self.first_problem(f, text),
                on_change=lambda e, f=field: self.changed(f, e.value),
            )
            .classes("w-full font-mono")
            .mark(field.identifier)
        )
        self.inputs[field.identifier] = box

        if field.nullable:
            none = model.is_none(field, self.answers)
            if none:
                box.disable()
            ui.checkbox(
                f"None: {field.null_meaning or 'no value'}",
                value=none,
                on_change=lambda e, f=field, b=box: self.none_toggled(f, b, e.value),
            ).classes("text-sm")

    def by_hand(self, field: schema.Field) -> None:
        """The one field the prompt does not ask either: its shape is the institution's."""
        self.help_text(field)
        value = self.answers.get(field.identifier)
        if value is None:
            # Only the state: the help text above already says when to set it.
            ui.label("Not set in the answers file.").classes("text-sm")
            return
        found = model.problems(field, value)
        if found:
            for message in found:
                ui.label(message).classes("text-sm text-negative")
        else:
            ui.label("Set in the answers file and valid.").classes("text-sm text-positive")

    def first_problem(self, field: schema.Field, text: str) -> str | None:
        found = model.problems(field, model.parse(field, text or ""))
        return found[0] if found else None

    def changed(self, field: schema.Field, text: str) -> None:
        if self.syncing:
            return
        value = model.parse(field, text or "")
        if model.problems(field, value):
            return  # the box shows why; the document keeps its last good value
        self.commit(field, value)

    def none_toggled(self, field: schema.Field, box: ui.input, none: bool) -> None:
        if none:
            box.disable()
            self.commit(field, None)
        else:
            box.enable()
            self.commit(field, model.parse(field, box.value or ""))

    def commit(self, field: schema.Field, value: Any) -> None:
        self.answers = model.update(self.answers, field, value)
        config.save(self.answers, self.answers_path)
        # A derived or suggested box follows its sources unless the deployer has
        # typed over it, which `update` has already decided; this only redraws the
        # result. Never the box being typed in: rewriting it mid-edit moves the cursor.
        self.syncing = True
        try:
            for other in schema.fields():
                if (
                    (other.is_derived or other.is_suggested)
                    and other.identifier != field.identifier
                    and other.identifier in self.inputs
                ):
                    self.inputs[other.identifier].value = model.to_text(
                        self.answers.get(other.identifier)
                    )
        finally:
            self.syncing = False

    # --- write the files ----------------------------------------------------

    def write_step(self) -> None:
        ui.markdown(
            "Writes `terraform.tfvars` and `backend.tf` into the Terraform root, exactly as "
            "`cloudpipe setup` does. Nothing is deployed."
        )
        # --force replaces any existing file, and drops lines a deployer added to
        # terraform.tfvars by hand (render.write); the label must say both.
        force = ui.checkbox(
            "Replace these files if they already exist (anything you added to them by hand is lost)"
        ).classes("text-sm")
        results = ui.column().classes("w-full")

        async def write() -> None:
            button.disable()
            results.clear()
            with results:
                ui.spinner()
            extra = ("--force",) if force.value else ()
            if not self.with_backend:
                extra += ("--no-backend",)
            # The CLI reads the file, not this page. A suggestion or derivation made
            # on load and never edited exists only in memory until it is saved.
            config.save(self.answers, self.answers_path)
            envelope = await run.io_bound(
                model.cli, "setup", answers_path=self.answers_path, root=self.root, extra=extra
            )
            results.clear()
            with results:
                self.setup_result(envelope)
            button.enable()

        button = ui.button("Write the files", on_click=write)

    def setup_result(self, envelope: model.Envelope) -> None:
        if not envelope.ok:
            self.error(envelope)
            return
        ui.label(
            "Your answers are saved and the files are written. Nothing has been deployed yet."
        ).classes("text-positive font-medium")
        for path in envelope.data.get("files_written", ()):
            ui.label(f"Wrote {path}").classes("text-sm font-mono")
        ui.label("Next steps, in order").classes("text-lg font-medium mt-4")
        steps = sorted(
            envelope.data.get("outstanding", ()),
            key=lambda step: STEP_ORDER.get(step["identifier"], len(STEP_ORDER)),
        )
        for step in steps:
            self.record(step)

    # --- preflight ----------------------------------------------------------

    def preflight_step(self) -> None:
        ui.markdown(
            "This checks your answers against your AWS and Cloudflare accounts: your AWS "
            "identity, the hosted zone if you gave a domain, the Cloudflare token and its "
            "permissions, and more. It only reads; it changes nothing. It uses the AWS sign-in "
            "and the `CLOUDFLARE_API_TOKEN` of the terminal that started this page. After "
            "`aws sso login` in any terminal, run the checks again. If you set the token after "
            "starting the page, stop it (Ctrl-C in its terminal) and start it again."
        )
        token = "set" if os.environ.get("CLOUDFLARE_API_TOKEN") else "not set"
        ui.label(
            f"CLOUDFLARE_API_TOKEN is {token} in the terminal that started this page."
        ).classes("text-sm")
        account = ui.input(
            label="AWS account ID you intend to deploy into (optional)",
            validation=lambda v: (
                None
                if not v or (v.isdigit() and len(v) == 12)
                else "An AWS account ID is 12 digits."
            ),
        ).classes("w-80")
        ui.label(
            "If you leave this empty, the account check is marked skipped, because there is "
            "nothing to compare your signed-in account with."
        ).classes("text-xs text-grey-7")
        results = ui.column().classes("w-full")

        async def check() -> None:
            button.disable()
            results.clear()
            with results:
                ui.spinner()
            extra = ("--account", account.value) if account.value else ()
            envelope = await run.io_bound(
                model.cli, "preflight", answers_path=self.answers_path, root=self.root, extra=extra
            )
            results.clear()
            with results:
                self.preflight_result(envelope)
            button.enable()

        button = ui.button("Run the checks", on_click=check)

    def preflight_result(self, envelope: model.Envelope) -> None:
        checks = envelope.data.get("checks")
        if checks is None:
            self.error(envelope)
            return
        counts = {state: sum(1 for c in checks if c["state"] == state) for state in COLOURS}
        failed = counts["fail"]
        ui.label(
            f"{plural(failed, 'check')} failed. Nothing was changed."
            if failed
            else "No checks failed. Nothing was changed."
        ).classes("font-medium " + ("text-negative" if failed else "text-positive"))
        if counts["skipped"]:
            ui.label(
                f"{plural(counts['skipped'], 'check')} could not run, so "
                f"{'it is' if counts['skipped'] == 1 else 'they are'} marked skipped, not "
                "passed. Each one says what was missing."
            ).classes("text-sm")
        for record in checks:
            self.record(wording.check(record))

    # --- install ------------------------------------------------------------

    def install_step(self) -> None:
        if self.install_runner is not None:
            from .install import InstallPanel

            InstallPanel(self.root, self.install_runner).build()
            return
        ui.markdown(
            "This page does not run the installer. Copy each command below into a terminal "
            "where `CLOUDFLARE_API_TOKEN` is exported and you are signed in to AWS. For a first "
            "install, run one phase at a time. If a phase fails, it prints the command that "
            "continues from where it stopped."
        )
        try:
            listed = model.phases()
        except Exception as err:  # the page still works without the list
            ui.label(f"Could not list the phases: {err}").classes("text-negative")
            return
        for phase in listed:
            with ui.card().classes("w-full"):
                with ui.row().classes("items-center gap-2"):
                    ui.badge(f"Phase {phase['id']}")
                    ui.label(phase["title"]).classes("font-medium")
                if not phase["endpoint_public_access"]:
                    ui.label(
                        "After this phase, the cluster can be reached only through WARP. Before "
                        "you run it, make sure at least one operator can sign in."
                    ).classes("text-xs text-grey-7")
                ui.code(model.phase_command(self.root, phase["id"]), language="bash").classes(
                    "w-full"
                )
        ui.label("Or run every phase with one command:").classes("text-sm mt-2")
        ui.code(model.phase_command(self.root), language="bash").classes("w-full")

    # --- shared -------------------------------------------------------------

    def record(self, record: dict[str, Any]) -> None:
        """One check or step, with its remedy: a command to copy, or a thing to do."""
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.badge(BADGES[record["state"]], color=COLOURS[record["state"]])
                ui.label(record["title"]).classes("font-medium")
            ui.label(record["message"]).classes("text-sm")
            remedy = record.get("remedy")
            if not remedy or record["state"] == "pass":
                return
            if remedy["kind"] == "command":
                command, _, aside = remedy["text"].partition("   (")
                ui.code("\n".join(display_command(command.strip())), language="bash").classes(
                    "w-full"
                )
                if aside:
                    ui.label(f"({aside}").classes("text-xs text-grey-7")
            else:
                ui.markdown(model.literal_markdown(remedy["text"])).classes("text-sm")

    def error(self, envelope: model.Envelope) -> None:
        # Reworded where the CLI's own words name a flag the page has no use for.
        error = wording.error(envelope.error or {})
        ui.label(error.get("message", "The command failed.")).classes("text-negative font-medium")
        for entry in (error.get("detail") or {}).get("field_errors", ()):
            ui.label(f"{field_title(entry['identifier'])}: {entry['message']}").classes("text-sm")
        remedy = error.get("remedy")
        if remedy:
            if remedy["kind"] == "command":
                ui.code(remedy["text"], language="bash").classes("w-full")
            else:
                ui.markdown(model.literal_markdown(remedy["text"])).classes("text-sm")
        if error.get("raw"):
            ui.code(error["raw"]).classes("w-full")
        if envelope.notes:
            with ui.expansion("Full output").classes("w-full text-sm"):
                ui.code(envelope.notes).classes("w-full")
