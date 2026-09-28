#!/usr/bin/env python3
"""Drop compacted rows whose workflow raw no longer holds — the one-time #382 repair.

Why this exists
---------------
Until #382, `prep_test_batch.py` flushed a subject's raw step-outcomes,
workflow-runs, subject-manifests and costs, and left compacted alone. The
nightly compactor rebuilds only its lookback window from raw, so every flushed
attempt older than that stayed in `*_compacted`: the dashboards (raw) and
`CloudpipeMetrics(compacted=True)` disagreed about those subjects' history.
#382 measured 83 failed step-outcome rows (dt >= 2026-08-18); this script's own
dry run on 2026-09-11 found the whole of it — 17,816 step-outcome rows of ~370
subjects (85 failed, 2 of them on 08-17 below the projection floor), plus 323
workflow_runs, 332 subject_manifests and 404 costs rows: every earlier attempt
of the 2026-09-10 reprocess batch. The flush now reaches compacted itself;
this clears what the earlier flushes left behind.

How
---
Every table here is workflow-keyed — each raw key embeds the Argo workflow name.
So for each compacted file, one LIST of the same table's raw `dt=` gives the
workflows raw still holds, and a compacted row whose `workflow_name` is not
among them is a flushed leftover. Rebuilding with the compactor would reach the
same answer by GETting every raw object (832k across the affected days, ~8 h
serial); this costs LISTs plus one GET per compacted file.

Stands down — reports, writes nothing — for a `dt` whose raw listing is empty
or holds a key it cannot parse, because either makes "raw has no such
workflow" unprovable (compact_prefix_dt stands down for the same reason).

The per-scan QC tables are not covered: their raw keys carry no workflow name,
and only `--flush-qc` ever deleted them, which also wiped compacted outright.

Safety
------
- Dry-run by default: prints the plan. `--write` applies it.
- Each write goes through compactor.drop_rows: a conditional PutObject that
  keeps the file's schema, never a delete. The rows it removes must equal what
  the plan counted for that file, or it is reported as a failure.
- The metrics bucket is versioned, so every rewritten file's previous version
  survives as a noncurrent version.
- Idempotent: a re-run finds nothing left to drop.

Usage
-----
    pixi run python scripts/reconcile_compacted_with_raw.py --metrics-bucket cloudpipe-metrics
    pixi run python scripts/reconcile_compacted_with_raw.py --metrics-bucket cloudpipe-metrics --write
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

import boto3

# src/metrics/ modules import each other flatly, so the package directory
# itself has to be on the path (as in backfill_qc_rejected_category.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "metrics"))

from compactor import RAW_PREFIXES, UnattributableRows, drop_rows  # noqa: E402
from prep_test_batch import COMPACTED_PREFIX, list_keys, workflow_name_from_key  # noqa: E402

# Compacted table -> the suffix every raw key of that table ends with. The
# workflow name is the key stem up to the first `__` (step-outcomes,
# workflow-runs, subject-manifests), or the whole stem after its `YYYY-MM-DD_`
# (costs, pod-costs). DNS-1123 names cannot contain `__`, so the split is exact.
RAW_SUFFIXES = {
    "step_outcomes": "_outcome.json",
    "workflow_runs": "_run_summary.json",
    "subject_manifests": "_manifest.json",
    "costs": "_cost_allocation.json",
    "pod_costs": "_pod_costs.json",
}

_DT_RE = re.compile(r"/dt=(\d{4}-\d{2}-\d{2})/")


def raw_workflows(s3, bucket: str, table: str, dt: str) -> set[str] | None:
    """Workflows with at least one raw record for (table, dt); None = no proof.

    None when the listing is empty or any key fails to parse — both would make
    the set an under-estimate, and an under-estimate here drops live rows.
    """
    prefix = RAW_PREFIXES[table]
    keys = list_keys(s3, bucket, f"{prefix}dt={dt}/")
    workflows = set()
    for key in keys:
        stem = workflow_name_from_key(key, prefix, RAW_SUFFIXES[table])
        if stem is None:
            return None
        workflows.add(stem.split("__", 1)[0])
    return workflows or None


def plan_file(s3, bucket: str, key: str, keep: set[str]) -> Counter:
    """Count what drop_rows(..., complement=True) would remove, and from what.

    Its own read, because drop_rows reports only a row count and the plan has
    to be checkable against #382's measurement (workflows, subjects, failures).
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    table = pq.read_table(pa.BufferReader(body))
    if "workflow_name" not in table.column_names:
        raise UnattributableRows(f"{key} has no workflow_name column")
    wf = table["workflow_name"].cast(pa.string())
    gone = table.filter(
        pc.and_(
            pc.invert(pc.is_in(wf, value_set=pa.array(sorted(keep), pa.string()))), pc.is_valid(wf)
        )
    )
    counts = Counter(rows=gone.num_rows)
    counts["workflows"] = len(set(gone["workflow_name"].to_pylist()))
    if "subject" in gone.column_names:
        counts["subjects"] = len(set(gone["subject"].to_pylist()))
    if "status" in gone.column_names:
        counts["failed"] = gone["status"].to_pylist().count("failed")
    return counts


def reconcile(s3, bucket: str, tables, write: bool) -> int:
    """Plan (and with `write`, apply) every table. Returns the failure count."""
    failures = 0
    for table in tables:
        totals: Counter = Counter()
        cache: dict[str, set[str] | None] = {}
        for key in sorted(list_keys(s3, bucket, f"{COMPACTED_PREFIX}{table}/")):
            match = _DT_RE.search(key)
            if not key.endswith(".parquet") or not match:
                continue
            dt = match.group(1)
            if dt not in cache:
                cache[dt] = raw_workflows(s3, bucket, table, dt)
            keep = cache[dt]
            if keep is None:
                print(f"    [stand down] {key}: raw dt={dt} is empty or unparseable")
                continue
            try:
                planned = plan_file(s3, bucket, key, keep)
            except UnattributableRows as exc:
                print(f"    [note] {exc} — left in place")
                continue
            if not planned["rows"]:
                continue
            print(f"    {key}: {dict(planned)}")
            totals += planned
            totals["files"] += 1
            if not write:
                continue
            try:
                dropped = drop_rows(s3, bucket, key, "workflow_name", keep, complement=True)
            except Exception as exc:  # e.g. 412 PreconditionFailed: changed underneath
                failures += 1
                print(f"    [FAILED] {key}: {exc}")
                continue
            if dropped != planned["rows"]:
                failures += 1
                print(f"    [FAILED] {key}: removed {dropped} rows, planned {planned['rows']}")
        verb = "removed" if write else "would remove"
        print(f"  {table}: {verb} {dict(totals) or 'nothing'}")
    return failures


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    # Required, never defaulted — the same stance as prep_test_batch.py.
    p.add_argument("--metrics-bucket", required=True)
    p.add_argument("--region", default="<YOUR_AWS_REGION>")
    p.add_argument("--write", action="store_true", help="Apply the plan (default: dry run).")
    args = p.parse_args()

    s3 = boto3.client("s3", region_name=args.region)
    print(f"{'Applying' if args.write else 'Planning (dry run)'}: s3://{args.metrics_bucket}")
    failures = reconcile(s3, args.metrics_bucket, list(RAW_SUFFIXES), args.write)
    if failures:
        print(f"\n{failures} file(s) failed — re-run to finish; it is idempotent.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
