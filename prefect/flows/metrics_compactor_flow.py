"""
Prefect flow: metrics-compactor

Runs nightly to compact one or more closed days (dt < today) of raw JSON
metrics into per-schema_version Parquet under metrics/compacted/, via
src/metrics/compactor.py. Never touches today's dt= partition, so the raw
path's "queryable immediately" freshness contract (docs/observability.md)
is unaffected by compaction.

Requires:
  - The Prefect worker SA to have S3 list/get/put on {bucket}/metrics/* —
    already granted (see terraform/modules/prefect/iam.tf's `worker` policy,
    same as kubecost-cost-scraper uses).

Deployment:
  Scheduled via Prefect deployment (see prefect/prefect.yaml), at 02:30 UTC —
  30 minutes after kubecost-cost-scraper's 02:00 UTC run, so yesterday's
  `costs` records exist before that day is ever compacted.
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

# E402 is expected and required: this resolves only via the sys.path insert above.
from compactor import RAW_PREFIXES, compact_date  # type: ignore[import-not-found]  # noqa: E402

log = logging.getLogger(__name__)


@task(name="compact-day", retries=2, retry_delay_seconds=60)
def compact_day_task(bucket: str, region: str, dt: str, tables: list[str]) -> dict:
    logger = get_run_logger()
    import boto3

    s3 = boto3.client("s3", region_name=region)
    result = compact_date(s3, bucket, region, dt, tables=tables)
    logger.info("Compacted dt=%s: %s", dt, result)
    return result


@flow(name="metrics-compactor", log_prints=True)
def metrics_compactor(
    bucket: str = "cloudpipe-metrics",
    region: str = "<YOUR_AWS_REGION>",
    date: str = "",
    lookback_days: int = 1,
    tables: str = "",
) -> dict:
    """Compact one or more closed days of raw JSON metrics into Parquet.

    Parameters
    ----------
    bucket:
        S3 bucket name (the metrics bucket — same one kubecost-cost-scraper
        writes to, NOT the <YOUR_S3_BUCKET> data bucket).
    region:
        AWS region for S3 calls.
    date:
        ISO 8601 anchor date (e.g. "2026-07-26"). Defaults to yesterday UTC —
        always a "closed" day, never today, so an in-flight write can't be
        compacted mid-write.
    lookback_days:
        Also re-compact the (lookback_days - 1) days before the anchor date.
        Compaction is idempotent (same input always overwrites the same S3
        keys — see compactor.py), so re-running is free; this exists to
        self-heal against any record that lands under an already-closed dt=
        partition after a prior run (e.g. a late-arriving backfill). A re-run
        also reconciles away any orphan schema_version= partition left by a
        version bump between the two runs (GitHub #180), so the re-scraped
        day cannot double-count in the compacted table.
    tables:
        Comma-separated table_name keys from compactor.RAW_PREFIXES to
        compact. Empty string (default) compacts all of them.
    """
    table_list = [t.strip() for t in tables.split(",") if t.strip()] or list(RAW_PREFIXES)
    anchor = _date.fromisoformat(date) if date else _date.today() - timedelta(days=1)

    results = {}
    for i in range(lookback_days):
        dt = (anchor - timedelta(days=i)).isoformat()
        results[dt] = compact_day_task(bucket=bucket, region=region, dt=dt, tables=table_list)
    return results
