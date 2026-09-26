"""Running external commands, injectably.

Every external call goes through a `Runner`, so the prerequisite checks, the
cluster probe, and the workflow guard can all be unit-tested without a shell,
a cluster, or an AWS account.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Completed:
    """What a command did. `found=False` means the executable is not on PATH."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    found: bool = True

    @property
    def ok(self) -> bool:
        return self.found and self.returncode == 0

    @property
    def output(self) -> str:
        """Both streams, for matching error signatures that may land on either."""
        return f"{self.stdout}\n{self.stderr}".strip()


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], *, timeout: float = 30.0) -> Completed: ...


def subprocess_runner(argv: Sequence[str], *, timeout: float = 30.0) -> Completed:
    argv = tuple(argv)
    if shutil.which(argv[0]) is None:
        return Completed(argv=argv, returncode=127, found=False)
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return Completed(
            argv=argv,
            returncode=124,
            stderr=f"timed out after {timeout}s: {exc}",
        )
    return Completed(
        argv=argv,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


class FakeRunner:
    """Test double: maps the first argv token (and optional second) to a Completed."""

    def __init__(self, responses: dict[str, Completed]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], *, timeout: float = 30.0) -> Completed:
        argv = tuple(argv)
        self.calls.append(argv)
        for key in (" ".join(argv[:2]), argv[0]):
            if key in self.responses:
                return self.responses[key]
        return Completed(argv=argv, returncode=127, found=False)
