#!/usr/bin/env python3
"""Draw a session-count-stratified subject sample for a scaled test batch.

Why stratified rather than random: cost and wall-clock scale near-linearly in a
subject's session count k ($0.096 at k=1 to $0.305 at k=4, issue #133), and k is
also what drives per-workflow footprint on cpu-light-nodepool (2 + 2k cpu,
5G + 4G*k memory). A random draw's k-mix drifts from the corpus, so extrapolating
its cost and duration to the full 11,628 is unreliable. Matching the corpus
proportions makes the batch's mean directly scalable.

Prior test subjects are re-usable, but ONLY after a flush. Once a subject has been
processed its FastSurfer derivatives exist under `derivatives/fastsurfer/{subj}/`,
and the pipeline's derivatives inventory then SKIPS the whole anatomical phase —
which would silently omit the GPU-heavy steps and understate load, the opposite of
what a capacity test needs. `scripts/prep_test_batch.py --flush-qc` clears both the
derivatives and the subject-keyed metrics; run it before submitting (docs/operations.md
Step 1). `--seed-from` therefore defaults to the previous batch's sample, making
it a strict subset of the new draw so the two batches are directly comparable at
different concurrencies. Pass `--exclude-prior` instead if you would rather not flush.

Session counts come from nss_volumes.csv, which is one row per subject-session.
That is the pipeline's own view of which sessions exist, so it is the right source
for k, but note it is not a run count: BOLD runs per session vary and are not
represented here.

Usage:
  pixi run python scripts/make_test_sample.py 200 -o tools/cloudpipe_test_sample_200.csv
"""

from __future__ import annotations

import argparse
import collections
import csv
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NSS = REPO / "tools/nss_volumes.csv"
ELIGIBLE = REPO / "tools/first-level-subjects.csv"
# The previous batch, carried into the new draw by default so the two runs differ
# only in concurrency. Requires a flush before submitting.
SEED_DEFAULT = "tools/cloudpipe_test_sample_200.csv"
# Every list that has been submitted to the cluster before. Subjects in any of
# these have derivatives in S3 and skip the anatomical phase unless flushed.
PRIOR_RUNS = (
    "tools/cloudpipe_test_sample.csv",
    "tools/cloudpipe_test_sample_200.csv",
    "tools/cloudpipe_test_sample_10.csv",
    "tools/subjectids_v611_50.csv",
    "tools/subjectids_v611_first9.csv",
    "tools/subjectids_v611_single.csv",
)
# Measured $/subject by session count, k-weighted (issue #133, 2026-08-03 batch).
# Day+1 Kubecost reads run 7-14% high, so these are a slight overestimate.
COST_BY_K = {1: 0.096, 2: 0.154, 3: 0.235, 4: 0.305}


def _read_ids(path: Path) -> set[str]:
    with path.open() as fh:
        return {r["subject_id"] for r in csv.DictReader(fh) if r.get("subject_id")}


def sessions_by_subject() -> dict[str, int]:
    """Session count k per subject, from the per-subject-session nss table."""
    seen: dict[str, set[str]] = {}
    with NSS.open() as fh:
        for row in csv.DictReader(fh):
            seen.setdefault(row["subject_id"], set()).add(row["session"])
    return {subj: len(sess) for subj, sess in seen.items()}


def allocate_quota(
    strata_sizes: dict[int, int], n: int, seeded_by_k: collections.Counter[int]
) -> dict[int, int]:
    """How many subjects to draw from each session-count stratum.

    Largest-remainder allocation against the pool's proportions, so the sample's
    k-mix matches the corpus and the quotas still sum to exactly n.

    Seeded subjects consume their own stratum's quota. Their k-mix will not match
    the corpus, so a stratum can be oversubscribed; the excess is borrowed from
    whichever strata have the most room above their own seeded count.
    """
    total = sum(strata_sizes.values())
    exact = {k: n * size / total for k, size in strata_sizes.items()}
    quota = {k: int(v) for k, v in exact.items()}
    shortfall = n - sum(quota.values())
    for k, _ in sorted(exact.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True)[:shortfall]:
        quota[k] += 1

    for k, used in seeded_by_k.items():
        if used <= quota.get(k, 0):
            continue
        debt = used - quota.get(k, 0)
        quota[k] = used
        for other in sorted(quota, key=lambda x: quota[x] - seeded_by_k[x], reverse=True):
            if other == k or debt == 0:
                continue
            take = min(debt, quota[other] - seeded_by_k[other])
            quota[other] -= take
            debt -= take
    return quota


def _resolve_prior(
    args: argparse.Namespace, k_by_subject: dict[str, int]
) -> tuple[set[str], list[str]]:
    """Either exclude every prior-run subject, or seed the draw from the last batch."""
    if args.exclude_prior:
        excluded: set[str] = set()
        for rel in PRIOR_RUNS:
            path = REPO / rel
            if path.exists():
                excluded |= _read_ids(path)
        return excluded, []
    if args.seed_from and args.seed_from.exists() and args.seed_from.is_file():
        return set(), sorted(s for s in _read_ids(args.seed_from) if s in k_by_subject)
    return set(), []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("n", type=int, help="sample size")
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument(
        "--seed",
        type=int,
        default=20260810,
        help="RNG seed; fixed so the draw is reproducible and reviewable",
    )
    ap.add_argument(
        "--seed-from",
        type=Path,
        default=REPO / SEED_DEFAULT,
        help="CSV of subjects to carry into the draw as a subset (default: the "
        "previous batch's sample). Pass /dev/null to disable.",
    )
    ap.add_argument(
        "--exclude-prior",
        action="store_true",
        help="exclude every previously-run subject instead of seeding from them; "
        "use when not flushing first",
    )
    args = ap.parse_args()

    k_by_subject = sessions_by_subject()
    eligible = _read_ids(ELIGIBLE)

    excluded, seeded = _resolve_prior(args, k_by_subject)
    if len(seeded) > args.n:
        print(
            f"error: --seed-from has {len(seeded)} subjects, more than n={args.n}",
            file=sys.stderr,
        )
        return 1

    pool = {s: k for s, k in k_by_subject.items() if s in eligible and s not in excluded}
    if len(pool) < args.n:
        print(f"error: pool has {len(pool)} subjects, need {args.n}", file=sys.stderr)
        return 1
    missing = [s for s in seeded if s not in pool]
    if missing:
        print(
            f"warning: {len(missing)} seeded subjects are not in the eligible pool "
            f"and were dropped: {missing[:5]}",
            file=sys.stderr,
        )
        seeded = [s for s in seeded if s in pool]

    by_k: dict[int, list[str]] = collections.defaultdict(list)
    for subj, k in pool.items():
        by_k[k].append(subj)
    for subjects in by_k.values():
        subjects.sort()  # deterministic before seeding

    total = len(pool)
    seeded_by_k = collections.Counter(k_by_subject[s] for s in seeded)
    quota = allocate_quota({k: len(v) for k, v in by_k.items()}, args.n, seeded_by_k)

    rng = random.Random(args.seed)
    sample: list[str] = list(seeded)
    for k in sorted(by_k):
        remaining = quota[k] - seeded_by_k[k]
        if remaining > 0:
            candidates = [s for s in by_k[k] if s not in set(seeded)]
            sample.extend(rng.sample(candidates, remaining))
    rng.shuffle(sample)  # interleave k values so early submissions are not all k=1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as fh:
        # lineterminator: csv's excel dialect emits CRLF, which would make this the
        # only CRLF file in tools/ and show up as a whole-file diff on regeneration.
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["subject_id"])
        writer.writerows([s] for s in sample)

    sessions = sum(k_by_subject[s] for s in sample)
    cost = sum(COST_BY_K[k_by_subject[s]] for s in sample)
    print(f"wrote {len(sample)} subjects to {args.output}")
    print(f"pool {total} eligible ({len(excluded)} prior-run subjects excluded)")
    if seeded:
        print(f"seeded {len(seeded)} from {args.seed_from.name} (flush before submitting)")
    print(f"{'k':>3} {'sample':>7} {'share':>7} {'pool share':>11}")
    counts = collections.Counter(k_by_subject[s] for s in sample)
    for k in sorted(by_k):
        print(
            f"{k:>3} {counts[k]:>7} {counts[k] / len(sample):>6.1%} {len(by_k[k]) / total:>10.1%}"
        )
    print(f"sessions {sessions} (mean k {sessions / len(sample):.2f})")
    print(f"projected cost ${cost:.2f} (${cost / len(sample):.4f}/subject)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
