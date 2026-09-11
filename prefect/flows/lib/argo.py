"""Argo Workflows API helpers — active-workflow count and workflow submission."""

import time
from collections.abc import Callable

from hera.exceptions import AlreadyExists, BadRequest, Forbidden, NotFound, Unauthorized
from hera.exceptions import NotImplemented as HeraNotImplemented
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

# Retry budget for the read path. A queue manager polls the active count once per
# `poll_interval` for the whole length of a cohort leg — roughly 17,000 calls over a
# six-day run — so a single unretried blip is enough to kill a multi-day flow. That
# is not hypothetical: leg 2 died at 300/8834 when one `list_workflows` call returned
# `http2: client connection lost` from the API server. Backoff runs 2,4,8,16,32,60s,
# so ~2 minutes of tolerance: long enough for a connection reset or a brief
# argo-server rollout, short enough that a genuine outage still surfaces promptly.
# No jitter — there is exactly one queue manager, so there is no herd to disperse.
READ_RETRY_ATTEMPTS = 6
READ_RETRY_BASE_SECONDS = 2.0
READ_RETRY_MAX_SECONDS = 60.0

# Failures a retry cannot fix: a malformed selector, missing RBAC, a wrong namespace.
# Retrying these would burn the entire backoff budget before surfacing the real
# problem, so they propagate on the first attempt.
PERMANENT_ERRORS = (
    BadRequest,
    Unauthorized,
    Forbidden,
    NotFound,
    HeraNotImplemented,
    AlreadyExists,
)

# How long `wait_for_slot` may go without a usable count before giving up. The read
# path already absorbs ~2 minutes on its own; this is the outer bound for an outage
# that outlasts it, spanning e.g. an EKS control-plane upgrade.
COUNT_FAILURE_BUDGET_SECONDS = 1800.0


def call_with_retries[T](
    call: Callable[[], T],
    *,
    attempts: int = READ_RETRY_ATTEMPTS,
    base_delay: float = READ_RETRY_BASE_SECONDS,
    max_delay: float = READ_RETRY_MAX_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> T:
    """Run `call`, retrying transient API failures with exponential backoff.

    Retries anything that is not in `PERMANENT_ERRORS`, rather than enumerating the
    transient failures. The set of ways a call can fail transiently is open-ended —
    connection resets, HTTP/2 GOAWAY, 502/503 from an ELB, a socket timeout — while
    the set that retrying cannot help is small and closed. A non-transient bug still
    surfaces; it is just delayed by the backoff budget before it re-raises.

    Only safe for idempotent calls. See `submit()` for why the create path opts out.
    """
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except PERMANENT_ERRORS:
            raise
        except Exception as exc:
            if attempt == attempts:
                raise
            delay = min(base_delay * 2 ** (attempt - 1), max_delay)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)
    raise AssertionError("unreachable: loop returns or raises")  # pragma: no cover


def _token() -> str:
    try:
        with open(_SA_TOKEN_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _service() -> WorkflowsService:
    return WorkflowsService(host=ARGO_SERVER, namespace=ARGO_NAMESPACE, token=_token())


def list_active_names(
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> set[str]:
    """Return the names of all workflows in the namespace that have not completed.

    Filters server-side on `workflows.argoproj.io/completed!=true` so the Argo
    server's informer does not have to deserialize every completed workflow still
    retained by the TTL strategy.

    Deliberately does *not* filter on the workflow-level `pipeline` label or on
    `workflows.argoproj.io/phase`: both are written by the workflow controller
    asynchronously, after the create API call has already returned, so any positive
    selector silently misses workflows submitted in the last few seconds (#206).
    The count is namespace-wide across pipelines, as ADR 008 documents.

    Read-only and therefore safe to retry, which it does: this call is the one a
    queue manager makes tens of thousands of times across a cohort leg, and an
    unretried failure here has already killed one.
    """

    def _list() -> set[str]:
        # `_service()` is rebuilt per attempt on purpose: it re-reads the projected
        # service-account token, so a retry that straddles a token refresh picks up
        # the new one instead of replaying the stale credential.
        wfs = _service().list_workflows(
            namespace=ARGO_NAMESPACE,
            label_selector=f"{LABEL_COMPLETED}!=true",
            fields="items.metadata.name",
        )
        return {wf.metadata.name for wf in (wfs.items or []) if wf.metadata.name}

    return call_with_retries(_list, on_retry=on_retry)


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


def cap_reader[T](
    read: Callable[[], T],
    *,
    initial: T,
    on_error: Callable[[Exception, T], None] | None = None,
) -> Callable[[], T]:
    """Wrap a live-tunable read so a transient failure holds the last good value.

    The cap is deliberately re-read every poll cycle so it can be retuned while a
    flow is running, which puts a second network call on the same hot path as the
    workflow count and gives it the same fragility. Losing it is not worth killing a
    multi-day run over: the value last read is still a correct cap, merely a possibly
    stale one. Kept free of any Prefect import so `lib` stays testable without a
    server — the caller injects the actual variable read.

    Generic over the value because the same posture serves every hot-reconfigurable
    Prefect Variable the queue manager reads, not only the integer concurrency cap
    (the `cloudpipe-fastsurfer-device` override is a string, see lib.gpu_drought).
    """
    last = initial

    def read_cap() -> T:
        nonlocal last
        try:
            last = read()
        except Exception as exc:
            if on_error is not None:
                on_error(exc, last)
        return last

    return read_cap


def wait_for_slot(
    gate: ConcurrencyGate,
    read_cap: Callable[[], int],
    *,
    poll_interval: float,
    on_wait: Callable[[int, int], None] | None = None,
    on_count_error: Callable[[Exception, float, float], None] | None = None,
    failure_budget: float = COUNT_FAILURE_BUDGET_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Block until active workflows drop below the cap; return the admitting count.

    A failure to read the count is treated as "cannot admit" rather than as a crash.
    That is the safe direction and the whole point of this helper: submitting without
    a trustworthy count would blow past the cap, whereas waiting only costs time. The
    flow therefore rides out an API outage the same way it rides out a full queue.

    The budget keeps that patience bounded — if the count has been unreadable for
    `failure_budget` seconds continuously, the last error propagates rather than
    letting a flow idle indefinitely against a permanently broken API. Any single
    successful read resets it, so intermittent failures never accumulate toward it.
    """
    failing_since: float | None = None

    while True:
        try:
            active = gate.count()
        except Exception as exc:
            now = clock()
            if failing_since is None:
                failing_since = now
            stalled = now - failing_since
            if stalled >= failure_budget:
                raise
            if on_count_error is not None:
                on_count_error(exc, stalled, failure_budget)
            sleep(poll_interval)
            continue

        failing_since = None
        cap = read_cap()
        if active < cap:
            return active
        if on_wait is not None:
            on_wait(active, cap)
        sleep(poll_interval)


def submit(template_name: str, subj_id: str, **params) -> str:
    """Submit a workflow from a deployed WorkflowTemplate and return its generated name.

    Deliberately *not* wrapped in `call_with_retries`. The workflow name comes from
    `generate_name`, so the call carries no idempotency key: if the server created
    the workflow and the response was lost in transit, a retry submits the subject a
    second time rather than recovering the first. Retrying reads is free; retrying
    this create would silently duplicate work. The callers' Prefect task retry is the
    one deliberate exception, and it accepts that risk at a much lower call volume.
    """
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
