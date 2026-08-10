"""Is low ribbon coverage FOV clipping, or a bad transform?

`surf_*_coverage_frac` in the surface QC drops for two unrelated reasons, and
the number alone cannot tell them apart:

  * **FOV clipping** — a geometric miss. The mesh leaves the acquired slab, so
    uncovered vertices form a contiguous slab-shaped region and coverage behaves
    like a step function along one or two anatomical axes.
  * **A bad transform** — the tkrRAS -> scanner RAS -> BOLD voxel chain is
    misaligned. Coverage then degrades diffusely or asymmetrically, with no
    clean spatial boundary.

This bins coverage by decile along each axis of the midthickness surface. A
large range on one axis with a flat left-right profile means clipping; broadly
flat-but-depressed profiles mean look at the transform.

Observed 2026-07-22 on sub-330E63GH (coverage 0.59, clipped) against
sub-7GTT6FAU (0.97, clean) — see the validation findings in
openspec/changes/add-surface-func-processing/tasks.md.

Inputs are Stage 1 outputs: the per-hemisphere fsnative timeseries from a
components tarball, and the session midthickness from the sibling anat/ prefix.

  python scripts/surface_coverage_profile.py \
      --func sub-X_ses-Y_task-rest_run-01_hemi-L_space-fsnative_bold.func.gii \
      --surf sub-X_ses-Y_hemi-L_midthickness.surf.gii
"""

from __future__ import annotations

import argparse
from pathlib import Path

import nibabel as nib
import numpy as np

AXES = ("x (L-R)", "y (P-A)", "z (I-S)")

# A step function this steep on y or z, with x flat, is the clipping signature.
CLIPPING_RANGE = 0.5
FLAT_RANGE = 0.25


def profile(func_gii: Path, surf_gii: Path) -> None:
    ts = np.stack([d.data for d in nib.load(func_gii).darrays], axis=1)
    coords = nib.load(surf_gii).darrays[0].data
    covered = np.any(ts != 0, axis=1)

    if coords.shape[0] != ts.shape[0]:
        raise SystemExit(
            f"vertex count mismatch: surface {coords.shape[0]}, "
            f"timeseries {ts.shape[0]} — are these the same hemisphere?"
        )

    print(f"vertices={ts.shape[0]}  frames={ts.shape[1]}  coverage={covered.mean():.4f}\n")

    ranges = []
    for ax in range(3):
        c = coords[:, ax]
        edges = np.percentile(c, np.linspace(0, 100, 11))
        fr = []
        for i in range(10):
            m = (c >= edges[i]) & (c <= edges[i + 1])
            fr.append(covered[m].mean() if m.any() else np.nan)
        rng = float(np.nanmax(fr) - np.nanmin(fr))
        ranges.append(rng)
        print(f"  {AXES[ax]:<9} " + " ".join(f"{v:4.2f}" for v in fr) + f"   range {rng:.2f}")
        print(
            f"  {'':<9} covered mean={c[covered].mean():7.2f}  "
            f"uncovered mean={c[~covered].mean():7.2f}  "
            f"delta={c[~covered].mean() - c[covered].mean():+7.2f}"
        )

    print()
    lr, pa, is_ = ranges
    if max(pa, is_) >= CLIPPING_RANGE and lr <= FLAT_RANGE:
        print(
            "VERDICT: step function on y/z with a flat L-R profile — "
            "consistent with FOV clipping (a data problem, not the transform)."
        )
    elif covered.mean() > 0.9:
        print("VERDICT: coverage is high and profiles are flat — nothing to see.")
    else:
        print(
            "VERDICT: coverage is depressed without a clean spatial boundary. "
            "Check the bold2t1w registration and the tkrRAS conversion before "
            "blaming the acquisition."
        )


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--func", required=True, type=Path, help="hemi-?_space-fsnative_bold.func.gii from Stage 1"
    )
    p.add_argument(
        "--surf", required=True, type=Path, help="matching hemi-?_midthickness.surf.gii from anat/"
    )
    args = p.parse_args()
    profile(args.func, args.surf)


if __name__ == "__main__":
    main()
