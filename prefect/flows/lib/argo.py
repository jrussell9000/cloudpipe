"""Argo Workflows API helpers — running count and workflow submission."""

from hera.workflows import Workflow, WorkflowsService
from hera.workflows.models import Parameter, WorkflowCreateRequest, WorkflowTemplateRef

ARGO_SERVER    = "http://argo-workflows-server.argo-workflows.svc.cluster.local:2746"
ARGO_NAMESPACE = "argo-workflows"
_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


def _token() -> str:
    try:
        with open(_SA_TOKEN_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _service() -> WorkflowsService:
    return WorkflowsService(host=ARGO_SERVER, namespace=ARGO_NAMESPACE, token=_token())


def count_running() -> int:
    """Return number of workflows currently in Running or Pending phase."""
    wfs = _service().list_workflows(namespace=ARGO_NAMESPACE, fields="items.status.phase")
    return sum(
        1 for w in (wfs.items or [])
        if (w.status and w.status.phase or "") in ("Running", "Pending")
    )


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
