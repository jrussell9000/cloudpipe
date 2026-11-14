"""Within-session ICC(1) per run-level endpoint, descriptive (task 4.15's last bullet).

The sample size was planned against a within-session correlation of
`equivalence.PLANNING_ICC`. This module measures it on the analysed sample, to
check that figure. It never reaches a published interval, because the primary
analysis tests session means.

Each run contributes the quantity its session's endpoint averages:

- ratio endpoints (`aor`, `aqi`, `gcor`, `tsnr_median`): log(cloudpipe / fMRIPrep);
- correlation endpoints (`mean_image_correlation`, `tsnr_map_correlation`): the
  run's correlation on the Fisher z scale, the scale sessions average on.

Sessions are the clusters. The 95% interval comes from resampling whole
sessions. Inputs are `equivalence.collect`'s outputs: `runs.jsonl` (one record
per compared run; session-level records without these fields are skipped) and
`values.csv` (its `session` column defines the analysed sample).

    python -m equivalence.within_session_icc --runs runs.jsonl --values values.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .equivalence import PLANNING_ICC, icc_one_way

RATIO_ENDPOINTS = ("aor", "aqi", "gcor", "tsnr_median")
CORRELATION_ENDPOINTS = ("mean_image_correlation", "tsnr_map_correlation")


@dataclass(frozen=True)
class SessionIcc:
    endpoint: str
    n_runs: int
    n_sessions: int
    icc: float
    lower: float
    upper: float


def run_value(record: dict, endpoint: str) -> float | None:
    """The run's contribution to `endpoint`, or None when it has none."""
    if endpoint in RATIO_ENDPOINTS:
        a = (record.get("cloudpipe") or {}).get(endpoint)
        b = (record.get("fmriprep") or {}).get(endpoint)
        if a is None or b is None or not (a > 0 and b > 0):
            return None
        v = math.log(a / b)
    elif endpoint in CORRELATION_ENDPOINTS:
        r = record.get(endpoint)
        if r is None or not -1.0 < r < 1.0:
            return None
        v = math.atanh(r)
    else:
        raise ValueError(f"not a run-level endpoint: {endpoint}")
    return v if math.isfinite(v) else None


def session_key(record: dict) -> str:
    return f"{record['subject']}_{record['session']}"


def within_session_icc(
    records: list[dict], endpoint: str, *, resamples: int = 2000, seed: int = 20261112
) -> SessionIcc:
    """ICC(1) of `endpoint` over runs clustered by session, with a session-bootstrap interval."""
    by_session: dict[str, list[float]] = defaultdict(list)
    for rec in records:
        v = run_value(rec, endpoint)
        if v is not None:
            by_session[session_key(rec)].append(v)
    keys = sorted(by_session)
    values = np.array([v for k in keys for v in by_session[k]])
    labels = np.array([i for i, k in enumerate(keys) for _ in by_session[k]])
    icc = icc_one_way(values, labels)

    rng = np.random.default_rng(seed)
    boot = []
    for _ in range(resamples):
        pick = rng.integers(0, len(keys), len(keys))
        bv = [v for i in pick for v in by_session[keys[i]]]
        bl = [j for j, i in enumerate(pick) for _ in by_session[keys[i]]]
        try:
            boot.append(icc_one_way(np.array(bv), np.array(bl)))
        except ValueError:  # a resample with no session of two or more runs
            continue
    lower, upper = (float(x) for x in np.percentile(boot, [2.5, 97.5]))
    return SessionIcc(endpoint, int(values.size), len(keys), icc, lower, upper)


def load(runs: Path, values: Path) -> list[dict]:
    """The analysed sample's run records."""
    with values.open() as f:
        sample = {row["session"] for row in csv.DictReader(f)}
    with runs.open() as f:
        records = [json.loads(line) for line in f if line.strip()]
    return [r for r in records if "subject" in r and "session" in r and session_key(r) in sample]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runs", type=Path, required=True, help="collect's runs.jsonl")
    p.add_argument("--values", type=Path, required=True, help="collect's values.csv")
    p.add_argument("--resamples", type=int, default=2000)
    args = p.parse_args(argv)

    records = load(args.runs, args.values)
    print(f"planning ICC {PLANNING_ICC}; ICC(1) over runs within sessions, session bootstrap")
    print(f"{'endpoint':24s} {'runs':>5s} {'sessions':>8s} {'ICC(1)':>7s}  95% interval")
    for ep in RATIO_ENDPOINTS + CORRELATION_ENDPOINTS:
        r = within_session_icc(records, ep, resamples=args.resamples)
        print(f"{ep:24s} {r.n_runs:5d} {r.n_sessions:8d} {r.icc:7.3f}  {r.lower:.3f}-{r.upper:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
