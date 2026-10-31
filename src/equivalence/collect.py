"""Merge the per-session measurements the cluster wrote into the one values file.

`fmriprep-bench-measure` runs `run_endpoints` once per session and publishes
`values.csv`, `runs.jsonl` and, last, `_measure_complete.json` under
`derivatives/fmriprep_bench/measure/{subj}/{ses}/`. This gathers the sessions of
a draw range into the single CSV `equivalence.reestimate --values` and the
analysis read, and the runs' JSON lines beside it.

It refuses, rather than fills in: a session without its completion marker is
listed and the exit code is 1 unless `--allow-missing` is given, and a session
that appears twice for one endpoint is an error. A rehearsal measurement (made
while a margin amendment was pending) is excluded unless `--include-rehearsals`,
because its values were never meant to be reported.

    python -m equivalence.collect --bucket <b> --draw <draw.csv> --from 1 --to 30 \\
        --values interim_values.csv --runs interim_runs.jsonl

Both outputs name subjects: keep them in `data/` (gitignored), never in the repo.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

from .run_endpoints import read_order
from .sessions import VALUE_COLUMNS

MEASURE_ROOT = "derivatives/fmriprep_bench/measure"
MEASURE_MARKER = "_measure_complete.json"


class CollectError(RuntimeError):
    """The measurements cannot be merged into one honest file."""


def _get(s3, bucket: str, key: str) -> bytes | None:
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return None


def fetch_session(s3, bucket: str, subject: str, session: str):
    """(marker, value rows, run lines) for one session, or None without its marker."""
    root = f"{MEASURE_ROOT}/{subject}/{session}/"
    marker = _get(s3, bucket, root + MEASURE_MARKER)
    if marker is None:
        return None
    values = _get(s3, bucket, root + "values.csv")
    runs = _get(s3, bucket, root + "runs.jsonl")
    if values is None or runs is None:
        raise CollectError(
            f"{subject} {session}: marker present but values.csv or runs.jsonl missing"
        )
    rows = list(csv.DictReader(io.StringIO(values.decode())))
    missing = set(VALUE_COLUMNS) - set(rows[0] if rows else VALUE_COLUMNS)
    if missing:
        raise CollectError(f"{subject} {session}: values.csv lacks columns {sorted(missing)}")
    return json.loads(marker), rows, [ln for ln in runs.decode().splitlines() if ln.strip()]


def merge(fetched: dict[tuple[str, str], tuple | None], include_rehearsals: bool = False):
    """One row list and one run-line list, with the sessions that could not be used."""
    rows: list[dict] = []
    lines: list[str] = []
    missing: list[str] = []
    rehearsals: list[str] = []
    seen: set[tuple[str, str]] = set()
    for (subject, session), got in fetched.items():
        key = f"{subject}_{session}"
        if got is None:
            missing.append(key)
            continue
        marker, session_rows, run_lines = got
        if marker.get("rehearsal") and not include_rehearsals:
            rehearsals.append(key)
            continue
        for r in session_rows:
            pair = (r["endpoint"], r["session"])
            if pair in seen:
                raise CollectError(f"{pair} appears twice; one value per session per endpoint")
            seen.add(pair)
            rows.append(r)
        lines.extend(run_lines)
    return rows, lines, missing, rehearsals


def main(argv: list[str] | None = None) -> int:
    import boto3

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bucket", required=True)
    p.add_argument("--region")
    p.add_argument("--draw", type=Path, required=True, help="equivalence.sample's ordered CSV")
    p.add_argument("--from", dest="first", type=int, default=1)
    p.add_argument("--to", dest="last", type=int)
    p.add_argument("--values", type=Path, required=True)
    p.add_argument("--runs", type=Path, required=True)
    p.add_argument("--allow-missing", action="store_true")
    p.add_argument("--include-rehearsals", action="store_true")
    args = p.parse_args(argv)

    s3 = boto3.client("s3", region_name=args.region)
    targets = read_order(args.draw, args.first, args.last)
    fetched = {(s, e): fetch_session(s3, args.bucket, s, e) for s, e in targets}
    rows, lines, missing, rehearsals = merge(fetched, args.include_rehearsals)

    with open(args.values, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=VALUE_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    args.runs.write_text("".join(ln + "\n" for ln in lines))
    sessions = len(targets) - len(missing) - len(rehearsals)
    print(
        f"{sessions} sessions, {len(rows)} values -> {args.values}; {len(lines)} run lines -> {args.runs}"
    )
    if rehearsals:
        print(f"{len(rehearsals)} rehearsal measurement(s) excluded: {rehearsals}", file=sys.stderr)
    if missing:
        print(f"{len(missing)} session(s) not measured: {missing}", file=sys.stderr)
        return 0 if args.allow_missing else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
