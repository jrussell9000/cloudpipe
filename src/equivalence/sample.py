"""The ordered, stratified draw of baseline sessions (design D19).

One draw, with a recorded seed, of up to `size` sessions (the re-estimation's cap,
200) from the eligible baseline sessions, ordered so that every prefix — the 30
of the interim look, the 100 of the floor, and whatever final n the look sets —
holds each stratum close to its population share. Sessions are run in that order,
so the sample can be extended to the final n without a second draw.

Strata are cells of run count × scanner group:

- run count: the session's rest + n-back runs, binned 1–3, 4, 5, 6, 7–8. It is the
  main driver of per-session cost; run length is near-constant at baseline.
- scanner group: the session's protocol count of non-steady-state frames (5, 8 or
  16), a proxy for scanner software, since agreement may differ where the
  initial frames carry a T1-saturation transient.

Allocation is proportional with no floor, so the sample is self-weighting. A cell
whose expected count within the first `interim` sessions is below 1 is merged,
before the draw, into the adjacent run-count bin of the same scanner group, moving
toward the modal bin (6 runs).

Ordering: each drawn session gets the key (its random rank within its cell,
counted from 0, + U) / (its cell's allocation), U uniform on (0, 1), and the
sequence is sorted on it. A cell with allocation a therefore contributes about
a·k/N of the first k sessions, for every k.

    python -m equivalence.sample --eligible eligible_sessions.csv --seed 20261028 \\
        --exclude <smoke-test subject> --out draw.csv

Input CSV columns: `subject`, `session`, `n_runs`, `nss_frames`.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

#: Run-count bins, in order. Merging moves a cell toward MODAL_BIN.
RUN_BINS = ("1-3", "4", "5", "6", "7-8")
MODAL_BIN = "6"
DEFAULT_SIZE = 200
DEFAULT_INTERIM = 30


@dataclass(frozen=True)
class Candidate:
    subject: str
    session: str
    n_runs: int
    nss_frames: int


@dataclass(frozen=True)
class Drawn:
    order: int  # 1-based position in the run sequence
    subject: str
    session: str
    cell: str


def run_bin(n_runs: int) -> str:
    if n_runs < 1 or n_runs > 8:
        raise ValueError(f"{n_runs} runs is outside the 1-8 a baseline session has")
    if n_runs <= 3:
        return "1-3"
    if n_runs >= 7:
        return "7-8"
    return str(n_runs)


def _cell(bin_: str, nss: int) -> str:
    return f"runs {bin_} / nss {nss}"


def merged_cells(candidates: list[Candidate], interim: int) -> dict[Candidate, str]:
    """Each candidate's cell, after merging cells too small for the interim.

    A cell is too small when interim × its population share < 1. It merges into
    the adjacent run-count bin of its scanner group, toward the modal bin, and the
    check repeats until no cell is too small or a scanner group is one cell.
    """
    total = len(candidates)
    bins = {c: run_bin(c.n_runs) for c in candidates}
    groups: dict[int, list[Candidate]] = defaultdict(list)
    for c in candidates:
        groups[c.nss_frames].append(c)

    for members in groups.values():
        while True:
            counts = Counter(bins[c] for c in members)
            small = [b for b in RUN_BINS if b in counts and interim * counts[b] / total < 1]
            if not small or len(counts) == 1:
                break
            # Merge the smallest offending bin first.
            b = min(small, key=lambda x: (counts[x], RUN_BINS.index(x)))
            target = _merge_target(b, set(counts) - {b})
            for c in members:
                if bins[c] == b:
                    bins[c] = target
    return {c: _cell(bins[c], c.nss_frames) for c in candidates}


def _merge_target(bin_: str, present: set[str]) -> str:
    """The nearest present bin toward the modal bin, else the nearest the other way."""
    i, modal = RUN_BINS.index(bin_), RUN_BINS.index(MODAL_BIN)
    up, down = range(i + 1, len(RUN_BINS)), range(i - 1, -1, -1)
    for direction in (up, down) if i < modal else (down, up):
        for j in direction:
            if RUN_BINS[j] in present:
                return RUN_BINS[j]
    raise ValueError(f"no bin to merge {bin_!r} into")  # pragma: no cover - len(counts) > 1


def allocate(cell_sizes: dict[str, int], size: int) -> dict[str, int]:
    """Proportional allocation of `size` by largest remainder (Hamilton)."""
    total = sum(cell_sizes.values())
    if size > total:
        raise ValueError(f"asked for {size} sessions from {total} eligible")
    quotas = {k: size * v / total for k, v in cell_sizes.items()}
    alloc = {k: int(q) for k, q in quotas.items()}
    for k in sorted(quotas, key=lambda k: (-(quotas[k] - alloc[k]), k))[
        : size - sum(alloc.values())
    ]:
        alloc[k] += 1
    return alloc


def draw(
    candidates: list[Candidate],
    seed: int,
    size: int = DEFAULT_SIZE,
    interim: int = DEFAULT_INTERIM,
    exclude: set[str] | None = None,
) -> list[Drawn]:
    """The ordered sample: `size` sessions, one per subject, every prefix balanced."""
    exclude = exclude or set()
    pool = [c for c in candidates if c.subject not in exclude]
    subjects = Counter(c.subject for c in pool)
    repeated = sorted(s for s, n in subjects.items() if n > 1)
    if repeated:
        raise ValueError(f"one baseline session per subject; {repeated[:3]} appear more than once")

    cells = merged_cells(pool, interim)
    by_cell: dict[str, list[Candidate]] = defaultdict(list)
    for c in sorted(pool, key=lambda c: (c.subject, c.session)):  # order-independent input
        by_cell[cells[c]].append(c)
    alloc = allocate({k: len(v) for k, v in by_cell.items()}, size)

    rng = np.random.default_rng(seed)
    keyed = []
    for cell in sorted(by_cell):
        a = alloc[cell]
        if a == 0:
            continue
        chosen = rng.choice(len(by_cell[cell]), size=a, replace=False)
        offsets = rng.uniform(size=a)
        for rank, (idx, u) in enumerate(zip(chosen, offsets, strict=True)):
            keyed.append(((rank + u) / a, cell, by_cell[cell][idx]))
    keyed.sort(key=lambda t: t[0])
    return [Drawn(i + 1, c.subject, c.session, cell) for i, (_, cell, c) in enumerate(keyed)]


def read_candidates(path: Path) -> list[Candidate]:
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        missing = {"subject", "session", "n_runs", "nss_frames"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing columns {sorted(missing)}")
        return [
            Candidate(r["subject"], r["session"], int(r["n_runs"]), int(r["nss_frames"]))
            for r in reader
        ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--eligible", type=Path, required=True)
    p.add_argument("--seed", type=int, required=True, help="recorded with the draw")
    p.add_argument("--size", type=int, default=DEFAULT_SIZE)
    p.add_argument("--interim", type=int, default=DEFAULT_INTERIM)
    p.add_argument("--exclude", action="append", default=[], help="subject; repeatable")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)

    sample = draw(
        read_candidates(args.eligible), args.seed, args.size, args.interim, set(args.exclude)
    )
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["order", "subject", "session", "cell"])
        w.writerows([d.order, d.subject, d.session, d.cell] for d in sample)
    counts = Counter(d.cell for d in sample)
    print(f"{len(sample)} sessions drawn with seed {args.seed} -> {args.out}")
    for cell, n in sorted(counts.items()):
        print(f"  {cell}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
