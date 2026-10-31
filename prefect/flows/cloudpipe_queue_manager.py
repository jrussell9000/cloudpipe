"""
Prefect flow: drip-feeds ABCD subjects into the cloudpipe Argo WorkflowTemplate.

Submits directly to the Argo Workflows API — no SQS or Argo Events required.
Concurrency is gated in-process: ConcurrencyGate.count() blocks until a slot opens.
The controller's `namespaceParallelism` is the server-side backstop behind it.

In https mode the Globus source collection UUID and base path are read from SSM at
runtime (unless the flow's arguments override them), so a change to either takes
effect without redeploying this flow.

Max concurrency is controlled via a Prefect variable so it can be adjusted
while the flow is running:

    prefect variable set cloudpipe-max-concurrent 30

Arrivals are paced separately (#393), so a cold start fills the cap gradually
instead of all at once. Also live, in workflows per minute (`0` disables pacing):

    prefect variable set cloudpipe-max-submissions-per-minute 5

Where FastSurfer segmentation runs is decided per submission (#373): during a GPU
spot drought — GPU pods Pending for 15+ minutes, see lib/gpu_drought.py — new
workflows are submitted with `fastsurfer-device=cpu` so their anatomy runs on
cpu-heavy-nodepool instead of waiting on a pool that is not granting nodes. A
second Prefect variable overrides the detector while the flow is running:

    prefect variable set cloudpipe-fastsurfer-device auto   # default: follow the detector
    prefect variable set cloudpipe-fastsurfer-device cpu    # force the fallback
    prefect variable set cloudpipe-fastsurfer-device cuda   # force the GPU

In https mode the run stops before the first submission if the Globus session
cannot carry the batch (lib/globus_session.py). Two Prefect variables tune it:

    prefect variable set cloudpipe-globus-session-margin-hours 24
    prefect variable set cloudpipe-globus-session-warn-days 5

Usage
-----
    prefect deployment run cloudpipe-queue-manager/cloudpipe-queue-manager \\
        -p subjects_file=s3://<bucket>/subjects.csv
"""

import json
import time

import boto3
from botocore.exceptions import ClientError
from lib import globus_session
from lib.argo import (
    ConcurrencyGate,
    cap_reader,
    list_active_names,
    parse_submission_rate,
    submit,
    wait_for_pace,
    wait_for_slot,
)
from lib.config import deployment_region
from lib.gpu_drought import (
    DEFAULT_MIN_PENDING_AGE_S,
    DEVICE_AUTO,
    DroughtDetector,
    choose_device,
    parse_device_mode,
)
from lib.subjects import load
from prefect.variables import Variable

from prefect import flow, get_run_logger, task

SSM_SOURCE_COLLECTION_PARAM = "/cloudpipe/globus/source-collection-id"
SSM_SOURCE_BASE_PATH_PARAM = "/cloudpipe/globus/source-base-path"

MAX_CONCURRENT_VARIABLE = "cloudpipe-max-concurrent"
DEFAULT_MAX_CONCURRENT = 50

# Workflows submitted per minute; `0` disables pacing (#393). The default sits above
# the https-transfer semaphore's measured drain (4 slots, 112 subjects/h, so ~1.9
# subjects/min, over hour-long windows in plan 017's 2026-10 func-reprocess waves),
# so pacing never starves an https batch's
# transfers, yet it spreads a 300-wide cold start over an hour rather than the 7
# minutes that throttled etcd. Not yet calibrated against a cold start: tune it
# live, then record what held.
SUBMISSION_RATE_VARIABLE = "cloudpipe-max-submissions-per-minute"
DEFAULT_SUBMISSIONS_PER_MINUTE = 5.0

# auto | cuda | cpu — see lib/gpu_drought.py and the module docstring.
FASTSURFER_DEVICE_VARIABLE = "cloudpipe-fastsurfer-device"


def _read_ssm(name: str) -> str:
    return boto3.client("ssm", region_name=deployment_region()).get_parameter(Name=name)[
        "Parameter"
    ]["Value"]


def _read_ssm_optional(name: str) -> str | None:
    """SSM read that treats an ABSENT parameter as "not configured", not an error.

    A denied one is a different thing and is raised. This used to swallow every
    exception, and that is how the session gate once spent months measuring
    against a default timeout: the worker had no `ssm:GetParameter` on the
    gateway's configuration document, the AccessDenied read back as None, and
    the gate fell back to the default on every run.

    "Not configured" is a real state — a deployment that has not logged in yet —
    and the gate answers it by refusing and naming the login. "You are not
    allowed to look" is a misconfiguration of this deployment, and answering it
    with a default is how a safety gate becomes decoration.
    """
    try:
        return _read_ssm(name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise


def _read_secret(secret_id: str) -> dict:
    payload = boto3.client("secretsmanager", region_name=deployment_region()).get_secret_value(
        SecretId=secret_id
    )
    return json.loads(payload["SecretString"])


def _transfer_client():
    """A TransferClient built from the stored refresh token, for the live gate check."""
    import globus_sdk

    secret = _read_secret(globus_session.SECRET_ID)
    native_client = globus_sdk.NativeAppAuthClient(secret["native-app-client-id"])
    authorizer = globus_sdk.RefreshTokenAuthorizer(secret["refresh-token"], native_client)
    return globus_sdk.TransferClient(authorizer=authorizer)


def _https_token():
    """The Auth client and the stored HTTPS refresh token (None if the login granted none)."""
    import globus_sdk

    secret = _read_secret(globus_session.SECRET_ID)
    native_client = globus_sdk.NativeAppAuthClient(secret["native-app-client-id"])
    return native_client, secret.get("https-refresh-token")


def check_https_session(
    logger,
    *,
    source_collection_id: str,
    source_base_path: str,
    read_ssm=_read_ssm_optional,
    read_variable=None,
    transfer_client_factory=_transfer_client,
    https_token_factory=_https_token,
) -> globus_session.GateDecision:
    """The `ingress-mode=https` gate: refuse the batch before the first submission.

    Raises `RuntimeError` when the batch must not start. That ends the flow run
    with the reason in the failure, which is the only place an operator reliably
    looks — a warning in the logs of a run that submitted anyway is how 300
    workflows died unnoticed on 2026-08-17.

    Life is measured against our 30-day rule, not the source collection's own
    timeout, which is a year. The age check cannot see a revoked session, so a
    live check follows it: one listing of the source base path with the transfer
    token, then one refresh of the HTTPS token — a login can grant the first and
    not the second.

    The ingress pod checks the age again before any request. This gate is the
    courtesy that refuses a batch once; the pod is the control, because a hand
    `argo submit` never passes through here.
    """
    margin_hours, warn_days = _gate_thresholds(read_variable)
    decision = globus_session.evaluate(
        read_ssm(globus_session.SSM_SESSION_PARAM),
        timeout_minutes=globus_session.HTTPS_RULE_MINUTES,
        margin_hours=margin_hours,
        warn_days=warn_days,
    )
    logger.info(
        f"Globus session limit: {globus_session.HTTPS_RULE_MINUTES} minutes, from "
        f"{globus_session.HTTPS_RULE_SOURCE}"
    )
    logger.info(decision.reason)
    if not decision.allowed:
        raise RuntimeError(
            f"Refusing to submit: {decision.reason} (The limit is "
            f"{globus_session.HTTPS_RULE_SOURCE}, not the source collection's timeout.)"
        )

    try:
        reachable, detail = globus_session.source_reachable(
            transfer_client_factory(), source_collection_id, source_base_path
        )
        if reachable:
            logger.info(detail)
            reachable, detail = globus_session.https_token_refreshes(
                *https_token_factory(), source_collection_id
            )
    except ImportError:
        logger.warning(
            "globus-sdk is not installed in this image, so the live source and HTTPS token "
            "checks were skipped. The session's age was checked; a revoked session or a "
            "missing HTTPS grant would not be caught."
        )
        return decision
    if not reachable:
        raise RuntimeError(f"Refusing to submit: {detail}")
    logger.info(detail)
    return decision


def _gate_thresholds(read_variable) -> tuple[float, float]:
    """The margin (hours) and warning window (days), from Prefect variables."""
    get_variable = read_variable or (lambda name, default: Variable.get(name, default=default))
    margin_hours = _as_float(
        get_variable(globus_session.MARGIN_VARIABLE, globus_session.DEFAULT_MARGIN_HOURS),
        globus_session.DEFAULT_MARGIN_HOURS,
    )
    warn_days = _as_float(
        get_variable(globus_session.WARN_VARIABLE, globus_session.DEFAULT_WARN_DAYS),
        globus_session.DEFAULT_WARN_DAYS,
    )
    return margin_hours, warn_days


def _as_float(value, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


# Must match the `ingress-mode` enum in cloudpipe-long-master-workflow-template.yaml
# (tests/prefect/test_ingress_mode.py pins the two together). `globus` left both
# with the Globus host (openspec retire-globus-host), so it is now an unknown mode.
INGRESS_MODES = ("https", "presynced")


def ingress_params(
    ingress_mode: str,
    read_ssm,
    *,
    source_collection_id: str | None,
    source_base_path: str | None,
    dest_base_path: str,
    scan_types: str,
) -> dict[str, str]:
    """The ingress half of a cloudpipe submission's parameters (#336).

    Raises ValueError for an unknown mode before anything is submitted. Argo does
    not enforce the template's enum, and while the DAG does fail a misspelt mode,
    it does so one workflow at a time — a typo here would burn the whole batch.

    In presynced mode no SSM parameter is read: a deployment with no Globus at all
    has none to read. The globus-* workflow parameters are still sent, empty,
    because the template declares them without defaults and Argo rejects a
    submission that omits them.

    In https mode only the source collection and base path are read. The copy
    goes straight to S3, so there is no destination collection in the path;
    `globus-dest-collection-id` is still sent, empty, because the template keeps
    it declared (deprecated, unread) for parameter sets from older submitters.
    """
    if ingress_mode not in INGRESS_MODES:
        raise ValueError(f"ingress_mode={ingress_mode!r} is not one of {list(INGRESS_MODES)}")
    if ingress_mode == "presynced":
        return {
            "ingress-mode": ingress_mode,
            "globus-source-collection-id": "",
            "globus-source-base-path": "",
            "globus-dest-collection-id": "",
            "globus-dest-base-path": "",
            "globus-scan-types": "",
        }
    if source_collection_id is None:
        source_collection_id = read_ssm(SSM_SOURCE_COLLECTION_PARAM)
    if source_base_path is None:
        source_base_path = read_ssm(SSM_SOURCE_BASE_PATH_PARAM)
    # The mode is sent explicitly even though it is the template's default, so a
    # submission never depends on which default the synced template carries.
    return {
        "ingress-mode": ingress_mode,
        "globus-source-collection-id": source_collection_id,
        "globus-source-base-path": source_base_path,
        "globus-dest-collection-id": "",
        "globus-dest-base-path": dest_base_path,
        "globus-scan-types": scan_types,
    }


# The WorkflowTemplates this flow may submit. `confounds-rerun` is the one-off
# confounds-only re-run (scripts/rerun_confounds.py); it declares every parameter
# the master takes, so one submission shape serves both
# (tests/prefect/test_workflow_template.py holds them together).
WORKFLOW_TEMPLATES = ("cloudpipe", "confounds-rerun")


def check_workflow_template(workflow_template: str, ingress_mode: str, batch_label: str) -> None:
    """Refuse a template this flow does not submit, or one submitted the wrong way.

    confounds-rerun stages its input over HTTPS only, and reads its plan from
    config/confounds-rerun/{batch_label}/, so a run without a label would submit
    every subject to a pod that finds no plan.
    """
    if workflow_template not in WORKFLOW_TEMPLATES:
        raise ValueError(
            f"workflow_template={workflow_template!r} is not one of {list(WORKFLOW_TEMPLATES)}"
        )
    if workflow_template == "confounds-rerun":
        if ingress_mode != "https":
            raise ValueError(f"confounds-rerun needs ingress_mode='https', not {ingress_mode!r}")
        if not batch_label:
            raise ValueError("confounds-rerun needs batch_label: it is the plan's label")


@task(name="submit-cloudpipe-workflow", retries=2, retry_delay_seconds=10)
def submit_workflow(workflow_template: str, subj_id: str, **globus_params) -> str:
    return submit(workflow_template, subj_id, **globus_params)


@flow(name="cloudpipe-queue-manager", log_prints=True)
def cloudpipe_queue_manager(
    subjects_file: str,
    poll_interval: int = 30,
    start_index: int = 0,
    end_index: int | None = None,
    globus_source_collection_id: str | None = None,
    globus_source_base_path: str | None = None,
    globus_dest_base_path: str = "/mmps_mproc",
    globus_scan_types: str = '["T1w","T2w","rest","nback"]',
    batch_label: str = "",
    ingress_mode: str = "https",
    workflow_template: str = "cloudpipe",
):
    """
    Submit ABCD subjects directly to the cloudpipe Argo WorkflowTemplate.

    Parameters
    ----------
    subjects_file:                local path or s3://bucket/key URI to a CSV of subject IDs.
    poll_interval:                seconds to wait between running-count checks when at capacity.
    start_index:                  skip the first N subjects (resume after a pause).
    end_index:                    stop at this index (exclusive); omit to process all.
    globus_source_collection_id:  UUID of the remote Globus source collection.
    globus_source_base_path:      Root path on the source collection (subject ID appended automatically).
    globus_dest_base_path:        Staging prefix in the data bucket (subject ID appended); the
                                  name is kept from the retired Globus ingress.
    globus_scan_types:            JSON array of BIDS scan types, e.g. '["T1w","T2w","rest"]'.
    batch_label:                  Free-text era/batch label stamped onto every WorkflowRun
                                  metrics record in this submission, e.g. "leg-2". Empty by
                                  default. Set it on any run whose records you will later want
                                  to isolate: without it, eras are separable only by timestamp,
                                  and batches whose runs overlap a date boundary cannot be
                                  told apart by `dt` at all.
    ingress_mode:                 "https" (default) lists each subject on the Globus source
                                  collection through Globus Transfer, then copies it over the
                                  collection's HTTPS server straight to S3. The batch is refused
                                  if the last `pixi run globus login` would pass 30 days within
                                  the safety margin. "presynced" skips ingress entirely — the
                                  data must already be under s3://<bucket>/mmps_mproc/{subj}/ —
                                  validates the staged input instead, and does NOT delete it on
                                  success. No Globus SSM parameters are read, and the globus_*
                                  arguments are ignored. Any other value, including the retired
                                  "globus", is refused before anything is submitted. See
                                  docs/data-ingress.md.
    workflow_template:            "cloudpipe" (default), or "confounds-rerun" for the one-off
                                  confounds-only re-run, which needs ingress_mode "https" and
                                  batch_label = the plan's label. scripts/rerun_confounds.py
                                  submit sets all three.

    Concurrency
    -----------
    Max concurrent Argo Workflows is read from the Prefect variable
    ``cloudpipe-max-concurrent`` (default 50) on every poll cycle, so it can
    be changed while the flow is running:

        prefect variable set cloudpipe-max-concurrent 30

    The count includes workflows this flow has just submitted but that the Argo
    API has not listed yet, so a fast submission burst cannot outrun the gate
    (#206). The controller's ``namespaceParallelism`` enforces a namespace-wide
    ceiling server-side regardless of what any client does.

    Submission pacing (#393)
    ------------------------
    The cap bounds standing population, not arrival rate. Submissions are also
    spaced to at most ``cloudpipe-max-submissions-per-minute`` (default 5, ``0``
    disables), re-read while waiting, so a cold start ramps up instead of creating
    the cap's whole width at once. A failed or malformed read holds the last good
    rate. Each "submitted" log line reports how long pacing held that submission.

    GPU drought fallback (#373)
    ---------------------------
    Each submission carries ``fastsurfer-device``. In the default ``auto`` mode
    it is ``cuda`` until GPU pods in ``argo-workflows`` have been Pending for
    15+ minutes in numbers (10 to enter, 3 to leave — hysteresis), then ``cpu``
    so the subject's FastSurfer segmentation runs on cpu-heavy-nodepool instead.
    The Prefect variable ``cloudpipe-fastsurfer-device`` (``auto``/``cuda``/``cpu``)
    is re-read every submission and overrides the detector. A failed read of
    either the variable or the pod list holds the previous decision; neither
    can end the leg.
    """
    logger = get_run_logger()
    # Retries inside the count are logged rather than silent: a run that is quietly
    # retrying every poll is a degrading API, and that should be visible long before
    # it exhausts the budget and ends the leg.
    gate = ConcurrencyGate(
        lister=lambda: list_active_names(
            on_retry=lambda attempt, exc, delay: logger.warning(
                f"Argo list attempt {attempt} failed ({type(exc).__name__}: {exc}) "
                f"— retrying in {delay:.0f}s"
            )
        )
    )

    # First, before loading subjects or touching Argo: a bad mode must end the run,
    # not reach a submission.
    check_workflow_template(workflow_template, ingress_mode, batch_label)
    if workflow_template != "cloudpipe":
        logger.info(f"Workflow template: {workflow_template}")
    params = ingress_params(
        ingress_mode,
        _read_ssm,
        source_collection_id=globus_source_collection_id,
        source_base_path=globus_source_base_path,
        dest_base_path=globus_dest_base_path,
        scan_types=globus_scan_types,
    )
    if ingress_mode == "https":
        # Before the subject list, before Argo: a batch that cannot transfer must
        # not exist. In presynced mode there is no Globus to check.
        logger.info(
            f"Ingress mode https: source {params['globus-source-collection-id']} read over "
            "HTTPS, straight to S3"
        )
        check_https_session(
            logger,
            source_collection_id=params["globus-source-collection-id"],
            source_base_path=params["globus-source-base-path"],
        )
    else:
        logger.info(
            f"Ingress mode {ingress_mode}: Globus skipped; staged input is validated, not deleted"
        )

    subjects = load(subjects_file)
    batch = subjects[start_index:end_index]
    total = len(batch)
    logger.info(
        f"Loaded {len(subjects)} subjects; submitting {total} (indices {start_index}–{end_index or len(subjects)})"
    )

    globus_params = {**params, "batch-label": batch_label}
    if batch_label:
        logger.info(f"Batch label: {batch_label}")

    read_cap = cap_reader(
        lambda: int(
            str(Variable.get(MAX_CONCURRENT_VARIABLE, default=str(DEFAULT_MAX_CONCURRENT)))
        ),
        initial=DEFAULT_MAX_CONCURRENT,
        on_error=lambda exc, held: logger.warning(
            f"Could not read {MAX_CONCURRENT_VARIABLE} ({type(exc).__name__}: {exc}) "
            f"— holding cap at {held}"
        ),
    )

    # Parsed inside the read for the same reason as the device mode below: a
    # malformed rate raises, and cap_reader holds the last good one.
    read_rate = cap_reader(
        lambda: parse_submission_rate(
            Variable.get(SUBMISSION_RATE_VARIABLE, default=str(DEFAULT_SUBMISSIONS_PER_MINUTE))
        ),
        initial=DEFAULT_SUBMISSIONS_PER_MINUTE,
        on_error=lambda exc, held: logger.warning(
            f"Could not read {SUBMISSION_RATE_VARIABLE} ({type(exc).__name__}: {exc}) "
            f"— holding submission rate at {held}/min"
        ),
    )
    rate = read_rate()
    logger.info(
        f"Submission pacing: {rate:g}/min" if rate else "Submission pacing: disabled (rate 0)"
    )

    # A malformed value raises inside the read, so cap_reader holds the last good
    # mode rather than submitting something the templates would misread as cuda.
    read_device_mode = cap_reader(
        lambda: parse_device_mode(Variable.get(FASTSURFER_DEVICE_VARIABLE, default=DEVICE_AUTO)),
        initial=DEVICE_AUTO,
        on_error=lambda exc, held: logger.warning(
            f"Could not read {FASTSURFER_DEVICE_VARIABLE} ({type(exc).__name__}: {exc}) "
            f"— holding device mode at {held!r}"
        ),
    )
    drought = DroughtDetector(
        on_change=lambda in_drought, starved, pending: logger.warning(
            f"GPU drought {'ENTERED' if in_drought else 'ended'}: {starved} GPU pods Pending "
            f"≥ {DEFAULT_MIN_PENDING_AGE_S // 60} min (of {pending} Pending) — new submissions "
            f"run FastSurfer segmentation on {'cpu' if in_drought else 'cuda'}"
        ),
        on_error=lambda exc, held: logger.warning(
            f"Could not read Pending GPU pods ({type(exc).__name__}: {exc}) "
            f"— holding drought state at {held}"
        ),
    )

    last_submitted_at: float | None = None
    for i, subj_id in enumerate(batch):
        # Pace before the slot check, not after: the active count must be the last
        # thing read before a submission, or a pacing sleep could let it go stale.
        paced_s = wait_for_pace(last_submitted_at, read_rate, max_sleep=poll_interval)
        active = wait_for_slot(
            gate,
            read_cap,
            poll_interval=poll_interval,
            on_wait=lambda active, cap, i=i: logger.info(
                f"[{i}/{total}] {active} active ≥ {cap} — waiting {poll_interval}s"
            ),
            on_count_error=lambda exc, stalled, budget, i=i: logger.warning(
                f"[{i}/{total}] could not read active count "
                f"({type(exc).__name__}: {exc}) — holding, "
                f"{stalled:.0f}s of {budget:.0f}s budget used"
            ),
        )

        # Decided at submission time, not per poll: the verdict only matters when a
        # workflow is about to be created, and a slot opening is the natural cadence.
        device = choose_device(read_device_mode(), drought.observe())
        name = submit_workflow(
            workflow_template, subj_id, **globus_params, **{"fastsurfer-device": device}
        )
        last_submitted_at = time.monotonic()
        gate.record(name)
        logger.info(
            f"[{i + 1}/{total}] submitted {subj_id} → {name} "
            f"({active + 1} active, fastsurfer-device={device}, paced {paced_s:.0f}s)"
        )
