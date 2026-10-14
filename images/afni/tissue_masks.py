"""
tissue_masks.py — one session's FastSurfer tissue masks in MNI152NLin2009cAsym, 1 mm.

The session's aseg (conformed T1w space) is warped onto the 1 mm MNI template —
the grid the t1w-to-MNI SyN warp is defined on — with nearest-neighbour
interpolation, through the same transform chain the subcortical CIFTI block uses
(preproc.py extract_subcortical). Every brain label is then assigned to exactly
one of three tissue classes (TISSUE_LABELS below). Writes, into --outdir:

  {subj}_{ses}_space-MNI152NLin2009cAsym_res-1_dseg.nii.gz     uint8, 1=GM 2=WM 3=CSF
  {subj}_{ses}_space-MNI152NLin2009cAsym_res-1_dseg.tsv        BIDS lookup table
  {subj}_{ses}_space-MNI152NLin2009cAsym_res-1_label-GM_mask.nii.gz   uint8 0/1
  {subj}_{ses}_space-MNI152NLin2009cAsym_res-1_label-WM_mask.nii.gz
  {subj}_{ses}_space-MNI152NLin2009cAsym_res-1_label-CSF_mask.nii.gz

With --brainmask it also writes probabilistic maps, from intensity rather than
anatomy:

  {subj}_{ses}_space-MNI152NLin2009cAsym_res-1_label-{GM,WM,CSF}_probseg.nii.gz  float32

FastSurfer's brainmask.mgz (skull-stripped, bias-corrected, intensity-normalised,
which is the input DIPY's example asks for) is warped to the same 1 mm grid
(linear), and DIPY's hidden-Markov-random-field classifier fits three classes to
it (TissueClassifierHMRF, the same family as FSL FAST, which is what fMRIPrep's
probseg comes from). Each voxel's three values sum to 1 inside the brain and are
0 outside it, where "brain" is intensity at or above PROBSEG_BRAIN_FLOOR: with the
floor at 0, 0.16% of a pilot session's brain summed below 0.99, all of it the
interpolation fringe; with it at 1.0, none did. They are intensity-based, so they disagree with the anatomical
masks where tissue does not look like its class (WM-bright deep grey nuclei),
and their CSF includes sulcal CSF, which aseg's does not.

These are anatomical tissue masks, NOT the aCompCor noise ROIs: aCompCor takes
only cerebral WM (2, 41) and the ventricles proper (4, 14, 15, 43), eroded, in
native BOLD space (preproc.py compute_confounds). They are on the 1 mm grid, not
the BOLD's 2 mm one, so resample before applying one to the MNI BOLD.

Usage:
  python tissue_masks.py --aseg aseg.auto.mgz --template MNI_res-01.nii \\
      --t1w2mni-affine affine.mat --t1w2mni-warp warp.nii.gz \\
      [--brainmask brainmask.mgz] --outdir out --subj sub-X --session ses-00A
"""

from __future__ import annotations

import argparse
import functools
import subprocess
import tempfile
from pathlib import Path

import nibabel as nib
import numpy as np

SPACE = "space-MNI152NLin2009cAsym_res-1"
# FreeSurferColorLUT labels per class. Every label FastSurfer's aseg uses inside
# the brain is listed exactly once; a label in none of them (unknown 0, vessels
# 30/62, non-WM hypointensities 80) stays background.
# fmt: off
TISSUE_LABELS: dict[str, tuple[int, ...]] = {
    "GM": (
        3, 42,  # cerebral cortex
        8, 47,  # cerebellum cortex
        10, 49,  # thalamus
        11, 50,  # caudate
        12, 51,  # putamen
        13, 52,  # pallidum
        17, 53,  # hippocampus
        18, 54,  # amygdala
        26, 58,  # accumbens
        28, 60,  # ventral DC
    ),
    "WM": (
        2, 41,  # cerebral white matter
        7, 46,  # cerebellum white matter
        16,  # brain stem
        77,  # white-matter hypointensities
        85,  # optic chiasm
        251, 252, 253, 254, 255,  # corpus callosum
    ),
    "CSF": (
        4, 43,  # lateral ventricles
        5, 44,  # inferior lateral ventricles
        14, 15,  # 3rd and 4th ventricles
        24,  # CSF
        31, 63,  # choroid plexus
        72,  # 5th ventricle
    ),
}
# fmt: on
# The HMRF's random draws are seeded with this, so the same input always gives the
# same maps. DIPY's classify() takes no generator: it calls add_noise(rng=None),
# which draws from fresh OS entropy, so np.random.seed does not reach it (two
# seeded runs differed by up to 0.197 in PVE). hmrf_pve() supplies one. That
# reaches into dipy.segment.tissue, so dipy is pinned to an exact version.
PROBSEG_SEED = 20261014
# DIPY's documented example value; its docs give 0-0.5 as the working range.
HMRF_BETA = 0.1
# Intensity below which a warped brainmask.mgz voxel is interpolation fringe, not
# brain. FastSurfer normalises WM to ~110, and the linear warp smears near-zero
# values a voxel or two past the mask edge. On a pilot session every voxel whose
# three PVEs summed below 0.5 sat within 2 voxels of the edge, at median
# intensity 0.1; DIPY's background class rightly claims them.
PROBSEG_BRAIN_FLOOR = 1.0
# DIPY orders classes by mean intensity, which on a T1w is CSF < GM < WM.
PVE_ORDER = ("CSF", "GM", "WM")

# dseg index for each class, in the order the lookup table lists them.
CLASS_INDEX = {"GM": 1, "WM": 2, "CSF": 3}
NAMES = {"GM": "gray matter", "WM": "white matter", "CSF": "cerebrospinal fluid"}


def classify(aseg_labels: np.ndarray) -> np.ndarray:
    """Map an aseg label volume to a uint8 dseg: 0 background, then CLASS_INDEX."""
    labels = np.rint(aseg_labels).astype(np.int32)
    dseg = np.zeros(labels.shape, dtype=np.uint8)
    for name, values in TISSUE_LABELS.items():
        dseg[np.isin(labels, values)] = CLASS_INDEX[name]
    return dseg


def warp_aseg(aseg: Path, template: Path, affine: Path, warp: Path, out: Path) -> None:
    """antsApplyTransforms, nearest-neighbour, aseg (conformed T1w) -> MNI template grid."""
    subprocess.run(
        [
            "antsApplyTransforms",
            "-d",
            "3",
            "-i",
            str(aseg),
            "-r",
            str(template),
            "-o",
            str(out),
            "-n",
            "NearestNeighbor",
            "-t",
            str(warp),
            "-t",
            str(affine),
        ],
        check=True,
    )


def warp_t1w(t1w: Path, template: Path, affine: Path, warp: Path, out: Path) -> None:
    """antsApplyTransforms, linear: an intensity image to the MNI template grid."""
    subprocess.run(
        [
            "antsApplyTransforms",
            "-d",
            "3",
            "-i",
            str(t1w),
            "-r",
            str(template),
            "-o",
            str(out),
            "-n",
            "Linear",
            "-t",
            str(warp),
            "-t",
            str(affine),
        ],
        check=True,
    )


def hmrf_pve(t1: np.ndarray, seed: int = PROBSEG_SEED) -> np.ndarray:
    """DIPY HMRF partial-volume estimates, (..., 3) in PVE_ORDER, reproducibly.

    Raises if DIPY no longer calls the module-level add_noise this patches, so a
    version change fails loudly instead of quietly becoming non-deterministic.
    """
    import dipy.segment.tissue as tissue
    from dipy.sims.voxel import add_noise

    if getattr(tissue, "add_noise", None) is not add_noise:
        raise RuntimeError("dipy.segment.tissue no longer uses add_noise; re-check the seeding")
    tissue.add_noise = functools.partial(add_noise, rng=np.random.default_rng(seed))
    try:
        _, _, pve = tissue.TissueClassifierHMRF(verbose=False).classify(
            np.asarray(t1, dtype=np.float64), nclasses=3, beta=HMRF_BETA
        )
    finally:
        tissue.add_noise = add_noise
    return np.asarray(pve, dtype=np.float32)


def write_probseg(t1_mni: Path, outdir: Path, prefix: str) -> list[Path]:
    """Write label-{GM,WM,CSF}_probseg for one MNI T1w brain; return their paths."""
    img = nib.load(t1_mni)
    t1 = np.asanyarray(img.dataobj).astype(np.float32)
    brain = t1 >= PROBSEG_BRAIN_FLOOR
    if not brain.any():
        raise ValueError("the warped brain is empty")
    pve = hmrf_pve(t1)
    # DIPY fills background voxels with noise, which leaves tissue probability
    # up to ~0.98 outside the brain; nothing outside it is tissue.
    pve[~brain] = 0.0
    header = img.header.copy()
    header.set_data_dtype(np.float32)
    header["scl_slope"], header["scl_inter"] = 1.0, 0.0
    outdir.mkdir(parents=True, exist_ok=True)
    out = []
    for index, name in enumerate(PVE_ORDER):
        path = outdir / f"{prefix}_{SPACE}_label-{name}_probseg.nii.gz"
        nib.save(nib.Nifti1Image(pve[..., index], img.affine, header), path)
        out.append(path)
    print(f"  probseg: {int(brain.sum())} brain voxels, seed {PROBSEG_SEED}", flush=True)
    return out


def write_masks(aseg_mni: Path, outdir: Path, prefix: str) -> list[Path]:
    """Write the dseg, its lookup table and the three binary masks; return their paths."""
    img = nib.load(aseg_mni)
    dseg = classify(np.asanyarray(img.dataobj))
    if not dseg.any():
        raise ValueError("no tissue voxels survived the aseg warp")
    header = img.header.copy()
    header.set_data_dtype(np.uint8)
    header["scl_slope"], header["scl_inter"] = 1.0, 0.0
    outdir.mkdir(parents=True, exist_ok=True)
    out = []
    path = outdir / f"{prefix}_{SPACE}_dseg.nii.gz"
    nib.save(nib.Nifti1Image(dseg, img.affine, header), path)
    out.append(path)
    tsv = outdir / f"{prefix}_{SPACE}_dseg.tsv"
    tsv.write_text(
        "index\tname\tabbreviation\n"
        + "".join(f"{CLASS_INDEX[k]}\t{NAMES[k]}\t{k}\n" for k in CLASS_INDEX)
    )
    out.append(tsv)
    for name, index in CLASS_INDEX.items():
        path = outdir / f"{prefix}_{SPACE}_label-{name}_mask.nii.gz"
        nib.save(nib.Nifti1Image((dseg == index).astype(np.uint8), img.affine, header), path)
        out.append(path)
    counts = {k: int((dseg == v).sum()) for k, v in CLASS_INDEX.items()}
    print(f"  tissue masks: {counts} voxels at 1 mm", flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--aseg", type=Path, required=True)
    p.add_argument("--template", type=Path, required=True, help="the 1 mm MNI template")
    p.add_argument("--t1w2mni-affine", type=Path, required=True)
    p.add_argument("--t1w2mni-warp", type=Path, required=True)
    p.add_argument(
        "--brainmask", type=Path, help="FastSurfer brainmask.mgz; adds the DIPY probseg maps"
    )
    p.add_argument("--outdir", type=Path, required=True)
    p.add_argument("--subj", required=True)
    p.add_argument("--session", required=True)
    args = p.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        aseg_mni = Path(tmp) / "aseg_mni.nii.gz"
        warp_aseg(args.aseg, args.template, args.t1w2mni_affine, args.t1w2mni_warp, aseg_mni)
        write_masks(aseg_mni, args.outdir, f"{args.subj}_{args.session}")
        if args.brainmask:
            t1_mni = Path(tmp) / "brainmask_mni.nii.gz"
            warp_t1w(args.brainmask, args.template, args.t1w2mni_affine, args.t1w2mni_warp, t1_mni)
            write_probseg(t1_mni, args.outdir, f"{args.subj}_{args.session}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
