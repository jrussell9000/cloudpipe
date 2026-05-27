"""
Prefect flow: drip-feeds ABCD subjects into the fmri-first-level-proc Argo WorkflowTemplate.

Submits directly to the Argo Workflows API — no SQS or Argo Events required.
Concurrency is gated in-process: count_running() blocks until a slot opens.

Note: count_running() counts ALL active Argo workflows (cloudpipe + first-level).
If both pipelines run simultaneously, set max_concurrent accordingly.

Usage
-----
    prefect deployment run first-level-queue-manager/first-level-queue-manager \\
        -p subjects_file=s3://<YOUR_TEMP_S3_BUCKET>/first-level-subjects.csv \\
        -p max_concurrent=25
"""

import time
import boto3
from prefect import flow, task, get_run_logger
from lib.argo import count_running, submit
from lib.subjects import load

S3_BUCKET = "<YOUR_INPUT_S3_BUCKET>"
S3_UPLOAD_PREFIX = "derivatives/first_levels"


@task(name="check-first-level-complete")
def is_completed(subj_id: str) -> bool:
    """Return True if any first-level output already exists in S3 for this subject."""
    resp = boto3.client("s3").list_objects_v2(
        Bucket=S3_BUCKET,
        Prefix=f"{S3_UPLOAD_PREFIX}/{subj_id}/",
        MaxKeys=1,
    )
    return resp.get("KeyCount", 0) > 0


@task(name="submit-first-level-workflow", retries=2, retry_delay_seconds=10)
def submit_workflow(subj_id: str) -> str:
    return submit("fmri-first-level-proc", subj_id)


@flow(name="first-level-queue-manager", log_prints=True)
def first_level_queue_manager(
    subjects_file: str,
    max_concurrent: int = 25,
    poll_interval: int = 30,
    start_index: int = 0,
    end_index: int | None = None,
):
    """
    Submit ABCD subjects directly to the fmri-first-level-proc Argo WorkflowTemplate.

    Parameters
    ----------
    subjects_file:  local path or s3://bucket/key URI to a CSV of subject IDs.
    max_concurrent: maximum number of simultaneously active Argo Workflows.
    poll_interval:  seconds to wait between running-count checks when at capacity.
    start_index:    skip the first N subjects (resume after a pause).
    end_index:      stop at this index (exclusive); omit to process all.
    """
    logger = get_run_logger()

    subjects = load(subjects_file)
    batch = subjects[start_index:end_index]
    total = len(batch)
    logger.info(f"Loaded {len(subjects)} subjects; submitting {total} (indices {start_index}–{end_index or len(subjects)})")

    for i, subj_id in enumerate(batch):
        if is_completed(subj_id):
            logger.info(f"[{i + 1}/{total}] skipping {subj_id} — outputs already in S3")
            continue

        while True:
            running = count_running()
            if running < max_concurrent:
                break
            logger.info(f"[{i}/{total}] {running} active ≥ {max_concurrent} — waiting {poll_interval}s")
            time.sleep(poll_interval)

        name = submit_workflow(subj_id)
        logger.info(f"[{i + 1}/{total}] submitted {subj_id} → {name} ({count_running()} active)")
