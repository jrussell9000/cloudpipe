"""The baseline sampling frame, its census, and the draw's input (design D11, D19).

Eligibility is decided at `(subject, session, task, run)` grain for every
baseline rest and n-back run cloudpipe has a FuncQC record for, before anything is
drawn. A run is eligible when all of these hold:

- the session's FastSurfer tree is complete (`_complete.json`), its anatomy and
  the anatomical comparison's input;
- the tree was written by the sampled FastSurfer release, read from the tree's
  own `scripts/build.log` (one build serves the whole sample; a subject rerun on
  a later release is `fastsurfer_release`);
- the run is listed in the session's func `_complete.json`, so it is a current
  exploded output rather than a legacy tarball;
- the run's newest FuncQC record is at the current schema (1.4 since #926), so
  its confounds are the current code's (`cloudpipe_output_superseded` otherwise);
- the session has a protocol NSS count in `config/nss_volumes.csv`, which both
  arms are given.

A session enters the draw when at least one of its runs is eligible, carrying its
eligible run count and NSS count, the two stratification variables.

    python -m equivalence.frame --bucket <data-bucket> --session ses-00A \\
        --census census.json --eligible eligible_sessions.csv

Both outputs name subjects, so they belong in `data/` (gitignored) or the
bucket's `config/`, never in the repository.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .eligibility import (
    ANATOMY_UNAVAILABLE,
    CLOUDPIPE_OUTPUT_SUPERSEDED,
    COMPLETE_MARKER,
    Census,
    Exclusion,
    RunKey,
    missing_in_arm,
)
from .sample import Candidate

FASTSURFER_RELEASE = "fastsurfer_release"
NSS_UNAVAILABLE = "nss_unavailable"
#: The release every tree in the sample was built by (task 7.1's census).
SAMPLED_FASTSURFER = "2.5.4+cdfccea"
TASKS = ("task-rest", "task-nback")


def newest_records(records: Iterable[dict]) -> dict[RunKey, dict]:
    """The newest FuncQC record per run, of the baseline tasks, any schema."""
    newest: dict[RunKey, dict] = {}
    for r in records:
        if r.get("task") not in TASKS:
            continue
        key = RunKey(r["subject"], r["session"], r["task"], r["run"])
        if key not in newest or str(r.get("completed_at", "")) > str(
            newest[key].get("completed_at", "")
        ):
            newest[key] = r
    return newest


def build_frame(
    records: Iterable[dict],
    *,
    current_schema: str,
    tree_complete: Callable[[str, str], bool],
    tree_release: Callable[[str, str], str | None],
    marker_runs: Callable[[str, str], set[str] | None],
    nss: dict[tuple[str, str], int],
) -> tuple[Census, list[Candidate]]:
    """The run-grain census and the eligible sessions, from injected lookups.

    The lookups are called once per session. `marker_runs` returns the
    `{task}_{run}` ids the session's func marker lists, or None when there is
    no current marker.
    """
    by_session: dict[tuple[str, str], list[tuple[RunKey, dict]]] = {}
    for key, rec in sorted(newest_records(records).items()):
        by_session.setdefault((key.subject, key.session), []).append((key, rec))

    census = Census()
    sessions: list[Candidate] = []
    for (subject, session), runs in by_session.items():
        session_reason = None
        if not tree_complete(subject, session):
            session_reason = (ANATOMY_UNAVAILABLE, "no FastSurfer tree, or no _complete.json on it")
        else:
            release = tree_release(subject, session)
            if release != SAMPLED_FASTSURFER:
                session_reason = (
                    FASTSURFER_RELEASE,
                    f"tree built by {release!r}, not the sampled {SAMPLED_FASTSURFER}",
                )
        if session_reason is None and (subject, session) not in nss:
            session_reason = (NSS_UNAVAILABLE, "no row in config/nss_volumes.csv")
        if session_reason is not None:
            census.exclusions += [Exclusion(k, *session_reason) for k, _ in runs]
            continue

        listed = marker_runs(subject, session) or set()
        eligible = []
        for key, rec in runs:
            if f"{key.task}_{key.run}" not in listed:
                census.exclusions.append(
                    Exclusion(
                        key,
                        missing_in_arm("cloudpipe"),
                        "not listed in the session's func _complete.json",
                    )
                )
            elif rec.get("schema_version") != current_schema:
                census.exclusions.append(
                    Exclusion(
                        key,
                        CLOUDPIPE_OUTPUT_SUPERSEDED,
                        f"newest FuncQC record is schema {rec.get('schema_version')}, "
                        f"not {current_schema}",
                    )
                )
            else:
                eligible.append(key)
        census.eligible += eligible
        if eligible:
            sessions.append(Candidate(subject, session, len(eligible), nss[(subject, session)]))
    return census, sessions


# ---------------------------------------------------------------------------
# S3 lookups
# ---------------------------------------------------------------------------


def _get(s3, bucket: str, key: str) -> bytes | None:
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return None


def s3_lookups(s3, bucket: str, sessions: list[tuple[str, str]], workers: int = 32):
    """Prefetch the three per-session lookups concurrently; returns them as callables."""

    def one(subj_ses):
        subj, ses = subj_ses
        tree = f"derivatives/fastsurfer/{subj}/{ses}/"
        listing = s3.list_objects_v2(Bucket=bucket, Prefix=tree + COMPLETE_MARKER, MaxKeys=1)
        complete = any(o["Key"] == tree + COMPLETE_MARKER for o in listing.get("Contents", []))
        build = _get(s3, bucket, tree + "scripts/build.log") if complete else None
        release = build.decode(errors="replace").splitlines()[0].split()[0] if build else None
        marker = _get(s3, bucket, f"derivatives/func/{subj}/{ses}/{COMPLETE_MARKER}")
        doc = json.loads(marker) if marker else {}
        runs = set(doc.get("runs") or {}) if doc.get("kind") == "func-session" else None
        return subj_ses, (complete, release, runs)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        found = dict(pool.map(one, sessions))
    return (
        lambda s, e: found[(s, e)][0],
        lambda s, e: found[(s, e)][1],
        lambda s, e: found[(s, e)][2],
    )


def read_nss(s3, bucket: str) -> dict[tuple[str, str], int]:
    body = _get(s3, bucket, "config/nss_volumes.csv")
    if body is None:
        raise SystemExit(f"s3://{bucket}/config/nss_volumes.csv not found")
    out = {}
    for row in csv.DictReader(io.StringIO(body.decode())):
        value = (row.get("nss_frames") or "").strip()
        if value.isdigit():
            out[(row["subject_id"], row["session"])] = int(value)
    return out


def write_eligible(path: Path, sessions: list[Candidate]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["subject", "session", "n_runs", "nss_frames"])
        w.writerows([c.subject, c.session, c.n_runs, c.nss_frames] for c in sessions)


def main(argv: list[str] | None = None) -> int:
    import boto3

    from metrics.athena import CloudpipeMetrics

    from .calibrate import FUNC_QC_SCHEMA_VERSION

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bucket", required=True)
    p.add_argument("--session", default="ses-00A")
    p.add_argument("--region")
    p.add_argument("--workers", type=int, default=32)
    p.add_argument("--census", type=Path, required=True)
    p.add_argument("--eligible", type=Path, required=True)
    args = p.parse_args(argv)

    df = CloudpipeMetrics(bucket=args.bucket, region=args.region).func_qc(session=args.session)
    records = df.to_dict("records") if hasattr(df, "to_dict") else list(df)
    print(f"{len(records)} FuncQC records for {args.session}", file=sys.stderr, flush=True)
    s3 = boto3.client("s3", region_name=args.region)
    sessions = sorted({(r["subject"], r["session"]) for r in newest_records(records).values()})
    complete, release, marker = s3_lookups(s3, args.bucket, sessions, args.workers)
    census, eligible = build_frame(
        records,
        current_schema=FUNC_QC_SCHEMA_VERSION,
        tree_complete=complete,
        tree_release=release,
        marker_runs=marker,
        nss=read_nss(s3, args.bucket),
    )
    census.write(args.census)
    write_eligible(args.eligible, eligible)
    print(census.render())
    print(f"eligible sessions: {len(eligible)} -> {args.eligible}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
