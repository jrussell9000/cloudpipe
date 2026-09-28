#!/usr/bin/env python3
"""Relabel historical QC-gate rejections from `unknown` to `qc_rejected` (#368).

Why this exists
---------------
Until #368 the recorder had no category for a registration step whose own QC
gate rejected its output (exit 65), so every rejection was stored as
`failure_category: unknown`, indistinguishable from a crash. The fix records the
new category going forward; this rewrites the rows written before it, so
queries do not have to special-case pre-fix partitions.

The category is stored in three tables, each twice (raw JSON and compacted
Parquet), and all six copies are patched:

  step_outcomes      one row per step attempt — where the label originates
  subject_manifests  steps[].failure_category, folded in from step_outcomes
  workflow_runs      failure_category of the workflow's first failed step

Compacted is patched IN PLACE, never rebuilt by re-running the compactor. When
this was written raw was not a superset of compacted (a reprocess flushed raw
per subject and left compacted alone), and compact_prefix_dt() rebuilds a
partition from raw alone — re-compacting would have changed more than the
label. #382 made the flush reach compacted too, but in-place stays the rule
here: it changes exactly the rows located, and nothing else.

What counts as a rejection
--------------------------
Only rows whose own evidence is decisive; anything ambiguous is left alone.

  b2t-run-exited-65         bold-to-t1w per-run row whose reason records the
                            driver's `bold_to_t1w.py exited 65` — the driver saw
                            the code itself. Same test validate_test_batch uses.
  b2t-session-all-rejected  bold-to-t1w session row (task=na) reading `exit code
                            1`, where every failed per-run row of that same
                            workflow and session is an exited-65 row. The driver
                            exited 1 for "every attempted run failed"; now it
                            exits 65 when all of them were rejections.
  t1w-verdict-fail          t1w-to-mni row reading `expected output not found`
                            (the recorder never received the code), where the
                            registration QC corpus holds a `verdict: fail` for
                            that subject/session dated 0-1 days before the row,
                            and nothing but `fail` in that window. The QC record
                            is written only once the gate has run, and the gate
                            exits 65 on `fail` without promoting — so a `fail`
                            there IS the cause of the missing output. The window
                            is the dashboard's: QC partitions by workflow
                            creation date, step outcomes by record date.
  recorder-exit-65          a registration-step row the DAG recorder wrote as
                            `exit code 65` but still labelled `unknown`: the
                            deploy window, where the template passing the code
                            has synced but the python image that maps it has
                            not yet been repinned. The code alone is decisive.

Only `failure_category` changes. `failure_reason` is a recorded fact, not a
classification, and is left as written.

Safety
------
- Dry-run by default: prints the plan and exits. `--write` applies it.
- Each object is read, patched, and the patched-row count must equal what
  Athena predicted for that object — otherwise the object is skipped and
  reported. A Parquet rewrite must also round-trip losslessly and differ from
  the original in `failure_category` alone.
- Writes are conditional (`IfMatch` on the ETag read), so an object rewritten
  in between — the nightly compactor, a still-running workflow — fails the
  write instead of being clobbered.
- The metrics bucket is versioned. `--write` logs every object's previous and
  new VersionId to a JSONL audit file; `--rollback FILE` restores the previous
  versions, again only where the object is still the version this wrote.
- Idempotent: a relabelled row no longer matches, so a re-run finds nothing.
  Re-run after in-flight workflows finish to pick up any late pre-fix rows.

Usage
-----
    pixi run python scripts/backfill_qc_rejected_category.py            # plan
    pixi run python scripts/backfill_qc_rejected_category.py --write    # apply
    pixi run python scripts/backfill_qc_rejected_category.py --rollback backfill-qc-rejected-<ts>.jsonl
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# src/metrics/ modules import each other flatly, so the package directory
# itself has to be on the path (as in backfill_cost_from_hourly.py).
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "metrics"))

from athena import CloudpipeMetrics  # noqa: E402

log = logging.getLogger("backfill_qc_rejected_category")

QC_REJECTED = "qc_rejected"
STALE_CATEGORIES = ("unknown", "")
DB = "cloudpipe_metrics"

# validate_test_batch.B2T_QC_REJECTION_RE, in SQL.
_EXITED_65 = r"exited\s+65\b"
# The DAG recorder's own reason for a known code: "exit code 65" (+ a gloss).
_RECORDER_EXIT_65 = r"^exit code 65\b"

# Step-outcome identity: one record per (workflow_name, step, subject, session,
# task, run) — see schemas.StepOutcome.
IDENTITY = ("workflow_name", "step", "subject", "session", "task", "run")

TABLES = ("step_outcomes", "subject_manifests", "workflow_runs")

_IDENT_SQL = ", ".join(IDENTITY)
_STALE_SQL = ", ".join(f"'{c}'" for c in STALE_CATEGORIES)


def _union(table: str, columns: str, where: str = "TRUE") -> str:
    """Raw UNION ALL compacted for one table, with dt as varchar on both sides."""
    return (
        f"SELECT {columns}, CAST(dt AS varchar) AS dt FROM {DB}.{table} WHERE {where} "
        f"UNION ALL "
        f"SELECT {columns}, CAST(dt AS varchar) AS dt FROM {DB}.{table}_compacted WHERE {where}"
    )


_ALL_STEP_OUTCOMES = _union(
    "step_outcomes",
    f"{_IDENT_SQL}, status, failure_category, "
    "COALESCE(failure_reason, '') AS failure_reason, recorded_at",
)
_T1W_MNI_QC = _union(
    "registration", "subject, session, verdict", "registration_type = 't1w_to_mni'"
)
_SO_IDENT_SQL = ", ".join(f"so.{c}" for c in IDENTITY)

# The evidence rules, as CTEs ending in `targets` — the step-outcome identities
# to relabel. Every locate query below is prefixed with this, so the three
# tables are patched against one definition.
TARGETS_CTE = f"""
so AS (SELECT DISTINCT * FROM ({_ALL_STEP_OUTCOMES})),
b2t_evidence AS (
  SELECT workflow_name, subject, session,
    COUNT_IF(status = 'failed' AND regexp_like(failure_reason, '{_EXITED_65}')) AS rejected,
    COUNT_IF(status = 'failed' AND NOT regexp_like(failure_reason, '{_EXITED_65}')) AS other_failed
  FROM so WHERE step = 'bold-to-t1w' AND task <> 'na'
  GROUP BY 1, 2, 3
),
qc AS (SELECT DISTINCT subject, session, verdict, dt FROM ({_T1W_MNI_QC})),
t1w AS (
  SELECT so.workflow_name, so.subject, so.session,
    COUNT_IF(qc.verdict = 'fail') AS fails,
    COUNT_IF(qc.verdict <> 'fail') AS non_fails
  FROM so LEFT JOIN qc
    ON qc.subject = so.subject AND qc.session = so.session
   AND date_diff('day', date(qc.dt), date(so.dt)) BETWEEN 0 AND 1
  WHERE so.step = 't1w-to-mni' AND so.status = 'failed'
    AND so.failure_category IN ({_STALE_SQL})
    AND so.failure_reason LIKE 'expected output not found:%'
  GROUP BY 1, 2, 3
),
candidates AS (
  SELECT DISTINCT {_IDENT_SQL}, 'b2t-run-exited-65' AS rule
  FROM so
  WHERE step = 'bold-to-t1w' AND task <> 'na' AND status = 'failed'
    AND failure_category IN ({_STALE_SQL})
    AND regexp_like(failure_reason, '{_EXITED_65}')
  UNION ALL
  SELECT DISTINCT {_SO_IDENT_SQL}, 'b2t-session-all-rejected'
  FROM so JOIN b2t_evidence e
    ON e.workflow_name = so.workflow_name AND e.subject = so.subject AND e.session = so.session
  WHERE so.step = 'bold-to-t1w' AND so.task = 'na' AND so.status = 'failed'
    AND so.failure_category IN ({_STALE_SQL})
    AND so.failure_reason = 'exit code 1'
    AND e.rejected > 0 AND e.other_failed = 0
  UNION ALL
  SELECT workflow_name, 't1w-to-mni', subject, session, 'na', 'na', 't1w-verdict-fail'
  FROM t1w WHERE fails > 0 AND non_fails = 0
  UNION ALL
  SELECT DISTINCT {_IDENT_SQL}, 'recorder-exit-65'
  FROM so
  WHERE step IN ('t1w-to-mni', 'bold-to-t1w') AND status = 'failed'
    AND failure_category IN ({_STALE_SQL})
    AND regexp_like(failure_reason, '{_RECORDER_EXIT_65}')
),
-- One row per identity even if rules ever overlap: a duplicate here would
-- double every located row and trip patch_rows' count guard.
targets AS (
  SELECT {_IDENT_SQL}, array_join(array_agg(DISTINCT rule ORDER BY rule), '+') AS rule
  FROM candidates GROUP BY {_IDENT_SQL}
)
"""

_JOIN_TARGETS = " AND ".join(f"t.{c} = s.{c}" for c in IDENTITY)
_S_IDENT_SQL = ", ".join(f"s.{c}" for c in IDENTITY)


def locate_sql(table: str, source: str) -> str:
    """One row per stale row to patch in `source` (a raw or compacted table),
    carrying the S3 object that holds it and the identity to match on."""
    if table == "step_outcomes":
        return f"""WITH {TARGETS_CTE}
SELECT s."$path" AS path, {_S_IDENT_SQL}, t.rule
FROM {DB}.{source} s JOIN targets t ON {_JOIN_TARGETS}
WHERE s.status = 'failed' AND s.failure_category IN ({_STALE_SQL})"""

    if table == "subject_manifests":
        return f"""WITH {TARGETS_CTE}
SELECT m."$path" AS path, m.workflow_name, st.step, m.subject, st.session, st.task, st.run,
       t.rule
FROM {DB}.{source} m CROSS JOIN UNNEST(m.steps) AS u(st)
JOIN targets t
  ON t.workflow_name = m.workflow_name AND t.subject = m.subject AND t.step = st.step
 AND t.session = st.session AND t.task = st.task AND t.run = st.run
WHERE st.status = 'failed' AND st.failure_category IN ({_STALE_SQL})"""

    if table == "workflow_runs":
        # exit_handler._first_failed_step: the category of the earliest-recorded
        # failed step outcome. Relabel only when every failed row tied for
        # earliest is a target of one step, so a tie can never borrow the label.
        return f"""WITH {TARGETS_CTE},
first_fail AS (
  SELECT workflow_name, min(recorded_at) AS ts FROM so WHERE status = 'failed' GROUP BY 1
),
first_rows AS (
  SELECT s.workflow_name, s.step, t.rule
  FROM so s
  JOIN first_fail f ON f.workflow_name = s.workflow_name AND f.ts = s.recorded_at
  LEFT JOIN targets t ON {_JOIN_TARGETS}
  WHERE s.status = 'failed'
),
wr_targets AS (
  SELECT workflow_name, arbitrary(step) AS step, arbitrary(rule) AS rule
  FROM first_rows GROUP BY 1
  HAVING bool_and(rule IS NOT NULL) AND count(DISTINCT step) = 1
)
SELECT w."$path" AS path, w.workflow_name, wt.step, wt.rule
FROM {DB}.{source} w JOIN wr_targets wt
  ON wt.workflow_name = w.workflow_name AND wt.step = w.failed_step
WHERE w.failure_category = 'unknown'"""

    raise ValueError(f"unknown table {table!r}")


# ---------------------------------------------------------------------------
# Patching — pure, so it is testable without S3
# ---------------------------------------------------------------------------


def row_key(table: str, row: dict) -> tuple:
    """What a located row matches on inside its object."""
    if table == "workflow_runs":
        return (row["workflow_name"], row["step"])
    return tuple(row[c] for c in IDENTITY)


def patch_record(table: str, rec: dict, wanted: set[tuple]) -> int:
    """Relabel the stale rows of one record that are in `wanted`; return how many.

    `wanted` holds row_key()s. A manifest can patch several steps[] entries; the
    other two tables patch at most one row per record.
    """
    if table == "step_outcomes":
        if (
            tuple(rec.get(c) for c in IDENTITY) in wanted
            and rec.get("status") == "failed"
            and rec.get("failure_category") in STALE_CATEGORIES
        ):
            rec["failure_category"] = QC_REJECTED
            return 1
        return 0

    if table == "subject_manifests":
        n = 0
        for st in rec.get("steps") or []:
            key = (rec.get("workflow_name"), st.get("step"), rec.get("subject"),
                   st.get("session"), st.get("task"), st.get("run"))  # fmt: skip
            if (
                key in wanted
                and st.get("status") == "failed"
                and st.get("failure_category") in STALE_CATEGORIES
            ):
                st["failure_category"] = QC_REJECTED
                n += 1
        return n

    if table == "workflow_runs":
        if (rec.get("workflow_name"), rec.get("failed_step")) in wanted and rec.get(
            "failure_category"
        ) == "unknown":
            rec["failure_category"] = QC_REJECTED
            return 1
        return 0

    raise ValueError(f"unknown table {table!r}")


def without_category(rec: dict) -> dict:
    """`rec` with every failure_category removed — the invariant a patch keeps."""
    out = {k: v for k, v in rec.items() if k != "failure_category"}
    if isinstance(out.get("steps"), list):
        out["steps"] = [
            {k: v for k, v in st.items() if k != "failure_category"} for st in out["steps"]
        ]
    return out


class PatchRefused(Exception):
    """The object does not look the way the plan said it would; left untouched."""


def patch_rows(table: str, rows: list[dict], wanted: set[tuple], expected: int) -> list[dict]:
    """Patch a copy of an object's rows, refusing if the result is unexpected."""
    patched = copy.deepcopy(rows)
    n = sum(patch_record(table, r, wanted) for r in patched)
    if n != expected:
        raise PatchRefused(f"patched {n} row(s), plan expected {expected}")
    if [without_category(r) for r in patched] != [without_category(r) for r in rows]:
        raise PatchRefused("patch changed something other than failure_category")
    return patched


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

    table = pa.Table.from_pylist(rows, schema=schema)
    if table.to_pylist() != rows:
        raise PatchRefused("Parquet round-trip is not lossless")
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)  # compactor.write_parquet_to_s3's defaults
    return sink.getvalue().to_pybytes()


def patch_object(s3, table: str, path: str, wanted: set[tuple], expected: int, write: bool):
    """Read, patch and (with write) conditionally rewrite one object."""
    bucket, key = _split(path)
    obj = s3.get_object(Bucket=bucket, Key=key)
    body, etag, prev_version = obj["Body"].read(), obj["ETag"], obj.get("VersionId")

    if key.endswith(".parquet"):
        schema, rows = _parquet_rows(body)
        new_body = _parquet_body(schema, patch_rows(table, rows, wanted, expected))
        content_type = "application/octet-stream"
    else:
        rec = json.loads(body)
        if not isinstance(rec, dict):
            raise PatchRefused("raw object is not a single JSON record")
        (patched,) = patch_rows(table, [rec], wanted, expected)
        new_body = json.dumps(patched).encode()  # writer.emit_to_s3's format
        content_type = "application/json"

    if not write:
        return None
    resp = s3.put_object(
        Bucket=bucket, Key=key, Body=new_body, ContentType=content_type, IfMatch=etag
    )
    return {
        "bucket": bucket,
        "key": key,
        "table": table,
        "patched": expected,
        "prev_version": prev_version,
        "new_version": resp.get("VersionId"),
    }


# ---------------------------------------------------------------------------
# Plan / apply / rollback
# ---------------------------------------------------------------------------


def locate(metrics: CloudpipeMetrics) -> list[tuple[str, str, object]]:
    """Every (table, source, located-rows DataFrame), raw before compacted."""
    out = []
    for table in TABLES:
        for source in (table, f"{table}_compacted"):
            df = metrics._run_sql(locate_sql(table, source))
            log.info("%-30s %5d stale row(s) in %d object(s)", source, len(df),
                     df["path"].nunique() if len(df) else 0)  # fmt: skip
            out.append((table, source, df))
    return out


def print_plan(located) -> None:
    print("\nRows to relabel unknown -> qc_rejected:")
    for _table, source, df in located:
        rules = Counter(df["rule"]) if len(df) else Counter()
        steps = Counter(df["step"]) if len(df) else Counter()
        objects = df["path"].nunique() if len(df) else 0
        print(f"  {source:30s} rows={len(df):5d} objects={objects:4d}  "
              f"rules={dict(rules)} steps={dict(steps)}")  # fmt: skip


def apply(s3, located, write: bool, audit_path: Path | None) -> int:
    failures = 0
    audit = audit_path.open("a") if audit_path else None
    try:
        for table, source, df in located:
            if not len(df):
                continue
            by_path: dict[str, list] = defaultdict(list)
            for row in df.to_dict("records"):
                by_path[row["path"]].append(row_key(table, row))
            for path, keys in sorted(by_path.items()):
                try:
                    entry = patch_object(s3, table, path, set(keys), len(keys), write)
                except PatchRefused as exc:
                    failures += 1
                    log.error("SKIPPED %s: %s", path, exc)
                    continue
                except Exception as exc:  # e.g. 412 PreconditionFailed: changed underneath
                    failures += 1
                    log.error("FAILED %s: %s", path, exc)
                    continue
                if entry and audit:
                    audit.write(json.dumps(entry) + "\n")
                    audit.flush()
            log.info("%s: %d object(s) %s", source, len(by_path),
                     "rewritten" if write else "verified (dry run)")  # fmt: skip
    finally:
        if audit:
            audit.close()
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
    p.add_argument("--bucket", default="cloudpipe-metrics")
    p.add_argument("--region", default="<YOUR_AWS_REGION>")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="apply the plan (default: dry run)")
    mode.add_argument("--rollback", type=Path, metavar="AUDIT_JSONL")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    import boto3

    s3 = boto3.client("s3", region_name=args.region)
    if args.rollback:
        return 1 if rollback(s3, args.rollback) else 0

    located = locate(CloudpipeMetrics(bucket=args.bucket, region=args.region))
    print_plan(located)

    audit = None
    if args.write:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        audit = Path(f"backfill-qc-rejected-{stamp}.jsonl")
        print(f"\nWriting; audit log (keep it — it is the rollback): {audit}")
    else:
        print("\nDry run: every object is read and patched in memory; nothing is written.")

    failures = apply(s3, located, args.write, audit)
    if failures:
        log.error("%d object(s) not patched — see above; re-run is safe", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
