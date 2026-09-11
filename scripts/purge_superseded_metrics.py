#!/usr/bin/env python3
"""Purge superseded per-scan QC records, so metrics hold each scan's FINAL value.

Why this exists
---------------
A per-scan QC record is keyed by scan and write DATE, not by workflow
(`metrics/registration/dt={day}/{subj}_{ses}_t1w_to_mni_reg_qc.json`). Re-processing
a scan on a later day therefore ADDS a record rather than replacing it: every
t1w-to-mni session the RANDOM rescue (#384) recovers keeps its original gate-failed
row next to the rescued one, in raw and in compacted. The dashboards reduce to the
latest record per scan, but the batch export (export_batch_metrics.py) and ad-hoc
SQL see both — and an exported metrics dataset has to describe the data that was
actually processed, not every attempt at it. The 2026-09-11 census found ~2.5% of
every per-scan table superseded (raw rows: registration t1w_to_mni 826 of 31,965,
bold_to_t1w 4,290, anat_qc 803, fsqc_qc 932, func_preproc 4,113). Only
`registration` is wired up so far; another table needs its scan key, and a
derivative check, in TABLES.

Step outcomes are deliberately NOT touched. They are an event log — a failure
happened — not a measurement, and the failure-triage dashboard counts them over time.

What "final" means
------------------
A scan's final record is its latest `completed_at` across raw and compacted
together. A record's identity is its scan plus `completed_at`, so the final record
and its copy in the other store share one identity and can never be planned for
removal, and two distinct records at the same instant collapse into one identity
that is kept.

A scan is HELD — reported, nothing removed — when:
  - its final record is not in compacted yet (re-run after the nightly compactor);
  - its final verdict is fail while an older record passed: a regression, not a
    rescue, and not something to resolve by deleting the pass;
  - its derivative contradicts the final verdict (a pass needs its completion
    marker in the data bucket; a t1w_to_mni fail must lack one);
  - the final is ambiguous (one instant, several spellings or verdicts), or a raw
    object to delete holds more than one record.

How
---
One Athena query plans everything ("$path" names each row's S3 object). Then:
  1. raw: delete each superseded record's object. The metrics bucket must be
     versioned (checked before any write), so the delete leaves a marker; the
     audit records the marker's VersionId, and deleting the marker restores it.
  2. compacted: rewrite each affected Parquet file without the superseded rows
     (compactor.drop_rows — conditional on the ETag it read, schema kept). Only
     for scans whose raw deletes all succeeded: a raw record left behind would be
     rebuilt into compacted by the nightly compactor for any dt in its lookback.
Dry run by default; `--write` applies and writes a JSONL audit under data/
(gitignored — it names participants). Idempotent: a re-run plans nothing.

Usage
-----
    pixi run python scripts/purge_superseded_metrics.py \\
        --metrics-bucket cloudpipe-metrics --data-bucket <YOUR_S3_BUCKET> \\
        --registration-type t1w_to_mni [--subjects data/reprocess/rescue_pilot.csv] [--write]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# src/metrics/ modules import each other flatly, so the package directory
# itself has to be on the path (as in backfill_qc_rejected_category.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "metrics"))

from athena import CloudpipeMetrics, sql_in_list  # noqa: E402
from compactor import drop_rows  # noqa: E402

DB = "cloudpipe_metrics"


@dataclass(frozen=True)
class TableSpec:
    raw: str
    compacted: str
    scan: tuple[str, ...]  # the columns that name one scan

    @property
    def identity(self) -> tuple[str, ...]:
        """One record: its scan plus when it completed."""
        return (*self.scan, "completed_at")


TABLES = {
    "registration": TableSpec(
        raw="registration",
        compacted="registration_compacted",
        scan=("subject", "session", "registration_type", "task", "run"),
    ),
}
REGISTRATION_TYPES = ("t1w_to_mni", "bold_to_t1w")


@dataclass
class ScanPlan:
    scan: tuple
    final_at: str
    final_verdict: str
    superseded: list[dict] = field(default_factory=list)  # the rows to remove
    hold: str | None = None

    @property
    def transition(self) -> str:
        """e.g. 'fail -> pass' — what a reader of this scan saw before, and will see."""
        older = sorted({r["verdict"] for r in self.superseded})
        return f"{'/'.join(older) or '?'} -> {self.final_verdict}"


def plan_sql(spec: TableSpec, registration_type: str, subjects: list[str] | None) -> str:
    """Every row of every scan with more than one distinct record, from both stores.

    path_rows is counted over the WHOLE raw table, before any filter, so a raw
    object that also held some other record can never be deleted for this one.
    """
    where = [f"registration_type = '{registration_type}'"]
    if subjects:
        where.append(f"subject IN ({sql_in_list(subjects)})")
    w = " AND ".join(where)
    cols = ", ".join(spec.scan)
    on = " AND ".join(f"u.{c} = m.{c}" for c in spec.scan)
    return f"""
WITH r AS (
  SELECT "$path" AS path, count(*) OVER (PARTITION BY "$path") AS path_rows,
         {cols}, completed_at, verdict
  FROM {DB}.{spec.raw}
),
u AS (
  SELECT 'raw' AS store, path, path_rows, {cols}, completed_at, verdict FROM r WHERE {w}
  UNION ALL
  SELECT 'compacted' AS store, "$path" AS path, 0 AS path_rows, {cols}, completed_at, verdict
  FROM {DB}.{spec.compacted} WHERE {w}
),
m AS (
  SELECT {cols} FROM u GROUP BY {cols} HAVING count(DISTINCT completed_at) > 1
)
SELECT u.store, u.path, u.path_rows, {", ".join(f"u.{c}" for c in spec.scan)},
       u.completed_at, u.verdict
FROM u JOIN m ON {on}
"""


def _instant(completed_at: str) -> datetime:
    t = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def plan_scans(spec: TableSpec, rows: list[dict]) -> list[ScanPlan]:
    """Decide, per scan, which rows are superseded — or why the scan is held."""
    by_scan: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_scan[tuple(r[c] for c in spec.scan)].append(r)

    plans = []
    for scan, rs in sorted(by_scan.items()):
        latest = max(_instant(r["completed_at"]) for r in rs)
        finals = [r for r in rs if _instant(r["completed_at"]) == latest]
        older = [r for r in rs if _instant(r["completed_at"]) < latest]
        verdicts = {r["verdict"] for r in finals}
        p = ScanPlan(scan, finals[0]["completed_at"], "/".join(sorted(verdicts)), older)

        if len({r["completed_at"] for r in finals}) > 1 or len(verdicts) > 1:
            p.hold = "final record is ambiguous (several records at its instant)"
        elif not any(r["store"] == "compacted" for r in finals):
            p.hold = "final record not compacted yet — re-run after the nightly compactor"
        elif p.final_verdict == "fail" and any(r["verdict"] == "pass" for r in older):
            p.hold = "a pass is superseded by a later fail — a regression, not a rescue"
        elif any(r["store"] == "raw" and int(r["path_rows"]) != 1 for r in older):
            p.hold = "a raw object to delete holds more than one record"
        plans.append(p)
    return plans


def marker_key(scan: tuple) -> str | None:
    """The data-bucket object whose presence means this registration's output landed."""
    subj, ses, rtype, task, run = scan
    if rtype == "t1w_to_mni":
        return (
            f"derivatives/registration/{subj}/{ses}/t1w_to_mni/{subj}_{ses}_desc-t1w2mni_affine.mat"
        )
    if rtype == "bold_to_t1w":
        prefix = f"{subj}_{ses}_{task}_{run}"
        return f"derivatives/registration/{subj}/{ses}/bold_to_t1w_{task}_{run}/{prefix}_desc-bold2t1w_itk.txt"
    return None


def _exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def check_derivatives(s3, data_bucket: str, plans: list[ScanPlan]) -> None:
    """Hold any scan whose derivative contradicts its final verdict.

    Only t1w_to_mni is checked in the fail direction: a rejected t1w-to-mni never
    uploads its affine (that is what makes inventory re-run it), whereas a
    rejected BOLD run's transform is not known to be withheld.
    """
    for p in plans:
        key = marker_key(p.scan)
        if p.hold or key is None:
            continue
        present = _exists(s3, data_bucket, key)
        if p.final_verdict == "pass" and not present:
            p.hold = "final record passed but its derivative is missing"
        elif p.final_verdict == "fail" and p.scan[2] == "t1w_to_mni" and present:
            p.hold = "final record failed but an affine exists"


def _key(path: str, bucket: str) -> str:
    prefix = f"s3://{bucket}/"
    if not path.startswith(prefix):
        raise ValueError(f"{path} is not in s3://{bucket}")
    return path.removeprefix(prefix)


def apply(s3, bucket: str, spec: TableSpec, plans: list[ScanPlan], audit) -> int:
    """Raw deletes, then compacted rewrites. Returns the failure count."""
    status = s3.get_bucket_versioning(Bucket=bucket).get("Status")
    if status != "Enabled":
        raise SystemExit(f"s3://{bucket} versioning is {status!r}: a delete would be permanent")

    failures = 0
    raw_done = []
    for p in (p for p in plans if not p.hold):
        ok = True
        for r in (r for r in p.superseded if r["store"] == "raw"):
            key = _key(r["path"], bucket)
            try:
                resp = s3.delete_object(Bucket=bucket, Key=key)
            except Exception as exc:
                failures, ok = failures + 1, False
                print(f"    [FAILED] raw {key}: {exc}")
                continue
            audit.write(
                json.dumps(
                    {
                        "store": "raw",
                        "key": key,
                        "delete_marker_version_id": resp.get("VersionId"),
                        "completed_at": r["completed_at"],
                        "verdict": r["verdict"],
                    }
                )
                + "\n"
            )
        if ok:
            raw_done.append(p)

    by_file: dict[str, list[dict]] = defaultdict(list)
    for p in raw_done:
        for r in (r for r in p.superseded if r["store"] == "compacted"):
            by_file[r["path"]].append(r)

    for path, rs in sorted(by_file.items()):
        key = _key(path, bucket)
        ids = {tuple(r[c] for c in spec.identity) for r in rs}
        try:
            before = s3.head_object(Bucket=bucket, Key=key).get("VersionId")
            dropped = drop_rows(s3, bucket, key, spec.identity, ids)
        except Exception as exc:  # e.g. 412 PreconditionFailed: changed underneath
            failures += 1
            print(f"    [FAILED] compacted {key}: {exc}")
            continue
        if dropped != len(rs):
            failures += 1
            print(f"    [FAILED] compacted {key}: removed {dropped} rows, planned {len(rs)}")
        audit.write(
            json.dumps(
                {"store": "compacted", "key": key, "rows": dropped, "previous_version_id": before}
            )
            + "\n"
        )
    return failures


def summarize(plans: list[ScanPlan], write: bool) -> None:
    live = [p for p in plans if not p.hold]
    stores = Counter(r["store"] for p in live for r in p.superseded)
    files = {r["path"] for p in live for r in p.superseded if r["store"] == "compacted"}
    verb = "Removing" if write else "Would remove"
    print(
        f"{len(plans)} scans with superseded records; {len(live)} to purge, "
        f"{len(plans) - len(live)} held"
    )
    print(
        f"  {verb}: {stores['raw']} raw objects, {stores['compacted']} compacted rows "
        f"in {len(files)} files"
    )
    for transition, n in sorted(Counter(p.transition for p in live).items()):
        print(f"    {n:>5}  {transition}")
    holds = Counter(p.hold for p in plans if p.hold)
    for reason, n in sorted(holds.items()):
        print(f"  HELD {n:>4}: {reason}")
        for p in [p for p in plans if p.hold == reason][:10]:
            print(
                f"           {'/'.join(x for x in p.scan if x)}  final {p.final_at} {p.transition}"
            )


def read_subjects(path: str) -> list[str]:
    with open(path, newline="") as f:
        rows = [r[0].strip() for r in csv.reader(f) if r and r[0].strip()]
    return [r for r in rows if r.startswith("sub-")]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    # Buckets are required, never defaulted — the same stance as prep_test_batch.py.
    p.add_argument("--metrics-bucket", required=True)
    p.add_argument("--data-bucket", required=True, help="Where derivatives live (marker checks).")
    p.add_argument("--table", choices=sorted(TABLES), default="registration")
    p.add_argument("--registration-type", choices=REGISTRATION_TYPES, required=True)
    p.add_argument("--subjects", help="CSV of subject IDs (first column) to limit the purge to.")
    p.add_argument("--region", default="<YOUR_AWS_REGION>")
    p.add_argument("--write", action="store_true", help="Apply the plan (default: dry run).")
    p.add_argument("--audit", help="Audit JSONL path (default: data/purge-superseded-<ts>.jsonl).")
    args = p.parse_args()

    spec = TABLES[args.table]
    subjects = read_subjects(args.subjects) if args.subjects else None
    metrics = CloudpipeMetrics(bucket=args.metrics_bucket, region=args.region)
    rows = metrics._run_sql(plan_sql(spec, args.registration_type, subjects)).to_dict("records")

    s3 = boto3.client("s3", region_name=args.region)
    plans = plan_scans(spec, rows)
    check_derivatives(s3, args.data_bucket, plans)
    print(
        f"{'Applying' if args.write else 'Planning (dry run)'}: {spec.raw} "
        f"{args.registration_type}{f' for {len(subjects)} subjects' if subjects else ''}"
    )
    summarize(plans, args.write)
    if not args.write:
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_path = Path(args.audit or f"data/purge-superseded-{stamp}.jsonl")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a") as audit:
        failures = apply(s3, args.metrics_bucket, spec, plans, audit)
    print(f"Audit: {audit_path}")
    if failures:
        print(
            f"\n{failures} action(s) failed — re-run to finish; it is idempotent.", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
