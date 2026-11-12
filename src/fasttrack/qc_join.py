"""Join FastTrack source series to ABCD 7.0 raw QC, pair field maps, and derive their verdicts.

Implements four requirements of the `add-fasttrack-minproc` conversion spec, on one
session's source `.json` files and its v7 raw-QC rows:

- **Series classification.** Each series is classified from its source path: the
  `anat/`, `func/` or `fmap/` directory and the BIDS suffix. DAIC's `ClassifyType[2]` is
  kept beside it for the cross-check.
- **The QC join.** A T1w, T2w or BOLD series is matched to its v7 run by
  `SeriesInstanceUID`, and the match is cross-checked on `StudyDate` + `SeriesTime`
  against the run's `__study_dt` + `__series_t` (`timestamp_mismatch` when they differ).
- **Spin-echo pairing by adjacency in time.** AP and PA are paired only when adjacent
  among the session's field-map and BOLD series on the same date. A combined GE series
  is a pair on its own.
- **Field-map verdicts.** Each BOLD run, of any task, is attached to the nearest pair
  acquired before it on the same date, and checked against v7's `__fm__mssng_indicator`
  (`fm_attachment_mismatch`). Each pair's verdict comes from the runs attached to it:
  usable, `no_qc_verdict`, `fm_failed_qc` or `conflicting_fm_verdicts`.

Pure functions over parsed records, so the convert job and the wave replay (task 1.1a)
share one implementation. Reading files and S3 is the caller's job. Nothing here returns
or logs a patient field; the source `.json`'s `DateOfBirth` and the like are never read.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

#: v7 raw-QC table stem per series class. Field maps have no table of their own.
TABLES = {
    "T1w": "smri__t1",
    "T2w": "smri__t2",
    "rest": "rsfmri",
    "nback": "tfmri__nback",
    "mid": "tfmri__mid",
    "sst": "tfmri__sst",
}
BOLD_TASKS = ("rest", "nback", "mid", "sst")

_RUN_COL = re.compile(r"^mr_y_qc__raw__(?P<table>[a-z0-9_]+?)__r(?P<n>\d\d)(?P<field>_.*)$")
_BOLD = re.compile(r"_task-(?P<task>[a-z]+)_run-\d+_bold\.json$")
_FMAP_DIR = re.compile(r"_acq-func_dir-(?P<dir>AP|PA)_run-\d+_epi\.json$")
_FMAP_COMBINED = re.compile(r"_acq-func_run-\d+_epi\.json$")


# --------------------------------------------------------------------------- records


@dataclass(frozen=True)
class Series:
    """One source series, from its path and its DAIC `.json` header summary."""

    path: str  # relative to the session, e.g. "func/..._task-rest_run-01_bold.json"
    kind: str  # "T1w", "T2w", "bold" or "fmap"
    task: str | None = None  # bold: rest / nback / mid / sst
    direction: str | None = None  # fmap: "AP", "PA" or "combined"
    norm: bool = False  # structural `rec-norm` reconstruction
    classify_type: str | None = None  # DAIC ClassifyType[2]
    uid: str | None = None
    acquired: datetime | None = None

    @property
    def table(self) -> str | None:
        key = self.task if self.kind == "bold" else self.kind
        return TABLES.get(key or "")


@dataclass(frozen=True)
class V7Run:
    """One `rNN` of one v7 raw-QC table row."""

    table: str
    index: int
    uid: str | None
    acquired: datetime | None
    indicator: int | None = None
    protcom: int | None = None
    comp: int | None = None
    fm: Mapping[str, int | None] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.indicator == 1 and self.protcom == 1 and self.comp == 1

    @property
    def fm_passes(self) -> bool:
        f = self.fm
        return (
            f.get("fm_indicator") == 1
            and f.get("fm__protcom_indicator") == 1
            and f.get("fm__comp_indicator") == 1
            and f.get("fm__invalid_indicator") == 0
        )


@dataclass
class Pair:
    members: tuple[Series, ...]  # (AP, PA) in time order, or (combined,)
    attached: list[Series] = field(default_factory=list)
    verdict_runs: list[V7Run] = field(default_factory=list)
    verdict: str = ""

    @property
    def acquired(self) -> datetime:
        return max(m.acquired for m in self.members if m.acquired is not None)

    @property
    def combined(self) -> bool:
        return len(self.members) == 1


# --------------------------------------------------------------------------- parsing


def classify_path(rel: str) -> dict | None:
    """Series fields from a session-relative `.json` path, or None for out-of-scope series."""
    directory, _, name = rel.partition("/")
    if directory == "anat":
        for suffix in ("T1w", "T2w"):
            if name.endswith(f"_{suffix}.json"):
                return {"kind": suffix, "norm": "_rec-norm_" in name}
        return None
    if directory == "func":
        m = _BOLD.search(name)
        return {"kind": "bold", "task": m.group("task")} if m else None
    if directory == "fmap":
        if m := _FMAP_DIR.search(name):
            return {"kind": "fmap", "direction": m.group("dir")}
        if _FMAP_COMBINED.search(name):
            return {"kind": "fmap", "direction": "combined"}
    return None


#: v7 stores whole seconds; the source `.json` keeps the DICOM fraction. The 2026-11-07
#: smoke replay found every disagreement was exactly 1 s, so they are one clock written
#: two ways. Anything this close is the same series; a mislabeled archive is minutes off.
TIME_TOLERANCE_SECONDS = 1.0


def parse_datetime(study_date: object, series_time: object) -> datetime | None:
    """`StudyDate` + `SeriesTime`, keeping any fractional second. Accepts DICOM
    (`20181001`, `101042.2475`) and v7 (`2018-10-01`, `10:10:42`) spellings; anything
    else is None."""
    d = str(study_date or "").strip().replace("-", "")
    whole, _, frac = str(series_time or "").strip().replace(":", "").partition(".")
    if not (re.fullmatch(r"\d{8}", d) and re.fullmatch(r"\d{6}", whole)):
        return None
    micro = int((frac + "000000")[:6]) if re.fullmatch(r"\d*", frac) else 0
    try:
        return datetime(
            int(d[:4]),
            int(d[4:6]),
            int(d[6:]),
            int(whole[:2]),
            int(whole[2:4]),
            int(whole[4:]),
            micro,
        )
    except ValueError:
        return None


def series_from_json(rel: str, header: Mapping) -> Series | None:
    """A Series from its path and DAIC `.json`, or None when the path is out of scope."""
    fields = classify_path(rel)
    if fields is None:
        return None
    # DAIC's series type is the `ABCD-…` element. It is usually ClassifyType[2], but the
    # smoke replay found BOLD series with "original" there and the type elsewhere.
    ct = header.get("ClassifyType")
    items = ct if isinstance(ct, list) else []
    classify_type = next((str(x) for x in items if str(x).startswith("ABCD-")), None)
    uid = header.get("SeriesInstanceUID")
    return Series(
        path=rel,
        classify_type=classify_type,
        uid=str(uid).strip() if uid else None,
        acquired=parse_datetime(header.get("StudyDate"), header.get("SeriesTime")),
        **fields,
    )


def _int(value: str | None) -> int | None:
    value = (value or "").strip()
    if value in ("", "n/a", "NA"):
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def read_v7_table(path: Path, table: str) -> dict[tuple[str, str], list[V7Run]]:
    """(participant_id, session_id) -> that row's runs with a series UID, from one table."""
    csv.field_size_limit(10**9)
    out: dict[tuple[str, str], list[V7Run]] = {}
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        groups: dict[int, dict[str, str]] = {}
        for col in reader.fieldnames or []:
            m = _RUN_COL.match(col)
            if m and m.group("table") == table:
                groups.setdefault(int(m.group("n")), {})[m.group("field")] = col
        for row in reader:
            runs = []
            for n, cols in sorted(groups.items()):
                uid = (row.get(cols.get("__series_uid", ""), "") or "").strip()
                if uid in ("", "n/a"):
                    continue
                fm = {
                    key.lstrip("_"): _int(row.get(cols[key]))
                    for key in (
                        "__fm_indicator",
                        "__fm__protcom_indicator",
                        "__fm__comp_indicator",
                        "__fm__invalid_indicator",
                        "__fm__mssng_indicator",
                        "__fm__wrngloc_indicator",
                    )
                    if key in cols
                }
                runs.append(
                    V7Run(
                        table=table,
                        index=n,
                        uid=uid,
                        acquired=parse_datetime(
                            row.get(cols.get("__study_dt", "")), row.get(cols.get("__series_t", ""))
                        ),
                        indicator=_int(row.get(cols.get("_indicator", ""))),
                        protcom=_int(row.get(cols.get("__protcom_indicator", ""))),
                        comp=_int(row.get(cols.get("__comp_indicator", ""))),
                        fm=fm,
                    )
                )
            out[(row["participant_id"], row["session_id"])] = runs
    return out


# --------------------------------------------------------------------------- the join


#: Structural twins: one acquisition reconstructed as `rec-norm` and un-normalized,
#: seconds apart. v7 QCs only one of them.
TWIN_SECONDS = 5


@dataclass
class JoinResult:
    series: Series
    # matched | unmatched | twin_matched | ambiguous | timestamp_mismatch | no_qc_row | no_uid
    status: str
    run: V7Run | None = None
    seconds_off: float | None = None


def join(series: Iterable[Series], v7: Mapping[str, list[V7Run] | None]) -> list[JoinResult]:
    """Match each T1w/T2w/BOLD series to its v7 run by UID, cross-checking the time.

    `v7` maps a table stem to the session's runs in it, or to None when the table has no
    row for the session (`no_qc_row`). Field maps are not joined.
    """
    results = [_join_one(s, v7) for s in series if s.kind != "fmap"]
    _shared_runs_are_ambiguous(results)
    _twins_of_matched(results)
    return results


def _join_one(s: Series, v7: Mapping[str, list[V7Run] | None]) -> JoinResult:
    runs = v7.get(s.table)
    if runs is None:
        return JoinResult(s, "no_qc_row")
    if not s.uid:
        return JoinResult(s, "no_uid")
    hits = [r for r in runs if r.uid == s.uid]
    if not hits:
        return JoinResult(s, "unmatched")
    if len(hits) > 1:
        return JoinResult(s, "ambiguous")
    run = hits[0]
    if not (s.acquired and run.acquired):
        return JoinResult(s, "matched", run)
    off = round((s.acquired - run.acquired).total_seconds(), 3)
    status = "timestamp_mismatch" if abs(off) > TIME_TOLERANCE_SECONDS else "matched"
    return JoinResult(s, status, run, off)


def _shared_runs_are_ambiguous(results: list[JoinResult]) -> None:
    """One v7 run claimed by two series is ambiguous for both."""
    claimed: dict[tuple[str, int], list[JoinResult]] = {}
    for r in results:
        if r.run is not None:
            claimed.setdefault((r.run.table, r.run.index), []).append(r)
    for group in claimed.values():
        if len(group) > 1:
            for r in group:
                r.status, r.run = "ambiguous", None


def _is_twin(a: Series, b: Series) -> bool:
    return (
        a.kind == b.kind
        and a.norm != b.norm
        and a.acquired is not None
        and b.acquired is not None
        and abs((a.acquired - b.acquired).total_seconds()) <= TWIN_SECONDS
    )


def _twins_of_matched(results: list[JoinResult]) -> None:
    """An unmatched structural whose twin (the other reconstruction, seconds apart)
    matched is covered by the twin's QC row, not missing from v7."""
    matched = [r.series for r in results if r.status == "matched"]
    for r in results:
        if r.status == "unmatched" and r.series.kind in ("T1w", "T2w"):
            if any(_is_twin(r.series, t) for t in matched):
                r.status = "twin_matched"


# --------------------------------------------------------------------------- pairing


def timeline(series: Iterable[Series]) -> list[Series]:
    """Field-map and BOLD series with a time, in acquisition order."""
    timed = [s for s in series if s.kind in ("fmap", "bold") and s.acquired is not None]
    return sorted(timed, key=lambda s: (s.acquired, s.path))


def pair_fieldmaps(series: Iterable[Series]) -> tuple[list[Pair], list[Series]]:
    """(pairs, unpaired field maps). AP and PA pair only when adjacent in the timeline,
    on one date; a combined GE series is its own pair."""
    line = timeline(series)
    pairs: list[Pair] = []
    unpaired: list[Series] = []
    i = 0
    while i < len(line):
        s = line[i]
        if s.kind != "fmap":
            i += 1
            continue
        if s.direction == "combined":
            pairs.append(Pair((s,)))
            i += 1
            continue
        nxt = line[i + 1] if i + 1 < len(line) else None
        if (
            nxt is not None
            and nxt.kind == "fmap"
            and {s.direction, nxt.direction} == {"AP", "PA"}
            and s.acquired.date() == nxt.acquired.date()
        ):
            pairs.append(Pair((s, nxt)))
            i += 2
        else:
            unpaired.append(s)
            i += 1
    return pairs, unpaired


def attach(bold: Series, pairs: list[Pair]) -> Pair | None:
    """The nearest pair acquired before the run on the same date, or None."""
    if bold.acquired is None:
        return None
    day: date = bold.acquired.date()
    earlier = [p for p in pairs if p.acquired < bold.acquired and p.acquired.date() == day]
    return max(earlier, key=lambda p: p.acquired) if earlier else None


# --------------------------------------------------------------------------- verdicts


@dataclass
class SessionResult:
    joins: list[JoinResult]
    pairs: list[Pair]
    unpaired: list[Series]
    # (bold series, attached pair or None, v7 run or None, attachment status)
    attachments: list[tuple[Series, Pair | None, V7Run | None, str]]
    dates: list[date]
    untimed: list[Series]


def evaluate(series: list[Series], v7: Mapping[str, list[V7Run] | None]) -> SessionResult:
    """The whole chain for one session: join, pair, attach, verdicts."""
    joins = join(series, v7)
    run_of = {j.series.path: j.run for j in joins if j.status == "matched"}
    pairs, unpaired = pair_fieldmaps(series)
    attachments = []
    for s in timeline(series):
        if s.kind != "bold":
            continue
        pair = attach(s, pairs)
        run = run_of.get(s.path)
        if run is None:
            status = "no_v7_run"
        else:
            missing = run.fm.get("fm__mssng_indicator")
            if missing is None:
                status = "no_fm_flags"
            elif (missing == 0 and pair is None) or (missing == 1 and pair is not None):
                status = "fm_attachment_mismatch"
            else:
                status = "agrees"
        if pair is not None:
            pair.attached.append(s)
            if status == "agrees":
                pair.verdict_runs.append(run)
        attachments.append((s, pair, run, status))
    for p in pairs:
        if not p.verdict_runs:
            p.verdict = "no_qc_verdict"
        else:
            passes = {r.fm_passes for r in p.verdict_runs}
            p.verdict = (
                "usable"
                if passes == {True}
                else "fm_failed_qc"
                if passes == {False}
                else "conflicting_fm_verdicts"
            )
    dates = sorted({s.acquired.date() for s in series if s.acquired is not None})
    untimed = [s for s in series if s.acquired is None]
    return SessionResult(joins, pairs, unpaired, attachments, dates, untimed)
