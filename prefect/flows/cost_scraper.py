"""
Prefect flow: kubecost-cost-scraper

Runs nightly at 02:00 UTC to fetch yesterday's per-subject cost allocations
from the in-cluster Kubecost API and write CostAllocation JSON records to S3.

Scrapes the same window at two grains: CostAllocation per workflow into
metrics/costs/, and PodCost per pod into metrics/pod-costs/ — the latter
carrying the per-template `cloudpipe.io/step` label so cost can be broken down
by pipeline component. The pod pass is best-effort and cannot fail the flow.

Each run also re-scrapes the report-date from SETTLED_AGE_DAYS ago at both
grains, overwriting its day+1 records with Kubecost's reconciled values — a
date's cost stops moving at age 3, so that second read is its last. The
metrics-compactor deployment's lookback_days is sized to re-compact that day;
if it ever shrinks below SETTLED_AGE_DAYS + 1, the Parquet copy keeps the
day+1 numbers while the raw JSON holds settled ones and the two disagree.

Requires:
  - Kubecost deployed in the `kubecost` namespace (label-based cost allocation
    configured with `subjectid` label on all cloudpipe workflow pods)
  - The Prefect worker SA to have S3 write access to {bucket}/metrics/costs/*
    and {bucket}/metrics/pod-costs/* (the worker policy is bucket-wide, so the
    new prefix needed no IAM change)

Deployment:
  Scheduled via Prefect deployment (see images/prefect-flow-runner/prefect.yaml).
  Must be run from inside the EKS cluster to reach the Kubecost in-cluster URL.
"""

from __future__ import annotations

import logging
import sys
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

from prefect import flow, get_run_logger, task

# In the flow-runner image, src/metrics/ is COPYed to /opt/prefect/metrics and
# placed on PYTHONPATH (see images/prefect-flow-runner/Dockerfile). For local dev
# outside the image, add src/metrics/ from the repo so imports resolve the same way.
_METRICS_PATH = str(Path(__file__).resolve().parents[2] / "src" / "metrics")
if _METRICS_PATH not in sys.path:
    sys.path.insert(0, _METRICS_PATH)

# E402 is expected and required: these resolve only via the sys.path insert above.
from kubecost_drift_probe import probe_and_upload  # type: ignore[import-not-found]  # noqa: E402
from kubecost_scraper import (  # type: ignore[import-not-found]  # noqa: E402
    KUBECOST_BASE,
    SETTLED_AGE_DAYS,
    scrape_and_upload,
    scrape_pod_costs_and_upload,
)

log = logging.getLogger(__name__)


@task(name="scrape-kubecost", retries=2, retry_delay_seconds=60)
def scrape_task(
    bucket: str,
    region: str,
    base_url: str,
    pipeline: str,
    report_date: _date | None,
) -> int:
    logger = get_run_logger()
    n = scrape_and_upload(
        bucket=bucket,
        region=region,
        base_url=base_url,
        pipeline=pipeline,
        report_date=report_date,
    )
    logger.info("Wrote %d cost allocation record(s)", n)
    return n


@task(name="scrape-kubecost-pods", retries=2, retry_delay_seconds=60)
def scrape_pods_task(
    bucket: str,
    region: str,
    base_url: str,
    pipeline: str,
    report_date: _date | None,
) -> int:
    """Write the per-pod grain of the same window to metrics/pod-costs/.

    Separate task from scrape_task, not an extension of it: this pass is the
    one that parses per-template pod labels, so it is the one that can break
    on a template change. Keeping it separate (and best-effort in the flow)
    means a labeling regression costs a night of component breakdown, not a
    night of cost data.
    """
    logger = get_run_logger()
    n = scrape_pod_costs_and_upload(
        bucket=bucket,
        region=region,
        base_url=base_url,
        pipeline=pipeline,
        report_date=report_date,
    )
    logger.info("Wrote %d pod cost record(s)", n)
    return n


@task(name="rescrape-settled-costs", retries=2, retry_delay_seconds=60)
def settled_rescrape_task(
    bucket: str,
    region: str,
    base_url: str,
    pipeline: str,
    report_date: _date,
    scrape_pod_costs: bool,
) -> int:
    """Re-read a report-date whose Kubecost reconciliation has settled.

    Overwrites that date's day+1 records in place at both grains. The day+1
    read overstates settled cost by 7-14%; reconciliation converges at age
    SETTLED_AGE_DAYS and never moves after, so this second read is final and
    no third one is scheduled. `scrape_age_days` on the rewritten records
    becomes 3, which is how a query tells a settled record from a day+1 one.

    Both scrape functions refuse to write when the response is far smaller
    than what is already stored (PartialReadError), so the failure mode this
    replaces — a partial Kubecost response silently overwriting good records
    with near-zero cost — cannot happen here.

    Best-effort in the flow: if this date never gets its settled read, the
    day+1 records simply stand, and the date can be recovered by hand with
    `kubecost_scraper.py --bucket <b> --date <report_date>`.
    """
    logger = get_run_logger()
    n = scrape_and_upload(
        bucket=bucket,
        region=region,
        base_url=base_url,
        pipeline=pipeline,
        report_date=report_date,
    )
    logger.info("Settled re-scrape of %s rewrote %d cost allocation record(s)", report_date, n)

    if scrape_pod_costs:
        n_pods = scrape_pod_costs_and_upload(
            bucket=bucket,
            region=region,
            base_url=base_url,
            pipeline=pipeline,
            report_date=report_date,
        )
        logger.info("Settled re-scrape of %s rewrote %d pod cost record(s)", report_date, n_pods)

    return n


@task(name="probe-cost-drift", retries=1, retry_delay_seconds=60)
def probe_task(bucket: str, region: str, base_url: str, lookback_days: int) -> int:
    """Record a Kubecost reconciliation snapshot for recent report-dates.

    Read-only on Kubecost; writes only to metrics/cost-drift-probe/. Failure here
    must not fail the nightly scrape, so the flow calls this with return_state.
    """
    logger = get_run_logger()
    n = probe_and_upload(
        bucket=bucket,
        region=region,
        base_url=base_url,
        lookback_days=lookback_days,
    )
    logger.info("Recorded drift-probe snapshot for %d report-date(s)", n)
    return n


@flow(name="kubecost-cost-scraper", log_prints=True)
def kubecost_cost_scraper(
    bucket: str = "cloudpipe-metrics",
    region: str = "<YOUR_AWS_REGION>",
    base_url: str = KUBECOST_BASE,
    pipeline: str = "cloudpipe_minproc",
    date: str = "",
    drift_probe_lookback_days: int = 21,
    scrape_pod_costs: bool = True,
    settled_rescrape: bool = True,
) -> int:
    """Fetch Kubecost allocations and write to S3 metrics/costs/.

    Runs three passes over Kubecost each night:
      1. the day+1 scrape of yesterday, at both grains;
      2. a settled re-scrape of the report-date SETTLED_AGE_DAYS ago, which
         overwrites that date's day+1 records with reconciled values;
      3. the drift-probe snapshot (metrics/cost-drift-probe/), which is what
         measured the age-3 convergence that passes 2 is timed on.
    Passes 2 and 3 are best-effort and never fail the nightly scrape.

    Parameters
    ----------
    bucket:
        S3 bucket name (default: <YOUR_S3_BUCKET>, the main cloudpipe bucket).
    region:
        AWS region for S3 writes.
    base_url:
        Kubecost cost-analyzer base URL (in-cluster default).
    pipeline:
        Pipeline label to embed in each CostAllocation record.
    date:
        ISO 8601 date to scrape (e.g. "2026-06-30"). Defaults to yesterday.
    drift_probe_lookback_days:
        How many recent report-dates the drift probe samples each run. Set to 0
        to skip the probe.
    scrape_pod_costs:
        Also scrape the per-pod grain into metrics/pod-costs/, which is what
        makes cost sliceable by pipeline component. Best-effort — see
        scrape_pods_task.
    settled_rescrape:
        Re-scrape the report-date SETTLED_AGE_DAYS ago with its reconciled
        values. Skipped when `date` is set: an explicit-date run is a backfill
        of that one date and must not also rewrite an unrelated one.

    Returns the workflow-grain record count (not the pod count), so the flow's
    return value keeps its existing meaning for anything reading it.
    """
    report_date = _date.fromisoformat(date) if date else None
    n = scrape_task(
        bucket=bucket,
        region=region,
        base_url=base_url,
        pipeline=pipeline,
        report_date=report_date,
    )

    if scrape_pod_costs:
        scrape_pods_task(
            bucket=bucket,
            region=region,
            base_url=base_url,
            pipeline=pipeline,
            report_date=report_date,
            return_state=True,
        )

    if settled_rescrape and report_date is None:
        # Best-effort, same reasoning as the probe below: this rewrites records
        # that are already usable, so it must never cost the night's scrape.
        settled_rescrape_task(
            bucket=bucket,
            region=region,
            base_url=base_url,
            pipeline=pipeline,
            report_date=_date.today() - timedelta(days=SETTLED_AGE_DAYS),
            scrape_pod_costs=scrape_pod_costs,
            return_state=True,
        )

    if drift_probe_lookback_days > 0:
        # Best-effort: return_state=True returns the terminal State instead of
        # raising, so a probe failure never fails the nightly scrape.
        probe_task(
            bucket=bucket,
            region=region,
            base_url=base_url,
            lookback_days=drift_probe_lookback_days,
            return_state=True,
        )

    return n
