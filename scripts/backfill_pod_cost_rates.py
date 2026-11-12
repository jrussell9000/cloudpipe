#!/usr/bin/env python3
"""NULL the node-rate columns of pod-cost rows whose instance cannot be proven (#1060).

Why this exists
---------------
Until #1063 the cost scraper priced pods from a {hostname: rate} table. EC2
hostnames are private IPs, the cluster reuses them within a day, and the table
let the later instance overwrite the earlier one, so every pod on a reused
hostname could carry another instance's rates. Under the metrics rule, a wrong
value does not stay unmarked: this sets the three node-rate columns of every
such row to unknown, the same output the fixed scraper writes for a pod whose
instance it cannot prove:

  node_capacity_type          -> ""
  node_effective_usd_per_hour -> NULL
  node_ondemand_usd_per_hour  -> NULL

Nothing else changes. The cost columns are Kubecost's own and were not affected,
and node_instance_type stays Kubecost's label (the fixed scraper keeps it too).
Rows on hostnames ONE instance held that day are correct and left alone. A later
pass can restore exact rates from Kubecost's hourly asset files; this one only
removes values that cannot be trusted.

Which rows
----------
A row qualifies when all of these hold:
- its report date is in the plan;
- its `node` was held by more than one instance (distinct providerID) on that
  date, per Kubecost's daily asset rollup;
- it still carries a rate or a capacity type.

The rollups merge those instances' windows, so they cannot say WHICH instance a
pod ran on, but they do list every instance that held each hostname. Rows written
by the fixed scraper are out of scope: a raw object modified at or after
--written-before (default: #1063's merge) is refused, and its dt is reported.
Schema-1.0 rows have no rate columns and never match.

Both copies are patched, raw JSONL first and then the compacted Parquet, so a
recompaction in between reproduces the patched raw.

Safety
------
These rules follow scripts/backfill_qc_rejected_category.py:
- **Two steps.** `plan` reads Kubecost and writes a plan file; `apply` is a dry
  run unless given `--write`.
- **Every rewrite is checked before it is written.**
  - It must change only the three columns, and exactly the rows the rule selects.
  - A raw object's untouched lines must re-serialise byte-identically.
  - A Parquet object must round-trip losslessly.
- **Conditional writes.** Writes use `IfMatch` on the ETag read, so an object
  rewritten in between (the nightly compactor, a scrape) fails instead of being
  clobbered.
- **Rollback.** The metrics bucket is versioned. `--write` logs each object's
  previous and new VersionId, and `--rollback FILE` restores the previous ones,
  but only where the object is still the version this wrote.
- **Idempotent.** A patched row no longer matches.

Usage
-----
    # with `kubectl -n kubecost port-forward svc/kubecost-frontend 19090:9090` running:
    pixi run python scripts/backfill_pod_cost_rates.py plan \\
        --kubecost-url http://localhost:19090 --from 2026-08-12 --to 2026-11-04 --out plan.json
    pixi run python scripts/backfill_pod_cost_rates.py apply plan.json --bucket cloudpipe-metrics
    pixi run python scripts/backfill_pod_cost_rates.py apply plan.json --bucket cloudpipe-metrics --write
    pixi run python scripts/backfill_pod_cost_rates.py rollback backfill-pod-cost-rates-<ts>.jsonl
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("backfill_pod_cost_rates")

RAW_PREFIX = "metrics/pod-costs/"
COMPACTED_PREFIX = "metrics/compacted/pod_costs/"

# What the fixed scraper writes for a pod whose instance it cannot prove.
UNKNOWN = {
    "node_capacity_type": "",
    "node_effective_usd_per_hour": None,
    "node_ondemand_usd_per_hour": None,
}
RATE_COLUMNS = tuple(UNKNOWN)

# #1063 (match pods to an instance) merged at this instant; nothing the old
# hostname table wrote can be newer.
FIX_MERGED_AT = "2026-11-07T21:58:12Z"


# ---------------------------------------------------------------------------
# Pure: which hostnames, which rows
# ---------------------------------------------------------------------------


def reused_hostnames(assets_response: dict[str, Any]) -> dict[str, list[str]]:
    """{hostname: sorted instance IDs} for hostnames held by more than one instance.

    From an Assets API (filterTypes=Node) response for one day. The instance is
    `properties.providerID`, falling back to the response key, which embeds it.
    """
    sets = assets_response.get("data") or []
    asset_set = sets[0] if isinstance(sets, list) and sets else sets
    if not asset_set:
        raise ValueError("Kubecost returned no node assets for the window")
    held: dict[str, set[str]] = {}
    for key, asset in asset_set.items():
        props = asset.get("properties") or {}
        name = props.get("name") or ""
        if name:
            held.setdefault(name, set()).add(props.get("providerID") or key)
    return {h: sorted(ids) for h, ids in sorted(held.items()) if len(ids) > 1}


def row_qualifies(row: dict[str, Any], reused: set[str]) -> bool:
    """A row on a reused hostname that still carries a rate or capacity type."""
    if row.get("node") not in reused:
        return False
    return any(row.get(c) not in (None, "") for c in RATE_COLUMNS)


class PatchRefused(Exception):
    """The object does not look the way the plan said it would; left untouched."""


def _without_rates(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in RATE_COLUMNS}


def patch_rows(rows: list[dict[str, Any]], reused: set[str]) -> tuple[list[dict[str, Any]], int]:
    """A patched copy of an object's rows, and how many were patched.

    Refuses if the patch changed anything but the three columns.
    """
    patched = copy.deepcopy(rows)
    n = 0
    for row in patched:
        if row_qualifies(row, reused):
            row.update(UNKNOWN)
            n += 1
    if [_without_rates(r) for r in patched] != [_without_rates(r) for r in rows]:
        raise PatchRefused("patch changed something other than the node-rate columns")
    return patched, n


def jsonl_rows(body: bytes) -> list[dict[str, Any]]:
    """Parse a raw pod-cost object, refusing one that writer.emit_jsonl_to_s3 did not write.

    The check is that the parsed rows re-serialise to exactly the original
    bytes. That is what makes a rewrite change only the patched rows.
    """
    rows = [json.loads(line) for line in body.decode().splitlines() if line.strip()]
    if jsonl_body(rows) != body:
        raise PatchRefused("raw object does not re-serialise byte-identically; not touching it")
    return rows


def jsonl_body(rows: list[dict[str, Any]]) -> bytes:
    """writer.emit_jsonl_to_s3's exact format."""
    return ("\n".join(json.dumps(r) for r in rows) + "\n").encode()


def _parquet_rows(body: bytes):
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pq.read_table(pa.BufferReader(body))
    return table.schema, table.to_pylist()


def _parquet_body(schema, rows: list[dict[str, Any]]) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_pylist(rows, schema=schema)
    if table.to_pylist() != rows:
        raise PatchRefused("Parquet round-trip is not lossless")
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)  # compactor.write_parquet_to_s3's defaults
    return sink.getvalue().to_pybytes()


def dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def fetch_assets(kubecost_url: str, day: date) -> dict[str, Any]:
    import requests

    window = f"{day.isoformat()}T00:00:00Z,{day.isoformat()}T23:59:59Z"
    resp = requests.get(
        f"{kubecost_url}/model/assets",
        params={"window": window, "accumulate": "true", "filterTypes": "Node"},
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(f"Kubecost assets API error for {day}: {data.get('message', data)}")
    return data


def build_plan(kubecost_url: str, start: date, end: date, written_before: str) -> dict[str, Any]:
    days: dict[str, dict[str, list[str]]] = {}
    empty: list[str] = []
    for day in dates(start, end):
        try:
            reused = reused_hostnames(fetch_assets(kubecost_url, day))
        except ValueError:
            empty.append(day.isoformat())
            continue
        days[day.isoformat()] = reused
        log.info("%s: %d reused hostname(s)", day, len(reused))
    return {
        "issue": 1060,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kubecost_url": kubecost_url,
        "written_before": written_before,
        "days": days,
        # A day with no assets has no evidence, so it is never patched.
        "days_without_assets": empty,
    }


# ---------------------------------------------------------------------------
# apply / rollback (S3)
# ---------------------------------------------------------------------------


def _list(s3, bucket: str, prefix: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out.extend(page.get("Contents", []))
    return out


def patch_object(
    s3, bucket: str, key: str, reused: set[str], write: bool, written_before: datetime
):
    """Read, patch and (with write) conditionally rewrite one object.

    Returns (patched_row_count, audit_entry_or_None).
    """
    obj = s3.get_object(Bucket=bucket, Key=key)
    body, etag, prev_version = obj["Body"].read(), obj["ETag"], obj.get("VersionId")

    if key.endswith(".parquet"):
        schema, rows = _parquet_rows(body)
        patched, n = patch_rows(rows, reused)
        new_body = _parquet_body(schema, patched) if n else body
        content_type = "application/octet-stream"
    else:
        if obj["LastModified"] >= written_before:
            raise PatchRefused(
                f"modified {obj['LastModified'].isoformat()}, at or after the fix; "
                "written by the fixed scraper, not in scope"
            )
        patched, n = patch_rows(jsonl_rows(body), reused)
        new_body = jsonl_body(patched)
        content_type = "application/json"

    if not n or not write:
        return n, None
    resp = s3.put_object(
        Bucket=bucket, Key=key, Body=new_body, ContentType=content_type, IfMatch=etag
    )
    return n, {
        "bucket": bucket,
        "key": key,
        "patched": n,
        "prev_version": prev_version,
        "new_version": resp.get("VersionId"),
    }


def apply(s3, plan: dict[str, Any], bucket: str, write: bool, audit_path: Path | None) -> int:
    written_before = datetime.fromisoformat(plan["written_before"].replace("Z", "+00:00"))
    failures = 0
    totals = {"objects_patched": 0, "rows_patched": 0}
    audit = audit_path.open("a") if audit_path else None
    try:
        for dt, reused_map in sorted(plan["days"].items()):
            reused = set(reused_map)
            if not reused:
                continue
            keys = [o["Key"] for o in _list(s3, bucket, f"{RAW_PREFIX}dt={dt}/")]
            keys += [
                o["Key"]
                for o in _list(s3, bucket, f"{COMPACTED_PREFIX}dt={dt}/")
                if o["Key"].endswith(".parquet")
            ]
            day_rows = {"raw": 0, "compacted": 0}
            for key in keys:  # raw first: _list order above
                try:
                    n, entry = patch_object(s3, bucket, key, reused, write, written_before)
                except PatchRefused as exc:
                    failures += 1
                    log.error("SKIPPED %s: %s", key, exc)
                    continue
                except Exception as exc:  # e.g. 412 PreconditionFailed: changed underneath
                    failures += 1
                    log.error("FAILED %s: %s", key, exc)
                    continue
                if n:
                    totals["objects_patched"] += 1
                    totals["rows_patched"] += n
                    day_rows["compacted" if key.endswith(".parquet") else "raw"] += n
                if entry and audit:
                    audit.write(json.dumps(entry) + "\n")
                    audit.flush()
            log.info(
                "dt=%s reused=%d rows raw=%d compacted=%d %s",
                dt,
                len(reused),
                day_rows["raw"],
                day_rows["compacted"],
                "rewritten" if write else "(dry run)",
            )
            if day_rows["raw"] != day_rows["compacted"]:
                # Not fatal (compacted can lag raw), but it is what to look at first.
                log.warning(
                    "dt=%s: raw and compacted patched different row counts (%d vs %d)",
                    dt,
                    day_rows["raw"],
                    day_rows["compacted"],
                )
    finally:
        if audit:
            audit.close()
    print(
        f"\n{'Patched' if write else 'Would patch'} {totals['rows_patched']} row(s) "
        f"in {totals['objects_patched']} object(s)"
    )
    return failures


def rollback(s3, audit_path: Path) -> int:
    """Restore each object's pre-backfill version, if it is still ours."""
    failures = 0
    for line in audit_path.read_text().splitlines():
        e = json.loads(line)
        head = s3.head_object(Bucket=e["bucket"], Key=e["key"])
        if head.get("VersionId") != e["new_version"]:
            failures += 1
            log.error("NOT restored %s: rewritten since the backfill", e["key"])
            continue
        s3.copy_object(
            Bucket=e["bucket"],
            Key=e["key"],
            CopySource={"Bucket": e["bucket"], "Key": e["key"], "VersionId": e["prev_version"]},
        )
        log.info("restored %s to %s", e["key"], e["prev_version"])
    return failures


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("plan", help="read Kubecost's daily rollups; write the plan file")
    pl.add_argument("--kubecost-url", required=True)
    pl.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    pl.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    pl.add_argument("--written-before", default=FIX_MERGED_AT)
    pl.add_argument("--out", type=Path, required=True)

    ap = sub.add_parser("apply", help="patch per the plan (dry run unless --write)")
    ap.add_argument("plan", type=Path)
    ap.add_argument("--bucket", required=True, help="the metrics bucket")
    ap.add_argument("--region")
    ap.add_argument("--write", action="store_true")

    rb = sub.add_parser("rollback", help="restore the versions an apply --write replaced")
    rb.add_argument("audit", type=Path)
    rb.add_argument("--region")

    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.cmd == "plan":
        plan = build_plan(args.kubecost_url, args.start, args.end, args.written_before)
        args.out.write_text(json.dumps(plan, indent=1) + "\n")
        n = sum(len(v) for v in plan["days"].values())
        print(f"Wrote {args.out}: {len(plan['days'])} day(s), {n} reused hostname-day(s)")
        if plan["days_without_assets"]:
            print(f"No assets (never patched): {', '.join(plan['days_without_assets'])}")
        return 0

    import boto3

    s3 = boto3.client("s3", region_name=args.region)
    if args.cmd == "rollback":
        return 1 if rollback(s3, args.audit) else 0

    plan = json.loads(args.plan.read_text())
    audit = None
    if args.write:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        audit = Path(f"backfill-pod-cost-rates-{stamp}.jsonl")
        print(f"Writing; audit log (keep it — it is the rollback): {audit}")
    else:
        print("Dry run: every object is read and patched in memory; nothing is written.")
    failures = apply(s3, plan, args.bucket, args.write, audit)
    if failures:
        log.error("%d object(s) not patched — see above; re-run is safe", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
