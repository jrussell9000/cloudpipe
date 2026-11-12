"""The descriptive set (methods 5.4) from the files the measurement already wrote.

No margin and no verdict: every quantity here is reported, with both pipelines'
values side by side, and decides nothing. It reads the same two files the primary
analysis does, `collect`'s session table and its per-run JSONL, and computes:

- **Median tSNR as agreement**: ICC(2,1) and Bland–Altman of the session geometric
  means. Tested as a ratio in tier 1, so it is not counted twice.
- **Motion and intensity summaries** (`mean_fd`, `max_fd`, `mean_dvars`): each
  pipeline's session means, ICC(2,1) and Bland–Altman. The pipelines estimate
  motion differently (ABCD's `3dvolreg` against fMRIPrep's `mcflirt`), so a
  difference is an estimator difference. The two `dvars` columns are on different
  intensity scales (fMRIPrep's normalised to a median of 1000, cloudpipe's in the
  input's units), so DVARS is also compared with cloudpipe's put on fMRIPrep's
  scale (`mean_dvars_common_scale`).
- **The two pipelines' own brain masks**: their Dice, per session.
- **Registration against both targets**: each pipeline's label Dice against its
  own engine's segmentation of the template (the primary endpoint's inputs) and
  against the other engine's.
- **Tier 1 by task**: the two agreement correlations for rest and n-back runs
  separately, each session's runs of a task averaged on Fisher z.

Grayordinates, aCompCor, BOLD-to-T1w displacement, warp folding and the anatomy
need measurements this table does not hold, and are not here.

    python -m equivalence.describe --values values.csv --runs runs.jsonl [--json describe.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .analyze import read_table
from .equivalence import BlandAltmanResult, IccResult, bland_altman, icc_2_1

#: Per-run fields each arm's record carries, summarised per session.
MOTION_FIELDS = ("mean_fd", "max_fd", "mean_dvars")
#: Per-run agreement statistics broken down by task.
TASK_FIELDS = ("mean_image_correlation", "tsnr_map_correlation")
#: Registration scorings: (arm, target engine).
TARGETS = (
    "cloudpipe_vs_cloudpipe_target",
    "fmriprep_vs_fmriprep_target",
    "cloudpipe_vs_fmriprep_target",
    "fmriprep_vs_cloudpipe_target",
)
_R_CLIP = 1.0 - 1e-7


@dataclass(frozen=True)
class Distribution:
    n: int
    mean: float
    sd: float
    p5: float
    p25: float
    median: float
    p75: float
    p95: float

    @classmethod
    def of(cls, values) -> Distribution:
        v = np.asarray([x for x in values if x is not None and math.isfinite(x)], dtype=float)
        if v.size < 2:
            raise ValueError(f"needs at least 2 values, got {v.size}")
        q = np.percentile(v, [5, 25, 50, 75, 95])
        return cls(int(v.size), float(v.mean()), float(v.std(ddof=1)), *map(float, q))


@dataclass(frozen=True)
class Agreement:
    """One quantity both pipelines produce: each side's distribution and how they agree."""

    quantity: str
    cloudpipe: Distribution
    fmriprep: Distribution
    icc: IccResult
    bland_altman: BlandAltmanResult
    note: str = ""


@dataclass(frozen=True)
class Description:
    n_sessions: int
    tsnr_median: Agreement
    motion: list[Agreement]
    own_mask_dice: Distribution
    registration: dict[str, Distribution]
    by_task: dict[str, dict[str, Distribution]]


def read_runs(path: Path) -> tuple[list[dict], list[dict]]:
    """(run records, registration records) from collect's JSONL."""
    runs, registration = [], []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        (registration if r.get("kind") == "registration" else runs).append(r)
    return runs, registration


def _session_key(r: dict) -> str:
    return f"{r['subject']}_{r['session']}"


def agreement(quantity: str, cloudpipe, fmriprep, note: str = "") -> Agreement:
    a = np.asarray(cloudpipe, dtype=float)
    b = np.asarray(fmriprep, dtype=float)
    return Agreement(
        quantity=quantity,
        cloudpipe=Distribution.of(a),
        fmriprep=Distribution.of(b),
        icc=icc_2_1(a, b, quantity),
        bland_altman=bland_altman(a, b, quantity),
        note=note,
    )


def session_arm_means(runs: list[dict], field: str) -> tuple[list[float], list[float]]:
    """Each session's unweighted mean over its runs, per arm, in matching order."""
    by = defaultdict(lambda: ([], []))
    for r in runs:
        a, b = r.get("cloudpipe", {}).get(field), r.get("fmriprep", {}).get(field)
        if a is None or b is None:
            continue
        by[_session_key(r)][0].append(float(a))
        by[_session_key(r)][1].append(float(b))
    keys = sorted(by)
    return [float(np.mean(by[k][0])) for k in keys], [float(np.mean(by[k][1])) for k in keys]


def session_mean(
    runs: list[dict], field: str, fisher: bool = False, task: str | None = None
) -> list[float]:
    """Each session's mean of a per-run value, on Fisher z for a correlation, back on r."""
    by = defaultdict(list)
    for r in runs:
        if task and r.get("task") != task:
            continue
        v = r.get(field)
        if v is None or not math.isfinite(float(v)):
            continue
        by[_session_key(r)].append(float(v))
    out = []
    for k in sorted(by):
        v = np.asarray(by[k])
        out.append(
            float(np.tanh(np.arctanh(np.clip(v, -_R_CLIP, _R_CLIP)).mean()))
            if fisher
            else float(v.mean())
        )
    return out


MOTION_NOTE = (
    "The pipelines estimate motion differently (ABCD's 3dvolreg parameters against "
    "fMRIPrep's mcflirt re-estimation), so a difference is an estimator difference, "
    "not a data-quality one."
)
DVARS_NOTE = (
    "Not comparable as recorded: fMRIPrep's dvars column is nipype ComputeDVARS, which "
    "first scales the run to an in-mask median of 1000; cloudpipe's is in the input's "
    "own intensity units, which differ about 60-fold between ABCD scanners (both "
    "pipelines' BOLD carries that scale). See mean_dvars_common_scale."
)
DVARS_COMMON_NOTE = (
    "cloudpipe's DVARS x 1000 / its run's mean global signal, against fMRIPrep's as "
    "recorded. The mean global signal stands in for fMRIPrep's in-mask median, so a "
    "small bias between the two is expected and is not a pipeline difference."
)
#: Field name of the DVARS comparison on one intensity scale.
DVARS_COMMON = "mean_dvars_common_scale"


def session_dvars_common_scale(runs: list[dict]) -> tuple[list[float], list[float]]:
    """Each session's mean DVARS on fMRIPrep's scale (x1000 / intensity), per arm.

    fMRIPrep's `dvars` is already on that scale. cloudpipe's is put on it by its
    run's mean global signal, the same intensity summary its record carries. A run
    whose global signal is missing or not positive is left out of both arms.
    """
    by = defaultdict(lambda: ([], []))
    for r in runs:
        c, f = r.get("cloudpipe", {}), r.get("fmriprep", {})
        a, gs, b = c.get("mean_dvars"), c.get("mean_global_signal"), f.get("mean_dvars")
        if a is None or b is None or not gs or gs <= 0:
            continue
        by[_session_key(r)][0].append(1000.0 * float(a) / float(gs))
        by[_session_key(r)][1].append(float(b))
    keys = sorted(by)
    return [float(np.mean(by[k][0])) for k in keys], [float(np.mean(by[k][1])) for k in keys]


def describe(values_path: Path, runs_path: Path) -> Description:
    table = read_table(values_path)
    tsnr = table["tsnr_median"]
    runs, registration = read_runs(runs_path)
    sessions = {_session_key(r) for r in runs}
    if len(sessions) != len(tsnr):
        raise ValueError(
            f"the run records cover {len(sessions)} sessions and the session table {len(tsnr)}"
        )

    motion = []
    for field in MOTION_FIELDS:
        a, b = session_arm_means(runs, field)
        motion.append(agreement(field, a, b, DVARS_NOTE if "dvars" in field else MOTION_NOTE))
    motion.append(agreement(DVARS_COMMON, *session_dvars_common_scale(runs), DVARS_COMMON_NOTE))

    tasks = sorted({r["task"] for r in runs if r.get("task")})
    by_task = {
        field: {t: Distribution.of(session_mean(runs, field, fisher=True, task=t)) for t in tasks}
        for field in TASK_FIELDS
    }
    return Description(
        n_sessions=len(tsnr),
        tsnr_median=agreement(
            "tsnr_median",
            [r.cloudpipe for r in tsnr],
            [r.reference for r in tsnr],
            "Tested in tier 1 as a ratio of geometric means; reported here as agreement only.",
        ),
        motion=motion,
        own_mask_dice=Distribution.of(session_mean(runs, "own_mask_dice")),
        registration={
            t: Distribution.of([r["mean_dice"].get(t) for r in registration]) for t in TARGETS
        },
        by_task=by_task,
    )


def _dist(d: Distribution, digits: int = 4) -> str:
    f = f"{{:.{digits}g}}"
    return (
        f"median {f.format(d.median)} [IQR {f.format(d.p25)}–{f.format(d.p75)}; "
        f"5th–95th {f.format(d.p5)}–{f.format(d.p95)}], mean {f.format(d.mean)} (n={d.n})"
    )


def render(d: Description) -> str:
    lines = [f"Descriptive set, {d.n_sessions} sessions. No margins; nothing here is a verdict."]

    def agree(a: Agreement) -> list[str]:
        ba = a.bland_altman
        out = [
            f"{a.quantity}:",
            f"    cloudpipe {_dist(a.cloudpipe)}",
            f"    fMRIPrep  {_dist(a.fmriprep)}",
            f"    ICC(2,1) {a.icc.icc:.3f} [{a.icc.ci_low:.3f}, {a.icc.ci_high:.3f}]; Bland–Altman "
            f"bias {ba.bias:+.4g} [{ba.bias_ci_low:+.4g}, {ba.bias_ci_high:+.4g}], limits "
            f"{ba.loa_low:+.4g} to {ba.loa_high:+.4g}",
        ]
        if a.note:
            out.append(f"    note: {a.note}")
        return out

    lines += agree(d.tsnr_median)
    for a in d.motion:
        lines += agree(a)
    lines.append(f"own brain-mask Dice: {_dist(d.own_mask_dice)}")
    lines.append("T1w-to-MNI label Dice by target (arm vs engine that segmented the template):")
    for t, dist in d.registration.items():
        lines.append(f"    {t:32s} {_dist(dist)}")
    for field, tasks in d.by_task.items():
        lines.append(f"{field} by task (session means on Fisher z, back on r):")
        for t, dist in tasks.items():
            lines.append(f"    {t:12s} {_dist(dist)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--values", type=Path, required=True)
    p.add_argument("--runs", type=Path, required=True)
    p.add_argument("--json", type=Path)
    args = p.parse_args(argv)
    d = describe(args.values, args.runs)
    print(render(d))
    if args.json:
        args.json.write_text(json.dumps(asdict(d), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
