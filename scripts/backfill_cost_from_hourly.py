#!/usr/bin/env python3
"""Rebuild a day's per-workflow cost records from HOURLY Kubecost allocations.

Why this exists
---------------
The nightly scraper queries one daily window per report-date, which reads the
aggregator's 1d rollup. After the 2026-09-05T10:38:58Z aggregator OOM, the 1d
rollups for 2026-09-03 and 2026-09-05 stopped returning — `/allocation` gave
`data: [null]` — and this script was written to rebuild those days from hourly
data instead.

Both rollups came back on 2026-09-08, once the aggregator was moved to a
2xlarge backend with a 14Gi cap, and both dates have since been re-scraped
normally. So this is a recovery tool for an aggregator that cannot serve a
daily window, not the preferred path: a normal scrape carries reconciliation
and this does not (see SUM_FIELDS below). Prefer re-scraping once the rollup
is available, and reach for this only while it is not.

The hourly source is intact. For 09-05 all 24 hourly bingen files are present in
the federated store (256,940,171 bytes, no gaps), and the hourly archive reaches
back to 2026-04-16. Per-subject cost does not need daily granularity: 24 hourly
windows summed per workflow give the same total from an undamaged source. That
is what this script does.

It requires the aggregator's `retention1h` to reach back far enough to cover the
target date — the chart default of 49 hours does not. See the aggregator block
in terraform/modules/finops/yamls/values-eks-cost-monitoring.yaml.

Safety
------
Dry-run by default: it prints what it would write and exits. Pass --write to
emit. Records are keyed (date, workflow_name), and a workflow's true cost is the
SUM across dates, so writing dt=<date> rows never invalidates rows this workflow
has on other dates — but re-running for a date that already holds rows WILL
overwrite them, which is why --write is opt-in and --force is needed when the
date is already populated.

Usage
-----
    # inspect first
    python scripts/backfill_cost_from_hourly.py --date 2026-09-05

    # then write
    python scripts/backfill_cost_from_hourly.py --date 2026-09-05 --write
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path

# src/metrics/ modules import each other flatly (e.g. `from ec2_pricing import
# ...`), so the package directory itself has to be on the path, not just src/.
# This mirrors how tests/metrics/ imports them (`import kubecost_scraper`).
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "metrics"))

from kubecost_scraper import (  # noqa: E402
    KUBECOST_BASE,
    fetch_allocations,
    parse_allocations,
)
from schemas import CostAllocation  # noqa: E402
from writer import emit_to_s3  # noqa: E402

log = logging.getLogger("backfill_cost_from_hourly")

# Fields that are genuinely additive across the hours of a day: measured
# resource consumption. CPU-hours, RAM-byte-hours and GPU-hours decompose by
# hour, so summing 24 windows reconstructs the day.
#
# total_adjustment_usd is NOT in this list, and total_cost_usd is derived rather
# than summed. Both were summed in the first version of this script, which
# understated every workflow by ~34%.
#
# The adjustment is Kubecost's reconciliation of allocated cost against actual
# cloud billing. It is a DERIVED correction, not a measured per-hour quantity,
# so it has no hourly meaning to add up — each window reports a share of the
# same correction and summing them over-applies it. Measured against 2026-09-04,
# where the daily rollup still works, on the same 1,636 workflows:
#
#   field                  daily      hourly-sum   ratio
#   cpu_cost_usd          164.38          191.12   1.163
#   memory_cost_usd        31.26           36.39   1.164
#   gpu_cost_usd           92.86           86.50   0.932
#   gross (cpu+mem+gpu)   288.50          314.02   1.088
#   total_adjustment_usd  -47.14         -154.38   3.275  <- over-applied
#   total_cost_usd        241.36          159.64   0.661
#
# Gross reconstructs to within ~9%; the adjustment does not reconstruct at all.
# So this writes GROSS cost with the adjustment explicitly zeroed: a record that
# is unreconciled (comparable to a day+1 scrape, where reconciliation has not
# landed yet) rather than one carrying a fabricated correction. Downstream reads
# scrape_age_days and total_adjustment_usd to tell settled from unsettled, and a
# zero adjustment is honest about which this is.
#
# The residual ~9% gross excess is unexplained — plausibly hour-boundary
# double-counting — so a day rebuilt this way is not exactly comparable to one
# measured the normal way. Anything comparing efficiency across days needs to
# know that.
SUM_FIELDS = (
    "cpu_cost_usd",
    "memory_cost_usd",
    "gpu_cost_usd",
)


def hourly_windows(day: _date) -> list[tuple[str, str]]:
    """The 24 RFC3339 hour windows covering `day` in UTC."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    out = []
    for h in range(24):
        a = start + timedelta(hours=h)
        b = a + timedelta(hours=1)
        out.append((a.strftime("%Y-%m-%dT%H:%M:%SZ"), b.strftime("%Y-%m-%dT%H:%M:%SZ")))
    return out


def collect(day: _date, base_url: str, pipeline: str, verify_ssl: bool = True):
    """Fetch all 24 hours and sum per workflow. Returns (records, stats)."""
    scrape_date = _date.today()
    agg: dict[str, CostAllocation] = {}
    hours_with_data = 0
    hours_empty: list[str] = []

    for a, b in hourly_windows(day):
        resp = fetch_allocations(base_url=base_url, window=f"{a},{b}", verify_ssl=verify_ssl)
        # parse_allocations already tolerates every no-data shape, including the
        # `data: [null]` payload that crashed the nightly scrape on 2026-09-06.
        recs = parse_allocations(resp, report_date=day, pipeline=pipeline, scrape_date=scrape_date)
        if not recs:
            hours_empty.append(a[11:16])
            continue
        hours_with_data += 1
        for r in recs:
            cur = agg.get(r.workflow_name)
            if cur is None:
                agg[r.workflow_name] = r
                continue
            for f in SUM_FIELDS:
                setattr(cur, f, getattr(cur, f) + getattr(r, f))
            # Subject label can be absent on some hours; keep the first non-empty.
            if not cur.subject and r.subject:
                cur.subject = r.subject

    # total is the sum of the measured components; the reconciliation adjustment
    # cannot be reconstructed hourly (see SUM_FIELDS), so it is zeroed rather
    # than guessed, and the total is therefore GROSS.
    for r in agg.values():
        r.total_adjustment_usd = 0.0
        r.total_cost_usd = round(r.cpu_cost_usd + r.memory_cost_usd + r.gpu_cost_usd, 5)

    stats = {
        "hours_with_data": hours_with_data,
        "hours_empty": hours_empty,
        "workflows": len(agg),
        "total_usd": round(sum(r.total_cost_usd for r in agg.values()), 4),
        "scrape_age_days": (scrape_date - day).days,
    }
    return list(agg.values()), stats


def stored_workflows(bucket: str, region: str, day: _date) -> set[str]:
    """Workflow names that already have a cost record for `day` in S3.

    Keys look like metrics/costs/dt=<date>/<date>_<workflow>_cost_allocation.json
    (CostAllocation.s3_key), so the workflow name is the middle segment.
    """
    import boto3

    s3 = boto3.client("s3", region_name=region)
    prefix = f"metrics/costs/dt={day}/"
    suffix = "_cost_allocation.json"
    out: set[str] = set()
    token = None
    while True:
        kw: dict = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for o in resp.get("Contents", []):
            stem = o["Key"].rsplit("/", 1)[-1]
            if stem.endswith(suffix):
                out.add(stem[len(str(day)) + 1 : -len(suffix)])
        if not resp.get("IsTruncated"):
            return out
        token = resp.get("NextContinuationToken")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--date", required=True, help="UTC report date to rebuild, YYYY-MM-DD")
    p.add_argument("--bucket", default="cloudpipe-metrics")
    p.add_argument("--region", default="<YOUR_AWS_REGION>")
    p.add_argument("--base-url", default=KUBECOST_BASE)
    p.add_argument("--pipeline", default="cloudpipe_minproc")
    p.add_argument("--write", action="store_true", help="actually emit to S3 (default: dry run)")
    p.add_argument("--force", action="store_true", help="allow overwriting a populated date")
    p.add_argument(
        "--min-hours",
        type=int,
        default=20,
        help="refuse to write if fewer hours returned data (default 20 of 24)",
    )
    p.add_argument(
        "--json-out",
        metavar="PATH",
        help="dump the computed records to PATH as JSON instead of/before writing; "
        "use this to diff the rebuild against what S3 already holds before --force",
    )
    p.add_argument(
        "--only-missing",
        action="store_true",
        help="write ONLY workflows that have no record for this date yet, leaving "
        "existing records untouched. Use when the stored records came from the "
        "normal daily scrape and are trusted, but are missing workflows.",
    )
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    day = _date.fromisoformat(args.date)

    records, stats = collect(day, args.base_url, args.pipeline)

    print(f"date               : {day}")
    print(f"hours with data    : {stats['hours_with_data']} of 24")
    if stats["hours_empty"]:
        print(f"hours EMPTY        : {', '.join(stats['hours_empty'])}")
    print(f"workflows          : {stats['workflows']}")
    print(f"total cost (USD)   : {stats['total_usd']}")
    print(f"scrape_age_days    : {stats['scrape_age_days']}")

    if not records:
        print("\nnothing to write — the aggregator returned no hourly data for this date.")
        print("check that retention1h reaches back far enough to cover it.")
        return 1

    if stats["hours_with_data"] < args.min_hours:
        print(f"\nREFUSING: only {stats['hours_with_data']} hours had data (min {args.min_hours}).")
        print("A partial day would understate every workflow that ran in the missing hours.")
        print("Wait for the backfill to finish, or lower --min-hours deliberately.")
        return 1

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump([r.to_dict() for r in records], fh, indent=2)
        print(f"\nwrote {len(records)} computed records to {args.json_out} (not S3)")

    if args.only_missing:
        existing = stored_workflows(args.bucket, args.region, day)
        before = len(records)
        records = [r for r in records if r.workflow_name not in existing]
        print(
            f"\n--only-missing: {len(existing)} already stored, "
            f"{before - len(records)} skipped, {len(records)} new to write"
        )
        if not records:
            print("nothing new to add.")
            return 0

    if not args.write:
        print("\nDRY RUN — no writes. Sample of what would be written:")
        for r in sorted(records, key=lambda x: -x.total_cost_usd)[:5]:
            print(f"  {r.workflow_name}  {r.subject}  ${r.total_cost_usd:.4f}")
        print(
            f"\nre-run with --write to emit {len(records)} records to "
            f"s3://{args.bucket}/metrics/costs/dt={day}/"
        )
        return 0

    if not args.force:
        # Guard against silently rewriting a date that already has records.
        import boto3

        s3 = boto3.client("s3", region_name=args.region)
        resp = s3.list_objects_v2(Bucket=args.bucket, Prefix=f"metrics/costs/dt={day}/", MaxKeys=1)
        if resp.get("KeyCount"):
            print(f"\nREFUSING: s3://{args.bucket}/metrics/costs/dt={day}/ already has objects.")
            print("Pass --force if overwriting them is intended.")
            return 1

    written = 0
    for r in records:
        key = CostAllocation.s3_key(r.date, r.workflow_name)
        emit_to_s3(r.to_dict(), bucket=args.bucket, key=key, region=args.region)
        written += 1
    print(f"\nwrote {written} records to s3://{args.bucket}/metrics/costs/dt={day}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
