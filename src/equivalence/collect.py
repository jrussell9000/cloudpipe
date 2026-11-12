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

Stale confounds (D21). The marker records the ETag of each cloudpipe confounds
TSV the measurement read. When the confounds rerun has since replaced one, the
session's confounds-reading endpoints (`CONFOUNDS_ENDPOINTS`) are stale: the
session is listed, and the exit code is 1, unless `--allow-stale`, which keeps
the session's other values and drops only the stale endpoints. The interim look
needs none of them; the final analysis re-measures first
(`fmriprep_bench.py measure`) and collects without the flag.

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

from .run_endpoints import confounds_etags, read_order
from .sessions import VALUE_COLUMNS

MEASURE_ROOT = "derivatives/fmriprep_bench/measure"
MEASURE_MARKER = "_measure_complete.json"
#: Session values computed from cloudpipe's confounds table. Every other declared
#: endpoint reads the MNI BOLD, the masks or the anatomy, which the confounds
#: rerun does not touch.
CONFOUNDS_ENDPOINTS = frozenset({"n_cosines"})


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


def stale_runs(marker: dict, current: dict[str, str]) -> list[str]:
    """Runs whose confounds changed since the measurement, by ETag.

    A marker without the record (written before D21) cannot show what it read,
    so every run it measured counts as stale.
    """
    read = marker.get("cloudpipe_confounds")
    if read is None:
        return sorted(current) or ["<unrecorded>"]
    return sorted(r for r, etag in read.items() if current.get(r) != etag)


def merge(
    fetched: dict[tuple[str, str], tuple | None],
    include_rehearsals: bool = False,
    stale: dict[tuple[str, str], list[str]] | None = None,
):
    """One row list and one run-line list, with the sessions that could not be used.

    `stale` maps a session to its stale runs; such a session keeps every value
    except the confounds-reading endpoints.
    """
    stale = stale or {}
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
            if stale.get((subject, session)) and r["endpoint"] in CONFOUNDS_ENDPOINTS:
                continue
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
    p.add_argument(
        "--allow-stale",
        action="store_true",
        help="keep sessions whose confounds changed since measurement, minus "
        f"{sorted(CONFOUNDS_ENDPOINTS)} (the interim look; never the final analysis)",
    )
    args = p.parse_args(argv)

    s3 = boto3.client("s3", region_name=args.region)
    targets = read_order(args.draw, args.first, args.last)
    fetched = {(s, e): fetch_session(s3, args.bucket, s, e) for s, e in targets}
    stale = {
        (s, e): runs
        for (s, e), got in fetched.items()
        if got is not None and (runs := stale_runs(got[0], confounds_etags(s3, args.bucket, s, e)))
    }
    rows, lines, missing, rehearsals = merge(fetched, args.include_rehearsals, stale)

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
    failed = False
    if stale:
        names = sorted(f"{s}_{e}" for s, e in stale)
        print(
            f"{len(stale)} session(s) with confounds changed since measurement "
            f"({sorted(CONFOUNDS_ENDPOINTS)} dropped for them): {names}",
            file=sys.stderr,
        )
        failed |= not args.allow_stale
    if missing:
        print(f"{len(missing)} session(s) not measured: {missing}", file=sys.stderr)
        failed |= not args.allow_missing
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
