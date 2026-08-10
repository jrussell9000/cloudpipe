"""Argo Workflows API helpers — active-workflow count and workflow submission."""

import time
from collections.abc import Callable

from hera.workflows import Workflow, WorkflowsService
from hera.workflows.models import Parameter, WorkflowCreateRequest, WorkflowTemplateRef

ARGO_SERVER = "http://argo-workflows-server.argo-workflows.svc.cluster.local:2746"
ARGO_NAMESPACE = "argo-workflows"
_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"

# Set to "false" by the controller on first reconcile and "true" when the workflow
# finishes. A `!=true` selector therefore matches active workflows *and* workflows
# the controller has not touched yet (Kubernetes inequality selectors match objects
# where the key is absent), while still excluding completed workflows retained by
# the templates' ttlStrategy.
LABEL_COMPLETED = "workflows.argoproj.io/completed"

# How long a just-submitted workflow name keeps counting toward the cap while it
# has not yet appeared in a list response. Covers create-to-informer-visibility
# latency; a name that never shows up (deleted, or rejected after create) ages out
# instead of inflating the count forever.
IN_FLIGHT_GRACE_SECONDS = 120


def _token() -> str:
    try:
        with open(_SA_TOKEN_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _service() -> WorkflowsService:
    return WorkflowsService(host=ARGO_SERVER, namespace=ARGO_NAMESPACE, token=_token())


def list_active_names() -> set[str]:
    """Return the names of all workflows in the namespace that have not completed.

    Filters server-side on `workflows.argoproj.io/completed!=true` so the Argo
    server's informer does not have to deserialize every completed workflow still
    retained by the TTL strategy.

    Deliberately does *not* filter on the workflow-level `pipeline` label or on
    `workflows.argoproj.io/phase`: both are written by the workflow controller
    asynchronously, after the create API call has already returned, so any positive
    selector silently misses workflows submitted in the last few seconds (#206).
    The count is namespace-wide across pipelines, as ADR 008 documents.
    """
    wfs = _service().list_workflows(
        namespace=ARGO_NAMESPACE,
        label_selector=f"{LABEL_COMPLETED}!=true",
        fields="items.metadata.name",
    )
    return {wf.metadata.name for wf in (wfs.items or []) if wf.metadata.name}


class ConcurrencyGate:
    """Counts active Argo workflows, including ones too recent to be listed yet.

    `list_active_names()` alone is race-free with respect to controller labeling,
    but there is still a window between a successful create call and the object
    becoming visible in the Argo server's informer cache. A submitter that polls
    faster than that window would keep reading a stale count, so names it has
    submitted itself are counted until a list response confirms them.
    """

    def __init__(
        self,
        lister: Callable[[], set[str]] = list_active_names,
        grace_seconds: float = IN_FLIGHT_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lister = lister
        self._grace_seconds = grace_seconds
        self._clock = clock
        self._in_flight: dict[str, float] = {}

    def record(self, name: str) -> None:
        """Note a workflow this process just submitted."""
        if name:
            self._in_flight[name] = self._clock()

    def count(self) -> int:
        """Return active workflows: those listed, plus unconfirmed recent submissions."""
        active = self._lister()
        now = self._clock()
        # Drop names the API can now see (they are already in `active`, so keeping
        # them would double-count) and names that aged out without ever appearing.
        self._in_flight = {
            name: submitted_at
            for name, submitted_at in self._in_flight.items()
            if name not in active and now - submitted_at < self._grace_seconds
        }
        return len(active) + len(self._in_flight)


def submit(template_name: str, subj_id: str, **params) -> str:
    """Submit a workflow from a deployed WorkflowTemplate and return its generated name."""
    parameters = [Parameter(name="subjID", value=subj_id)]
    parameters += [Parameter(name=k, value=v) for k, v in params.items()]

    wf = Workflow(
        generate_name=f"{template_name}-",
        namespace=ARGO_NAMESPACE,
        workflow_template_ref=WorkflowTemplateRef(name=template_name),
        arguments=parameters,
    )
    result = _service().create_workflow(
        namespace=ARGO_NAMESPACE,
        req=WorkflowCreateRequest(workflow=wf.build()),  # type: ignore[arg-type]
    )
    return result.metadata.name or ""
