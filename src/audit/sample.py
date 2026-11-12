"""Draw the reprocess-audit sample (openspec add-reprocess-audit, tasks 2.5 and 2.6; spec audit-sample).

Four parts, recorded with the seed and every source file's sha256:

  special     every subject whose stored derivatives took an unusual path
              (anatomy reruns, qsphere, 2.6.0 -> 2.5.4, NSS per run, anatomy
              rerun inside the func waves, exclude-sessions, QC-rejected
              t1w->MNI) plus a seeded, rescue-depth-stratified draw of the
              rescue-ladder t1w->MNI subjects
  stratified  ~600 drawn proportionally across scanner software class x
              template size k, then topped up so every non-empty stratum of
              every other dimension (campaign wave, FastSurfer device, mixed
              recommendation template, leg-1 reuse, 09-09 reprocess) has at
              least TOPUP_MIN subjects
  bench       every subject of the fMRIPrep benchmark's measured sessions
  noise       ~50 drawn from the union of the three, run twice

and, per subject, what the audit must replay (2.6): production's session set,
the sessions to exclude so the longitudinal template is built from exactly
those, template-qsphere, and fastsurfer-device where the tree records it.

A source that a stratum depends on and that is missing stops the draw
(spec: "A source is missing"). Read-only; writes only --out-dir.

    pixi run -e ops python -m audit.sample --seed 20261108 \\
        --out-dir data/reprocess/audit-sample-2026-11-08
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The gitignored data/ of a main checkout (worktrees lack it), and the
# operator's benchmark/registration-QC extracts. Both overridable.
DATA = Path("data")
ANCHORS = Path.home() / "anchors-out"

STRATIFIED_TARGET = 600
NOISE_TARGET = 50
RESCUE_DRAW = 20
TOPUP_MIN = 3

# Subject IDs never appear in this file: src/ is published, and ABCD subject IDs
# are DUA-controlled. The two per-subject lists the draw needs live in a gitignored
# overrides file beside the other sources, read and checksummed with them:
#
#   data/reprocess/audit-overrides.json
#   {"never_draw":       {"<subject>": "<why a fresh run measures nothing>"},
#    "exclude_sessions": {"<subject>": ["<session>", ...]}}
#
# never_draw: known to fail by design (e.g. a 1-volume BOLD run; no BOLD at the
# source). exclude_sessions: an operator's production exclude-sessions parameter,
# replayed on top of the sessions production never built.
OVERRIDES = "audit-overrides.json"

# 05-12 was cancelled at this index of func_reprocess_05_12.csv and resumed.
CUT_05_12 = 4013


def sw_class(manufacturer: Any, software: Any) -> str:
    """ABCD scanner software class: what sets the NSS count and the transient."""
    try:
        man, sw = int(manufacturer), int(software)
    except (TypeError, ValueError):
        return "unknown"
    if man == 1:
        return "GE_DV25" if sw in (1, 2) else "GE_DV26plus"
    if man == 2:
        return "Philips"
    if man == 3:
        return "Siemens_XA30" if sw == 30 else "Siemens_preXA30"
    return "unknown"


def wave_of(label: str, subject: str, pos_05_12: dict[str, int]) -> str:
    """Normalise a plan-017 batch label to a campaign stratum."""
    w = label.removeprefix("func-reprocess-")
    if w.startswith("pilot"):
        return "pilot"
    if w.endswith("-retry") or w.endswith("-retry2"):
        return "retry"
    if w == "05-12":
        return "05-12-pre-cancel" if pos_05_12.get(subject, 0) < CUT_05_12 else "05-12-resumed"
    return w


@dataclass
class Subject:
    subject: str
    sessions: list[str] = field(default_factory=list)  # production FastSurfer session trees
    released: list[str] = field(default_factory=list)  # sessions in the ABCD release table
    sw: str = "unknown"
    k: int = 0
    wave: str = "none"
    device: str = "unknown"
    mixed_recommendation: bool = False
    leg1_reuse: bool = False
    reprocess_0909: bool = False
    special: list[str] = field(default_factory=list)
    extra_exclude: list[str] = field(default_factory=list)  # production exclude-sessions

    def strata(self) -> dict[str, str]:
        return {
            "sw_class": self.sw,
            "k": str(self.k),
            "wave": self.wave,
            "device": self.device,
            "mixed_recommendation": str(self.mixed_recommendation),
            "leg1_reuse": str(self.leg1_reuse),
            "reprocess_0909": str(self.reprocess_0909),
        }

    def replay(self) -> dict[str, Any]:
        exclude = sorted(set(self.released) - set(self.sessions))
        exclude = sorted(set(exclude) | set(self.extra_exclude))
        return {
            "production_sessions": self.sessions,
            "exclude_sessions": ",".join(exclude),
            "template_qsphere": "freesurfer" if "qsphere" in self.special else "spectral",
            "fastsurfer_device": self.device if self.device in ("cuda", "cpu") else "",
            # Production's surface route. No production subject had a newMSM
            # registration (derivatives/newmsm/) when newMSM became available
            # (2026-11-10: none), so every sampled subject's CIFTIs are FS-route.
            # Passed explicitly so a later change of the template default cannot
            # reach the audit (the queue manager refuses an audit row without it).
            "surface_registration": "fs",
        }


def draw_stratified(
    subjects: dict[str, Subject], eligible: list[str], already: set[str], rng: random.Random
) -> list[str]:
    """Proportional draw over sw_class x k, then the top-up over every other dimension."""
    pool = [s for s in eligible if s not in already]
    cells: dict[tuple[str, int], list[str]] = defaultdict(list)
    for s in pool:
        cells[(subjects[s].sw, subjects[s].k)].append(s)
    picked: list[str] = []
    for key in sorted(cells):
        members = sorted(cells[key])
        n = max(1, round(STRATIFIED_TARGET * len(members) / len(pool)))
        picked += rng.sample(members, min(n, len(members)))
    chosen = set(picked) | already
    for dim in ("wave", "device", "mixed_recommendation", "leg1_reuse", "reprocess_0909"):
        by_value: dict[str, list[str]] = defaultdict(list)
        for s in eligible:
            by_value[subjects[s].strata()[dim]].append(s)
        for value in sorted(by_value):
            have = sum(1 for s in chosen if subjects[s].strata()[dim] == value)
            if have >= TOPUP_MIN:
                continue
            spare = sorted(set(by_value[value]) - chosen)
            extra = rng.sample(spare, min(TOPUP_MIN - have, len(spare)))
            picked += extra
            chosen |= set(extra)
    return sorted(set(picked))


def draw_noise(subjects: dict[str, Subject], union: list[str], rng: random.Random) -> list[str]:
    """~NOISE_TARGET from the union, at least one per (device, sw_class) cell present."""
    cells: dict[tuple[str, str], list[str]] = defaultdict(list)
    for s in union:
        cells[(subjects[s].device, subjects[s].sw)].append(s)
    picked = [rng.choice(sorted(m)) for _, m in sorted(cells.items())]
    rest = sorted(set(union) - set(picked))
    picked += rng.sample(rest, max(0, min(NOISE_TARGET - len(picked), len(rest))))
    return sorted(picked)


def build(subjects: dict[str, Subject], specials: dict[str, list[str]], rescue: dict[str, int],
          bench: set[str], seed: int, never_draw: frozenset[str] = frozenset()) -> dict[str, Any]:  # fmt: skip
    rng = random.Random(seed)
    eligible = sorted(s for s, v in subjects.items() if v.k > 0 and s not in never_draw)
    eligible_set = set(eligible)

    special: dict[str, list[str]] = defaultdict(list)
    for reason, members in sorted(specials.items()):
        for s in members:
            if s in eligible_set:
                special[s].append(reason)
    by_depth: dict[int, list[str]] = defaultdict(list)
    for s, depth in rescue.items():
        if s in eligible_set:
            by_depth[depth].append(s)
    per_depth = max(1, RESCUE_DRAW // max(1, len(by_depth)))
    for depth in sorted(by_depth):
        for s in rng.sample(sorted(by_depth[depth]), min(per_depth, len(by_depth[depth]))):
            special[s].append(f"t1w_to_mni_rescue_depth_{depth}")

    bench_in = sorted(s for s in bench if s in eligible_set)
    stratified = draw_stratified(subjects, eligible, set(special) | set(bench_in), rng)
    union = sorted(set(special) | set(stratified) | set(bench_in))
    noise = draw_noise(subjects, union, rng)

    rows = []
    for s in union:
        subjects[s].special = special.get(s, [])
        parts = [p for p, members in (("special", special), ("stratified", stratified),
                                      ("bench", bench_in)) if s in members]  # fmt: skip
        rows.append({"subject": s, "parts": parts, "noise_floor": s in noise,
                     "special_reasons": special.get(s, []), **subjects[s].strata(),
                     **subjects[s].replay()})  # fmt: skip

    report: dict[str, Any] = {"seed": seed, "eligible": len(eligible), "sample": len(union),
                              "parts": {"special": len(special), "stratified": len(stratified),
                                        "bench": len(bench_in), "noise_floor": len(noise)},
                              "strata": {}}  # fmt: skip
    for dim in (
        "sw_class",
        "k",
        "wave",
        "device",
        "mixed_recommendation",
        "leg1_reuse",
        "reprocess_0909",
    ):
        pop = Counter(subjects[s].strata()[dim] for s in eligible)
        smp = Counter(subjects[s].strata()[dim] for s in union)
        report["strata"][dim] = {v: {"population": pop[v], "sample": smp[v]} for v in sorted(pop)}
    report["special_reasons"] = dict(Counter(r for rs in special.values() for r in rs))
    report["missing_strata"] = [
        f"{dim}={v}" for dim, vals in report["strata"].items()
        for v, c in vals.items() if c["population"] and not c["sample"]
    ]  # fmt: skip
    return {"rows": rows, "report": report}


# ---------------------------------------------------------------------------
# Loading the frame from local records
# ---------------------------------------------------------------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _need(path: Path) -> Path:
    if not path.exists():
        raise SystemExit(f"missing source {path}: a stratum depends on it, so the draw stops here")
    return path


def _ids(path: Path) -> list[str]:
    import pandas as pd

    return pd.read_csv(_need(path))["subject_id"].astype(str).tolist()


SPECIAL_FILES = {
    "histoalloc_422": ["anat-422-pilot.csv", "anat-422-sessions.csv", "anat-rerun-17.csv"],
    "qsphere": ["anat-topo-3.csv"],
    "topology_hang": ["anat-topology-hang.csv"],
    "exclude_sessions": ["anat-3u45.csv"],
    "fastsurfer_260_rerun": ["fastsurfer_254_rerun.csv"],
    "nss_per_run": ["func-reprocess-nss-per-run.csv"],
    "anatomy_rerun_in_func_wave": ["func_reprocess_anatomy.csv"],
}


def _sources(census: Path, data: Path, anchors: Path) -> dict[str, Path]:
    """Every file the draw reads; a missing one stops it (spec: A source is missing)."""
    rp = data / "reprocess"
    sources = {
        "sessions": data / "consort/sessions.parquet",
        "scanner": data / "abcd-tabulated/mr_y_adm__info.parquet",
        "census": census,
        "ledger": rp / "func_reprocess_ledger.jsonl",
        "wave_05_12": rp / "func_reprocess_05_12.csv",
        "t1w_to_mni": anchors / "regqc/t1w_to_mni_after.parquet",
        "bench": anchors / "final-2026-11-05/values.csv",
        "leg1": rp / "leg1_reuse_reprocess_subjects.csv",
        "reprocess_0909": rp / "baseline_reprocess_subjects.csv",
        "overrides": rp / OVERRIDES,
    }
    sources |= {f: rp / f for fs in SPECIAL_FILES.values() for f in fs}
    return {k: _need(v) for k, v in sources.items()}


def _uniform(values: set, empty: str = "unknown") -> str:
    """One value -> it; several -> "mixed"; any missing (None) or none at all -> `empty`.

    A session with no recorded device is unknown, not evidence of a mix.
    """
    if not values or None in values:
        return empty
    return str(next(iter(values))) if len(values) == 1 else "mixed"


def _subjects(sources: dict[str, Path]) -> dict[str, Subject]:
    """Production session set, scanner class, k, device and recommendation mix per subject."""
    import pandas as pd

    scan = pd.read_parquet(sources["scanner"])
    scan_cls = {
        (r.participant_id, r.session_id): sw_class(r.mr_y_adm__info__dev_manufact, r.mr_y_adm__info__dev__sftw_ver)
        for r in scan.itertuples()
    }  # fmt: skip
    rows = (json.loads(line) for line in sources["census"].read_text().splitlines())
    trees = {(r["subject"], r["tree"]): r for r in rows if r["tree"] != "long-template"}

    subjects: dict[str, Subject] = {}
    rec: dict[str, set] = defaultdict(set)
    for r in pd.read_parquet(sources["sessions"]).itertuples():
        s = subjects.setdefault(r.subject, Subject(r.subject))
        s.released.append(r.session)
        if trees.get((r.subject, r.session), {}).get("complete"):
            s.sessions.append(r.session)
            rec[r.subject].add(bool(r.abcd_t1))
    for s in subjects.values():
        s.sessions.sort()
        s.released.sort()
        s.k = len(s.sessions)
        s.sw = _uniform({scan_cls.get((s.subject, ses), "unknown") for ses in s.sessions})
        s.device = _uniform({trees[(s.subject, ses)]["device"] for ses in s.sessions})
        s.mixed_recommendation = rec[s.subject] == {True, False}
    return subjects


def _campaign(subjects: dict[str, Subject], sources: dict[str, Path]) -> None:
    """Plan-017 wave (the subject's last flush), leg-1 reuse and the 09-09 reprocess."""
    import pandas as pd

    pos = {
        sid: i for i, sid in enumerate(pd.read_csv(sources["wave_05_12"])["subject_id"].astype(str))
    }
    last: dict[str, str] = {}
    for line in sources["ledger"].read_text().splitlines():
        ev = json.loads(line)
        if ev.get("event") == "flushed" and ev.get("batch_label"):
            last[ev["subject"]] = ev["batch_label"]
    for sid, label in last.items():
        if sid in subjects:
            subjects[sid].wave = wave_of(label, sid, pos)
    for flag, key in (("leg1_reuse", "leg1"), ("reprocess_0909", "reprocess_0909")):
        for sid in _ids(sources[key]):
            if sid in subjects:
                setattr(subjects[sid], flag, True)


def load_frame(census: Path, data: Path = DATA, anchors: Path = ANCHORS) -> tuple[dict[str, Subject], dict[str, list[str]], dict[str, int], set[str], dict[str, str], frozenset[str]]:  # fmt: skip
    import pandas as pd

    sources = _sources(census, data, anchors)
    subjects = _subjects(sources)
    _campaign(subjects, sources)
    overrides = json.loads(sources["overrides"].read_text())
    for sid, sessions in overrides.get("exclude_sessions", {}).items():
        if sid in subjects:
            subjects[sid].extra_exclude = list(sessions)
    never_draw = frozenset(overrides.get("never_draw", {}))
    specials = {k: [s for f in fs for s in _ids(sources[f])] for k, fs in SPECIAL_FILES.items()}

    reg = pd.read_parquet(sources["t1w_to_mni"])
    specials["t1w_to_mni_qc_rejected"] = sorted(set(reg.loc[reg["verdict"] == "fail", "subject"]))
    rescued = reg[reg["rescue_ticket"].fillna(0) > 0]
    rescue = {r.subject: int(r.rescue_ticket) for r in rescued.itertuples()}

    bench = {v.split("_ses-")[0] for v in pd.read_csv(sources["bench"])["session"].astype(str)}
    checksums = {k: _sha(p) for k, p in sources.items()}
    return subjects, specials, rescue, bench, checksums, never_draw


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--data-dir", type=Path, default=DATA)
    p.add_argument("--anchors-dir", type=Path, default=ANCHORS)
    p.add_argument(
        "--census",
        type=Path,
        help="default: <data-dir>/reprocess/fastsurfer-tree-census-2026-11-08.jsonl",
    )
    p.add_argument("--out-dir", type=Path, required=True)
    args = p.parse_args(argv)

    census = args.census or args.data_dir / "reprocess/fastsurfer-tree-census-2026-11-08.jsonl"
    subjects, specials, rescue, bench, checksums, never_draw = load_frame(
        census, args.data_dir, args.anchors_dir
    )
    result = build(subjects, specials, rescue, bench, args.seed, never_draw)
    result["report"]["sources_sha256"] = checksums
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "sample.jsonl").open("w") as fh:
        for row in result["rows"]:
            fh.write(json.dumps(row) + "\n")
    (args.out_dir / "report.json").write_text(json.dumps(result["report"], indent=1) + "\n")
    rep = result["report"]
    print(
        json.dumps({k: rep[k] for k in ("seed", "eligible", "sample", "parts", "missing_strata")})
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
