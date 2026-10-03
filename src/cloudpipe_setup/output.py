"""Output plumbing: human text on stderr, one JSON envelope on stdout.

The split is what makes the CLI drivable by another program without a parsing
mode: a caller reads stdout as JSON and can show stderr verbatim as progress.
Every JSON envelope carries `schema_version`, so a consumer can tell what it is
looking at.

`Record` is the shape every field, check and step reports in. It exists so a
consumer can render any of them — including its remedy — without parsing the
human-readable message, which is the one thing prose cannot promise.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, TextIO

from . import CONTRACT_VERSION
from .exits import CliError, ExitCode, Remedy

STATES = ("pass", "fail", "skipped", "blocked")
"""The states a `Record` may report.

`skipped` is load-bearing: a check that could not be performed reports it rather
than `pass`. A green report from a check that could not look is the failure mode
preflight exists to prevent. See design.md D7.
"""


@dataclass(frozen=True)
class Record:
    """One field, check or step, in the shape a consumer renders.

    `identifier` is stable across releases — a schema field name, or a check id
    like `aws.identity`. `state` is one of `STATES`. `remedy` is `None` only when
    there is nothing to do, which for a `fail` or a `blocked` is a bug.
    """

    identifier: str
    title: str
    state: str
    message: str
    remedy: Remedy | None = None

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"state must be one of {STATES}, got {self.state!r}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "title": self.title,
            "state": self.state,
            "message": self.message,
            "remedy": self.remedy.as_dict() if self.remedy else None,
        }


class Emitter:
    """Collects human notes and emits at most one JSON envelope.

    A command calls `note()` freely, then exactly one of `result()` or
    `failure()`. Emitting twice is a bug, not a recoverable state, so it raises.
    """

    def __init__(
        self,
        command: str,
        *,
        json_mode: bool = False,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self.command = command
        self.json_mode = json_mode
        self._stdout = stdout if stdout is not None else sys.stdout
        self._stderr = stderr if stderr is not None else sys.stderr
        self._emitted = False

    @property
    def stderr(self) -> TextIO:
        """The stream `note` writes to.

        Exposed so a caller can ask it whether colour is appropriate. Nothing may
        write to it directly — `note` is the only writer — but deciding on colour
        needs to ask the actual destination, not `sys.stderr`, or a captured run
        emits escape codes into a string buffer.
        """
        return self._stderr

    def note(self, text: str = "", *, end: str = "\n") -> None:
        """Human-readable progress. Always stderr, in both modes.

        `end=""` is for a prompt the deployer types on the same line. It is still
        stderr: `input("prompt")` would write the prompt to stdout and put prose
        in the middle of the JSON envelope.
        """
        print(text, file=self._stderr, end=end, flush=True)

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
        for field_error in error.detail.get("field_errors", ()):
            self.note(f"  {field_error['identifier']}: {field_error['message']}")
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
            "ok": ok,
            "exit_code": int(exit_code),
            "data": data,
            "error": error,
        }
        json.dump(envelope, self._stdout, indent=2, sort_keys=False, default=str)
        self._stdout.write("\n")
        self._stdout.flush()
