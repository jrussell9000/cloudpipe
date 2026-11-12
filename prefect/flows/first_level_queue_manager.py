"""
Prefect flow: drip-feeds ABCD subjects into the fmri-first-level-proc Argo WorkflowTemplate.

Submits directly to the Argo Workflows API — no SQS or Argo Events required.
Concurrency is gated in-process: ConcurrencyGate.count() blocks until a slot opens.

Note: the gate counts ALL active Argo workflows (cloudpipe + first-level).
If both pipelines run simultaneously, set each variable accordingly.

Max concurrency is controlled via a Prefect variable so it can be adjusted
while the flow is running:

    prefect variable set first-level-max-concurrent 15

Usage
-----
    prefect deployment run first-level-queue-manager/first-level-queue-manager

With no ``subjects_file`` the flow walks every subject with a folder under
``derivatives/func/``, so first-level covers exactly what cloudpipe produced; the
completion check (is_completed) skips subjects with nothing to run. Pass a list only
for a deliberate subset, such as a reprocess slice:

    prefect deployment run first-level-queue-manager/first-level-queue-manager \\
        -p subjects_file=s3://<bucket>/config/first-level/<slice>.csv -p missing_only=true

``bucket`` needs no ``-p``: the deployment stores it, from the cloudpipe-config
ConfigMap, when ``pixi run prefect-deploy`` registers it.
"""

import boto3
from botocore.exceptions import ClientError
from lib.argo import (
    ConcurrencyGate,
    cap_reader,
    list_active_names,
    list_active_subjects,
    submit,
    wait_for_slot,
)
from lib.subjects import load
from prefect.variables import Variable

from prefect import flow, get_run_logger, task

FUNC_PREFIX = "derivatives/func"
S3_UPLOAD_PREFIX = "derivatives/first_levels"
# The only existence test for an exploded tree (ADR 017): a prefix listing reads
# the same for a session whose upload died halfway.
MARKER = "_complete.json"

WORKFLOW_TEMPLATE = "fmri-first-level-proc"
MAX_CONCURRENT_VARIABLE = "first-level-max-concurrent"
DEFAULT_MAX_CONCURRENT = 25


def _marked_sessions(s3, bucket: str, prefix: str) -> set[str]:
    """Session names under `prefix` whose `_complete.json` exists."""
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
    sessions = set()
    for cp in resp.get("CommonPrefixes", []):
        try:
            s3.head_object(Bucket=bucket, Key=f"{cp['Prefix']}{MARKER}")
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                continue
            raise
        sessions.add(cp["Prefix"][len(prefix) :].rstrip("/"))
    return sessions


def func_subjects(bucket: str) -> list[str]:
    """Every subject with a folder under derivatives/func/, sorted, so start_index and
    end_index name the same subjects on every run. The default cohort: first-level
    follows cloudpipe's own func output, not a list built from another pipeline's
    coverage (the old first-level-subjects.csv was ABCC fMRIPrep's subjects and left
    117 of ours out)."""
    s3 = boto3.client("s3")
    prefix = f"{FUNC_PREFIX}/"
    subjects, token = [], None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/"}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        subjects += [
            cp["Prefix"][len(prefix) :].rstrip("/") for cp in resp.get("CommonPrefixes", [])
        ]
        if not resp.get("IsTruncated"):
            return sorted(subjects)
        token = resp["NextContinuationToken"]


@task(name="check-first-level-complete")
def is_completed(subj_id: str, bucket: str) -> tuple[bool, str]:
    """(done, why): done when every session with a func `_complete.json` also has a
    first-level `_complete.json`. A subject with no complete func session has
    nothing to run, so it is done too, and `why` says which case it is."""
    subj = subj_id if subj_id.startswith("sub-") else f"sub-{subj_id}"
    s3 = boto3.client("s3")
    func = _marked_sessions(s3, bucket, f"{FUNC_PREFIX}/{subj}/")
    if not func:
        return True, "no complete func session"
    missing = func - _marked_sessions(s3, bucket, f"{S3_UPLOAD_PREFIX}/{subj}/")
    if missing:
        return False, f"no first-level marker for {sorted(missing)}"
    return True, f"all {len(func)} session(s) published"


@task(name="submit-first-level-workflow", retries=2, retry_delay_seconds=10)
def submit_workflow(subj_id: str, missing_only: bool = False) -> str:
    # The template's own default is "false"; only a True here is ever sent, so a
    # submission without it is byte-for-byte the one before missing_only existed.
    params = {"missing-only": "true"} if missing_only else {}
    return submit(WORKFLOW_TEMPLATE, subj_id, **params)


@flow(name="first-level-queue-manager", log_prints=True)
def first_level_queue_manager(
    bucket: str,
    subjects_file: str | None = None,
    poll_interval: int = 30,
    start_index: int = 0,
    end_index: int | None = None,
    missing_only: bool = False,
):
    """
    Submit ABCD subjects directly to the fmri-first-level-proc Argo WorkflowTemplate.

    Parameters
    ----------
    bucket:         the data bucket: func sessions and first-level markers are
                    compared there (is_completed).
                    Stored on the deployment from cloudpipe-config; see prefect/deploy.sh.
    subjects_file:  local path or s3://bucket/key URI to a CSV of subject IDs, for a
                    deliberate subset. Omitted, every subject with func output
                    (func_subjects).
    poll_interval:  seconds to wait between running-count checks when at capacity.
    start_index:    skip the first N subjects (resume after a pause).
    end_index:      stop at this index (exclusive); omit to process all.
    missing_only:   run only each subject's sessions that have no first-level
                    marker (the template's `missing-only`). For a session-scoped
                    reprocess (scripts/reprocess_t1w_mni.py, #1105), so a subject's
                    untouched sessions are not refitted.

    Concurrency
    -----------
    Max concurrent Argo Workflows is read from the Prefect variable
    ``first-level-max-concurrent`` (default 25) on every poll cycle, so it can
    be changed while the flow is running:

        prefect variable set first-level-max-concurrent 15

    The count includes workflows this flow has just submitted but that the Argo
    API has not listed yet, so a fast submission burst cannot outrun the gate
    (#206). The controller's ``namespaceParallelism`` enforces a namespace-wide
    ceiling server-side regardless of what any client does.
    """
    logger = get_run_logger()
    # See cloudpipe_queue_manager: retries are logged so a degrading API is visible
    # before it exhausts the budget.
    gate = ConcurrencyGate(
        lister=lambda: list_active_names(
            on_retry=lambda attempt, exc, delay: logger.warning(
                f"Argo list attempt {attempt} failed ({type(exc).__name__}: {exc}) "
                f"— retrying in {delay:.0f}s"
            )
        )
    )

    if subjects_file:
        subjects, source = load(subjects_file), subjects_file
    else:
        subjects, source = func_subjects(bucket), f"s3://{bucket}/{FUNC_PREFIX}/"
    batch = subjects[start_index:end_index]
    total = len(batch)
    logger.info(
        f"Loaded {len(subjects)} subjects from {source}; submitting {total} "
        f"(indices {start_index}–{end_index or len(subjects)})"
    )

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

    # Read once, before the walk: when Prefect restarts this flow it walks the list from
    # the start, and a subject whose workflow is still running has no marker yet.
    running = list_active_subjects(
        WORKFLOW_TEMPLATE,
        on_retry=lambda attempt, exc, delay: logger.warning(
            f"Argo list attempt {attempt} failed ({type(exc).__name__}: {exc}) — retrying in {delay:.0f}s"
        ),
    )
    if running:
        logger.info(
            f"{len(running)} subject(s) already have an active {WORKFLOW_TEMPLATE} workflow"
        )

    for i, subj_id in enumerate(batch):
        if subj_id.removeprefix("sub-") in running:
            logger.info(f"[{i + 1}/{total}] skipping {subj_id} — its workflow is still running")
            continue
        done, why = is_completed(subj_id, bucket)
        if done:
            logger.info(f"[{i + 1}/{total}] skipping {subj_id} — {why}")
            continue

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

        name = submit_workflow(subj_id, missing_only)
        gate.record(name)
        logger.info(f"[{i + 1}/{total}] submitted {subj_id} → {name} ({active + 1} active)")
