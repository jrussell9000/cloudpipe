#!/usr/bin/env python3
"""Delete pod-cost records whose workflow's cost row was deliberately removed (#1060 follow-up).

Why this exists
---------------
A test-batch flush without --flush-qc deletes a workflow's `costs` row but
leaves its `pod-costs` object. The pods it describes belong to a run whose
derivatives were flushed, so under the metrics rule (the store holds only
records that describe what is in derivatives/) they go. Plan 017 counted 1,127
such workflow-days. Found again on 2026-11-07 by the pod_costs compaction
census: on 7 of 8 stale days the only rows missing from compacted were these,
and they also sit in both copies on most days.

What counts as an orphan
------------------------
A raw pod-costs object `metrics/pod-costs/dt={d}/{d}_{wf}_pod_costs.json` is
an orphan only when `metrics/costs/dt={d}/{d}_{wf}_cost_allocation.json` exists
in the bucket's version history and its LATEST version is a delete marker. That
marker is the proof of a deliberate removal.

A workflow with no cost object at all, in any version, is NOT an orphan. That
can be a workflow-grain scrape that missed it (the cost-gap detector's domain),
so it is reported and left alone. So is any workflow whose cost object is live.

Action
------
- The raw pod-costs object is deleted. The bucket is versioned, so this is a
  delete marker and the data survives as a noncurrent version.
- That workflow's rows are dropped from the dt's compacted Parquet, in place.
  The rewrite must change nothing but those rows, must round-trip losslessly,
  and is written with `IfMatch`.

Safety
------
- **Dry run by default.** `--write` applies. Every action is appended to an
  audit JSONL.
- **Rollback.** `rollback AUDIT` removes each delete marker this run created and
  restores each compacted object's previous version, but only where the current
  version is still the one this run wrote.
- **Idempotent.** A purged workflow has no raw object left to find.

Usage
-----
    pixi run -e ops python scripts/purge_orphan_pod_costs.py --bucket cloudpipe-metrics
    pixi run -e ops python scripts/purge_orphan_pod_costs.py --bucket cloudpipe-metrics --write
    pixi run -e ops python scripts/purge_orphan_pod_costs.py rollback purge-orphan-pod-costs-<ts>.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from backfill_pod_cost_rates import (  # noqa: E402
    PatchRefused,
    _parquet_body,
    _parquet_rows,
)

log = logging.getLogger("purge_orphan_pod_costs")

POD_PREFIX = "metrics/pod-costs/"
COST_PREFIX = "metrics/costs/"
COMPACTED_PREFIX = "metrics/compacted/pod_costs/"
POD_SUFFIX = "_pod_costs.json"
COST_SUFFIX = "_cost_allocation.json"

ORPHAN = "orphan"  # cost row deleted on purpose: purge
NEVER_COSTED = "never_costed"  # no cost object in any version: report only
LIVE = "live"  # cost row exists: not an orphan


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


def workflow_of(key: str, dt: str, prefix: str, suffix: str) -> str:
    """The workflow name embedded in a costs or pod-costs key."""
    name = key.removeprefix(f"{prefix}dt={dt}/")
    if not (name.startswith(f"{dt}_") and name.endswith(suffix)):
        raise ValueError(f"unexpected key shape: {key}")
    return name[len(dt) + 1 : -len(suffix)]


def classify(versions: list[dict[str, Any]], markers: list[dict[str, Any]]) -> str:
    """ORPHAN / NEVER_COSTED / LIVE for one cost key, from list_object_versions entries."""
    latest = [v for v in versions + markers if v.get("IsLatest")]
    if not versions and not markers:
        return NEVER_COSTED
    if latest and latest[0] in markers:
        return ORPHAN
    return LIVE


def drop_workflows(rows: list[dict[str, Any]], workflows: set[str]) -> tuple[list[dict], int]:
    """Rows minus those of `workflows`, and how many were dropped."""
    kept = [r for r in rows if r.get("workflow_name") not in workflows]
    return kept, len(rows) - len(kept)


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


def _keys(s3, bucket: str, prefix: str) -> list[str]:
    out: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out.extend(o["Key"] for o in page.get("Contents", []))
    return out


def _dts(s3, bucket: str) -> list[str]:
    out: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=POD_PREFIX, Delimiter="/"):
        out.extend(p["Prefix"][len(POD_PREFIX) + 3 : -1] for p in page.get("CommonPrefixes", []))
    return sorted(out)


def cost_key_state(s3, bucket: str, key: str) -> str:
    resp = s3.list_object_versions(Bucket=bucket, Prefix=key)
    versions = [v for v in resp.get("Versions", []) if v["Key"] == key]
    markers = [m for m in resp.get("DeleteMarkers", []) if m["Key"] == key]
    return classify(versions, markers)


def find_orphans(s3, bucket: str, dt: str) -> dict[str, list[str]]:
    """{state: [workflow, ...]} for the pod-costs workflows of one dt with no live cost row."""
    pod_wfs = {
        workflow_of(k, dt, POD_PREFIX, POD_SUFFIX)
        for k in _keys(s3, bucket, f"{POD_PREFIX}dt={dt}/")
    }
    cost_wfs = {
        workflow_of(k, dt, COST_PREFIX, COST_SUFFIX)
        for k in _keys(s3, bucket, f"{COST_PREFIX}dt={dt}/")
    }
    states: dict[str, list[str]] = {ORPHAN: [], NEVER_COSTED: []}
    for wf in sorted(pod_wfs - cost_wfs):
        state = cost_key_state(s3, bucket, f"{COST_PREFIX}dt={dt}/{dt}_{wf}{COST_SUFFIX}")
        if state in states:
            states[state].append(wf)
    return states


def purge_dt(s3, bucket: str, dt: str, orphans: list[str], write: bool, audit) -> Counter:
    tally: Counter = Counter()
    for wf in orphans:
        key = f"{POD_PREFIX}dt={dt}/{dt}_{wf}{POD_SUFFIX}"
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        tally["raw_rows"] += sum(1 for line in body.splitlines() if line.strip())
        tally["raw_objects"] += 1
        if write:
            resp = s3.delete_object(Bucket=bucket, Key=key)
            audit.write(json.dumps({"op": "delete", "bucket": bucket, "key": key,
                                    "marker": resp.get("VersionId")}) + "\n")  # fmt: skip
            audit.flush()
    for key in _keys(s3, bucket, f"{COMPACTED_PREFIX}dt={dt}/"):
        if not key.endswith(".parquet"):
            continue
        obj = s3.get_object(Bucket=bucket, Key=key)
        schema, rows = _parquet_rows(obj["Body"].read())
        kept, n = drop_workflows(rows, set(orphans))
        if not n:
            continue
        tally["compacted_rows"] += n
        new_body = _parquet_body(schema, kept)
        if write:
            resp = s3.put_object(Bucket=bucket, Key=key, Body=new_body,
                                 ContentType="application/octet-stream", IfMatch=obj["ETag"])  # fmt: skip
            audit.write(json.dumps({"op": "rewrite", "bucket": bucket, "key": key,
                                    "prev_version": obj.get("VersionId"),
                                    "new_version": resp.get("VersionId"), "dropped": n}) + "\n")  # fmt: skip
            audit.flush()
    return tally


def rollback(s3, audit_path: Path) -> int:
    failures = 0
    for line in reversed(audit_path.read_text().splitlines()):
        e = json.loads(line)
        if e["op"] == "delete":
            # Removing the marker this run created makes the prior version current again.
            head = s3.list_object_versions(Bucket=e["bucket"], Prefix=e["key"])
            latest = [
                m for m in head.get("DeleteMarkers", []) if m["Key"] == e["key"] and m["IsLatest"]
            ]
            if not latest or latest[0]["VersionId"] != e["marker"]:
                failures += 1
                log.error("NOT restored %s: changed since the purge", e["key"])
                continue
            s3.delete_object(Bucket=e["bucket"], Key=e["key"], VersionId=e["marker"])
        else:
            if (
                s3.head_object(Bucket=e["bucket"], Key=e["key"]).get("VersionId")
                != e["new_version"]
            ):
                failures += 1
                log.error("NOT restored %s: rewritten since the purge", e["key"])
                continue
            s3.copy_object(Bucket=e["bucket"], Key=e["key"],
                           CopySource={"Bucket": e["bucket"], "Key": e["key"],
                                       "VersionId": e["prev_version"]})  # fmt: skip
        log.info("restored %s", e["key"])
    return failures


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("cmd", nargs="?", default="purge", choices=["purge", "rollback"])
    p.add_argument("audit", nargs="?", type=Path, help="rollback: the audit JSONL")
    p.add_argument("--bucket", help="the metrics bucket (purge)")
    p.add_argument("--region")
    p.add_argument("--dt", action="append", help="limit to these dt(s); default: every dt")
    p.add_argument("--write", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    import boto3

    s3 = boto3.client("s3", region_name=args.region)
    if args.cmd == "rollback":
        return 1 if rollback(s3, args.audit) else 0
    if not args.bucket:
        p.error("--bucket is required")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_path = Path(f"purge-orphan-pod-costs-{stamp}.jsonl")
    audit = audit_path.open("a") if args.write else None
    totals: Counter = Counter()
    failures = 0
    try:
        for dt in args.dt or _dts(s3, args.bucket):
            states = find_orphans(s3, args.bucket, dt)
            if states[NEVER_COSTED]:
                log.warning("dt=%s %d workflow(s) never costed (left alone): %s", dt,
                            len(states[NEVER_COSTED]), ", ".join(states[NEVER_COSTED][:5]))  # fmt: skip
            totals["never_costed_workflows"] += len(states[NEVER_COSTED])
            if not states[ORPHAN]:
                continue
            try:
                tally = purge_dt(s3, args.bucket, dt, states[ORPHAN], args.write, audit)
            except (PatchRefused, Exception) as exc:  # noqa: BLE001 — reported; re-run is safe
                failures += 1
                log.error("FAILED dt=%s: %s", dt, exc)
                continue
            totals.update(tally)
            totals["orphan_workflows"] += len(states[ORPHAN])
            log.info("dt=%s orphans=%d %s %s", dt, len(states[ORPHAN]), dict(tally),
                     "purged" if args.write else "(dry run)")  # fmt: skip
    finally:
        if audit:
            audit.close()
    print(f"\n{'Purged' if args.write else 'Would purge'}: {dict(totals)}")
    if args.write:
        print(f"Audit log (keep it — it is the rollback): {audit_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
