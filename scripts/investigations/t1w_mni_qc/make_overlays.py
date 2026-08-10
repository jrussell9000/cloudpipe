#!/usr/bin/env python
"""make_overlays.py — tri-planar overlay PNGs for T1w->MNI visual QC.

nibabel + matplotlib only (nilearn is not installed). Two uses:
  * warped T1w (base) over MNI template (overlay contour) -> alignment quality
  * orig.mgz (base) with brainmask.mgz (overlay contour)  -> skull-strip quality
"""

from __future__ import annotations

import argparse

import matplotlib
import nibabel as nib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def _mid_slices(arr: np.ndarray):
    x, y, z = (s // 2 for s in arr.shape[:3])
    return [arr[x, :, :], arr[:, y, :], arr[:, :, z]]


def tri_planar(base_path: str, out_path: str, overlay_path: str | None = None) -> None:
    base = np.asarray(nib.load(base_path).get_fdata(), dtype=np.float32)
    base_slices = _mid_slices(base)
    ov_slices = None
    if overlay_path is not None:
        ov = np.asarray(nib.load(overlay_path).get_fdata(), dtype=np.float32)
        ov_slices = _mid_slices(ov)

    fig, axes = plt.subplots(1, 3, figsize=(12, 4))
    for i, ax in enumerate(axes):
        ax.imshow(np.rot90(base_slices[i]), cmap="gray")
        if ov_slices is not None:
            ax.contour(np.rot90(ov_slices[i]) > 0, levels=[0.5], colors="red", linewidths=0.6)
        ax.axis("off")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--overlay", default=None)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    tri_planar(args.base, args.out, overlay_path=args.overlay)


if __name__ == "__main__":
    main()
