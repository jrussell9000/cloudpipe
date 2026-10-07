"""Exit codes and the error type that carries them.

The codes are part of the integration contract, because they are the one thing a
caller can act on without parsing anything: `BLOCKED` means "a human must do
something", which drives a different screen in a wizard than "a check failed" or
"your answers are wrong".

This is a deliberate copy of `globus_admin/exits.py` rather than an import of it,
with `tests/test_setup_wizard_exits.py` asserting the two mappings stay equal. Importing it would make the general setup tool depend on the Globus
subsystem's package, whose other modules pull in `globus-sdk` — the wrong
direction, and a real import cost on a command that needs none of it. Extracting
a shared `src/cloudpipe_cli/` is the right end state; the parity test is what
will prove that extraction changed nothing. See design.md D8.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class ExitCode(IntEnum):
    """Stable exit codes. Do not renumber."""

    OK = 0
    ERROR = 1
    """Unexpected failure — a bug, or something this tool does not model."""

    BLOCKED = 2
    """Waiting on a human: a missing prerequisite, an answer nobody has supplied.

    Also returned when `--non-interactive` was set and a required field is absent
    from the answers document, since that too is a human decision that has not
    been made.
    """

    CHECK_FAILED = 3
    """A check or verification failed: the system is reachable and the answer is no."""

    INVALID = 4
    """Invalid input or configuration: bad answers document, not a stack root."""


@dataclass(frozen=True)
class Remedy:
    """What to do about a failure.

    `kind` is `command` when `text` can be run as-is, `human` when it describes an
    action (read a value off a dashboard, ask an administrator). A wizard renders
    the two differently, so the distinction is data rather than prose.
    """

    kind: str
    text: str

    def __post_init__(self) -> None:
        if self.kind not in ("command", "human"):
            raise ValueError(f"remedy kind must be 'command' or 'human', got {self.kind!r}")

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "text": self.text}


class CliError(Exception):
    """A failure with an exit code, a stable identifier, and a next step.

    `code` is a stable, machine-readable identifier (e.g. `root.not_a_stack_root`).
    `raw` keeps the underlying error text, which is always shown: translating an
    error must never hide it, or a novel failure becomes undebuggable.

    `detail` carries the structured body a caller acts on. Validation puts its
    per-field errors there rather than in `message`, because reporting one error
    per offending field is a requirement, and a caller must not have to split
    prose to recover them.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: ExitCode = ExitCode.ERROR,
        remedy: Remedy | None = None,
        raw: str | None = None,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.remedy = remedy
        self.raw = raw
        self.detail = detail or {}

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "remedy": self.remedy.as_dict() if self.remedy else None,
            "raw": self.raw,
            "detail": self.detail,
        }
