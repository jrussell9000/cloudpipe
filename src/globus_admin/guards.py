"""The guard every mutating command runs first.

Two things have to be true before changing Globus state: the operator has seen
what will happen and agreed, and no batch is mid-flight. Rotating a gateway key
or reconciling a gateway under 300 running transfers is how a whole batch dies.

When the cluster cannot be reached, the second question is unanswerable. That is
reported as such rather than assumed either way: interactively the operator can
accept the risk, unattended the command stops with BLOCKED.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .cluster import ARGO_NAMESPACE, ClusterAccess
from .exits import CliError, ExitCode, Remedy
from .output import Emitter
from .shell import Runner, subprocess_runner

ACTIVE_PHASES = ("Running", "Pending")
TRANSFER_STEP_LABEL = "cloudpipe.io/step=globus-transfer"


def _answer(prompt_fn) -> str:
    """Read the operator's answer.

    `input` is resolved here rather than as a default argument on purpose: a
    default is bound when the function is defined, so a test (or a wizard)
    replacing `input` afterwards would be silently ignored and the prompt would
    block on a terminal that is not there.
    """
    return (prompt_fn or input)("Continue? [y/N] ").strip().lower()


@dataclass(frozen=True)
class WorkflowActivity:
    """What is running. `known=False` means nobody could look."""

    known: bool
    workflows: tuple[str, ...] = ()
    transfer_pods: int = 0
    detail: dict = field(default_factory=dict)

    @property
    def busy(self) -> bool:
        return bool(self.workflows) or self.transfer_pods > 0


def workflow_activity(
    access: ClusterAccess,
    runner: Runner = subprocess_runner,
    *,
    namespace: str = ARGO_NAMESPACE,
) -> WorkflowActivity:
    """Which cloudpipe workflows are active, and how many transfer pods are up."""
    if not access.reachable:
        return WorkflowActivity(known=False, detail={"reason": "cluster unreachable"})

    workflows: list[str] = []
    result = runner(
        [
            "kubectl",
            "get",
            "workflows.argoproj.io",
            "-n",
            namespace,
            "-o",
            "json",
            "--request-timeout=15s",
        ]
    )
    if not result.ok:
        return WorkflowActivity(known=False, detail={"reason": result.output[:300]})
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return WorkflowActivity(known=False, detail={"reason": "unparseable kubectl output"})
    for item in payload.get("items", []):
        phase = ((item.get("status") or {}).get("phase")) or ""
        if phase in ACTIVE_PHASES:
            workflows.append(str((item.get("metadata") or {}).get("name", "")))

    pods = runner(
        [
            "kubectl",
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            TRANSFER_STEP_LABEL,
            "--field-selector=status.phase=Running",
            "-o",
            "json",
            "--request-timeout=15s",
        ]
    )
    transfer_pods = 0
    if pods.ok:
        try:
            transfer_pods = len(json.loads(pods.stdout or "{}").get("items", []))
        except json.JSONDecodeError:
            transfer_pods = 0

    return WorkflowActivity(known=True, workflows=tuple(workflows), transfer_pods=transfer_pods)


def ask(
    emitter: Emitter,
    *,
    action: str,
    non_interactive: bool,
    assume_yes: bool,
    prompt_fn=None,
) -> bool:
    """Agreement to `action`, as a yes or no. Never raises.

    For decisions where "no" is an ordinary answer rather than a refusal —
    whether to stop an instance afterwards, for instance. Unattended, silence is
    "no": a command with nobody watching must not take an optional action just
    because nobody said not to.
    """
    if assume_yes:
        return True
    if non_interactive:
        return False
    emitter.note(f"About to {action}.")
    return _answer(prompt_fn) in ("y", "yes")


def require(
    emitter: Emitter,
    *,
    action: str,
    non_interactive: bool,
    assume_yes: bool,
    reason: str = "a confirmation is required",
    detail: dict | None = None,
    prompt_fn=None,
) -> None:
    """`ask`, where a no is a refusal: raises `CliError(BLOCKED)`.

    BLOCKED rather than a failure code, in both the declined and the unattended
    case, because both mean the same thing to a caller: a human decision that has
    not been made yet.
    """
    if assume_yes:
        return

    if non_interactive:
        raise CliError(
            "guard.confirmation_required",
            f"Refusing to {action}: {reason}, and --non-interactive was set without --yes.",
            exit_code=ExitCode.BLOCKED,
            remedy=Remedy(
                "human",
                "re-run with --yes to accept, or connect WARP so the check can run",
            ),
            gate="operator_confirmation",
            detail=(detail or {}) | {"action": action},
        )

    emitter.note(f"About to {action}.")
    if _answer(prompt_fn) not in ("y", "yes"):
        raise CliError(
            "guard.declined",
            f"Declined: {action} was not performed.",
            exit_code=ExitCode.BLOCKED,
            gate="operator_confirmation",
        )


def confirm(
    emitter: Emitter,
    *,
    action: str,
    activity: WorkflowActivity,
    non_interactive: bool,
    assume_yes: bool,
    prompt_fn=None,
) -> None:
    """Require agreement before a mutation, having first reported what is running."""
    if activity.known and activity.busy:
        emitter.note(
            f"warning: {len(activity.workflows)} workflow(s) active and "
            f"{activity.transfer_pods} transfer pod(s) running."
        )
        for name in activity.workflows[:10]:
            emitter.note(f"  - {name}")
        if len(activity.workflows) > 10:
            emitter.note(f"  … and {len(activity.workflows) - 10} more")
    elif not activity.known:
        emitter.note(
            "warning: could not check for running workflows — the cluster was unreachable. "
            "Connect Cloudflare WARP to check."
        )

    if assume_yes and not activity.known:
        emitter.note("proceeding on --yes without a workflow check.")

    require(
        emitter,
        action=action,
        non_interactive=non_interactive,
        assume_yes=assume_yes,
        reason=(
            "running workflows could not be checked (cluster unreachable)"
            if not activity.known
            else "a confirmation is required"
        ),
        detail={"workflows_known": activity.known},
        prompt_fn=prompt_fn,
    )
