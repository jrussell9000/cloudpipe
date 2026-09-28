"""Output plumbing: human text on stderr, one JSON envelope on stdout.

The split is what makes the CLI drivable by another program without a parsing
mode: a caller reads stdout as JSON and can show stderr verbatim as progress.
Every JSON envelope carries `schema_version`, so a consumer can tell what it is
looking at.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from . import CONTRACT_VERSION
from .exits import CliError, ExitCode


class Emitter:
    """Collects human notes and emits at most one JSON envelope.

    A command calls `note()` freely, then exactly one of `result()` or
    `failure()`. Emitting twice is a bug, not a recoverable state, so it raises.
    """

    def __init__(
        self,
        command: str,
        *,
        env: str,
        json_mode: bool = False,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self.command = command
        self.env = env
        self.json_mode = json_mode
        self._stdout = stdout if stdout is not None else sys.stdout
        self._stderr = stderr if stderr is not None else sys.stderr
        self._emitted = False

    def note(self, text: str) -> None:
        """Human-readable progress. Always stderr, in both modes."""
        print(text, file=self._stderr, flush=True)

    def result(
        self, data: dict[str, Any] | None = None, *, exit_code: ExitCode = ExitCode.OK
    ) -> ExitCode:
        self._envelope(
            ok=exit_code == ExitCode.OK, exit_code=exit_code, data=data or {}, error=None
        )
        return exit_code

    def failure(self, error: CliError) -> ExitCode:
        self.note(f"error: {error.message}")
        if error.raw:
            self.note(f"  raw: {error.raw}")
        if error.remedy:
            label = "run" if error.remedy.kind == "command" else "do"
            self.note(f"  {label}: {error.remedy.text}")
        self._envelope(ok=False, exit_code=error.exit_code, data={}, error=error.as_dict())
        return error.exit_code

    def _envelope(
        self,
        *,
        ok: bool,
        exit_code: ExitCode,
        data: dict[str, Any],
        error: dict[str, Any] | None,
    ) -> None:
        if self._emitted:
            raise RuntimeError(f"{self.command}: emitted twice")
        self._emitted = True
        if not self.json_mode:
            return
        envelope = {
            "schema_version": CONTRACT_VERSION,
            "command": self.command,
            "env": self.env,
            "ok": ok,
            "exit_code": int(exit_code),
            "data": data,
            "error": error,
        }
        json.dump(envelope, self._stdout, indent=2, sort_keys=False, default=str)
        self._stdout.write("\n")
        self._stdout.flush()
