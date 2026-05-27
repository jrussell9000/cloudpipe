"""
Prefect flow: kubecost-cost-scraper

Runs nightly at 02:00 UTC to fetch yesterday's per-subject cost allocations
from the in-cluster Kubecost API and write CostAllocation JSON records to S3.

Requires:
  - Kubecost deployed in the `kubecost` namespace (label-based cost allocation
    configured with `subjectid` label on all cloudpipe workflow pods)
  - The Prefect worker SA to have S3 write access to {bucket}/metrics/costs/*

Deployment:
  Scheduled via Prefect deployment (see images/prefect-flow-runner/prefect.yaml).
  Must be run from inside the EKS cluster to reach the Kubecost in-cluster URL.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from prefect import flow, task, get_run_logger

# Tools/metrics lives two levels up from prefect/flows/.
# Add it to sys.path so the scraper can import schemas and writer.
_METRICS_PATH = str(Path(__file__).resolve().parents[2] / "tools" / "metrics")
if _METRICS_PATH not in sys.path:
    sys.path.insert(0, _METRICS_PATH)

from kubecost_scraper import scrape_and_upload, KUBECOST_BASE  # type: ignore[import-not-found]

log = logging.getLogger(__name__)


@task(name="scrape-kubecost", retries=2, retry_delay_seconds=60)
def scrape_task(
    bucket: str,
    region: str,
    base_url: str,
    pipeline: str,
) -> int:
    logger = get_run_logger()
    n = scrape_and_upload(bucket=bucket, region=region, base_url=base_url, pipeline=pipeline)
    logger.info("Wrote %d cost allocation record(s)", n)
    return n


@flow(name="kubecost-cost-scraper", log_prints=True)
def kubecost_cost_scraper(
    bucket: str = "abcd-v7",
    region: str = "<YOUR_AWS_REGION>",
    base_url: str = KUBECOST_BASE,
    pipeline: str = "cloudpipe_minproc",
) -> int:
    """Fetch yesterday's Kubecost allocations and write to S3 metrics/costs/.

    Parameters
    ----------
    bucket:
        S3 bucket name (default: abcd-v7, the main cloudpipe bucket).
    region:
        AWS region for S3 writes.
    base_url:
        Kubecost cost-analyzer base URL (in-cluster default).
    pipeline:
        Pipeline label to embed in each CostAllocation record.
    """
    return scrape_task(bucket=bucket, region=region, base_url=base_url, pipeline=pipeline)
