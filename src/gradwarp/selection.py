"""Session selection for the gradwarp pilot (add-fasttrack-gradwarp-recovery 2.1).

The rule (spec, "Sessions come from one coil-model stratum"):
- the session's `mr_y_adm__info` row decodes to Siemens / Prisma / VE11C;
- it has a v7 `mr_y_qc__raw__smri__t1` run that is usable (manual QC ∧ protocol compliance
  ∧ complete);
- at most one session per subject, so each subject adds one anatomy and one head position;
- candidates are drawn from the whole stratum, and each scanner's are put in a seeded order,
  with sessions whose DAIC T1w is already staged in S3 first (they cost no DAIC request);
- the target is spread over scanners as evenly as availability allows (`allocate`).

Whether DAIC's T1w exists on NBDC is known only at fetch, where an excluded session is
replaced by its scanner's next candidate and the allocation is recomputed.

Scanners are referred to by index (1-based, in sorted serial order within the stratum);
serials, subject and session IDs never leave the pod.
"""

from __future__ import annotations

import random
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

STRATUM = ("Siemens", "Prisma", "VE11C")
TARGET = 80
MINIMUM = 50
SEED = 1112


@dataclass(frozen=True)
class AdmRow:
    subject: str
    session: str
    manufacturer: str
    model: str
    software: str
    serial: str


@dataclass(frozen=True)
class Candidate:
    subject: str
    session: str
    scanner: int
    t1_runs: tuple[tuple[int, str], ...]  # usable v7 runs: (rNN index, series UID)
    staged: bool

    def to_dict(self) -> dict:
        return {
            "subject": self.subject,
            "session": self.session,
            "scanner": self.scanner,
            "t1_runs": [list(r) for r in self.t1_runs],
            "staged": self.staged,
        }


def scanner_index(
    rows: Iterable[AdmRow], stratum: tuple[str, str, str] = STRATUM
) -> dict[str, int]:
    serials = sorted({r.serial for r in rows if (r.manufacturer, r.model, r.software) == stratum})
    return {s: i + 1 for i, s in enumerate(serials)}


def candidates(
    rows: Iterable[AdmRow],
    t1: Mapping[tuple[str, str], Sequence],
    staged: set[tuple[str, str]],
    stratum: tuple[str, str, str] = STRATUM,
) -> tuple[dict[int, list[Candidate]], Counter]:
    """Every stratum session with a usable T1 run, by scanner index; and why others failed.

    `t1` is `qc_join.read_v7_table(..., "smri__t1")`: runs with `.usable`, `.index`, `.uid`.
    """
    rows = list(rows)
    index = scanner_index(rows, stratum)
    out: dict[int, list[Candidate]] = defaultdict(list)
    reasons: Counter = Counter()
    for r in rows:
        if (r.manufacturer, r.model, r.software) != stratum:
            continue
        key = (r.subject, r.session)
        runs = t1.get(key) or []
        if not runs:
            reasons["no_v7_t1_row"] += 1
            continue
        usable = tuple((run.index, run.uid) for run in runs if run.usable)
        if not usable:
            reasons["no_usable_v7_t1_run"] += 1
            continue
        reasons["eligible"] += 1
        out[index[r.serial]].append(
            Candidate(r.subject, r.session, index[r.serial], usable, key in staged)
        )
    return dict(out), reasons


def order(
    by_scanner: Mapping[int, list[Candidate]], seed: int = SEED
) -> dict[int, list[Candidate]]:
    """Each scanner's candidates in a seeded order, staged first, one session per subject.

    A subject keeps the first of its sessions in that order, whichever scanner it is on.
    """
    rng = random.Random(seed)
    shuffled = {}
    for scanner in sorted(by_scanner):
        pool = sorted(by_scanner[scanner], key=lambda c: (c.subject, c.session))
        rng.shuffle(pool)
        shuffled[scanner] = sorted(pool, key=lambda c: not c.staged)  # stable: staged first
    # Interleave scanners while deduplicating, so no scanner wins a subject by going first.
    seen: set[str] = set()
    queues = {s: list(v) for s, v in shuffled.items()}
    kept: dict[int, list[Candidate]] = {s: [] for s in queues}
    while any(queues.values()):
        for s in sorted(queues):
            while queues[s]:
                c = queues[s].pop(0)
                if c.subject not in seen:
                    seen.add(c.subject)
                    kept[s].append(c)
                    break
    return kept


def allocate(available: Mapping[int, int], target: int = TARGET) -> dict[int, int]:
    """Spread `target` over scanners as evenly as `available` allows.

    Water-filling: every scanner gets the same share until it runs out; what it cannot take
    goes to the others. A remainder smaller than the number of open scanners goes to the
    lowest-indexed open scanners.
    """
    alloc = {s: 0 for s in available}
    remaining = min(target, sum(available.values()))
    while remaining > 0:
        open_ = [s for s in sorted(available) if alloc[s] < available[s]]
        share = max(1, remaining // len(open_))
        for s in open_:
            give = min(share, available[s] - alloc[s], remaining)
            alloc[s] += give
            remaining -= give
            if remaining == 0:
                break
    return alloc


#: The structural archive v7 QCs on Siemens (task 1.1b): the `rec-norm` reconstruction.
NORM_T1 = re.compile(r"_rec-norm_run-(?P<run>\d+)_T1w\.dicom\.tgz$")


def pick_archive(names: Iterable[str], usable_runs: Iterable[int]) -> str | None:
    """The rec-norm T1w archive to fetch: the one whose run number is a usable v7 run's
    index if any, else the lowest run. The UID check after download decides."""
    usable = set(usable_runs)
    found = sorted((int(m["run"]), n) for n in names if (m := NORM_T1.search(n)))
    if not found:
        return None
    for run, name in found:
        if run in usable:
            return name
    return found[0][1]


def plan(queues: Mapping[int, list], accepted: Mapping[int, int], target: int) -> list[int]:
    """Scanners that still need a session under the allocation recomputed from what is left.

    A scanner's availability is what it has accepted plus what is still queued, so an
    exclusion shrinks it and its share moves to the others.
    """
    avail = {s: accepted[s] + len(q) for s, q in queues.items()}
    alloc = allocate(avail, target)
    return [s for s in sorted(queues) if accepted[s] < alloc[s] and queues[s]]


@dataclass
class FetchGuard:
    """Stops the fetch before a systematic failure turns into a stream of requests.

    Every transient failure (`*_failed_*`: an expired token, a server fault) moves on to
    the next candidate, which makes requests of its own. Left alone, that could walk
    thousands of candidates. The guard stops the run after `max_consecutive` transient
    failures in a row, or once `cap` NBDC requests have been made.
    """

    cap: int
    max_consecutive: int = 3
    consecutive: int = 0

    def check(self, outcome: str, requests: int) -> str | None:
        """The reason to stop, or None to go on."""
        self.consecutive = self.consecutive + 1 if "_failed_" in outcome else 0
        if self.consecutive >= self.max_consecutive:
            return f"{self.consecutive} transient failures in a row (last: {outcome})"
        if requests >= self.cap:
            return f"request cap reached ({requests} >= {self.cap})"
        return None
