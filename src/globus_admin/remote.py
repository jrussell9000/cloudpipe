"""Running something on the GCS instance without SSH.

Everything the operator CLI needs to do *on* the instance goes through SSM Run
Command: the instance has no inbound admin path, no key material has to exist on
anyone's laptop, and every invocation is recorded in CloudTrail against the
identity that made it. The cost is that there is no terminal — a command is
started, and its output is fetched by polling — so this module is the polling
loop, the output streaming, and the translation of SSM's result back into this
CLI's exit vocabulary.

The translation is the part worth reading. SSM collapses every non-zero exit
into the single status `Failed`, so `Failed` alone cannot tell "the plan is
blocked on something a human must decide" (3) from "the script crashed" (1). The
number in `ResponseCode` can, because the on-instance reconcile exits with the
same codes as `globus_admin.exits.ExitCode`. That shared vocabulary is what lets
`globus configure` return a meaningful code for something that happened on
another machine.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .aws import COMMAND_PENDING, AwsGateway, CommandInvocation
from .exits import CliError, ExitCode, Remedy
from .output import Emitter

#: How long to keep polling before giving up on an invocation. Deliberately
#: longer than the SSM document's own `timeoutSeconds`: when the document times
#: out, SSM says so, and a specific answer beats this module's generic one.
WAIT_TIMEOUT = 900.0
POLL_INTERVAL = 3.0

#: Exit statuses that mean what this CLI means by them, passed straight through.
#: A shell-level status (126 "not executable", 127 "not found", 137 "killed") is
#: not in this vocabulary and must not be read as one — 127 is not `ERROR + 126`.
_PASS_THROUGH = {int(code) for code in ExitCode}


@dataclass(frozen=True)
class RemoteRun:
    """What a remote command did, and what this CLI should exit with."""

    invocation: CommandInvocation
    exit_code: ExitCode

    @property
    def ok(self) -> bool:
        return self.exit_code is ExitCode.OK

    def as_dict(self) -> dict:
        return self.invocation.as_dict() | {"exit_code": int(self.exit_code)}


def run_document(
    aws: AwsGateway,
    *,
    document_name: str,
    instance_id: str,
    parameters: dict[str, str],
    emitter: Emitter,
    comment: str = "",
    stream_output: bool = True,
    timeout: float = WAIT_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> RemoteRun:
    """Run `document_name` on one instance, streaming its output as it arrives.

    Raises `CliError` when the command could not run to completion — a send that
    was refused, an SSM-side timeout, a cancellation. A command that ran and
    failed is not an error here: its exit code is returned, because deciding what
    a failure means is the caller's job.

    `stream_output=False` suppresses the standard output only, for a caller that
    will present it itself. Standard error is always streamed: nobody presents
    that, and a warning nobody saw is the same as one that was never printed.
    """
    command_id = aws.send_command(document_name, instance_id, parameters, comment=comment)
    emitter.note(f"sent {document_name} to {instance_id} (command {command_id})")

    out = _Stream(emitter, prefix="", enabled=stream_output)
    err = _Stream(emitter, prefix="stderr: ")

    deadline = clock() + timeout
    invocation = CommandInvocation(command_id, instance_id, COMMAND_PENDING)
    while True:
        invocation = aws.command_invocation(command_id, instance_id)
        out.emit(invocation.stdout)
        err.emit(invocation.stderr)
        if invocation.terminal:
            break
        if clock() >= deadline:
            raise CliError(
                "ssm.wait_timeout",
                f"Gave up waiting for command {command_id} on {instance_id} after "
                f"{int(timeout)}s. It may still be running.",
                exit_code=ExitCode.ERROR,
                remedy=Remedy(
                    "command",
                    f"aws ssm get-command-invocation --command-id {command_id} "
                    f"--instance-id {instance_id}",
                ),
                detail={"command_id": command_id, "status": invocation.status},
            )
        sleep(poll_interval)

    return RemoteRun(invocation, _exit_code(invocation, emitter))


def _exit_code(invocation: CommandInvocation, emitter: Emitter) -> ExitCode:
    """SSM's verdict, as one of this CLI's exit codes."""
    if invocation.succeeded:
        return ExitCode.OK

    if invocation.status in ("TimedOut", "Cancelled"):
        raise CliError(
            "ssm.command_not_completed",
            f"The command on {invocation.instance_id} ended as {invocation.status} "
            "without producing a result.",
            exit_code=ExitCode.ERROR,
            remedy=Remedy("human", "check the instance and re-run"),
            detail=invocation.as_dict(),
        )

    code = invocation.response_code
    if code is None:
        # `Failed` with no exit status: the agent could not run the script at all.
        raise CliError(
            "ssm.command_failed",
            f"The command on {invocation.instance_id} failed before the script reported "
            "an exit status.",
            exit_code=ExitCode.ERROR,
            remedy=Remedy("human", "check the SSM agent log on the instance"),
            detail=invocation.as_dict(),
        )

    if code in _PASS_THROUGH:
        return ExitCode(code)

    # Out of vocabulary, so it means nothing here beyond "it failed". Saying the
    # number is what makes it debuggable; guessing what it meant would not.
    emitter.note(f"the remote command exited {code}, which is not one of this tool's codes")
    return ExitCode.ERROR


class _Stream:
    """Prints the part of a growing output buffer that has not been printed yet.

    SSM returns the output collected so far on every poll, capped at 24,000
    characters, so each poll's content starts with the last one's. The prefix
    check is a safety net: if that ever stops holding, this reprints rather than
    silently showing an operator a spliced, wrong transcript.
    """

    def __init__(self, emitter: Emitter, *, prefix: str, enabled: bool = True) -> None:
        self._emitter = emitter
        self._prefix = prefix
        self._enabled = enabled
        self._seen = ""

    def emit(self, text: str) -> None:
        if not self._enabled or not text or text == self._seen:
            return
        fresh = text[len(self._seen) :] if text.startswith(self._seen) else text
        self._seen = text
        for line in fresh.splitlines():
            self._emitter.note(f"{self._prefix}{line}")
