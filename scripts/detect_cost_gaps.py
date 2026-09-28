#!/usr/bin/env python3
"""Flag report-dates whose cost data is missing or under-captured, while it is still fixable.

The logic lives in `src/metrics/cost_gaps.py` so the nightly kubecost-cost-scraper flow can
import it too — the flow-runner image ships `src/metrics/`, not `scripts/`. This is the
operator-facing CLI over the same code, for checking a wider window by hand.

Two signals, both counted in cpu-core-hours rather than dollars:
  MISSING   workflow-days with pod activity and no row in metrics/costs/
  SHORTFALL billed cpu-core-hours / snapshot cpu-core-hours below --min-capture

Exit 1 if anything fires. Re-scrape the named dates with `kubecost_scraper.py --date <d>`
while Kubecost still has them (~2 weeks); past retention, `reconstruct_costs.py` can rebuild
the missing rows -- but as GAP-FILL only, never over a settled scraped row. See that script.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "metrics"))

from cost_gaps import (  # noqa: E402
    DEFAULT_MIN_CAPTURE,
    DEFAULT_MIN_CORE_HOURS,
    DEFAULT_SINCE_DAYS,
    format_day,
    has_gap,
    scan_recent_days,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--metrics-bucket", required=True)
    ap.add_argument(
        "--since-days",
        type=int,
        default=DEFAULT_SINCE_DAYS,
        help=f"How many report-dates back to check (default {DEFAULT_SINCE_DAYS}: day+1 "
        "through the settled re-scrape at SETTLED_AGE_DAYS).",
    )
    ap.add_argument(
        "--min-capture",
        type=float,
        default=DEFAULT_MIN_CAPTURE,
        help="Billed cpu-core-hours / snapshot cpu-core-hours below this is an "
        "under-capture. Clean days measure ~0.95-1.0; 2026-09-04 measured 0.5.",
    )
    ap.add_argument(
        "--min-core-hours",
        type=float,
        default=DEFAULT_MIN_CORE_HOURS,
        help="An unbilled workflow is reported only above this many cpu+gpu core-hours. "
        "The argo-nodes-snapshot cron and one-off probes sit at ~0.000; real unbilled "
        "subject work measured 0.78-22.7.",
    )
    ap.add_argument("--region", default="<YOUR_AWS_REGION>")
    args = ap.parse_args()

    s3 = boto3.client("s3", region_name=args.region)
    results = scan_recent_days(
        s3,
        args.metrics_bucket,
        since_days=args.since_days,
        min_capture=args.min_capture,
        min_core_hours=args.min_core_hours,
    )
    for g in results:
        print(format_day(g))
        if g["missing"]:
            print(f"      missing e.g. {', '.join(g['missing'][:5])}")
    if has_gap(results):
        print(
            "\nRe-scrape the flagged dates NOW (kubecost_scraper.py --date <d>) — Kubecost keeps "
            "~2 weeks. Past that, reconstruct_costs.py is the only route."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
