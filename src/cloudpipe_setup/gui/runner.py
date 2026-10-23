"""Run the published installer for the setup page, with no NiceGUI anywhere in it.

The archived change `openspec/changes/archive/2026-10-22-run-installer-phases-from-the-setup-page/`,
whose design D1-D9 the comments here cite. The runner executes `scripts/stack/install.sh` and nothing
else, with `--root` and at most one of `--phase N` / `--from-phase N` (D2). Every
gate stays inside the installer; this module repeats none of them.

A run belongs to the server process, not to a browser tab (D3): one `Runner` per
Terraform root, from `for_root`, holding the subprocess, its state and the last
`BUFFER_LINES` lines of its output. A tab reads it; closing the tab changes
nothing. Across page servers, an exclusive `flock` on a file under the root's
`.install-logs/` keeps a second run from starting, and the kernel releases it if
its holder dies.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import model

#: How many lines of output a tab opened mid-run can replay. The installer's
#: full Terraform output is in its own per-run log directory, not here (D7).
BUFFER_LINES = 5000

#: The line `install.sh` prints as each phase starts: `==> [3/8] Phase 3: <title>`.
#: `tests/test_setup_wizard_runner.py` holds this to the installer's own `echo`,
#: so rewording that line fails CI instead of blinding the page (D6).
PROGRESS = re.compile(r"^==> \[(?P<id>\d+)/(?P<last>\d+)\] Phase (?P=id): ")

#: Colour and cursor codes. With no terminal the installer prints few, but
#: Terraform's error blocks and any tool it calls may still carry them.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\r")

#: Exit codes, as `targets.sh` declares them. An outcome is read from the code
#: alone; the installer's messages are shown as written, never parsed (D6).
OUTCOMES = {0: "complete", 2: "blocked", 3: "check_failed", 4: "invalid"}

LOCK_NAME = "gui-run.lock"

#: How long to keep reading output after the installer has exited.
READ_GRACE_S = 2.0


class Busy(Exception):
    """A run is already in progress for this root, from this server or another."""


@dataclass
class Run:
    """One invocation of the installer, and what is known about it so far."""

    argv: list[str]
    phases: list[int]
    state: str = "running"  # running, stopping, then an outcome
    exit_code: int | None = None
    current_phase: int | None = None
    lines: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=BUFFER_LINES)
    )
    #: Lines emitted in total, so a reader can tell which buffered lines it has
    #: not seen even after older ones have been dropped.
    emitted: int = 0

    @property
    def finished(self) -> bool:
        return self.state not in ("running", "stopping")

    def unseen(self, seen: int) -> list[str]:
        """The lines emitted after the first `seen`, as far as the buffer still holds them."""
        missing = self.emitted - seen
        if missing <= 0:
            return []
        return list(self.lines)[-min(missing, len(self.lines)) :]


class Runner:
    """The one place a run for a given Terraform root is started, watched and cancelled."""

    def __init__(self, root: Path, *, installer: Path | None = None) -> None:
        self.root = root.resolve()
        self.installer = installer or model.installer()
        self.run: Run | None = None
        self._process: subprocess.Popen | None = None
        self._lock: int | None = None
        self._done: asyncio.Event | None = None
        self._cancelled = False

    # --- what may be run -----------------------------------------------------

    def listed(self) -> list[dict]:
        """The phases the installer declares, from `--list-phases --json`, which reads nothing."""
        completed = subprocess.run(
            ["bash", str(self.installer), "--list-phases", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(completed.stdout)

    def phase_ids(self) -> list[int]:
        return [int(phase["id"]) for phase in self.listed()]

    def argv(self, *, phase: int | None = None, from_phase: int | None = None) -> list[str]:
        """The whole command line, which is the only thing a run may execute (D2)."""
        if phase is not None and from_phase is not None:
            raise ValueError("--phase and --from-phase are alternatives")
        known = self.phase_ids()
        chosen = phase if phase is not None else from_phase
        if chosen is not None and chosen not in known:
            raise ValueError(f"{chosen} is not a phase of this installer; phases are {known}")
        argv = ["bash", str(self.installer), "--root", str(self.root)]
        if phase is not None:
            argv += ["--phase", str(phase)]
        elif from_phase is not None:
            argv += ["--from-phase", str(from_phase)]
        return argv

    def phases_for(self, *, phase: int | None = None, from_phase: int | None = None) -> list[int]:
        known = self.phase_ids()
        if phase is not None:
            return [phase]
        if from_phase is not None:
            return [p for p in known if p >= from_phase]
        return known

    # --- one run at a time -----------------------------------------------------

    @property
    def lock_path(self) -> Path:
        return self.root / ".install-logs" / LOCK_NAME

    def _acquire(self, argv: list[str]) -> None:
        if self.run is not None and not self.run.finished:
            raise Busy(f"a run is already in progress from this page: {' '.join(self.run.argv)}")
        # 0700, as the installer creates it: the logs beside this file hold plan
        # output, which carries attribute values from state.
        self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.read(descriptor, 4096).decode(errors="replace").strip()
            os.close(descriptor)
            raise Busy(
                f"another setup page is running the installer for {self.root}"
                + (f": {holder}" if holder else "")
            ) from None
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"pid {os.getpid()}: {' '.join(argv)}\n".encode())
        self._lock = descriptor

    def _release(self) -> None:
        if self._lock is None:
            return
        with contextlib.suppress(OSError):
            os.ftruncate(self._lock, 0)
        os.close(self._lock)  # closing the descriptor releases the flock
        self._lock = None

    # --- running ---------------------------------------------------------------

    async def start(self, *, phase: int | None = None, from_phase: int | None = None) -> Run:
        """Start a run and return at once; the output is read in the background."""
        argv = self.argv(phase=phase, from_phase=from_phase)
        phases = self.phases_for(phase=phase, from_phase=from_phase)
        self._acquire(argv)
        # Recorded before the first await, so a second click that arrives while the
        # process is being spawned is refused as this page's own run.
        run = self.run = Run(argv=argv, phases=phases)
        self._cancelled = False
        self._done = asyncio.Event()
        try:
            # Its own session, so the installer and everything it starts are one
            # process group, which Cancel signals and the page's terminal does not (D4).
            #
            # `subprocess`, not asyncio's: asyncio's `Process.wait()` returns only
            # once every pipe has closed, so a child the installer left behind
            # holding stdout would keep a finished run looking like a running one.
            self._process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd=self.root,
            )
        except BaseException as err:
            run.state = "error"
            run.lines.append(f"could not start the installer: {err}")
            run.emitted += 1
            self._release()
            self._done.set()
            raise
        loop = asyncio.get_running_loop()
        reader = threading.Thread(target=_read, args=(self._process, run), daemon=True)
        reader.start()
        loop.create_task(self._follow(self._process, run, reader))
        return run

    async def _follow(self, process: subprocess.Popen, run: Run, reader: threading.Thread) -> None:
        try:
            code = await asyncio.to_thread(process.wait)
            # The run ends when the installer exits, not when its output pipe
            # closes. What it wrote before exiting is still read, for a short
            # grace period; after that the reader is abandoned to the orphan.
            await asyncio.to_thread(reader.join, READ_GRACE_S)
            run.exit_code = code
            run.state = outcome(code, cancelled=self._cancelled)
        finally:
            self._release()
            if self._done is not None:
                self._done.set()

    def cancel(self) -> bool:
        """Interrupt the run once, as Ctrl-C would. False when there was nothing to interrupt.

        Never a second signal: Terraform's first interrupt finishes the operation
        in progress and releases the state lock, and its second abandons it (D4).
        """
        if self.run is None or self.run.state != "running" or self._process is None:
            return False
        self._cancelled = True
        self.run.state = "stopping"
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self._process.pid, signal.SIGINT)
        return True

    async def wait(self) -> Run | None:
        if self._done is not None:
            await self._done.wait()
        return self.run

    async def shutdown(self) -> None:
        """For the server's shutdown: interrupt a run in progress once, and wait for it to exit."""
        if self.run is not None and not self.run.finished:
            self.cancel()
            await self.wait()


def _read(process: subprocess.Popen, run: Run) -> None:
    """The reader thread: every output line into the run, colour stripped, phase noted.

    A deque append and an int increment, from one writer thread, read by the event
    loop: each is a single operation under the GIL, and a reader that sees `emitted`
    one ahead of the deque merely shows that line on its next refresh.
    """
    assert process.stdout is not None
    for raw in process.stdout:
        line = _ANSI.sub("", raw.decode(errors="replace").rstrip("\n"))
        match = PROGRESS.match(line)
        if match:
            run.current_phase = int(match["id"])
        run.lines.append(line)
        run.emitted += 1


def outcome(code: int, *, cancelled: bool) -> str:
    """What a finished run's exit code means. Any non-zero code after a cancel is a cancel."""
    if cancelled and code != 0:
        return "cancelled"
    return OUTCOMES.get(code, "error")


_RUNNERS: dict[Path, Runner] = {}


def for_root(root: Path) -> Runner:
    """The server's runner for this root, created on first use and shared by every tab."""
    key = root.resolve()
    if key not in _RUNNERS:
        _RUNNERS[key] = Runner(key)
    return _RUNNERS[key]


async def shutdown_all() -> None:
    for runner in list(_RUNNERS.values()):
        await runner.shutdown()
