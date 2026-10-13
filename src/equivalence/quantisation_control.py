"""The quantisation positive control: bound cloudpipe's own storage floor.

cloudpipe does not store its MNI BOLD at full precision, so tier-1 agreement has
a ceiling that has nothing to do with registration. This control measures it, so
a margin is never set tighter than the floor cloudpipe can meet against itself.

**The mechanism changed on 2026-10-06 and so did this control.** Until then the
warp wrote float16 bytes mislabelled `DT_UINT16`, and the floor was float16's
~3.3 significant digits (see
`docs-internal/investigations/2026-10-06-mni-bold-float16-corruption-handoff.md`).
`#629` replaced that with scaled int16: `apply_transforms` takes
`slope = max|value| / 32767` over the whole warped volume, stores
`rint(value / slope)` clipped to ±32767, and declares the slope in
`scl_slope`. Stage 5 then passes `-datum float`, so the published file is float32
carrying the slope-applied values.

That swaps *relative* precision for *absolute* precision, and the difference is
the thing worth measuring:

- float16 gave every voxel ~3.3 significant digits, so the error scaled with the value;
- int16-with-a-slope gives the whole run **one** step, and that step is set by the single largest absolute value anywhere in the volume — including outside the brain, because Stage 3 deliberately writes the BOLD unmasked.

So a bright non-brain voxel inflates the step for every brain voxel. This control
reports the step, where the extreme sits, and the step as a fraction of a typical
in-mask intensity, because that ratio is what a tier-1 margin has to clear.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import nibabel as nib
import numpy as np

from .endpoints import Tier1Result, tier1

#: The writer's symmetric int16 range. `apply_transforms` clips to ±32767 rather
#: than ±32768, because the frame holding max|value| lands on ±32767 up to
#: float32 rounding and can otherwise tip over and wrap.
WRITER_INT16_MAX = 32767


def writer_slope(data: np.ndarray) -> float:
    """The `scl_slope` the production writer would choose for this volume.

    Mirrors `apply_transforms`: the max is taken over the **whole** array, masked
    or not, because Stage 3 writes the BOLD unmasked so that interpolation never
    meets the native-space brain boundary.
    """
    max_abs = float(np.abs(np.asarray(data, dtype=np.float32)).max())
    return max_abs / float(WRITER_INT16_MAX) if max_abs > 0 else 1.0


def quantise_like_writer(data: np.ndarray) -> tuple[np.ndarray, float]:
    """Round-trip `data` through the production writer's int16 quantisation.

    Returns the slope-applied values a reader gets back, and the slope. Mirrors
    `apply_transforms` step for step — `rint`, the ±32767 clip, then the reader's
    multiply — and `test_quantisation_control.py` checks that mirror against the
    real writer rather than trusting it.
    """
    values = np.asarray(data, dtype=np.float32)
    slope = writer_slope(values)
    codes = np.rint(values / np.float32(slope))
    np.clip(codes, -WRITER_INT16_MAX, WRITER_INT16_MAX, out=codes)
    return (codes.astype(np.int16).astype(np.float32) * np.float32(slope)), slope


def finest_spacing(values: np.ndarray) -> float | None:
    """The smallest gap between distinct values, or None if there are too few.

    For data stored on a lattice this *is* the lattice step. For full-precision
    float32 data it is a float32 ULP somewhere in the distribution, which is
    orders of magnitude finer — that difference is the whole test below.
    """
    distinct = np.unique(np.asarray(values, dtype=np.float64).ravel())
    if distinct.size < 64:
        return None
    gaps = np.diff(distinct)
    gaps = gaps[gaps > 0]
    return float(gaps.min()) if gaps.size else None


def already_quantised(values: np.ndarray) -> tuple[bool, float | None, float]:
    """Do these values already carry the writer's int16 scaling?

    The published derivative does: Stage 5's `-datum float` keeps float32 storage
    while the values stay slope-applied int16 codes, so the dtype proves nothing
    and the distribution is the only evidence available.

    The signature is the **step count**. Scaling to int16 puts at most
    `WRITER_INT16_MAX` steps between zero and the extreme, so
    `max|value| / finest_spacing` lands at or below ~32767. Full-precision
    float32 data has ULP-scale gaps and a count orders of magnitude higher — the
    identity warp in the tests gives 5.0e7 against 3.3e4.

    Deliberately a one-sided bound. A masked subset's own extreme can be smaller
    than the run's, which lowers the count but never raises it, so requiring the
    count to sit *near* 32767 would miss exactly the case that matters. Anything
    at or below the bound is stored too coarsely to quantise again, whether this
    writer did it or something else.

    Returns `(flagged, spacing, step_count)`; `step_count` is `inf` when there
    are too few distinct values to judge, which is reported rather than guessed.
    """
    spacing = finest_spacing(values)
    if spacing is None or spacing <= 0:
        return False, None, float("inf")
    count = float(np.abs(np.asarray(values, dtype=np.float64)).max()) / spacing
    return count <= 1.05 * WRITER_INT16_MAX, spacing, count


@dataclass(frozen=True)
class QuantisationFloor:
    """The tier-1 agreement one run reaches against a quantised copy of itself."""

    source: str
    n_frames: int
    slope: float
    max_abs_overall: float
    max_abs_in_mask: float
    extreme_is_in_mask: bool
    median_in_mask_intensity: float
    step_as_pct_of_median: float
    mean_image_correlation: float
    tsnr_map_correlation: float
    tsnr_median_absolute_difference: float
    max_absolute_voxel_difference: float

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "n_frames": self.n_frames,
            "slope": self.slope,
            "max_abs_overall": self.max_abs_overall,
            "max_abs_in_mask": self.max_abs_in_mask,
            "extreme_is_in_mask": self.extreme_is_in_mask,
            "median_in_mask_intensity": self.median_in_mask_intensity,
            "step_as_pct_of_median": self.step_as_pct_of_median,
            "mean_image_correlation": self.mean_image_correlation,
            "tsnr_map_correlation": self.tsnr_map_correlation,
            "tsnr_median_absolute_difference": self.tsnr_median_absolute_difference,
            "max_absolute_voxel_difference": self.max_absolute_voxel_difference,
        }

    def __str__(self) -> str:
        where = "inside the brain mask" if self.extreme_is_in_mask else "OUTSIDE the brain mask"
        return (
            f"quantisation floor from {self.source} ({self.n_frames} frames):\n"
            f"  scl_slope         {self.slope:.6g}  (set by max|value| "
            f"{self.max_abs_overall:.4g}, {where})\n"
            f"  step vs median    {self.step_as_pct_of_median:.4f}% of "
            f"{self.median_in_mask_intensity:.4g}\n"
            f"  mean-image r      {self.mean_image_correlation:.6f}\n"
            f"  tSNR-map r        {self.tsnr_map_correlation:.6f}\n"
            f"  |Δ tsnr_median|   {self.tsnr_median_absolute_difference:.4f}\n"
            f"  max |Δ voxel|     {self.max_absolute_voxel_difference:.4g}"
        )


def quantisation_floor(bold, mask, source: str = "") -> tuple[QuantisationFloor, Tier1Result]:
    """Tier-1 agreement between a full-precision BOLD and its quantised self.

    `bold` must be the warp's float32 output, before the writer touched it — a
    published derivative is already quantised, and running this on one measures a
    *second* quantisation rather than the floor. That was the exact trap the
    float16 version of this control guarded by dtype; dtype no longer tells you,
    so the guard is a lattice test.
    """
    image = bold if hasattr(bold, "get_fdata") else nib.load(bold)
    mask_image = mask if hasattr(mask, "get_fdata") else nib.load(mask)

    data = np.asanyarray(image.dataobj).astype(np.float32)
    binary = np.asanyarray(mask_image.dataobj) > 0

    in_mask = data[binary]
    quantised, spacing, count = already_quantised(in_mask)
    if quantised:
        raise ValueError(
            f"{source or bold}: these values already sit on a lattice of spacing "
            f"{spacing:.6g}, only {count:.0f} steps up to max|value| — at or below the "
            f"{WRITER_INT16_MAX} an int16 scaling allows. This is a published derivative, "
            "not the warp's float32 output, so quantising it again would measure a second "
            "round trip rather than the floor. Take the array before the writer, or a run "
            "whose Stage 3 wrote float32."
        )

    quantised_data, slope = quantise_like_writer(data)
    result = tier1(
        image,
        mask_image,
        nib.Nifti1Image(quantised_data, image.affine, image.header),
        mask_image,
    )

    max_abs_overall = float(np.abs(data).max())
    max_abs_in_mask = float(np.abs(in_mask).max()) if in_mask.size else 0.0
    median_in_mask = float(np.median(np.abs(in_mask))) if in_mask.size else 0.0
    absolute = np.abs(data[binary] - quantised_data[binary])

    floor = QuantisationFloor(
        source=source or str(bold),
        n_frames=int(image.shape[-1]),
        slope=slope,
        max_abs_overall=max_abs_overall,
        max_abs_in_mask=max_abs_in_mask,
        # The slope is set by the whole unmasked volume, so an extreme outside the
        # brain raises the floor for every voxel inside it. That is a finding, not
        # a detail: it is fixable by clipping the warp's output and it is not
        # fixable by any margin.
        extreme_is_in_mask=bool(max_abs_in_mask >= max_abs_overall - slope),
        median_in_mask_intensity=median_in_mask,
        step_as_pct_of_median=(100.0 * slope / median_in_mask) if median_in_mask else float("inf"),
        mean_image_correlation=result.mean_image.correlation,
        tsnr_map_correlation=result.tsnr_map.correlation,
        tsnr_median_absolute_difference=result.tsnr_median_absolute_difference,
        max_absolute_voxel_difference=float(absolute.max()) if absolute.size else 0.0,
    )
    return floor, result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bold", type=Path, help="the warp's float32 MNI BOLD, pre-writer")
    parser.add_argument("mask", type=Path, help="its MNI brain mask")
    parser.add_argument("--json", type=Path, default=None, help="write the figures here as well")
    args = parser.parse_args(argv)

    floor, _ = quantisation_floor(args.bold, args.mask, source=str(args.bold))
    print(floor)
    if not floor.extreme_is_in_mask:
        print(
            "\nNOTE: the slope is set by a voxel outside the brain mask, so the floor "
            "inside the brain is worse than it needs to be.",
        )
    if args.json:
        args.json.write_text(json.dumps(floor.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
