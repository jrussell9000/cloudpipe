"""
Prefect flow: drip-feeds ABCD subjects into the cloudpipe Argo WorkflowTemplate.

Submits directly to the Argo Workflows API — no SQS or Argo Events required.
Concurrency is gated in-process: ConcurrencyGate.count() blocks until a slot opens.
The controller's `namespaceParallelism` is the server-side backstop behind it.

The Globus destination collection UUID is read from SSM at runtime so that
instance replacements (which update the SSM value) take effect automatically
without redeploying this flow.

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

Usage
-----
    prefect deployment run cloudpipe-queue-manager/cloudpipe-queue-manager \\
        -p subjects_file=s3://<YOUR_S3_BUCKET>/subjects_v611.csv
"""

import time

import boto3
from lib.argo import (
    ConcurrencyGate,
    cap_reader,
    list_active_names,
    parse_submission_rate,
    submit,
    wait_for_pace,
    wait_for_slot,
)
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

SSM_COLLECTION_PARAM = "/cloudpipe/globus/collection-id"
SSM_SOURCE_COLLECTION_PARAM = "/cloudpipe/globus/source-collection-id"
SSM_SOURCE_BASE_PATH_PARAM = "/cloudpipe/globus/source-base-path"
REGION = "<YOUR_AWS_REGION>"

MAX_CONCURRENT_VARIABLE = "cloudpipe-max-concurrent"
DEFAULT_MAX_CONCURRENT = 50

# Workflows submitted per minute; `0` disables pacing (#393). The default sits above
# the globus-transfer semaphore's measured drain (8 slots, ~2.4 subjects/min on
# 2026-09-14), so a globus batch never starves its transfers, yet it spreads a
# 300-wide cold start over an hour rather than the 7 minutes that throttled etcd.
# Not yet calibrated against a cold start: tune it live, then record what held.
SUBMISSION_RATE_VARIABLE = "cloudpipe-max-submissions-per-minute"
DEFAULT_SUBMISSIONS_PER_MINUTE = 5.0

# auto | cuda | cpu — see lib/gpu_drought.py and the module docstring.
FASTSURFER_DEVICE_VARIABLE = "cloudpipe-fastsurfer-device"


def _read_ssm(name: str) -> str:
    return boto3.client("ssm", region_name=REGION).get_parameter(Name=name)["Parameter"]["Value"]


# Must match the `ingress-mode` enum in cloudpipe-long-master-workflow-template.yaml
# (tests/prefect/test_ingress_mode.py pins the two together).
INGRESS_MODES = ("globus", "presynced")


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
    # No "ingress-mode" key: the template defaults it to globus, so the production
    # submission stays exactly what it was before #336 — and still submits cleanly
    # to a template ArgoCD has not yet synced to declare the parameter.
    return {
        "globus-source-collection-id": source_collection_id,
        "globus-source-base-path": source_base_path,
        "globus-dest-collection-id": read_ssm(SSM_COLLECTION_PARAM),
        "globus-dest-base-path": dest_base_path,
        "globus-scan-types": scan_types,
        "globus-use-s3-gateway": "true",
    }


@task(name="submit-cloudpipe-workflow", retries=2, retry_delay_seconds=10)
def submit_workflow(subj_id: str, **globus_params) -> str:
    return submit("cloudpipe", subj_id, **globus_params)


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
    ingress_mode: str = "globus",
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
    globus_dest_base_path:        Root path on the destination collection.
    globus_scan_types:            JSON array of BIDS scan types, e.g. '["T1w","T2w","rest"]'.
    batch_label:                  Free-text era/batch label stamped onto every WorkflowRun
                                  metrics record in this submission, e.g. "leg-2". Empty by
                                  default. Set it on any run whose records you will later want
                                  to isolate: without it, eras are separable only by timestamp,
                                  and batches whose runs overlap a date boundary cannot be
                                  told apart by `dt` at all.
    ingress_mode:                 "globus" (default) pulls each subject from the Globus source
                                  collection. "presynced" skips Globus entirely — the data must
                                  already be under s3://<bucket>/mmps_mproc/{subj}/ — validates
                                  the staged input instead, and does NOT delete it on success.
                                  No Globus SSM parameters are read, and the globus_* arguments
                                  are ignored. See docs/data-ingress.md.

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
    params = ingress_params(
        ingress_mode,
        _read_ssm,
        source_collection_id=globus_source_collection_id,
        source_base_path=globus_source_base_path,
        dest_base_path=globus_dest_base_path,
        scan_types=globus_scan_types,
    )
    if ingress_mode == "globus":
        logger.info(f"Globus dest collection: {params['globus-dest-collection-id']}")
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
        name = submit_workflow(subj_id, **globus_params, **{"fastsurfer-device": device})
        last_submitted_at = time.monotonic()
        gate.record(name)
        logger.info(
            f"[{i + 1}/{total}] submitted {subj_id} → {name} "
            f"({active + 1} active, fastsurfer-device={device}, paced {paced_s:.0f}s)"
        )
