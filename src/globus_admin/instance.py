"""Getting the GCS instance up, and putting it back as it was found.

Three commands run something on the instance through SSM — `configure`,
`bootstrap-endpoint`, `cleanup-endpoint` — and all three meet the same two
facts. The instance is normally **stopped**, because it costs money and serves
nothing between transfers; and an instance found *already* running may be
running for someone else, a batch or another operator, so stopping it would kill
their transfers. The answers are identical in all three, which is why they are
here rather than copied: start only with consent, and offer to stop only what
this process started.

Two details are easy to get wrong when this logic is moved around, so both are
parameters rather than assumptions:

* **Error identifiers are namespaced by the calling command** (`code_prefix`).
  `configure.no_instance` names the command an operator ran, and it is in the
  JSON contract; hoisting the function must not silently retitle it.
* **Waiting for GridFTP is opt-in.** `configure` waits because a reconcile
  usually follows a transfer, and GridFTP answering is the readiness signal an
  operator recognizes. `bootstrap-endpoint` must not: there is no endpoint yet,
  so nothing can be listening, and waiting three minutes to say so would look
  like a hang at the worst moment.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from . import guards
from .doctor import GRIDFTP_PORT, GRIDFTP_WAIT_TIMEOUT, INSTANCE_START_TIMEOUT, tcp_probe
from .exits import CliError, ExitCode, Remedy

if TYPE_CHECKING:  # pragma: no cover - import cycle: cli imports the commands
    from .cli import Context

POLL_INTERVAL = 10.0

# Seams, looked up as module attributes at call time so a test can replace them.
# One set for the whole package: a second copy in a command module would be a
# second thing every test suite has to remember to patch.
_sleep = time.sleep
_clock = time.monotonic
_probe = tcp_probe


def require_id(ctx: Context, *, code_prefix: str) -> str:
    """The instance id Terraform recorded, or a check failure naming the parameter."""
    instance_id = ctx.aws.get_parameter(ctx.env.instance_id_param)
    if instance_id:
        return instance_id
    raise CliError(
        f"{code_prefix}.no_instance",
        f"No instance id is recorded in {ctx.env.instance_id_param}, so there is nothing "
        "to run this on.",
        exit_code=ExitCode.CHECK_FAILED,
        remedy=Remedy("command", "terraform apply"),
    )


def ensure_running(
    ctx: Context, instance_id: str, *, code_prefix: str, await_gridftp: bool = False
) -> bool:
    """Make sure the instance is up. Returns whether this command started it."""
    state = ctx.aws.instance_state(instance_id)
    if state == "running":
        ctx.emitter.note(f"instance {instance_id} is already running")
        return False

    guards.require(
        ctx.emitter,
        action=f"start the GCS instance {instance_id} (currently {state})",
        non_interactive=ctx.non_interactive,
        assume_yes=ctx.assume_yes,
        reason="the work runs on the instance and it is not running",
    )

    ctx.aws.start_instance(instance_id)
    state = wait_for(lambda: ctx.aws.instance_state(instance_id), "running", INSTANCE_START_TIMEOUT)
    if state != "running":
        raise CliError(
            f"{code_prefix}.instance_did_not_start",
            f"Instance {instance_id} was still {state} after {int(INSTANCE_START_TIMEOUT)}s.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("human", "check the instance in the EC2 console"),
        )
    ctx.emitter.note(f"instance {instance_id} is running")
    if await_gridftp:
        wait_for_gridftp(ctx, instance_id)
    return True


def wait_for_gridftp(ctx: Context, instance_id: str) -> None:
    """Wait for GCS to answer, but never fail on it.

    A note rather than a failure because nothing that calls this needs the port:
    the reconcile talks to the management API. GridFTP answering is the readiness
    signal an operator recognizes, so it is worth waiting for and worth
    reporting — it is just not worth refusing to do the work over.
    """
    address = ctx.aws.instance_address(instance_id)
    if not address:
        ctx.emitter.note("the instance has no public address yet; continuing")
        return

    deadline = _clock() + GRIDFTP_WAIT_TIMEOUT
    while True:
        reachable, detail = _probe(address, GRIDFTP_PORT, 5.0)
        if reachable:
            ctx.emitter.note(f"GridFTP answers on {address}:{GRIDFTP_PORT}")
            return
        if _clock() >= deadline:
            ctx.emitter.note(
                f"nothing answered on {address}:{GRIDFTP_PORT} within "
                f"{int(GRIDFTP_WAIT_TIMEOUT)}s ({detail}); continuing, since the work uses "
                "the management API rather than this port"
            )
            return
        _sleep(POLL_INTERVAL)


def offer_stop(ctx: Context, instance_id: str) -> bool:
    """Offer to stop an instance this command started. Failing to stop is not fatal."""
    if ctx.args.keep_running:
        ctx.emitter.note(f"leaving {instance_id} running (--keep-running)")
        return False

    if not guards.ask(
        ctx.emitter,
        action=f"stop the GCS instance {instance_id} again",
        non_interactive=ctx.non_interactive,
        assume_yes=ctx.assume_yes,
    ):
        ctx.emitter.note(f"leaving {instance_id} running")
        return False

    try:
        ctx.aws.stop_instance(instance_id)
    except CliError as err:
        # Reported, not raised: this runs on the way out of a failure as well as a
        # success, and masking the original error with this one would be worse
        # than an instance left running, which is visible and costs money slowly.
        ctx.emitter.note(f"could not stop {instance_id}: {err.message}")
        return False
    ctx.emitter.note(f"stopping {instance_id}")
    return True


def wait_for(read_state, wanted: str, timeout: float) -> str:
    deadline = _clock() + timeout
    state = read_state()
    while state != wanted and _clock() < deadline:
        _sleep(POLL_INTERVAL)
        state = read_state()
    return state
