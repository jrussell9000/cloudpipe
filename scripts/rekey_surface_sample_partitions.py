#!/usr/bin/env python3
"""One-off: move flat `metrics/surface-sample/` objects into `dt=` partitions (#241).

Until 2026-08-11 the `surf-qc` output artifact key in
`functional-preprocessing-workflow-template.yaml` carried no `dt=` component, so
3,031 records landed flat at the prefix root. Nothing could read them: partition
projection is the only thing publishing partitions since the Glue crawlers were
removed, and `duckdb_query._s3_glob` restricts to `dt=*/` deliberately (a
recursive glob makes DuckDB's `hive_partitioning` throw "Hive partition
mismatch"). The template is fixed, but the already-written objects still need a
home — hence this script, which does for `surface-sample/` what issue #65 did
for the other prefixes.

The partition date comes from each object's own `LastModified`, in UTC. That is
the closest available stand-in for the workflow creationTimestamp the fixed
template now templates in: the records themselves carry no timestamp at all
(`completed_at` was added by the same fix), so there is nothing inside them to
date them by. The two differ only for a workflow that spans midnight, which puts
a handful of runs one partition later than the template would have — harmless,
since every reader scans a `dt` range rather than a single day.

Copy-then-delete, never move: every copy is verified present before a single
delete is issued, and the bucket is versioned, so a delete leaves a recoverable
noncurrent version behind either way.

Usage:
    python scripts/rekey_surface_sample_partitions.py              # dry run
    python scripts/rekey_surface_sample_partitions.py --apply
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import timezone

import boto3

BUCKET = "cloudpipe-metrics"
PREFIX = "metrics/surface-sample/"


def flat_objects(s3) -> list[tuple[str, str]]:
    """(key, dt) for every object sitting directly at the prefix root.

    Anything already under a `dt=` folder is skipped, which is what makes the
    script re-runnable after a partial failure.
    """
    out: list[tuple[str, str]] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=PREFIX):
        for obj in page.get("Contents", []):
            rest = obj["Key"][len(PREFIX) :]
            if "/" in rest:  # already partitioned
                continue
            # UTC explicitly: boto3 hands back a tz-aware UTC datetime, and a
            # bare .astimezone() would silently re-date it into the caller's
            # local zone — which for this corpus (written 23:32Z–00:30Z) would
            # collapse both partitions into one wrong day. Every other dt= in
            # the corpus is UTC.
            last_modified = obj["LastModified"].astimezone(timezone.utc)
            out.append((obj["Key"], last_modified.strftime("%Y-%m-%d")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually copy and delete")
    args = ap.parse_args()

    s3 = boto3.client("s3")
    objects = flat_objects(s3)
    if not objects:
        print("Nothing to do — no flat objects under", PREFIX)
        return 0

    by_dt = Counter(dt for _, dt in objects)
    print(f"{len(objects)} flat objects under {PREFIX}")
    for dt, n in sorted(by_dt.items()):
        print(f"  dt={dt}  {n}")
    if not args.apply:
        print("\nDry run — re-run with --apply to copy and then delete.")
        return 0

    # Phase 1: copy. Nothing is deleted until every copy is confirmed present.
    copied: list[tuple[str, str]] = []
    for key, dt in objects:
        dest = f"{PREFIX}dt={dt}/{key[len(PREFIX) :]}"
        s3.copy_object(Bucket=BUCKET, Key=dest, CopySource={"Bucket": BUCKET, "Key": key})
        copied.append((key, dest))
    print(f"Copied {len(copied)} objects.")

    # Phase 2: verify. head_object on each destination — a copy_object that
    # returned without raising is good evidence, but not the same as the object
    # being there, and the next phase is irreversible-ish.
    missing = []
    for _, dest in copied:
        try:
            s3.head_object(Bucket=BUCKET, Key=dest)
        except Exception as exc:  # noqa: BLE001 — any failure means stop
            missing.append((dest, exc))
    if missing:
        print(f"ABORT: {len(missing)} copies not readable; deleting nothing.", file=sys.stderr)
        for dest, exc in missing[:5]:
            print(f"  {dest}: {exc}", file=sys.stderr)
        return 1
    print(f"Verified {len(copied)} copies.")

    # Phase 3: delete the flat originals, 1000 at a time (the API maximum).
    deleted = 0
    for i in range(0, len(copied), 1000):
        batch = [{"Key": src} for src, _ in copied[i : i + 1000]]
        resp = s3.delete_objects(Bucket=BUCKET, Delete={"Objects": batch, "Quiet": True})
        if resp.get("Errors"):
            print(f"ABORT: delete errors: {resp['Errors'][:5]}", file=sys.stderr)
            return 1
        deleted += len(batch)
    print(f"Deleted {deleted} flat originals.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
