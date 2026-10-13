"""Calibration against ABCD's tabulated motion. Not an endpoint.

cloudpipe does not estimate head motion. It reads the `3dvolreg` parameters ABCD
ships beside each BOLD run (`preproc.py --motion-file`, staged from
`mmps_mproc/{subj}/{ses}/func/{prefix}_motion.tsv`) and derives `mean_fd`,
`max_fd` and the FD threshold counts from them. DAIC's tabulated motion comes from
the same parameters, so the two should agree to within a definition, and a
disagreement means cloudpipe is reading or summarising their data differently than
they do.

That makes this a sibling of the D2 calibration gate rather than a comparison of
pipelines. It carries no claim about either pipeline's quality, and the study makes
none: DAIC is the data provider, their tabulated measures are what the field's ABCD
papers use, and the derived-measure comparison that would contest them is a
separate pre-registered change (D14).

Verified against ABCD release v7 on 2026-10-09, reading each column's definition in
`mr_y_qc__mot.json` rather than inferring it from the name. Four structural facts
that a guessed mapping would have got wrong:

- The table is `mr_y_qc__mot`, one row per `(participant_id, session_id)`, 134
  columns. There is **no run column**: run identity is encoded in the column name.
- Run-level motion exists for **task** fMRI only — `__{task}__r01__`, `__r02__` for
  `mid`, `nback`, `sst`. Resting-state motion is **session-pooled**, with no
  per-run column at all, so a per-run rest comparison is not available.
- `participant_id` is `sub-XXXXXXXX` and `session_id` is `ses-00A` / `ses-02A` /
  `ses-04A` / `ses-06A`, both matching cloudpipe's own spelling. The join needs no
  identifier translation.
- `mot_mean` is exactly `trans_mean + rotat_mean` (verified: ratio 1.0000 over 70
  runs), so ABCD's FD is the Power-style sum of translation and
  rotation-converted-to-displacement — the same definition family cloudpipe uses.

cloudpipe processes `task-rest` and `task-nback`, so `nback` is the only task whose
runs can be calibrated per run here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

#: The table holding every motion summary, and its grain.
MOTION_TABLE = "mr_y_qc__mot"
MOTION_GRAIN = ("participant_id", "session_id")

#: Tasks whose motion ABCD tabulates per run. Rest is pooled across runs.
PER_RUN_TASKS = ("mid", "nback", "sst")

#: Agreement this tight is what "the same motion parameters" should produce. A
#: calibration tolerance, not an equivalence margin: there is no hypothesis here
#: about two pipelines differing.
DEFAULT_TOLERANCE = 1e-3


class UnverifiedMapping(RuntimeError):
    """A pair whose ABCD column has not been confirmed against the data dictionary."""


class RefusedComparison(ValueError):
    """A pair that looks comparable by name and is not the same quantity."""


@dataclass(frozen=True)
class TabulatedPair:
    """One reviewed correspondence between a `FuncQC` field and an ABCD column.

    `column_suffix` is appended to `mr_y_qc__mot__tfmri__{task}__r{NN}__` for a
    per-run task column.
    """

    funcqc_field: str
    column_suffix: str | None
    relationship: str
    verified: bool = False

    def confirm(self, column_suffix: str) -> TabulatedPair:
        """Return this pair with its column confirmed against the data dictionary."""
        return replace(self, column_suffix=column_suffix, verified=True)


#: The reviewed table, for per-run task columns.
PAIRS: tuple[TabulatedPair, ...] = (
    TabulatedPair(
        funcqc_field="n_frames",
        column_suffix="frame_count",
        verified=True,
        relationship=(
            "'Number of frames in acquisition'. Matches cloudpipe's n_frames exactly — "
            "70/70 runs, zero difference — which is what establishes that the join is "
            "right before any disagreeing field is interpreted. NOT vol_count, which is "
            "the count after ABCD removes dummy frames; see the refusal list."
        ),
    ),
    TabulatedPair(
        funcqc_field="tr_seconds",
        column_suffix="_tr",
        verified=True,
        relationship=(
            "'Motion QC - Repetition time'. Exact in principle, so a mismatch means a "
            "header problem. Note the single underscore: this column is "
            "`__{task}__r{NN}_tr`, not `__r{NN}__tr`."
        ),
    ),
)

#: Pairs that name-matching would join and that are not the same quantity.
#: Each entry is `(FuncQC field, ABCD column suffix) -> why not`.
REFUSALS: dict[tuple[str, str], str] = {
    ("mean_fd", "mot_mean"): (
        "DIAGNOSED, and not a vocabulary mismatch: cloudpipe differences the RAW motion "
        "estimates while ABCD's tabulated FD is computed from respiration-filtered "
        "ones.\n"
        "The definitions agree — ABCD's mot_mean is exactly trans_mean + rotat_mean — "
        "yet over 70 nback runs cloudpipe's mean_fd ran 1.03x to 3.99x theirs, median "
        "1.47x. At TR = 0.8 s respiration (~0.3-0.5 Hz in this age range) sits just "
        "under Nyquist and aliases into the motion traces as large frame-to-frame "
        "apparent motion — respiratory pseudomotion (Fair et al. 2020, Power et al. "
        "2019). Differencing the raw estimates captures it; filtering it out first does "
        "not. That also explains why the ratio varies per subject: it tracks their "
        "respiration amplitude, not any constant.\n"
        "Evidence, on four runs with their motion TSVs: applying a notch in the "
        "paediatric respiratory band reproduces ABCD's published mot_mean to within "
        "0.0016 mm, at centre frequencies of 0.305-0.420 Hz. Ruled out along the way — "
        "the frame set (dropping the non-steady-state block moves mean_fd 0.3778 -> "
        "0.3706, about 2%, and leaves max_fd identical), a global scale (the ratio is "
        "not constant), spike concentration (the top 10 frames hold 5.4% of total "
        "displacement) and drift (per-50-frame means are flat across the run). An "
        "earlier least-squares fit suggested 1.26*trans + 1.06*rotat; those "
        "coefficients were a collinearity artifact and mean nothing.\n"
        "ABCD's exact filter is not established here — only that a respiratory-band "
        "notch reproduces their numbers — so this pair stays refused rather than "
        "mapped through a filter this repository guessed. Recorded in issue #753, CLOSED "
        "as no-change: motion filtering is a post-processing step in this ecosystem "
        "(XCP-D's --motion-filter-type, required in its abcd mode), so preproc.py keeps "
        "emitting the raw estimates exactly as fMRIPrep does, and the downstream layer is "
        "not being changed either. The "
        "consequence is cloudpipe's "
        "own, independent of any comparison: FuncQC's FD fields and every FD threshold "
        "count on them overstate head motion by 2-4x against field convention."
    ),
    ("max_fd", "mot_max"): (
        "Same cause as mean_fd: median 4.25 against 2.69 over the same 70 runs, worst "
        "run 60.5 against 39.8. Not the frame set — on the run checked, the largest FD "
        "sits at frame 133, mid-run, and dropping the non-steady-state block leaves "
        "max_fd identical. A maximum is more exposed to pseudomotion than a mean, which "
        "is why its ratio is the larger of the two."
    ),
    ("n_frames", "vol_count"): (
        "'Number of frames after removing dummy frames'. cloudpipe's n_frames is the "
        "acquired length, so this would read as a systematic shortfall. frame_count is "
        "the comparable column."
    ),
    ("n_frames", "vol__censor_count"): (
        "Frames surviving ABCD's censoring (FD < 0.2 mm, 5 contiguous). cloudpipe "
        "censors nothing, so this is a different quantity again."
    ),
    ("mean_fd", "mot__censor_mean"): (
        "Mean FD over frames remaining after ABCD's censoring. cloudpipe censors "
        "nothing, so this compares a censored mean against an uncensored one."
    ),
}


def pair_for(funcqc_field: str, pairs: tuple[TabulatedPair, ...] = PAIRS) -> TabulatedPair:
    """The reviewed pair for a `FuncQC` field, or a raise."""
    for pair in pairs:
        if pair.funcqc_field == funcqc_field:
            return pair
    raise KeyError(
        f"{funcqc_field!r} is not in the reviewed table "
        f"({[p.funcqc_field for p in pairs]}). Add a reviewed row, or see REFUSALS; "
        "do not match on names."
    )


def refuse(funcqc_field: str, column_suffix: str) -> None:
    """Raise where this pairing is on the refusal list."""
    reason = REFUSALS.get((funcqc_field, column_suffix))
    if reason is not None:
        raise RefusedComparison(
            f"refusing to compare cloudpipe {funcqc_field!r} against ABCD's "
            f"{column_suffix!r}: {reason}"
        )


def require_verified(pair: TabulatedPair) -> str:
    """The pair's column suffix, or a raise if it was never confirmed."""
    if not pair.verified or pair.column_suffix is None:
        raise UnverifiedMapping(
            f"{pair.funcqc_field}: no ABCD column confirmed for this field. Read the "
            "release's data dictionary, check the definition rather than the name, then "
            "record it with TabulatedPair.confirm(). A guessed column name matched "
            "silently is the failure this check exists to prevent."
        )
    return pair.column_suffix


def motion_column(task: str, run: int | str, column_suffix: str) -> str:
    """The per-run task column for one motion quantity.

    Raises for rest, which ABCD tabulates only pooled across runs — the one case
    where a caller is likeliest to assume a column exists because the task does.
    """
    task = str(task).removeprefix("task-")
    if task == "rest":
        raise RefusedComparison(
            "ABCD tabulates resting-state motion pooled across runs, with no per-run "
            "column, so a per-run rest comparison is not available. Either compare at "
            "session grain against mr_y_qc__mot__rsfmri__mot_mean — stating the pooling "
            "cloudpipe applies, since ABCD's own weighting is not documented in the "
            "dictionary — or restrict the calibration to the task runs."
        )
    if task not in PER_RUN_TASKS:
        raise KeyError(f"{task!r} has no per-run motion columns; known: {PER_RUN_TASKS}")
    number = int(str(run).removeprefix("run-"))
    separator = "" if column_suffix.startswith("_") else "__"
    return f"mr_y_qc__mot__tfmri__{task}__r{number:02d}{separator}{column_suffix}"


@dataclass(frozen=True)
class MotionCalibration:
    """How well cloudpipe's motion summary reproduces ABCD's, for one field."""

    funcqc_field: str
    tabulated_column: str
    n_compared: int
    max_absolute_difference: float
    median_absolute_difference: float
    n_beyond_tolerance: int
    tolerance: float

    @property
    def agrees(self) -> bool:
        return self.n_compared > 0 and self.n_beyond_tolerance == 0

    def __str__(self) -> str:
        verdict = "OK" if self.agrees else "MISMATCH"
        return (
            f"{verdict}  {self.funcqc_field} vs {self.tabulated_column}: "
            f"n={self.n_compared}, max |Δ| {self.max_absolute_difference:.3g}, "
            f"median |Δ| {self.median_absolute_difference:.3g}, "
            f"{self.n_beyond_tolerance} beyond ±{self.tolerance:g}"
        )


def calibrate_motion(
    cloudpipe_values,
    tabulated_values,
    pair: TabulatedPair,
    tolerance: float = DEFAULT_TOLERANCE,
    column: str = "",
) -> MotionCalibration:
    """Compare one motion field, paired per run, against ABCD's tabulated column.

    Both sequences must be in the same run order. Pairs where either side is
    missing are dropped and excluded from `n_compared`, so a partial join reads as
    a smaller comparison rather than as agreement.
    """
    suffix = require_verified(pair)

    ours = np.asarray(cloudpipe_values, dtype=np.float64)
    theirs = np.asarray(tabulated_values, dtype=np.float64)
    if ours.shape != theirs.shape:
        raise ValueError(f"paired values differ in length, {ours.size} vs {theirs.size}")

    both = np.isfinite(ours) & np.isfinite(theirs)
    difference = np.abs(ours[both] - theirs[both])

    return MotionCalibration(
        funcqc_field=pair.funcqc_field,
        tabulated_column=column or suffix,
        n_compared=int(both.sum()),
        max_absolute_difference=float(difference.max()) if difference.size else 0.0,
        median_absolute_difference=float(np.median(difference)) if difference.size else 0.0,
        n_beyond_tolerance=int((difference > tolerance).sum()),
        tolerance=tolerance,
    )


def tabulated_path_is_present(path: Path) -> bool:
    """Is the tabulated release available locally?

    Nothing in this repository ships it: the ABCD tabulated tables are an NDA
    release download governed by the DUA, and cloudpipe is otherwise independent of
    ABCD tooling. A read-only TSV is a mild exception to that and is stated as one.
    """
    return Path(path).is_file()
