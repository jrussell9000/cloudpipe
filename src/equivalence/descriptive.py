"""The descriptive measures that need both pipelines' files, not the session table (methods 5.4).

Arrays and file contents in, numbers out; no S3 and no arm layout (the runner,
`run_descriptive`, does that). None of these has a margin or decides anything.

- **aCompCor principal angles**, per tissue. cloudpipe's `a_comp_cor_{wm,csf}_NN`
  against fMRIPrep's `w_comp_cor_NN` (WM) and `c_comp_cor_NN` (CSF), over the
  steady-state frames. Both pipelines keep a variable number of components (the
  50%-of-variance rule), so the comparison is reported twice: over every
  component each retained, and over each one's leading five, the set most often
  regressed out. Never paired by index (`confound_map.principal_angles`).
- **Anatomy from the stats files**: total cortical grey-matter volume, eTIV, mean
  cortical thickness and white-surface area per hemisphere, the 14 subcortical
  volumes, and the DKT regional thickness, area and grey-matter volume. Each
  pipeline's own `aseg.stats` and DKT stats, parsed the same way.
- **Per-structure segmentation Dice**, over the registration endpoint's 76 labels,
  in each session's own T1w space. The two pipelines conform the same T1w around
  different centres, so fMRIPrep's segmentation is mapped onto cloudpipe's grid
  through the two header affines alone, by nearest neighbour, with no
  registration. A session whose conformed images do not agree through those
  affines (intensity r below `GRID_AGREEMENT_FLOOR`) is flagged, not scored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .confound_map import principal_angles
from .registration import (
    DKT_LABELS,
    ENDPOINT_LABELS,
    SUBCORTICAL_LABELS,
    label_dice,
    labels_on_grid,
)

#: (cloudpipe column pattern, fMRIPrep column pattern) per tissue.
COMPCOR_FAMILIES = {
    "wm": (r"^a_comp_cor_wm_(\d+)$", r"^w_comp_cor_(\d+)$"),
    "csf": (r"^a_comp_cor_csf_(\d+)$", r"^c_comp_cor_(\d+)$"),
}
LEADING = 5

#: The conformed T1w images must correlate at least this well once mapped through
#: their affines, or the segmentations are not on one anatomy and are not scored.
GRID_AGREEMENT_FLOOR = 0.95

#: aseg.stats StructName for the 14 subcortical labels, in SUBCORTICAL_LABELS order.
#: FreeSurfer 7 names the thalamus "Thalamus"; older trees say "Thalamus-Proper".
SUBCORTICAL_NAMES = {
    10: "Left-Thalamus", 11: "Left-Caudate", 12: "Left-Putamen", 13: "Left-Pallidum",
    17: "Left-Hippocampus", 18: "Left-Amygdala", 26: "Left-Accumbens-area",
    49: "Right-Thalamus", 50: "Right-Caudate", 51: "Right-Putamen", 52: "Right-Pallidum",
    53: "Right-Hippocampus", 54: "Right-Amygdala", 58: "Right-Accumbens-area",
}  # fmt: skip


# ---------------------------------------------------------------- aCompCor


def _family(columns: list[str], pattern: str) -> list[str]:
    rx = re.compile(pattern)
    found = [(int(m.group(1)), c) for c in columns if (m := rx.match(c))]
    return [c for _, c in sorted(found)]


@dataclass(frozen=True)
class SubspaceComparison:
    n_cloudpipe: int
    n_fmriprep: int
    #: Principal angles in degrees, ascending; min(n_cloudpipe, n_fmriprep) of them.
    angles_deg: list[float]

    @property
    def mean_cosine(self) -> float:
        return float(np.mean(np.cos(np.radians(self.angles_deg)))) if self.angles_deg else 0.0


@dataclass(frozen=True)
class CompcorComparison:
    tissue: str
    all_retained: SubspaceComparison
    leading: SubspaceComparison


def compcor_angles(cloudpipe, fmriprep, n_nss_frames: int) -> list[CompcorComparison]:
    """Per tissue, the principal angles between the two pipelines' aCompCor subspaces.

    `cloudpipe` and `fmriprep` are the runs' confounds tables (pandas DataFrames,
    full length). Frames before `n_nss_frames` are dropped from both.
    """
    if len(cloudpipe) != len(fmriprep):
        raise ValueError(f"confounds tables differ in length, {len(cloudpipe)} and {len(fmriprep)}")
    out = []
    for tissue, (cp_pattern, fp_pattern) in COMPCOR_FAMILIES.items():
        cp_cols = _family(list(cloudpipe.columns), cp_pattern)
        fp_cols = _family(list(fmriprep.columns), fp_pattern)
        if not cp_cols or not fp_cols:
            raise ValueError(f"no {tissue} aCompCor columns in one of the tables")
        a = cloudpipe[cp_cols].to_numpy(dtype=float)[n_nss_frames:]
        b = fmriprep[fp_cols].to_numpy(dtype=float)[n_nss_frames:]
        a, b = np.nan_to_num(a), np.nan_to_num(b)

        def compare(x, y):
            return SubspaceComparison(
                x.shape[1], y.shape[1], [float(v) for v in np.degrees(principal_angles(x, y))]
            )

        out.append(
            CompcorComparison(tissue, compare(a, b), compare(a[:, :LEADING], b[:, :LEADING]))
        )
    return out


# ---------------------------------------------------------------- stats files


@dataclass(frozen=True)
class Stats:
    """One FreeSurfer-format .stats file: its `# Measure` lines and its table."""

    measures: dict[str, float]
    rows: dict[str, dict[str, float]] = field(default_factory=dict)


def read_stats(text: str) -> Stats:
    """Parse `# Measure A, B, desc, value, unit` lines and the `# ColHeaders` table.

    Measures are keyed by their second field (`CortexVol`, `eTIV`,
    `MeanThickness`, `WhiteSurfArea`); table rows by `StructName`.
    """
    measures: dict[str, float] = {}
    headers: list[str] = []
    rows: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        if line.startswith("# Measure"):
            parts = [p.strip() for p in line.removeprefix("# Measure").split(",")]
            if len(parts) >= 4:
                try:
                    measures[parts[1]] = float(parts[3])
                except ValueError:
                    pass
        elif line.startswith("# ColHeaders"):
            headers = line.removeprefix("# ColHeaders").split()
        elif line and not line.startswith("#") and headers:
            cells = line.split()
            if len(cells) != len(headers):
                continue
            row = dict(zip(headers, cells, strict=True))
            name = row.pop("StructName", None)
            if name is None:
                continue
            numeric = {}
            for k, v in row.items():
                try:
                    numeric[k] = float(v)
                except ValueError:
                    pass
            rows[name] = numeric
    return Stats(measures, rows)


def _subcortical(aseg: Stats) -> dict[str, float | None]:
    out = {}
    for name in SUBCORTICAL_NAMES.values():
        row = aseg.rows.get(name) or aseg.rows.get(name.replace("Thalamus", "Thalamus-Proper"))
        out[name] = row.get("Volume_mm3") if row else None
    return out


def anatomy_measures(aseg: Stats, lh_dkt: Stats, rh_dkt: Stats) -> dict:
    """One pipeline's anatomy for one session, on the natural scale (mm, mm², mm³)."""
    regional = {}
    for hemi, stats in (("lh", lh_dkt), ("rh", rh_dkt)):
        for name, row in stats.rows.items():
            regional[f"{hemi}.{name}"] = {
                k: row.get(k) for k in ("ThickAvg", "SurfArea", "GrayVol")
            }
    return {
        "cortex_volume": aseg.measures.get("CortexVol"),
        "etiv": aseg.measures.get("eTIV"),
        "mean_thickness": {"lh": lh_dkt.measures.get("MeanThickness"), "rh": rh_dkt.measures.get("MeanThickness")},
        "white_surface_area": {"lh": lh_dkt.measures.get("WhiteSurfArea"), "rh": rh_dkt.measures.get("WhiteSurfArea")},
        "subcortical_volume": _subcortical(aseg),
        "dkt": regional,
    }  # fmt: skip


def read_stats_file(path: Path) -> Stats:
    return read_stats(Path(path).read_text())


# ---------------------------------------------------------------- segmentation Dice


def _onto(src, ref, order: int) -> np.ndarray:
    """`src`'s data on `ref`'s grid through the two affines, by spline of `order`."""
    from scipy import ndimage

    m = np.linalg.inv(src.affine) @ ref.affine
    return ndimage.affine_transform(
        np.asarray(src.dataobj, dtype=np.float64), m[:3, :3], offset=m[:3, 3],
        output_shape=ref.shape[:3], order=order,
    )  # fmt: skip


@dataclass(frozen=True)
class SegmentationDice:
    grid_agreement: float
    scored: bool
    mean: float | None = None
    mean_subcortical: float | None = None
    mean_dkt: float | None = None
    per_label: dict[int, float] = field(default_factory=dict)
    missing: tuple[int, ...] = ()


def segmentation_dice(
    cloudpipe_orig, fmriprep_orig, cloudpipe_seg, fmriprep_seg
) -> SegmentationDice:
    """Per-label Dice of the two segmentations of one session, on cloudpipe's grid.

    All four arguments are nibabel images. The guard first: fMRIPrep's conformed
    T1w mapped onto cloudpipe's must correlate with it at GRID_AGREEMENT_FLOOR or
    better, or the two are not one anatomy in one world and nothing is scored.
    """
    a = np.asarray(cloudpipe_orig.dataobj, dtype=np.float64)
    b = _onto(fmriprep_orig, cloudpipe_orig, order=1)
    brain = (a > 0) | (b > 0)
    agreement = float(np.corrcoef(a[brain], b[brain])[0, 1])
    if agreement < GRID_AGREEMENT_FLOOR:
        return SegmentationDice(agreement, scored=False)
    seg_a = np.asarray(cloudpipe_seg.dataobj).astype(np.int32)
    seg_b = labels_on_grid(fmriprep_seg, cloudpipe_seg)
    dice = label_dice(seg_a, seg_b, ENDPOINT_LABELS)
    sub = [dice.per_label[k] for k in SUBCORTICAL_LABELS]
    dkt = [dice.per_label[k] for k in DKT_LABELS]
    return SegmentationDice(
        grid_agreement=agreement,
        scored=True,
        mean=dice.mean,
        mean_subcortical=float(np.mean(sub)),
        mean_dkt=float(np.mean(dkt)),
        per_label=dice.per_label,
        missing=dice.missing,
    )
