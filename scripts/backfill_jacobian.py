#!/usr/bin/env python3
"""Recompute and patch the Jacobian QC of past T1w->MNI registrations (#1020).

Why this exists
---------------
Until #1022, registration_qc.jacobian_stats() differentiated fireANTs' LPS
displacement vectors against the NIfTI's RAS voxel axes. Every jac_det_* and
log_jac_* value in a t1w_to_mni RegistrationQC record is therefore wrong (r =
-0.40 against ANTs' own Jacobian on a production field). The warps are still in
S3, so the jacobian-backfill WorkflowTemplate recomputes the 13 fields from each
one (images/freesurfer/jacobian_backfill.py), and this script patches them into
the records in place: the metrics represent the derivatives, so the wrong values
are replaced, never kept beside the right ones.

  plan    find every t1w_to_mni record (raw and compacted, via Athena), tie each
          session's records to its current warp (below), and with --write store
          the plan at config/jacobian-backfill/{label}/plan.json in the data
          bucket. A local copy is always written under data/reprocess/.
  submit  print, or with --write run, the `argo submit` for that plan
  patch   read the shards' outcome files and patch the 13 fields into each
          matched record's raw JSON and compacted Parquet copies. Dry run by
          default; --write applies it; --rollback FILE undoes a --write.
  tree    the same 13 fields in each planned derivative's own
          `{prefix}_desc-t1w2mni_qc.json` (patch fixed the records only), on the
          same proof: unchanged warp, the qc.json's completed_at, old-or-new values.
          Conditional on the ETag; the data bucket is unversioned, so the audit
          holds the old values. Dry run by default; --write; --rollback FILE.
  prune   delete the records the plan proves do not describe a published
          derivative (below). Dry run by default; --write; --rollback FILE.

Exactly these fields change: registration_qc.jacobian_stats()'s 13 (FIELDS).
NOT patched, deliberately:
  verdict    a corrected folding fraction could move a recorded verdict; that
             is a separate decision. `patch` lists the sessions whose corrected
             jac_det_frac_negative is above the gate's bound.
  none_*     the NONE attempt of a rescued registration left no warp behind, so
             none_jac_det_frac_negative cannot be recomputed.
  superseded records, which describe a warp that no longer exists (below).

Tying a record to the warp
--------------------------
A session can hold several t1w_to_mni records: one per registration, in the dt
of the workflow that ran it, and twice over (raw JSON and compacted Parquet).
Only one describes the warp in S3 now. "The latest record" is not it in general:
a later registration that FAILED the gate exits 65 before promoting anything, so
the warp in S3 is still the earlier one while the latest record is the failure.

So the tie is the warp's own QC record. fst1w_to_mni.py writes the metrics
record and `{prefix}_desc-t1w2mni_qc.json` from one in-memory dict, and the
qc.json is promoted and uploaded beside the warp, by the same pod, only when the
warp is. A record is the CURRENT warp's record when:

  1. its (subject, session, completed_at) equals the in-tree qc.json's
     (completed_at is to the second; one session cannot register twice in one);
  2. the qc.json and the warp were uploaded together: their LastModified
     differ by at most UPLOAD_WINDOW (a pod lost mid-upload can leave one new
     and one old; that session is left unplanned);
  3. the warp was uploaded after the record was stamped, by at most
     RECORD_TO_UPLOAD (completed_at precedes the artifact upload);
  4. at compute time the warp is still the object the plan saw (the shard
     fetches it with IfMatch on the plan's ETag), and again at patch time (a
     fresh HEAD); and
  5. at patch time each matched row still holds the qc.json's (old) values,
     or already holds the new ones; anything else refuses the whole object.

Every other record of a planned session is listed under `superseded` with why
(older; a later registration that failed the gate; a later one that published
nothing), and every record of an unplanned session under `not_planned` with
the reason. `patch` touches neither.

Pruning what does not describe a derivative
-------------------------------------------
Under the metrics rule (replace, never keep beside), `prune` deletes, from the
stored plan, only records KNOWN not to describe the derivative in S3:

  older               a superseded record of a session whose current warp is a
                      newer record's (rules 1-3 above prove which)
  later-gate-fail     a later attempt that failed the gate and promoted nothing
  later-unpublished   a later attempt whose warp never reached S3
  no-warp-not-latest  a session with no warp: every record but the LATEST, which
                      stays whatever its verdict, because it says why there is
                      no derivative

Everything else unplanned (no-tree-qc, no-record-for-warp, the two upload-time
reasons, pre-2.1) is only reported: a timing or bookkeeping miss is not proof
of a mismatch. A record that documents a failure (verdict fail, or
later-unpublished) is pruned only if step_outcomes holds a failed t1w-to-mni row
for the session 0-1 days after it, so the event outlives the record; outcomes
are recorded only since 2026-06-28, so older failures are held. At prune time a
target is also held if its session's warp changed (or appeared) since the plan,
or Athena locates a copy the plan does not name.

prune never removes a record patch patches, nor the kept latest record of a
no-warp session (it refuses outright if the plan would). The two touch disjoint
rows and both re-read every object under IfMatch, so either order is safe; run
prune after patch, and roll back in the reverse order. Raw is deleted first (a
delete marker on the versioned bucket), and a record's compacted rows are
rewritten only once all its raw copies are gone, or the nightly compactor would
rebuild them from raw.

Safety (as scripts/backfill_qc_rejected_category.py)
----------------------------------------------------
- Dry run by default: plan, submit and patch read everything and write nothing.
- patch re-locates the rows via Athena, patches each object in memory, and
  refuses an object whose patched-plus-already row count differs from what
  Athena located in it, or whose rewrite differs from the original anywhere but
  the 13 fields. Parquet must round-trip losslessly with its schema unchanged.
- Writes are conditional (IfMatch on the ETag read), so an object the nightly
  compactor rewrote in between fails the write instead of being clobbered.
  Raw is patched before compacted, so a compaction in between rebuilds from
  patched raw and the compacted object then reads as `already`.
- The metrics bucket is versioned. --write logs each object's previous and new
  VersionId to a JSONL audit file; `patch --rollback FILE` restores the previous
  versions, only where the object is still the version this wrote.
- Idempotent: a patched row holds the new values, so a re-run finds it already
  done.
- prune, likewise: a raw object is deleted (IfMatch) only when it holds exactly
  the one planned record; a Parquet object is rewritten (IfMatch) minus exactly
  the planned rows, losslessly, schema unchanged, even to zero rows; a
  located-row count that differs from the plan refuses the object. --write
  checks the bucket is versioned first, and its audit names each delete's
  removed version and marker; --rollback copies the removed version back while
  the key is still deleted. A re-run finds the pruned records gone.

Usage
-----
    pixi run -e ops python scripts/backfill_jacobian.py plan --label jacobian-1 \\
        --data-bucket "$CLOUDPIPE_BUCKET" --metrics-bucket "$CLOUDPIPE_METRICS_BUCKET"
    ... plan --write;  submit [--write];  patch [--write | --rollback FILE]
    ... prune [--write | --rollback FILE]
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / "src" / "metrics"))
sys.path.insert(0, str(REPO / "images" / "shared"))
sys.path.insert(0, str(REPO / "images" / "freesurfer"))

from jacobian_backfill import (  # noqa: E402
    FIELDS,
    TEMPLATE_KEY,
    outcomes_prefix,
    plan_key,
    warp_key,
)
from registration_qc import _T1W_MNI_THRESHOLDS  # noqa: E402

log = logging.getLogger("backfill_jacobian")

DB = "cloudpipe_metrics"
TEMPLATE = "jacobian-backfill"
# The template's spec.parallelism: the shard count is the concurrency.
MAX_SHARDS = 100
ABSENT = ("NoSuchKey", "404", "NotFound")
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Rule 2: an Argo pod uploads its output artifacts within seconds of each other.
UPLOAD_WINDOW = timedelta(hours=1)
# Rule 3: completed_at is stamped before the promote and the upload. The lower
# bound allows for clock skew between the pod and S3.
RECORD_TO_UPLOAD = (timedelta(minutes=-5), timedelta(hours=6))

# Local copies (plan, audit, the above-bound list); gitignored.
OUT_DIR = REPO / "data" / "reprocess"

FOLDING_BOUND = _T1W_MNI_THRESHOLDS["jac_det_frac_negative"]["fail"]

LOCATE_SQL = f"""
SELECT "$path" AS path, 'raw' AS source, subject, session, completed_at, verdict,
       CAST(schema_version AS varchar) AS schema_version, CAST(dt AS varchar) AS dt
FROM {DB}.registration WHERE registration_type = 't1w_to_mni'
UNION ALL
SELECT "$path" AS path, 'compacted' AS source, subject, session, completed_at, verdict,
       CAST(schema_version AS varchar) AS schema_version, CAST(dt AS varchar) AS dt
FROM {DB}.registration_compacted WHERE registration_type = 't1w_to_mni'
"""


def tree_qc_key(subj: str, ses: str) -> str:
    return warp_key(subj, ses).removesuffix("_warp.nii.gz") + "_qc.json"


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FORMAT).replace(tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(TS_FORMAT)


def _code(exc) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


# ---------------------------------------------------------------------------
# Matching — pure, so it is testable without S3
# ---------------------------------------------------------------------------


def group_records(rows: list[dict]) -> dict[tuple[str, str], dict[str, dict]]:
    """(subject, session) -> completed_at -> {verdict, schema_version, copies}.

    One record is one completed_at; its raw and compacted rows are its copies.
    """
    out: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for r in rows:
        rec = out[(r["subject"], r["session"])].setdefault(
            r["completed_at"],
            {"verdict": r.get("verdict"), "schema_version": r.get("schema_version"), "copies": []},
        )
        rec["copies"].append({"source": r["source"], "path": r["path"], "dt": r.get("dt")})
    return dict(out)


def _null_nan(v):
    return None if isinstance(v, float) and not math.isfinite(v) else v


def _summary(completed_at: str, rec: dict, reason: str | None = None) -> dict:
    out = {
        "completed_at": completed_at,
        "verdict": rec["verdict"],
        "schema_version": rec["schema_version"],
        "copies": [c["path"] for c in rec["copies"]],
    }
    return out | ({"reason": reason} if reason else {})


def classify(subj: str, ses: str, records: dict, warp: dict | None, tree: dict | None) -> dict:
    """Which of a session's records (if any) describes its current warp.

    `records` is group_records()'s value for the session; `warp` is {etag,
    last_modified} from a HEAD, or None; `tree` is {record, last_modified} for
    the in-tree `_desc-t1w2mni_qc.json`, or None. Returns {"planned": entry} or
    {"not_planned": entry}, plus "superseded": [...] for a planned session.
    """

    def unplanned(reason: str, **extra) -> dict:
        records_out = [_summary(c, r) for c, r in sorted(records.items())]
        entry = {"subject": subj, "session": ses, "reason": reason, "records": records_out}
        return {"not_planned": entry | extra, "superseded": []}

    if warp is None:
        return unplanned("no-warp")
    if tree is None:
        return unplanned("no-tree-qc")
    current = (tree["record"] or {}).get("completed_at")
    if not current or current not in records:
        return unplanned("no-record-for-warp", tree_completed_at=current)
    if abs(warp["last_modified"] - tree["last_modified"]) > UPLOAD_WINDOW:
        return unplanned("warp-and-qc-json-not-one-upload")
    lag = warp["last_modified"] - parse_ts(current)
    if not RECORD_TO_UPLOAD[0] <= lag <= RECORD_TO_UPLOAD[1]:
        return unplanned("warp-not-uploaded-after-record", lag_s=lag.total_seconds())
    if any(f not in tree["record"] for f in FIELDS):
        return unplanned("pre-2.1-record")  # no log_jac_* fields to patch

    rec = records[current]
    superseded = []
    for completed_at, other in sorted(records.items()):
        if completed_at == current:
            continue
        if completed_at < current:
            reason = "older"
        elif other["verdict"] == "fail":
            reason = "later-gate-fail"  # exited 65 before promoting: no warp
        else:
            reason = "later-unpublished"
        superseded.append({"subject": subj, "session": ses} | _summary(completed_at, other, reason))
    planned = {
        "subject": subj,
        "session": ses,
        "warp_etag": warp["etag"],
        "warp_last_modified": iso(warp["last_modified"]),
        "completed_at": current,
        "verdict": rec["verdict"],
        "schema_version": rec["schema_version"],
        "latest": current == max(records),
        "copies": len(rec["copies"]),
        # NaN as null, as the shard writes its values (json_safe).
        "old": {f: _null_nan(tree["record"][f]) for f in FIELDS},
    }
    return {"planned": planned, "superseded": superseded}


# ---------------------------------------------------------------------------
# Patching — pure, so it is testable without S3
# ---------------------------------------------------------------------------


class PatchRefused(Exception):
    """The object does not look the way the plan said it would; left untouched."""


def _same(a, b) -> bool:
    """Equal as recorded values: null and NaN are one "unmeasured" value."""
    a_none = a is None or (isinstance(a, float) and math.isnan(a))
    b_none = b is None or (isinstance(b, float) and math.isnan(b))
    if a_none or b_none:
        return a_none and b_none
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
        except TypeError:
            return False
    return a == b


def _same_rows(a: list[dict], b: list[dict]) -> bool:
    return len(a) == len(b) and all(
        x.keys() == y.keys() and all(_same(x[k], y[k]) for k in x)
        for x, y in zip(a, b, strict=True)
    )


def without_fields(rows: list[dict]) -> list[dict]:
    """The rows with the 13 fields removed: the invariant a patch keeps."""
    return [{k: v for k, v in r.items() if k not in FIELDS} for r in rows]


def record_key(rec: dict) -> tuple | None:
    if rec.get("registration_type") != "t1w_to_mni":
        return None
    return (rec.get("subject"), rec.get("session"), rec.get("completed_at"))


def patch_record(rec: dict, targets: dict[tuple, dict]) -> str | None:
    """Patch one record in place if it is a target. Returns "patched",
    "already" (holds the new values), or None (not a target).

    `targets` maps (subject, session, completed_at) to {"old", "new"}: the
    in-tree qc.json's values at plan time and the shard's recomputed ones.
    """
    key = record_key(rec)
    if key is None or key not in targets:
        return None
    missing = [f for f in FIELDS if f not in rec]
    if missing:
        raise PatchRefused(f"{key}: no {', '.join(missing)} to patch")
    t = targets[key]
    if all(_same(rec[f], t["new"][f]) for f in FIELDS):
        return "already"
    if not all(_same(rec[f], t["old"][f]) for f in FIELDS):
        raise PatchRefused(f"{key}: Jacobian fields match neither the plan's values nor the new")
    for f in FIELDS:
        rec[f] = t["new"][f]
    return "patched"


def patch_rows(rows: list[dict], targets: dict, expected: int) -> tuple[list[dict], Counter]:
    """Patch a copy of an object's rows, refusing if the result is unexpected."""
    patched = copy.deepcopy(rows)
    counts = Counter(r for r in (patch_record(rec, targets) for rec in patched) if r)
    if counts["patched"] + counts["already"] != expected:
        raise PatchRefused(
            f"matched {counts['patched'] + counts['already']} row(s), Athena located {expected}"
        )
    if not _same_rows(without_fields(patched), without_fields(rows)):
        raise PatchRefused("patch changed something other than the Jacobian fields")
    return patched, counts


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


def _split(path: str) -> tuple[str, str]:
    bucket, _, key = path.removeprefix("s3://").partition("/")
    return bucket, key


def _parquet_rows(body: bytes):
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pq.read_table(pa.BufferReader(body))
    return table.schema, table.to_pylist()


def _parquet_body(schema, rows: list[dict]) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    try:
        table = pa.Table.from_pylist(rows, schema=schema)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as exc:  # e.g. an all-null column
        raise PatchRefused(f"rows do not fit the object's schema: {exc}") from exc
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)  # compactor.write_parquet_to_s3's defaults
    body = sink.getvalue().to_pybytes()
    back_schema, back = _parquet_rows(body)
    if not back_schema.equals(schema) or not _same_rows(back, rows):
        raise PatchRefused("Parquet round-trip is not lossless")
    return body


def patch_object(s3, path: str, targets: dict, expected: int, write: bool) -> dict:
    """Read, patch and (with write) conditionally rewrite one object.

    Returns {"counts": Counter, "audit": entry or None}. Nothing is written when
    every matched row already holds the new values.
    """
    bucket, key = _split(path)
    obj = s3.get_object(Bucket=bucket, Key=key)
    body, etag, prev_version = obj["Body"].read(), obj["ETag"], obj.get("VersionId")

    if key.endswith(".parquet"):
        schema, rows = _parquet_rows(body)
        patched, counts = patch_rows(rows, targets, expected)
        new_body = _parquet_body(schema, patched) if counts["patched"] else b""
        content_type = "application/octet-stream"
    else:
        rec = json.loads(body)
        if not isinstance(rec, dict):
            raise PatchRefused("raw object is not a single JSON record")
        (patched_rec,), counts = patch_rows([rec], targets, expected)
        new_body = json.dumps(patched_rec).encode()  # fst1w_to_mni.py's format
        if not _same_rows(without_fields([json.loads(new_body)]), without_fields([rec])):
            raise PatchRefused("raw JSON round-trip is not lossless")
        content_type = "application/json"

    if not write or not counts["patched"]:
        return {"counts": counts, "audit": None}
    resp = s3.put_object(
        Bucket=bucket, Key=key, Body=new_body, ContentType=content_type, IfMatch=etag
    )
    audit = {
        "bucket": bucket,
        "key": key,
        "patched": counts["patched"],
        "prev_version": prev_version,
        "new_version": resp.get("VersionId"),
    }
    return {"counts": counts, "audit": audit}


def head_warp(s3, bucket: str, subj: str, ses: str) -> dict | None:
    from botocore.exceptions import ClientError

    try:
        head = s3.head_object(Bucket=bucket, Key=warp_key(subj, ses))
    except ClientError as exc:
        if _code(exc) in ABSENT:
            return None
        raise
    return {"etag": head["ETag"], "last_modified": head["LastModified"]}


def get_tree_qc(s3, bucket: str, subj: str, ses: str) -> dict | None:
    from botocore.exceptions import ClientError

    try:
        obj = s3.get_object(Bucket=bucket, Key=tree_qc_key(subj, ses))
    except ClientError as exc:
        if _code(exc) in ABSENT:
            return None
        raise
    try:
        record = json.loads(obj["Body"].read())
    except ValueError:
        record = None
    if not isinstance(record, dict):
        return None
    return {"record": record, "last_modified": obj["LastModified"]}


def get_json(s3, bucket: str, key: str) -> dict | None:
    from botocore.exceptions import ClientError

    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except ClientError as exc:
        if _code(exc) in ABSENT:
            return None
        raise


def read_outcomes(s3, bucket: str, label: str) -> dict[tuple[str, str], dict]:
    """(subject, session) -> its latest outcome line, across every shard file.

    A resubmit with another shard count spreads a session over two files; the
    later line wins.
    """
    out: dict[tuple[str, str], dict] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=outcomes_prefix(label)
    ):
        for o in page.get("Contents", []):
            body = s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read().decode()
            for line in body.splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                k = (r["subject"], r["session"])
                if k not in out or r["at"] >= out[k]["at"]:
                    out[k] = r
    return out


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def locate(metrics) -> list[dict]:
    df = metrics._run_sql(LOCATE_SQL)
    # NULLs arrive as NaN; a record with no completed_at keys as "" and so can
    # never be the warp's (it sorts as `older`).
    rows = [
        {k: _null_nan(v) for k, v in r.items()} for r in (df.to_dict("records") if len(df) else [])
    ]
    for r in rows:
        r["completed_at"] = r["completed_at"] or ""
    log.info(
        "located %d t1w_to_mni row(s): %s", len(rows), dict(Counter(r["source"] for r in rows))
    )
    return rows


def local_path(label: str, name: str) -> Path:
    path = OUT_DIR / f"jacobian_backfill_{label}_{name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def build_plan(s3, args, rows: list[dict]) -> dict:
    by_session = group_records(rows)

    def one(item):
        (subj, ses), records = item
        warp = head_warp(s3, args.data_bucket, subj, ses)
        tree = get_tree_qc(s3, args.data_bucket, subj, ses) if warp else None
        return classify(subj, ses, records, warp, tree)

    from reprocess_func import progress

    items = sorted(by_session.items())
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(progress(pool.map(one, items), "sessions", len(items), "session"))
    return {
        "label": args.label,
        "planned_at": iso(datetime.now(UTC)),
        "template_key": TEMPLATE_KEY,
        "sessions": [r["planned"] for r in results if "planned" in r],
        "not_planned": [r["not_planned"] for r in results if "not_planned" in r],
        "superseded": [s for r in results for s in r["superseded"]],
    }


def print_plan(doc: dict) -> None:
    sessions, unplanned, superseded = doc["sessions"], doc["not_planned"], doc["superseded"]
    print(f"sessions with t1w_to_mni records   {len(sessions) + len(unplanned)}")
    print(f"  planned (record tied to warp)    {len(sessions)}")
    print(f"    record copies to patch         {sum(s['copies'] for s in sessions)}")
    print(f"    current record is not latest   {sum(not s['latest'] for s in sessions)}")
    print(f"  not planned                      {len(unplanned)}")
    for reason, n in sorted(Counter(u["reason"] for u in unplanned).items()):
        print(f"    {reason:31s}  {n}")
    print(f"superseded records (left as is)    {len(superseded)}")
    for reason, n in sorted(Counter(s["reason"] for s in superseded).items()):
        print(f"    {reason:31s}  {n}")


def plan(s3, metrics, args) -> int:
    doc = build_plan(s3, args, locate(metrics))
    print_plan(doc)
    path = local_path(args.label, "plan.json")
    path.write_text(json.dumps(doc, indent=1))
    print(f"local copy: {path}")
    key = plan_key(args.label)
    if not args.write:
        print(f"dry run: --write stores the plan at s3://{args.data_bucket}/{key}")
        return 0
    if get_json(s3, args.data_bucket, key) is not None:
        raise SystemExit(f"refusing: s3://{args.data_bucket}/{key} exists; use a new --label")
    s3.put_object(Bucket=args.data_bucket, Key=key, Body=json.dumps(doc).encode())
    print(f"wrote s3://{args.data_bucket}/{key}")
    return 0


def submit_command(label: str, shards: int) -> list[str]:
    return [
        "argo", "-n", "argo-workflows", "submit", "--from", f"workflowtemplate/{TEMPLATE}",
        "--generate-name", f"{TEMPLATE}-{label}-",
        "-p", f"plan-key={plan_key(label)}", "-p", f"shards={shards}",
    ]  # fmt: skip


def submit(s3, args) -> int:
    doc = get_json(s3, args.data_bucket, plan_key(args.label))
    if doc is None:
        raise SystemExit(f"no plan at s3://{args.data_bucket}/{plan_key(args.label)}")
    if args.shards > MAX_SHARDS:
        raise SystemExit(f"refusing --shards {args.shards}: the template runs at most {MAX_SHARDS}")
    n = len(doc["sessions"])
    shards = min(args.shards, n)
    if shards < 1:
        raise SystemExit("the plan has no sessions")
    cmd = submit_command(args.label, shards)
    print(f"{n} sessions in {shards} shards (~{-(-n // shards)} each)")
    print(" ".join(cmd))
    if not args.write:
        print("dry run: --write submits it")
        return 0
    return subprocess.run(cmd).returncode


def build_targets(doc: dict, outcomes: dict, current: dict) -> tuple[dict, Counter, list]:
    """The rows to patch: (subject, session, completed_at) -> {"old", "new"}.

    `current` maps (subject, session) to the warp's ETag now (None if gone).
    Returns (targets, a count of every planned session's fate, the sessions
    whose corrected folding fraction is above the gate's bound).
    """
    fate: Counter = Counter()
    targets, above_bound = {}, []
    for entry in doc["sessions"]:
        k = (entry["subject"], entry["session"])
        out = outcomes.get(k)
        if out is None:
            fate["no-outcome"] += 1
        elif out["status"] != "computed":
            fate[out["status"]] += 1
        elif out["warp_etag"] != entry["warp_etag"] or out["completed_at"] != entry["completed_at"]:
            fate["outcome-not-this-plan"] += 1
        elif current.get(k) != entry["warp_etag"]:
            fate["raced-since-compute"] += 1
        else:
            fate["computed"] += 1
            targets[(*k, entry["completed_at"])] = {"old": entry["old"], "new": out["values"]}
            frac = out["values"].get("jac_det_frac_negative")
            if frac is not None and frac > FOLDING_BOUND:
                above_bound.append({"subject": k[0], "session": k[1], "verdict": entry["verdict"],
                                    "jac_det_frac_negative": frac})  # fmt: skip
    return targets, fate, above_bound


def located_by_path(rows: list[dict], targets: dict) -> dict[str, int]:
    """path -> how many target rows Athena located in it; raw paths first."""
    counts = Counter(
        r["path"] for r in rows if (r["subject"], r["session"], r["completed_at"]) in targets
    )
    return dict(sorted(counts.items(), key=lambda kv: (kv[0].endswith(".parquet"), kv[0])))


def apply(s3, by_path: dict[str, int], targets: dict, write: bool, audit_path, workers=16):
    """Patch every located object; returns (row counts, failures)."""
    totals: Counter = Counter()
    failures = 0

    def one(item):
        path, expected = item
        try:
            return path, patch_object(s3, path, targets, expected, write), None
        except PatchRefused as exc:
            return path, None, f"SKIPPED {path}: {exc}"
        except Exception as exc:  # e.g. 412 PreconditionFailed: changed underneath
            return path, None, f"FAILED {path}: {exc}"

    audit = None  # opened at the first write, so a no-op re-run leaves no empty file
    try:
        # Raw before compacted (see the module docstring): two passes.
        for parquet in (False, True):
            items = [(p, n) for p, n in by_path.items() if p.endswith(".parquet") == parquet]
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for _path, result, error in pool.map(one, items):
                    if error:
                        failures += 1
                        log.error(error)
                        continue
                    totals.update(result["counts"])
                    if result["audit"] and audit_path:
                        audit = audit or audit_path.open("a")
                        audit.write(json.dumps(result["audit"]) + "\n")
                        audit.flush()
            log.info("%s: %d object(s) %s", "compacted" if parquet else "raw", len(items),
                     "patched" if write else "verified (dry run)")  # fmt: skip
    finally:
        if audit:
            audit.close()
    return totals, failures


def patch(s3, metrics, args) -> int:
    doc = get_json(s3, args.data_bucket, plan_key(args.label))
    if doc is None:
        raise SystemExit(f"no plan at s3://{args.data_bucket}/{plan_key(args.label)}")
    outcomes = read_outcomes(s3, args.data_bucket, args.label)

    def now_etag(entry):
        warp = head_warp(s3, args.data_bucket, entry["subject"], entry["session"])
        return (entry["subject"], entry["session"]), warp and warp["etag"]

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        current = dict(pool.map(now_etag, doc["sessions"]))
    targets, fate, above_bound = build_targets(doc, outcomes, current)
    print(f"planned sessions                   {len(doc['sessions'])}")
    for status, n in sorted(fate.items()):
        print(f"  {status:33s}{n}")

    rows = locate(metrics)
    by_path = located_by_path(rows, targets)
    found = {(r["subject"], r["session"], r["completed_at"]) for r in rows}
    gone = [k for k in targets if k not in found]
    print(f"objects to patch                   {len(by_path)} "
          f"({sum(not p.endswith('.parquet') for p in by_path)} raw)")  # fmt: skip
    print(f"rows to patch                      {sum(by_path.values())}")
    if gone:
        print(f"  computed, but no record left     {len(gone)}")
    print(f"corrected folding fraction above {FOLDING_BOUND} (verdict NOT changed): "
          f"{len(above_bound)}")  # fmt: skip
    if above_bound:
        path = local_path(args.label, "above_folding_bound.json")
        path.write_text(json.dumps(above_bound, indent=1))
        print(f"  listed in {path}")

    audit = None
    if args.write:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        audit = local_path(args.label, f"patch_{stamp}.jsonl")
        print(f"\nWriting; audit log (keep it, it is the rollback): {audit}")
    else:
        print("\nDry run: every object is read and patched in memory; nothing is written.")
    totals, failures = apply(s3, by_path, targets, args.write, audit)
    print(f"rows patched {totals['patched']}, already correct {totals['already']}, "
          f"objects not patched {failures}")  # fmt: skip
    if failures:
        log.error("%d object(s) not patched, see above; a re-run is safe", failures)
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# tree: the same 13 fields in the derivative's own qc.json
# ---------------------------------------------------------------------------


def patch_tree_one(s3, bucket: str, key3: tuple, targets: dict, write: bool) -> dict:
    """Patch one session's in-tree `{prefix}_desc-t1w2mni_qc.json`.

    The qc.json is the same dict as the metrics record (fst1w_to_mni.py writes both
    from it), so patch_record's rules apply unchanged: it must still be THIS warp's
    record (completed_at), and hold the plan's old values or already the new ones.
    The data bucket is unversioned, so the rewrite is conditional on the ETag read,
    and the audit keeps the old values and the ETag written, which is the rollback.
    """
    subj, ses, completed_at = key3
    key = tree_qc_key(subj, ses)
    obj = s3.get_object(Bucket=bucket, Key=key)
    etag = obj["ETag"]
    rec = json.loads(obj["Body"].read())
    if rec.get("completed_at") != completed_at:
        raise PatchRefused(
            f"{key}: completed_at {rec.get('completed_at')} is another registration's"
        )
    old = {f: rec.get(f) for f in FIELDS}
    status = patch_record(rec, targets)
    if status is None:
        raise PatchRefused(f"{key}: not a t1w_to_mni record for this session")
    if status == "already" or not write:
        return {"status": status, "audit": None}
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(rec).encode(), IfMatch=etag,
                  ContentType="application/json")  # fmt: skip
    new_etag = s3.head_object(Bucket=bucket, Key=key)["ETag"]
    return {"status": status,
            "audit": {"action": "tree", "bucket": bucket, "key": key, "old": old, "new_etag": new_etag}}  # fmt: skip


def apply_tree(s3, bucket: str, targets: dict, write: bool, audit_path, workers: int):
    """patch_tree_one over every target; returns (status counts, failures). Audits each write."""

    def one(key3):
        try:
            return patch_tree_one(s3, bucket, key3, targets, write), None
        except PatchRefused as exc:
            return None, f"SKIPPED {exc}"
        except Exception as exc:  # e.g. 412 PreconditionFailed: rewritten underneath
            return None, f"FAILED {tree_qc_key(key3[0], key3[1])}: {exc}"

    totals: Counter = Counter()
    failures = 0
    audit = None  # opened at the first write, so a no-op re-run leaves no empty file
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for result, error in pool.map(one, list(targets)):
                if error:
                    failures += 1
                    log.error(error)
                    continue
                totals[result["status"]] += 1
                if result["audit"]:
                    audit = audit or audit_path.open("a")
                    audit.write(json.dumps(result["audit"]) + "\n")
                    audit.flush()
    finally:
        if audit:
            audit.close()
    return totals, failures


def patch_tree(s3, metrics, args) -> int:
    """Patch the in-tree qc.json of every session `patch` patched the records of.

    `patch` corrected the metrics records only; each derivative's own qc.json kept
    the pre-#1022 values. Same plan, same shard outcomes, same warp check: a
    session re-registered since (a new warp ETag, a new qc.json) is skipped.
    """
    doc = get_json(s3, args.data_bucket, plan_key(args.label))
    if doc is None:
        raise SystemExit(f"no plan at s3://{args.data_bucket}/{plan_key(args.label)}")
    outcomes = read_outcomes(s3, args.data_bucket, args.label)

    def now_etag(entry):
        warp = head_warp(s3, args.data_bucket, entry["subject"], entry["session"])
        return (entry["subject"], entry["session"]), warp and warp["etag"]

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        current = dict(pool.map(now_etag, doc["sessions"]))
    targets, fate, _ = build_targets(doc, outcomes, current)
    print(f"planned sessions                   {len(doc['sessions'])}")
    for status, n in sorted(fate.items()):
        print(f"  {status:33s}{n}")

    audit_path = None
    if args.write:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        audit_path = local_path(args.label, f"tree_{stamp}.jsonl")
        print(f"\nWriting; audit log (keep it, it is the rollback): {audit_path}")
    else:
        print("\nDry run: every qc.json is read and patched in memory; nothing is written.")

    totals, failures = apply_tree(
        s3, args.data_bucket, targets, args.write, audit_path, args.workers
    )
    print(f"qc.json patched {totals['patched']}, already correct {totals['already']}, "
          f"not patched {failures}")  # fmt: skip
    if failures:
        log.error("%d qc.json not patched, see above; a re-run is safe", failures)
    return 1 if failures else 0


def rollback_tree(s3, audit_path: Path) -> int:
    """Undo a `tree --write`: put the old values back where the qc.json is still as written."""
    failures = 0
    for line in audit_path.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        obj = s3.get_object(Bucket=e["bucket"], Key=e["key"])
        if obj["ETag"] != e["new_etag"]:
            failures += 1
            log.error("NOT restored %s: rewritten since", e["key"])
            continue
        rec = json.loads(obj["Body"].read())
        rec.update(e["old"])
        s3.put_object(Bucket=e["bucket"], Key=e["key"], Body=json.dumps(rec).encode(),
                      IfMatch=obj["ETag"], ContentType="application/json")  # fmt: skip
        log.info("restored %s", e["key"])
    return failures


# ---------------------------------------------------------------------------
# prune: delete the records that do not describe a published derivative
# ---------------------------------------------------------------------------

# Why each pruned record is known not to describe the derivative in S3.
PRUNED_REASONS = {
    "older": "an earlier registration; the current warp is a newer record's",
    "later-gate-fail": "a later attempt that failed the gate and promoted nothing",
    "later-unpublished": "a later attempt whose warp was never published",
    "no-warp-not-latest": "a session with no warp: every record but the latest",
}
# A record removed on these grounds documents a failure. It is pruned only when
# step_outcomes holds that failure, so the event is not lost with it.
FAILURE_SQL = f"""
SELECT subject, session, CAST(dt AS varchar) AS dt FROM {DB}.step_outcomes
WHERE step = 't1w-to-mni' AND status = 'failed'
UNION ALL
SELECT subject, session, CAST(dt AS varchar) AS dt FROM {DB}.step_outcomes_compacted
WHERE step = 't1w-to-mni' AND status = 'failed'
"""
# backfill_qc_rejected_category's window: QC partitions by workflow creation
# date, step outcomes by record date, 0-1 days later.
FAILURE_WINDOW_DAYS = 1
_DT = re.compile(r"/dt=(\d{4}-\d{2}-\d{2})/")


# What prune reads from a stored plan; every plan the `plan` subcommand has
# written carries these.
_PRUNE_FIELDS = {
    "sessions": ("subject", "session", "completed_at", "warp_etag"),
    "superseded": ("subject", "session", "completed_at", "verdict", "copies", "reason"),
    "not_planned": ("subject", "session", "reason", "records"),
}
_RECORD_FIELDS = ("completed_at", "verdict", "copies")


def require_prunable(doc: dict) -> None:
    """Refuse, with the fix, a plan that lacks what prune decides by."""
    missing = set()
    for section, fields in _PRUNE_FIELDS.items():
        if not isinstance(doc.get(section), list):
            missing.add(section)
            continue
        for entry in doc[section]:
            missing |= {f"{section}[].{f}" for f in fields if f not in entry}
            for rec in entry.get("records") or []:
                missing |= {f"not_planned[].records[].{f}" for f in _RECORD_FIELDS if f not in rec}
    if missing:
        raise SystemExit(
            f"refusing: the plan lacks {', '.join(sorted(missing))}; "
            "re-run `plan --write` with a new --label, then prune that label"
        )


def prune_plan(doc: dict) -> dict:
    """What the stored plan says may be pruned, kept, and only reported.

    Returns {"targets": key -> {reason, verdict, copies, warp_etag}, "kept":
    [keys], "held": [(key, why)], "review": [not_planned entries]}. A key is
    (subject, session, completed_at). Refuses (SystemExit) if a target is a
    record `patch` patches or a no-warp session's kept latest record: that
    would be the one record that describes, or explains, the derivative.
    """
    require_prunable(doc)
    protected = {(s["subject"], s["session"], s["completed_at"]) for s in doc["sessions"]}
    etags = {(s["subject"], s["session"]): s["warp_etag"] for s in doc["sessions"]}
    targets, kept, held, review = {}, [], [], []

    def add(subj, ses, rec, reason, warp_etag):
        key = (subj, ses, rec["completed_at"])
        if not rec["completed_at"]:  # no identity to delete by
            held.append((key, "no completed_at"))
            return
        targets[key] = {"reason": reason, "verdict": rec["verdict"],
                        "copies": list(rec["copies"]), "warp_etag": warp_etag}  # fmt: skip

    for s in doc["superseded"]:
        add(s["subject"], s["session"], s, s["reason"], etags[(s["subject"], s["session"])])
    for u in doc["not_planned"]:
        if u["reason"] != "no-warp":
            review.append(u)  # a timing or bookkeeping miss is not proof of a mismatch
            continue
        *older, latest = sorted(u["records"], key=lambda r: r["completed_at"])
        kept.append((u["subject"], u["session"], latest["completed_at"]))
        for rec in older:
            add(u["subject"], u["session"], rec, "no-warp-not-latest", None)
    clash = (protected | set(kept)) & set(targets)
    if clash:
        raise SystemExit(f"refusing: the plan would prune a record it keeps: {sorted(clash)[:3]}")
    return {"targets": targets, "kept": kept, "held": held, "review": review}


def record_dt(path: str) -> str | None:
    m = _DT.search(path)
    return m[1] if m else None


def failure_recorded(subj, ses, copies, failures: set[tuple[str, str, str]]) -> bool:
    """A failed t1w-to-mni step outcome for the session, 0-1 days after the record."""
    for path in copies:
        if dt := record_dt(path):
            day = datetime.strptime(dt, "%Y-%m-%d")
            for n in range(FAILURE_WINDOW_DAYS + 1):
                if (subj, ses, (day + timedelta(days=n)).strftime("%Y-%m-%d")) in failures:
                    return True
    return False


def check_prune(targets: dict, rows: list[dict], failures: set, warps: dict):
    """Prune-time checks of each planned target against S3 and Athena now.

    `rows` are locate()'s rows, `failures` the (subject, session, dt) of failed
    t1w-to-mni step outcomes, `warps` (subject, session) -> the warp's ETag now
    (None if absent). Returns ({key: Counter of paths to prune}, [(key, why)],
    how many targets are already gone).
    """
    located: dict[tuple, Counter] = defaultdict(Counter)
    for r in rows:
        located[(r["subject"], r["session"], r["completed_at"])][r["path"]] += 1
    ok, held, gone = {}, [], 0
    for key, t in sorted(targets.items()):
        subj, ses, _ = key
        now = located.get(key, Counter())
        if t["warp_etag"] is not None and warps.get((subj, ses)) != t["warp_etag"]:
            held.append((key, "the warp changed since the plan; re-plan"))
        elif t["warp_etag"] is None and warps.get((subj, ses)) is not None:
            held.append((key, "a warp exists now; re-plan"))
        elif now - Counter(t["copies"]):
            # More copies than the plan named (a compaction since): refuse.
            held.append((key, "Athena locates copies the plan does not name; re-plan"))
        elif not now:
            gone += 1  # pruned by an earlier run
        elif (t["verdict"] == "fail" or t["reason"] == "later-unpublished") and not (
            failure_recorded(subj, ses, t["copies"], failures)
        ):
            held.append((key, "the failure is not in step_outcomes"))
        else:
            ok[key] = now
    return ok, held, gone


def _norm_key(rec: dict) -> tuple | None:
    if rec.get("registration_type") != "t1w_to_mni":
        return None
    return (rec.get("subject"), rec.get("session"), rec.get("completed_at") or "")


def prune_rows(rows: list[dict], keys: set, expected: int) -> list[dict]:
    """The rows minus exactly those whose key is in `keys`; refuses a count mismatch."""
    kept = [r for r in rows if _norm_key(r) not in keys]
    if len(rows) - len(kept) != expected:
        raise PatchRefused(f"matched {len(rows) - len(kept)} row(s), the plan names {expected}")
    return kept


def prune_object(s3, path: str, keys: set, expected: int, write: bool) -> dict | None:
    """Delete a raw record's object, or rewrite a Parquet object without the rows.

    A raw object is deleted only when it holds exactly one record and that
    record is a target. Both are conditional on the ETag read. Returns the
    audit entry (None on a dry run).
    """
    bucket, key = _split(path)
    obj = s3.get_object(Bucket=bucket, Key=key)
    body, etag, prev_version = obj["Body"].read(), obj["ETag"], obj.get("VersionId")
    if key.endswith(".parquet"):
        schema, rows = _parquet_rows(body)
        new_body = _parquet_body(schema, prune_rows(rows, keys, expected))
        if not write:
            return None
        resp = s3.put_object(
            Bucket=bucket, Key=key, Body=new_body, ContentType="application/octet-stream",
            IfMatch=etag,
        )  # fmt: skip
        return {"action": "rewrite", "bucket": bucket, "key": key, "pruned": expected,
                "prev_version": prev_version, "new_version": resp.get("VersionId")}  # fmt: skip
    rec = json.loads(body)
    if not isinstance(rec, dict) or expected != 1 or _norm_key(rec) not in keys:
        raise PatchRefused("raw object does not hold exactly the one planned record")
    if not write:
        return None
    resp = s3.delete_object(Bucket=bucket, Key=key, IfMatch=etag)
    return {"action": "delete", "bucket": bucket, "key": key, "pruned": 1,
            "prev_version": prev_version, "delete_marker": resp.get("VersionId")}  # fmt: skip


def _prune_pass(s3, items: list, write: bool, workers: int):
    """prune_object over (path, keys, expected) items; yields (keys, entry, error)."""

    def one(item):
        path, keys, expected = item
        try:
            return keys, prune_object(s3, path, keys, expected, write), None
        except PatchRefused as exc:
            return keys, None, f"SKIPPED {path}: {exc}"
        except Exception as exc:  # e.g. 412 PreconditionFailed: changed underneath
            return keys, None, f"FAILED {path}: {exc}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        yield from pool.map(one, items)


def compacted_items(ok: dict, skip: set) -> list:
    """(path, keys, expected rows) per Parquet object, for the records not in `skip`."""
    by_path: dict[str, Counter] = defaultdict(Counter)
    for k, paths in ok.items():
        for p, n in paths.items():
            if p.endswith(".parquet") and k not in skip:
                by_path[p][k] += n
    return [(p, set(c), sum(c.values())) for p, c in sorted(by_path.items())]


def apply_prune(s3, ok: dict, write: bool, audit_path, workers=16):
    """Raw deletes, then compacted rewrites for the records whose raw copies all
    went (a raw copy left behind would be compacted back by the nightly run)."""
    failures, done, raw_failed = 0, Counter(), set()
    audit = None
    raw = [(p, {k}, n) for k, paths in ok.items() for p, n in paths.items()
           if not p.endswith(".parquet")]  # fmt: skip
    try:
        for store in ("raw", "compacted"):
            items = raw if store == "raw" else compacted_items(ok, raw_failed)
            for keys, entry, error in _prune_pass(s3, items, write, workers):
                if error:
                    failures += 1
                    raw_failed |= keys if store == "raw" else set()
                    log.error(error)
                    continue
                done[store] += 1
                if entry and audit_path:
                    audit = audit or audit_path.open("a")
                    audit.write(json.dumps(entry) + "\n")
                    audit.flush()
    finally:
        if audit:
            audit.close()
    return done, failures


def prune(s3, metrics, args) -> int:
    doc = get_json(s3, args.data_bucket, plan_key(args.label))
    if doc is None:
        raise SystemExit(f"no plan at s3://{args.data_bucket}/{plan_key(args.label)}")
    pp = prune_plan(doc)
    targets = pp["targets"]
    print(f"records to prune (planned)         {len(targets)}")
    for reason, n in sorted(Counter(t["reason"] for t in targets.values()).items()):
        print(f"  {reason:33s}{n}   ({PRUNED_REASONS[reason]})")
    print(f"no-warp sessions: latest kept      {len(pp['kept'])}")
    print(f"sessions for review, NOT pruned    {len(pp['review'])}")
    for reason, n in sorted(Counter(u["reason"] for u in pp["review"]).items()):
        records = sum(len(u["records"]) for u in pp["review"] if u["reason"] == reason)
        print(f"  {reason:33s}{n} session(s), {records} record(s)")

    rows = locate(metrics)
    fdf = metrics._run_sql(FAILURE_SQL)
    failures = {(r["subject"], r["session"], r["dt"]) for r in fdf.to_dict("records")}
    sessions = sorted({k[:2] for k in targets})
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        heads = pool.map(lambda s: head_warp(s3, args.data_bucket, *s), sessions)
        warps = {s: (h and h["etag"]) for s, h in zip(sessions, heads, strict=True)}
    ok, held, gone = check_prune(targets, rows, failures, warps)
    held = pp["held"] + held
    print(f"pruned by an earlier run           {gone}")
    print(f"held at prune time, NOT pruned     {len(held)}")
    for why, n in sorted(Counter(w for _, w in held).items()):
        print(f"  {why:50s}{n}")
    review = local_path(args.label, "prune_review.json")
    review.write_text(json.dumps({"held": held, "not_planned": pp["review"]}, indent=1))
    print(f"for review: {review}")
    print(f"copies to remove                   {sum(sum(c.values()) for c in ok.values())}")

    audit = None
    if args.write:
        for bucket in {_split(p)[0] for c in ok.values() for p in c}:
            status = s3.get_bucket_versioning(Bucket=bucket).get("Status")
            if status != "Enabled":
                raise SystemExit(f"s3://{bucket} versioning is {status!r}: a delete is permanent")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        audit = local_path(args.label, f"prune_{stamp}.jsonl")
        print(f"\nWriting; audit log (keep it, it is the rollback): {audit}")
    else:
        print("\nDry run: every object is read and pruned in memory; nothing is written.")
    done, failed = apply_prune(s3, ok, args.write, audit)
    print(f"raw objects {'deleted' if args.write else 'verified'} {done['raw']}, "
          f"compacted objects {'rewritten' if args.write else 'verified'} {done['compacted']}, "
          f"objects not pruned {failed}")  # fmt: skip
    if failed:
        log.error("%d object(s) not pruned, see above; a re-run is safe", failed)
    return 1 if failed else 0


def rollback(s3, audit_path: Path) -> int:
    """Undo a patch or a prune from its audit, only where nothing has moved since.

    A rewrite is restored when the object is still the version written. A
    delete is restored, from the version it removed, while the key is still
    deleted. Roll back in reverse order (prune, then patch): a compacted object
    both rewrote is restorable only by the later run's audit first.
    """
    from botocore.exceptions import ClientError

    failures = 0
    for line in audit_path.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        try:
            head = s3.head_object(Bucket=e["bucket"], Key=e["key"])
        except ClientError as exc:
            if _code(exc) not in ABSENT:
                raise
            head = None
        if e.get("action") == "delete":
            if head is not None:
                failures += 1
                log.error("NOT restored %s: written again since the prune", e["key"])
                continue
        elif head is None or head.get("VersionId") != e["new_version"]:
            failures += 1
            log.error("NOT restored %s: rewritten since", e["key"])
            continue
        s3.copy_object(
            Bucket=e["bucket"],
            Key=e["key"],
            CopySource={"Bucket": e["bucket"], "Key": e["key"], "VersionId": e["prev_version"]},
        )
        log.info("restored %s to %s", e["key"], e["prev_version"])
    return failures


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "submit", "patch", "tree", "prune"):
        c = sub.add_parser(name)
        undoable = name in ("patch", "tree", "prune")
        c.add_argument("--label", required=not undoable)
        c.add_argument("--data-bucket", required=not undoable)
        c.add_argument("--metrics-bucket", default="cloudpipe-metrics")
        c.add_argument("--region", help="default: AWS_REGION, or the AWS profile's")
        c.add_argument("--workers", type=int, default=32)
        if undoable:
            mode = c.add_mutually_exclusive_group()
            mode.add_argument("--write", action="store_true", help="apply (default: dry run)")
            mode.add_argument("--rollback", type=Path, metavar="AUDIT_JSONL")
        else:
            c.add_argument("--write", action="store_true")
        if name == "submit":
            c.add_argument("--shards", type=int, default=MAX_SHARDS)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    import boto3

    s3 = boto3.client("s3", region_name=args.region)
    if getattr(args, "rollback", None):
        undo = rollback_tree if args.cmd == "tree" else rollback
        return 1 if undo(s3, args.rollback) else 0
    if not (args.label and args.data_bucket):
        p.error(f"{args.cmd} needs --label and --data-bucket (or --rollback FILE)")
    if args.write or args.cmd == "plan":
        from reprocess_func import require_same_account

        require_same_account()
    if args.cmd == "submit":
        return submit(s3, args)
    if args.cmd == "tree":  # S3 only: the plan and the shard outcomes, no Athena
        return patch_tree(s3, None, args)

    from athena import CloudpipeMetrics

    metrics = CloudpipeMetrics(bucket=args.metrics_bucket, region=args.region)
    return {"plan": plan, "patch": patch, "prune": prune}[args.cmd](s3, metrics, args)


if __name__ == "__main__":
    sys.exit(main())
