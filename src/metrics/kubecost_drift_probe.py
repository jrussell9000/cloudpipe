"""
Kubecost reconciliation convergence probe.

Kubecost prices allocations from Prometheus usage x estimated rates, then
reconciles toward actual billed cost as the AWS CUR finalizes. The nightly cost
scraper (kubecost_scraper) snapshots each report-date at day+1 — the
least-reconciled point. This probe records how a date's cost and reconciliation
adjustment evolve with age, which is what timed the "settled re-scrape" (a
delayed second scrape overwriting the day+1 snapshot with reconciled values)
now implemented in kubecost_scraper/cost_scraper.

MEASURED, from this probe's own longitudinal history (one report_date tracked
across many snapshots, not one snapshot across dates), over every date carrying
both an age-1 and an age-3 reading (2026-08-06, GitHub #171):

  * Reconciliation converges at age 3 and never moves again — confirmed on all
    10 dates, flat through age 8 on 2026-07-28 and age 13 on 2026-07-23. There
    is no "day+14-20 settled window"; SETTLED_AGE_DAYS is 3 for that reason.
    This is the robust finding and the one the re-scrape is timed on.
  * Day+1 overstates the settled total by a MEDIAN of ~51%, and the spread is
    wide — it is not a correctable constant:

        date        wf   day+1   settled  overstatement
        2026-07-23  10    5.268    4.896        8%
        2026-07-28 103   42.586   37.325       14%
        2026-08-02   5    1.256    0.895       40%
        2026-08-01  20    6.857    4.779       43%
        2026-08-03  40    8.659    5.801       49%
        2026-07-30  21    5.925    3.898       52%
        2026-07-31  21   10.644    6.891       54%
        2026-07-29  11    4.815    2.843       69%
        2026-07-27   1    0.900    0.492       83%
        2026-07-24   1    0.138    0.041      240%

    An earlier revision of this docstring claimed "only 7-14%". That range came
    from reading just 2026-07-23 and 2026-07-28, which are the two mildest dates
    in the entire record — every other date is 40%+. Do not quote 7-14%, and do
    not treat a day+1 total as nearly-settled: use the re-scraped value.
  * The often-quoted "~2x" is RAW-ESTIMATE vs settled ($84.7 raw -> $37.3
    settled = 2.27x on 2026-07-28), i.e. total_cost_usd - total_adjustment_usd
    vs total_cost_usd. It is still a different quantity from day+1 error, but
    for a typical date day+1 error (~1.5x) is the same order of magnitude, so
    the two are no longer safe to describe as wildly different. The same earlier
    revision also claimed "~20% reconciled at day+1", from 2026-07-17 — a date
    that sits at -19.9% flat from age 7 through 19 and looks like it never
    reconciled at all.
  * Ages 1-2 are unstable in BOTH directions, so nothing before age 3 is safe to
    treat as final.
  * Small dates are the wildest (the 83% and 240% rows are single-workflow
    days), so a probe reading over a tiny batch says little about a real one.

age_days here is day-granular (it counts calendar days from report_date), so two
reads of one report-date on the same day are indistinguishable in the record even
when their values differ. Same caveat applies to CostAllocation.scrape_age_days.

Each run records current totalCost + total *CostAdjustment per recent report-date
to s3://<bucket>/metrics/cost-drift-probe/. That prefix is deliberately outside
metrics/costs/, so probe records never enter the glob that subject_costs() sums.

This is the cross-sectional view (one snapshot re-reading many past dates).
CostAllocation itself (schema 1.2+) now also carries total_adjustment_usd and
scrape_age_days per row — the per-allocation, at-scrape-time view of the same
signal, captured once and stored rather than re-queried live. Use this probe to
watch reconciliation *evolve*; use CostAllocation's own fields to know how
reconciled a *specific stored record* was when it was written.

Read-only on Kubecost. Importable (probe_and_upload) for the nightly Prefect flow,
and runnable as a CLI:
  pixi run python src/metrics/kubecost_drift_probe.py [--lookback-days 21]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from kubecost_scraper import (  # type: ignore[import-not-found]
    ADJUSTMENT_FIELDS as _ADJ_FIELDS,
)
from kubecost_scraper import KUBECOST_BASE, fetch_allocations  # type: ignore[import-not-found]
from writer import emit_to_s3  # type: ignore[import-not-found]

PROBE_PREFIX = "metrics/cost-drift-probe/"


def _probe_one(base_url: str, d: _date, verify_ssl: bool) -> dict[str, Any]:
    """Query one report-date window and sum cost + reconciliation adjustment."""
    window = f"{d.isoformat()}T00:00:00Z,{d.isoformat()}T23:59:59Z"
    allocs = (
        fetch_allocations(window=window, base_url=base_url, verify_ssl=verify_ssl).get("data")
        or [{}]
    )[0] or {}
    total = adj = 0.0
    n = 0
    for name, a in allocs.items():
        if name == "__idle__" or not name.startswith("cloudpipe-"):
            continue
        n += 1
        total += a.get("totalCost", 0) or 0
        adj += sum(a.get(k, 0) or 0 for k in _ADJ_FIELDS)
    return {
        "report_date": d.isoformat(),
        "n_workflows": n,
        "total_cost_usd": round(total, 4),
        "total_adjustment_usd": round(adj, 4),
        "raw_estimate_usd": round(total - adj, 4),
    }


def _collect(
    base_url: str, verify_ssl: bool, lookback_days: int, run_ts: datetime
) -> list[dict[str, Any]]:
    """Probe the last `lookback_days` report-dates; return one row per date."""
    today = run_ts.date()
    rows = []
    for i in range(1, lookback_days + 1):
        d = today - timedelta(days=i)
        rec = _probe_one(base_url, d, verify_ssl)
        rec.update(age_days=i, run_ts=run_ts.isoformat())
        rows.append(rec)
    return rows


def probe_and_upload(
    bucket: str = "cloudpipe-metrics",
    region: str = "<YOUR_AWS_REGION>",
    base_url: str = KUBECOST_BASE,
    verify_ssl: bool = True,
    lookback_days: int = 21,
) -> int:
    """Probe recent report-dates and write one snapshot to S3.

    Returns the number of dates recorded. Never touches metrics/costs/.
    """
    run_ts = datetime.now(timezone.utc)
    rows = _collect(base_url, verify_ssl, lookback_days, run_ts)
    key = f"{PROBE_PREFIX}{run_ts.strftime('%Y-%m-%dT%H%M%SZ')}.json"
    emit_to_s3(
        {"run_ts": run_ts.isoformat(), "readings": rows}, bucket=bucket, key=key, region=region
    )
    return len(rows)


def main() -> None:
    p = argparse.ArgumentParser(description="Kubecost reconciliation convergence probe")
    p.add_argument("--bucket", default="cloudpipe-metrics")
    p.add_argument("--region", default="<YOUR_AWS_REGION>")
    p.add_argument("--base-url", default=KUBECOST_BASE)
    p.add_argument("--lookback-days", type=int, default=21)
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL verification (for the external Kubecost URL)",
    )
    args = p.parse_args()

    run_ts = datetime.now(timezone.utc)
    rows = _collect(args.base_url, not args.insecure, args.lookback_days, run_ts)
    for rec in rows:
        recon = (
            rec["total_adjustment_usd"] / rec["raw_estimate_usd"] * 100
            if rec["raw_estimate_usd"]
            else 0.0
        )
        flag = "  (no batch / no data)" if rec["n_workflows"] == 0 else f"  recon={recon:+.0f}%"
        print(
            f"  {rec['report_date']} age={rec['age_days']:2d}d  wf={rec['n_workflows']:3d}  "
            f"total=${rec['total_cost_usd']:8.3f}  adj=${rec['total_adjustment_usd']:+9.3f}{flag}"
        )

    key = f"{PROBE_PREFIX}{run_ts.strftime('%Y-%m-%dT%H%M%SZ')}.json"
    emit_to_s3(
        {"run_ts": run_ts.isoformat(), "readings": rows},
        bucket=args.bucket,
        key=key,
        region=args.region,
    )
    print(f"\nwrote {len(rows)} dates to s3://{args.bucket}/{key}")


if __name__ == "__main__":
    main()
