"""Exit codes and the error type that carries them.

The codes are part of the integration contract, because they are the one thing a
caller can act on without parsing anything: `BLOCKED` means "a human must do
something", which drives a different screen in a wizard than "a check failed" or
"your answers are wrong".
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
    """Waiting on a human: a browser login, an expired session, an email to an admin.

    Also returned when a confirmation was required and `--non-interactive` was set
    without `--yes`, since that too is a human decision that has not been made.
    """

    CHECK_FAILED = 3
    """A check or verification failed: the system is reachable and the answer is no."""

    INVALID = 4
    """Invalid input or configuration: bad answers document, wrong AWS account."""


@dataclass(frozen=True)
class Remedy:
    """What to do about a failure.

    `kind` is `command` when `text` can be run as-is, `human` when it describes an
    action (log in, email an administrator). A wizard renders the two differently,
    so the distinction is data rather than prose.
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

    `code` is a stable, machine-readable identifier (e.g. `aws.sso_login_expired`).
    `raw` keeps the underlying error text, which is always shown: translating an
    error must never hide it, or a novel failure becomes undebuggable.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: ExitCode = ExitCode.ERROR,
        remedy: Remedy | None = None,
        gate: str | None = None,
        raw: str | None = None,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.remedy = remedy
        self.gate = gate
        self.raw = raw
        self.detail = detail or {}

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "remedy": self.remedy.as_dict() if self.remedy else None,
            "gate": self.gate,
            "raw": self.raw,
            "detail": self.detail,
        }
