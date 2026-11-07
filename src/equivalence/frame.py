"""The baseline sampling frame, its census, and the draw's input (design D11, D19, D21).

The population is ABCD's own imaging manifest, `mr_y_adm__info`, at `ses-00A`:
every baseline session ABCD released, whether or not cloudpipe produced anything
for it (D21, operator 2026-10-31). Each is accounted for in the census, so the
frame is a known part of a known whole rather than a residue of what happened to
be processed.

A session with no cloudpipe FuncQC record is excluded whole, as
`no_cloudpipe_output`, its detail naming what ABCD recommends for it (most
recommend no BOLD at all). Otherwise eligibility is decided at
`(subject, session, task, run)` grain for every baseline rest and n-back run.
A run is eligible when all of these hold:

- ABCD recommends the run's task for the session (`mr_y_qc__incl`,
  `rsfmri_indicator` / `tfmri__nback_indicator`), so the comparison covers the
  data an analyst would use (`abcd_not_recommended` otherwise);
- the session's FastSurfer tree is complete (`_complete.json`), its anatomy and
  the anatomical comparison's input;
- the tree was written by the sampled FastSurfer release, read from the tree's
  own `scripts/build.log` (`fastsurfer_release` otherwise);
- the session has a protocol NSS count in `config/nss_volumes.csv`, which both
  arms are given;
- the run is listed in the session's func `_complete.json`, so it is a current
  exploded output rather than a legacy tarball;
- the run's newest FuncQC record is at a schema with current VOLUMES (1.3 or
  later: the plan-017 reprocess) (`cloudpipe_output_superseded` otherwise).

The confounds are not an eligibility condition (D21). The MNI BOLD every primary
endpoint reads is the same at FuncQC 1.3 and 1.4; only the confounds table moved
(#926). So a session is eligible at 1.3, and the frame records whether all its
eligible runs' confounds are current (1.4). The measurement records which
confounds it read, and a session whose confounds the rerun later replaces is
measured again (`fmriprep_bench.py run-draw`), so `n_cosines` and the aCompCor
angles are never reported from 1.3 confounds.

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
from dataclasses import dataclass
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
ABCD_NOT_RECOMMENDED = "abcd_not_recommended"
NO_CLOUDPIPE_OUTPUT = "no_cloudpipe_output"
NOT_IN_ABCD_MANIFEST = "not_in_abcd_manifest"
#: The release every tree in the sample was built by (task 7.1's census).
SAMPLED_FASTSURFER = "2.5.4+cdfccea"
TASKS = ("task-rest", "task-nback")
#: The oldest FuncQC schema whose run's MNI BOLD is current: the plan-017
#: reprocess wrote 1.3, and the confounds-only rerun moves a run to 1.4 without
#: touching its volumes.
CURRENT_VOLUMES_SCHEMA = "1.3"
#: The schema whose confounds are current (nipype CompCor and cosines, #926).
CURRENT_CONFOUNDS_SCHEMA = "1.4"

PHENOTYPE = "tabulated/abcd/rawdata/phenotype"
INDICATORS = {
    "task-rest": "mr_y_qc__incl__rsfmri_indicator",
    "task-nback": "mr_y_qc__incl__tfmri__nback_indicator",
}


def _schema(value) -> tuple[int, ...]:
    """'1.3' -> (1, 3); anything unparseable sorts below every real schema."""
    try:
        return tuple(int(p) for p in str(value).split("."))
    except ValueError:
        return ()


@dataclass(frozen=True)
class Recommended:
    """ABCD's Recommended Image Inclusion for one session, per task."""

    tasks: frozenset[str]

    def __str__(self) -> str:
        return ", ".join(sorted(self.tasks)) or "neither rest nor n-back"


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


def _session_reason(subject, session, tree_complete, tree_release, nss):
    """Why a session with cloudpipe output is excluded whole, or None."""
    if not tree_complete(subject, session):
        return ANATOMY_UNAVAILABLE, "no FastSurfer tree, or no _complete.json on it"
    release = tree_release(subject, session)
    if release != SAMPLED_FASTSURFER:
        return (
            FASTSURFER_RELEASE,
            f"tree built by {release!r}, not the sampled {SAMPLED_FASTSURFER}",
        )
    if (subject, session) not in nss:
        return NSS_UNAVAILABLE, "no row in config/nss_volumes.csv"
    return None


def _run_reason(key: RunKey, rec: dict, recommended: Recommended, listed: set[str]):
    """Why one run is excluded, or None if it is eligible."""
    if key.task not in recommended.tasks:
        return ABCD_NOT_RECOMMENDED, f"mr_y_qc__incl does not recommend {key.task} for the session"
    if f"{key.task}_{key.run}" not in listed:
        return missing_in_arm("cloudpipe"), "not listed in the session's func _complete.json"
    schema = rec.get("schema_version")
    if _schema(schema) < _schema(CURRENT_VOLUMES_SCHEMA):
        return (
            CLOUDPIPE_OUTPUT_SUPERSEDED,
            f"newest FuncQC record is schema {schema}, before the reprocessed volumes "
            f"({CURRENT_VOLUMES_SCHEMA})",
        )
    return None


def build_frame(
    records: Iterable[dict],
    *,
    population: dict[tuple[str, str], Recommended],
    tree_complete: Callable[[str, str], bool],
    tree_release: Callable[[str, str], str | None],
    marker_runs: Callable[[str, str], set[str] | None],
    nss: dict[tuple[str, str], int],
) -> tuple[Census, list[Candidate]]:
    """The census over ABCD's manifest and the eligible sessions, from injected lookups.

    `population` is every manifest session with its recommended tasks. The
    lookups are called once per session that has cloudpipe output.
    `marker_runs` returns the `{task}_{run}` ids the session's func marker
    lists, or None when there is no current marker.
    """
    by_session: dict[tuple[str, str], list[tuple[RunKey, dict]]] = {}
    for key, rec in sorted(newest_records(records).items()):
        by_session.setdefault((key.subject, key.session), []).append((key, rec))

    census = Census()
    census.population = len(population)
    sessions: list[Candidate] = []
    for subject, session in sorted(set(population) | set(by_session)):
        runs = by_session.get((subject, session), [])
        recommended = population.get((subject, session))
        if recommended is None:
            census.exclude_session(subject, session, NOT_IN_ABCD_MANIFEST, "not in mr_y_adm__info")
            continue
        if not runs:
            census.exclude_session(
                subject, session, NO_CLOUDPIPE_OUTPUT, f"ABCD recommends {recommended}"
            )
            continue
        session_reason = _session_reason(subject, session, tree_complete, tree_release, nss)
        if session_reason is not None:
            census.exclusions += [Exclusion(k, *session_reason) for k, _ in runs]
            continue

        listed = marker_runs(subject, session) or set()
        eligible = []
        for key, rec in runs:
            reason = _run_reason(key, rec, recommended, listed)
            if reason is None:
                eligible.append((key, rec))
            else:
                census.exclusions.append(Exclusion(key, *reason))
        census.eligible += [k for k, _ in eligible]
        if eligible:
            current = all(
                _schema(r.get("schema_version")) >= _schema(CURRENT_CONFOUNDS_SCHEMA)
                for _, r in eligible
            )
            sessions.append(
                Candidate(
                    subject,
                    session,
                    len(eligible),
                    nss[(subject, session)],
                    confounds_current=current,
                )  # fmt: skip
            )
    return census, sessions


# ---------------------------------------------------------------------------
# S3 lookups
# ---------------------------------------------------------------------------


def _get(s3, bucket: str, key: str) -> bytes | None:
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return None


def read_population(s3, bucket: str, session: str) -> dict[tuple[str, str], Recommended]:
    """ABCD's manifest at `session`, each with the tasks `mr_y_qc__incl` recommends.

    A session the inclusion table has no row for recommends nothing: the table
    holds an explicit "1"/"0" for every released session, so a missing row is
    not a recommendation.
    """

    def table(name: str) -> list[dict]:
        body = _get(s3, bucket, f"{PHENOTYPE}/{name}.tsv")
        if body is None:
            raise SystemExit(f"s3://{bucket}/{PHENOTYPE}/{name}.tsv not found")
        return list(csv.DictReader(io.StringIO(body.decode()), delimiter="\t"))

    incl = {(r["participant_id"], r["session_id"]): r for r in table("mr_y_qc__incl")}
    out = {}
    for r in table("mr_y_adm__info"):
        key = (r["participant_id"], r["session_id"])
        if key[1] != session:
            continue
        row = incl.get(key, {})
        out[key] = Recommended(
            frozenset(task for task, col in INDICATORS.items() if row.get(col) == "1")
        )
    return out


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
        w.writerow(["subject", "session", "n_runs", "nss_frames", "confounds_current"])
        w.writerows(
            [c.subject, c.session, c.n_runs, c.nss_frames, int(c.confounds_current)]
            for c in sessions
        )


def main(argv: list[str] | None = None) -> int:
    import boto3

    from metrics.athena import CloudpipeMetrics

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bucket", required=True)
    p.add_argument("--session", default="ses-00A")
    p.add_argument("--region")
    p.add_argument("--workers", type=int, default=32)
    p.add_argument("--census", type=Path, required=True)
    p.add_argument("--eligible", type=Path, required=True)
    args = p.parse_args(argv)

    s3 = boto3.client("s3", region_name=args.region)
    population = read_population(s3, args.bucket, args.session)
    print(f"{len(population)} {args.session} sessions in mr_y_adm__info", file=sys.stderr)
    df = CloudpipeMetrics(bucket=args.bucket, region=args.region).func_qc(session=args.session)
    records = df.to_dict("records") if hasattr(df, "to_dict") else list(df)
    print(f"{len(records)} FuncQC records for {args.session}", file=sys.stderr, flush=True)
    sessions = sorted({(r["subject"], r["session"]) for r in newest_records(records).values()})
    complete, release, marker = s3_lookups(s3, args.bucket, sessions, args.workers)
    census, eligible = build_frame(
        records,
        population=population,
        tree_complete=complete,
        tree_release=release,
        marker_runs=marker,
        nss=read_nss(s3, args.bucket),
    )
    census.write(args.census)
    write_eligible(args.eligible, eligible)
    print(census.render())
    current = sum(c.confounds_current for c in eligible)
    print(
        f"eligible sessions: {len(eligible)} -> {args.eligible} "
        f"({current} with current confounds, {len(eligible) - current} awaiting the rerun)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
