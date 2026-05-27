"""
Prefect flow: drip-feeds ABCD subjects into the cloudpipe Argo WorkflowTemplate.

Submits directly to the Argo Workflows API — no SQS or Argo Events required.
Concurrency is gated in-process: count_running() blocks until a slot opens.

The Globus destination collection UUID is read from SSM at runtime so that
instance replacements (which update the SSM value) take effect automatically
without redeploying this flow.

Usage
-----
    prefect deployment run cloudpipe-queue-manager/cloudpipe-queue-manager \\
        -p subjects_file=s3://abcd-v7/subjects.csv \\
        -p max_concurrent=50
"""

import time
import boto3
from prefect import flow, task, get_run_logger
from lib.argo import count_running, submit
from lib.subjects import load

SSM_COLLECTION_PARAM = "/cloudpipe/globus/collection-id"
SSM_SOURCE_COLLECTION_PARAM = "/cloudpipe/globus/source-collection-id"
SSM_SOURCE_BASE_PATH_PARAM = "/cloudpipe/globus/source-base-path"
REGION = "<YOUR_AWS_REGION>"


def _read_ssm(name: str) -> str:
    return boto3.client("ssm", region_name=REGION).get_parameter(Name=name)["Parameter"]["Value"]


@task(name="submit-cloudpipe-workflow", retries=2, retry_delay_seconds=10)
def submit_workflow(subj_id: str, **globus_params) -> str:
    return submit("cloudpipe", subj_id, **globus_params)


@flow(name="cloudpipe-queue-manager", log_prints=True)
def cloudpipe_queue_manager(
    subjects_file: str,
    max_concurrent: int = 50,
    poll_interval: int = 30,
    start_index: int = 0,
    end_index: int | None = None,
    globus_source_collection_id: str | None = None,
    globus_source_base_path: str | None = None,
    globus_dest_base_path: str = "/mmps_mproc",
    globus_scan_types: str = '["T1w","T2w","rest","nback"]',
):
    """
    Submit ABCD subjects directly to the cloudpipe Argo WorkflowTemplate.

    Parameters
    ----------
    subjects_file:                local path or s3://bucket/key URI to a CSV of subject IDs.
    max_concurrent:               maximum number of simultaneously active Argo Workflows.
    poll_interval:                seconds to wait between running-count checks when at capacity.
    start_index:                  skip the first N subjects (resume after a pause).
    end_index:                    stop at this index (exclusive); omit to process all.
    globus_source_collection_id:  UUID of the remote Globus source collection.
    globus_source_base_path:      Root path on the source collection (subject ID appended automatically).
    globus_dest_base_path:        Root path on the destination collection.
    globus_scan_types:            JSON array of BIDS scan types, e.g. '["T1w","T2w","rest"]'.
    """
    logger = get_run_logger()

    globus_dest_collection_id = _read_ssm(SSM_COLLECTION_PARAM)
    if globus_source_collection_id is None:
        globus_source_collection_id = _read_ssm(SSM_SOURCE_COLLECTION_PARAM)
    if globus_source_base_path is None:
        globus_source_base_path = _read_ssm(SSM_SOURCE_BASE_PATH_PARAM)
    logger.info(f"Globus dest collection: {globus_dest_collection_id}")

    subjects = load(subjects_file)
    batch = subjects[start_index:end_index]
    total = len(batch)
    logger.info(f"Loaded {len(subjects)} subjects; submitting {total} (indices {start_index}–{end_index or len(subjects)})")

    globus_params = {
        "globus-source-collection-id": globus_source_collection_id,
        "globus-source-base-path": globus_source_base_path,
        "globus-dest-collection-id": globus_dest_collection_id,
        "globus-dest-base-path": globus_dest_base_path,
        "globus-scan-types": globus_scan_types,
        "globus-use-s3-gateway": "true",
    }

    for i, subj_id in enumerate(batch):
        while True:
            running = count_running()
            if running < max_concurrent:
                break
            logger.info(f"[{i}/{total}] {running} active ≥ {max_concurrent} — waiting {poll_interval}s")
            time.sleep(poll_interval)

        name = submit_workflow(subj_id, **globus_params)
        logger.info(f"[{i + 1}/{total}] submitted {subj_id} → {name} ({count_running()} active)")
