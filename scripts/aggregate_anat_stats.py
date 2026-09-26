"""
aggregate_anat_stats.py — Aggregate every FastSurfer and subcortical-subregion
`.stats` file in the cohort into one queryable Parquet dataset, optionally with
the per-session QC that belongs next to it.

Replaces scripts/aggregate_freesurfer_stats.py, which cannot run: it passes five
arguments to a four-parameter function, and the glob it would have used
(`lh.aparc.stats`) is a filename FastSurfer never writes, so its aparc pass was
a silent no-op that logged success. It also assumed a local `$SUBJECTS_DIR` and
shelled out to `asegstats2table`; nothing here leaves python or touches disk
except to write output.

Output is LONG format — one row per (subject, session, source, structure,
measure) — sharded so an interrupted run resumes. Pivot to wide at query time:

  pixi run python scripts/aggregate_anat_stats.py --out data/anat-stats
  pixi run python scripts/aggregate_anat_stats.py --out /tmp/x --subject sub-003RTV85
  pixi run python scripts/aggregate_anat_stats.py --out data/anat-stats --with-qc

  -- then, e.g.
  SELECT subject, session,
         max(value) FILTER (measure = 'Volume_mm3' AND structure = 'Left-Hippocampus')
    FROM read_parquet('data/anat-stats/shard-*.parquet')
   WHERE source = 'aseg.stats' GROUP BY 1, 2;
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from anat_stats.aggregate import (  # noqa: E402
    DEFAULT_SHARDS,
    DEFAULT_WORKERS,
    aggregate,
    plan_shards,
)
from anat_stats.discovery import list_subjects  # noqa: E402

DEFAULT_BUCKET = "<YOUR_S3_BUCKET>"
REGION = "<YOUR_AWS_REGION>"

log = logging.getLogger("aggregate_anat_stats")


def subjects_from_csv(path: Path) -> list[str]:
    """Subject IDs from a CSV with a `subject_id` (or `subject`) column."""
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return []
    column = next((c for c in ("subject_id", "subject") if c in rows[0]), None)
    if column is None:
        raise SystemExit(f"{path}: no subject_id or subject column (found {list(rows[0])})")
    return sorted({row[column].strip() for row in rows if row[column].strip()})


def subjects_from_census(path: Path) -> list[str]:
    """Subject IDs from a census scan (`data/census/s3_scan.jsonl`).

    A fast path only. The census is a per-SUBJECT rollup of `_complete.json`
    timestamps, so it names the subjects but says nothing about which files
    exist — completeness is still re-checked against S3 per subject, because the
    census is a snapshot and the cohort is not frozen.
    """
    subjects = []
    with path.open() as handle:
        for line in handle:
            if line.strip():
                subject = json.loads(line).get("subject")
                if subject:
                    subjects.append(subject)
    return sorted(set(subjects))


def resolve_subjects(args, s3) -> list[str]:
    if args.subject:
        return sorted(set(args.subject))
    if args.subjects_csv:
        return subjects_from_csv(Path(args.subjects_csv))
    if args.census_scan:
        return subjects_from_census(Path(args.census_scan))
    log.info("Listing subjects under both derivative trees in s3://%s ...", args.bucket)
    return list_subjects(s3, args.bucket)


def write_qc(out: Path, args) -> None:
    from anat_stats.qc import anatomical_qc

    frame = anatomical_qc(
        engine=args.qc_engine,
        bucket=args.metrics_bucket,
        dt_from=args.dt_from,
        dt_to=args.dt_to,
    )
    path = out / "qc.parquet"
    frame.to_parquet(path, index=False)
    log.info("Wrote %s (%d rows, %d columns)", path, len(frame), len(frame.columns))


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--out", required=True, type=Path, help="Output directory for the shard files.")
    p.add_argument("--bucket", default=DEFAULT_BUCKET, help="Derivatives bucket.")
    p.add_argument("--region", default=REGION)
    p.add_argument(
        "--subject",
        action="append",
        help="Aggregate only this subject (repeatable). For spot checks.",
    )
    p.add_argument("--subjects-csv", help="CSV with a subject_id column.")
    p.add_argument(
        "--census-scan",
        help="Census scan JSONL to take the subject list from (data/census/s3_scan.jsonl).",
    )
    p.add_argument("--shards", type=int, default=DEFAULT_SHARDS)
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    p.add_argument(
        "--no-resume",
        action="store_true",
        help="Rewrite shards that already exist. Needed after a parser change.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the shard plan and what resume would skip, then stop.",
    )
    p.add_argument("--with-qc", action="store_true", help="Also write qc.parquet alongside.")
    p.add_argument("--qc-engine", choices=("athena", "duckdb"), default="athena")
    p.add_argument("--metrics-bucket", default="cloudpipe-metrics")
    p.add_argument("--dt-from", help="Restrict QC to scans from this dt (YYYY-MM-DD).")
    p.add_argument("--dt-to", help="Restrict QC to scans up to this dt (YYYY-MM-DD).")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s3 = boto3.client("s3", region_name=args.region)
    subjects = resolve_subjects(args, s3)
    if not subjects:
        raise SystemExit("No subjects to aggregate.")

    plan = plan_shards(subjects, args.shards)
    existing = sum(1 for index in plan if (args.out / f"shard-{index:04d}.parquet").exists())
    log.info(
        "%d subjects across %d shards; %d already written%s",
        len(subjects),
        len(plan),
        existing,
        " (will be rewritten)" if args.no_resume else " (will be skipped)",
    )
    if args.dry_run:
        return

    aggregate(
        subjects,
        args.bucket,
        args.out,
        shards=args.shards,
        workers=args.workers,
        resume=not args.no_resume,
    )
    if args.with_qc:
        write_qc(args.out, args)


if __name__ == "__main__":
    main()
