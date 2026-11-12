"""The confounds TSV's `csf` and `white_matter` columns: mean signal in FastSurfer's masks.

Each column is the per-frame mean of a run's MNI BOLD, NSS frames included, within the
session's 1 mm `space-MNI152NLin2009cAsym_res-1_label-{CSF,WM}_mask`. The mask is taken
to the BOLD's 2 mm grid by nearest neighbour and intersected with the run's brain mask.
That is how `fmri-first-level-proc` builds its rest regressors (fork `4.0.0+cp.2`,
first-level-direct-inputs D17, after Taylor's orchestrator):

    3dresample -master <brainmask> -rmode NN -input <label>
    3dcalc -a <resampled> -b <brainmask> -expr 'step(a)*step(b)'
    3dmaskave -quiet -mask <that> <trimmed BOLD>

so the columns, less the first n_remove rows, equal first-level's regressors to
3dmaskave's printed precision (6 significant digits). add-tissue-confound-columns D2-D5.

**Nearest neighbour is AFNI's, reproduced exactly.** On these grids every 2 mm voxel
centre lies exactly between eight 1 mm voxel centres, so nearest neighbour is a tie
everywhere, and the tie-breaking decides the mask. AFNI 26.3.07 (first-level's version)
picks a separable index per axis, measured on synthetic volumes
(tests/images/shared/fixtures/afni_nn_lookup.json, made by
tests/images/shared/make_afni_nn_fixture.py): 2 mm index t takes 1 mm index 192 - 2t on
x, 228 - 2t on y and 2t on z, except z slice 0, which lies outside the 1 mm grid. A
general-purpose resampler breaks the ties differently. Any other grid is refused rather
than resampled some other way.

**Both writers produce the same bytes.** The func session pod (images/afni/
tissue_confounds.py) and the backfill pod (images/afni/tissue_confounds_backfill.py)
both call compute() and add_columns() on the same published files. Means accumulate
in float64 over the float32 voxels, and each value is written as the shortest text that
round-trips its float32 value, so the text does not depend on the CPU.

An unusable mask never fails a run (D5): a column with no voxel left is n/a on every
frame, and the status says why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import nibabel as nb
import numpy as np

SPACE = "space-MNI152NLin2009cAsym"
#: (column, the label's tissue name), in the TSV's order: fMRIPrep's names and order.
COLUMNS = (("csf", "CSF"), ("white_matter", "WM"))
#: The column the two follow.
AFTER = "global_signal"
NA = "n/a"

# Status values, recorded in FuncQC's `tissue_confounds` (schema 1.5).
OK = "ok"
NO_MAPS = "no-tissue-maps"
EMPTY = "empty-mask"
ERROR = "error"
NOT_COMPUTED = "not-computed"
STATUSES = (OK, NO_MAPS, EMPTY, ERROR, NOT_COMPUTED)

#: The 1 mm grid of the session's tissue maps (MNI152NLin2009cAsym_res-01_T1w_brain).
LABEL_SHAPE = (193, 229, 193)
LABEL_AFFINE = np.array(
    [[1, 0, 0, -96], [0, 1, 0, -132], [0, 0, 1, -78], [0, 0, 0, 1]], dtype=float
)
#: The 2 mm grid of every MNI BOLD and brain mask (MNI152NLin2009cAsym_T1w_brain_res-2_RAI).
BOLD_SHAPE = (97, 115, 97)
BOLD_AFFINE = np.array(
    [[-2, 0, 0, 95.5], [0, -2, 0, 95.5], [0, 0, 2, -78.5], [0, 0, 0, 1]], dtype=float
)
_ATOL = 1e-4


def afni_nn_indices() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The 1 mm index AFNI's `3dresample -rmode NN` reads for each 2 mm index, per axis.

    -1 means outside the 1 mm grid (the voxel reads 0).
    """
    x = 192 - 2 * np.arange(BOLD_SHAPE[0])
    y = 228 - 2 * np.arange(BOLD_SHAPE[1])
    z = 2 * np.arange(BOLD_SHAPE[2])
    z[0] = -1
    return x, y, z


class GridError(ValueError):
    """A volume is not on the grid this module was validated on."""


def _check_grid(img, shape: tuple, affine: np.ndarray, what: str) -> None:
    if tuple(img.shape[:3]) != shape or not np.allclose(img.affine, affine, atol=_ATOL):
        raise GridError(
            f"{what}: shape {tuple(img.shape[:3])}, affine {np.round(img.affine, 4).tolist()}; "
            f"expected {shape} and {affine.tolist()}"
        )


def resample_label(label: np.ndarray) -> np.ndarray:
    """A 1 mm label volume on LABEL's grid -> a boolean 2 mm mask, as AFNI's NN does."""
    if label.shape[:3] != LABEL_SHAPE:
        raise GridError(f"label shape {label.shape}, expected {LABEL_SHAPE}")
    x, y, z = afni_nn_indices()
    picked = label[np.ix_(np.clip(x, 0, None), np.clip(y, 0, None), np.clip(z, 0, None))] > 0
    picked[:, :, z < 0] = False
    picked[x < 0] = False
    picked[:, y < 0] = False
    return picked


def load_label(path: Path) -> np.ndarray:
    """A session's 1 mm tissue mask, resampled to the BOLD grid (boolean)."""
    img = nb.load(path)
    _check_grid(img, LABEL_SHAPE, LABEL_AFFINE, path.name)
    return resample_label(np.asanyarray(img.dataobj))


def load_brainmask(path: Path) -> np.ndarray:
    img = nb.load(path)
    _check_grid(img, BOLD_SHAPE, BOLD_AFFINE, path.name)
    return np.asanyarray(img.dataobj) > 0


def format_value(value: float) -> str:
    """The shortest text that round-trips the value as float32; n/a if not finite."""
    v = np.float32(value)
    return str(v) if np.isfinite(v) else NA


@dataclass
class RunColumns:
    """One run's two columns and what FuncQC records about them."""

    n_frames: int
    fields: dict[str, list[str]] = field(default_factory=dict)  # column -> one field per frame
    voxels: dict[str, int] = field(default_factory=dict)  # column -> mask voxels (0 = empty)
    status: str = NOT_COMPUTED
    error: str = ""

    def qc_fields(self) -> dict:
        """The three FuncQC 1.5 fields."""
        return {
            "wm_mask_voxels": int(self.voxels.get("white_matter", 0)),
            "csf_mask_voxels": int(self.voxels.get("csf", 0)),
            "tissue_confounds": self.status,
        }


def unavailable(n_frames: int, status: str, error: str = "") -> RunColumns:
    """Both columns n/a, for a session without maps or a failed computation."""
    return RunColumns(
        n_frames=n_frames,
        fields={c: [NA] * n_frames for c, _ in COLUMNS},
        voxels={c: 0 for c, _ in COLUMNS},
        status=status,
        error=error,
    )


def compute(bold: Path, brainmask: Path, labels: dict[str, np.ndarray] | None) -> RunColumns:
    """The run's columns. `labels` maps each column to its resampled label mask
    (load_label), or is None when the session has no tissue maps.

    Raises GridError or an I/O error; callers record those as `error`.
    """
    img = nb.load(bold)
    _check_grid(img, BOLD_SHAPE, BOLD_AFFINE, bold.name)
    n_frames = int(img.shape[3])
    if labels is None:
        return unavailable(n_frames, NO_MAPS)
    brain = load_brainmask(brainmask)
    masks = {c: labels[c] & brain for c, _ in COLUMNS}
    data = np.asarray(img.dataobj, dtype=np.float32)  # (x, y, z, t)
    out = RunColumns(n_frames=n_frames, status=OK)
    for column, _ in COLUMNS:
        mask = masks[column]
        out.voxels[column] = int(mask.sum())
        if not mask.any():
            out.fields[column] = [NA] * n_frames
            out.status = EMPTY
            continue
        means = data[mask].mean(axis=0, dtype=np.float64)  # (t,)
        out.fields[column] = [format_value(m) for m in means]
    return out


# ---------------------------------------------------------------------------
# The TSV text
# ---------------------------------------------------------------------------


class ConfoundsFormatError(ValueError):
    """The text is not a confounds TSV the columns can go into."""


def _table(text: str) -> tuple[list[list[str]], bool]:
    if not text:
        raise ConfoundsFormatError("empty TSV")
    trailing = text.endswith("\n")
    rows = [line.split("\t") for line in (text[:-1] if trailing else text).split("\n")]
    width = len(rows[0])
    ragged = [i for i, row in enumerate(rows) if len(row) != width]
    if ragged:
        raise ConfoundsFormatError(f"row {ragged[0]} has a different field count than the header")
    return rows, trailing


def _text(rows: list[list[str]], trailing: bool) -> str:
    return "\n".join("\t".join(row) for row in rows) + ("\n" if trailing else "")


def n_frames(text: str) -> int:
    rows, _ = _table(text)
    return len(rows) - 1


def add_columns(text: str, cols: RunColumns) -> str:
    """The TSV with `csf` and `white_matter` inserted right after `global_signal`.

    Raises ConfoundsFormatError if either column is already there, `global_signal` is
    missing, a row is ragged, or the frame counts disagree.
    """
    rows, trailing = _table(text)
    header = rows[0]
    if AFTER not in header:
        raise ConfoundsFormatError(f"no {AFTER} column")
    present = [c for c, _ in COLUMNS if c in header]
    if present:
        raise ConfoundsFormatError(f"{'/'.join(present)} already a column")
    if len(rows) - 1 != cols.n_frames:
        raise ConfoundsFormatError(f"TSV has {len(rows) - 1} frames, the BOLD {cols.n_frames}")
    at = header.index(AFTER) + 1
    for offset, (column, _) in enumerate(COLUMNS):
        header.insert(at + offset, column)
        for row, value in zip(rows[1:], cols.fields[column], strict=True):
            row.insert(at + offset, value)
    return _text(rows, trailing)


def remove_columns(text: str) -> str:
    """The TSV without the two columns: the exact bytes add_columns() was given."""
    rows, trailing = _table(text)
    names = {c for c, _ in COLUMNS}
    keep = [i for i, name in enumerate(rows[0]) if name not in names]
    return _text([[row[i] for i in keep] for row in rows], trailing)


def state(text: str) -> str:
    """`absent` (neither column), `present` (both, right after global_signal, in order),
    or `misplaced` (anything else)."""
    rows, _ = _table(text)
    header = rows[0]
    if AFTER not in header:
        raise ConfoundsFormatError(f"no {AFTER} column")
    found = [c for c, _ in COLUMNS if c in header]
    if not found:
        return "absent"
    at = header.index(AFTER) + 1
    if header[at : at + len(COLUMNS)] == [c for c, _ in COLUMNS]:
        return "present"
    return "misplaced"


def column_fields(text: str) -> dict[str, list[str]]:
    """The two columns' fields as written (state() must be `present`)."""
    rows, _ = _table(text)
    return {c: [r[rows[0].index(c)] for r in rows[1:]] for c, _ in COLUMNS}


def status_of(text: str) -> str:
    """The status the written columns imply: ok, empty-mask (one column n/a), or
    unavailable (both n/a: no maps, or an error)."""
    fields = column_fields(text)
    na = {c: all(v == NA for v in vals) for c, vals in fields.items()}
    if not any(na.values()):
        return OK
    if all(na.values()):
        return "unavailable"
    return EMPTY
