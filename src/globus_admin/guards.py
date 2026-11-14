"""What is running in the cluster, for `login` to mention before the credential moves.

Contract 2.0 kept only this. The confirmation guard every mutating command ran
first went with those commands (openspec retire-globus-host): `login` replaces a
credential the operator just authenticated for, and needs no confirmation, but a
transfer running right now is still worth knowing about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .cluster import ARGO_NAMESPACE, ClusterAccess
from .shell import Runner, subprocess_runner

ACTIVE_PHASES = ("Running", "Pending")
# The one ingress step that holds the stored Globus token: the ingress-mode=https
# copy, which lists through Transfer before it reads. (`globus-transfer` held it
# too, until the Globus host was retired.)
TRANSFER_STEP_LABEL = "cloudpipe.io/step in (https-transfer)"


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
