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

With --brainmask and --orig-nu it also writes probabilistic maps, from intensity
rather than anatomy:

  {subj}_{ses}_space-MNI152NLin2009cAsym_res-1_label-{GM,WM,CSF}_probseg.nii.gz  float32

FastSurfer's orig_nu.mgz (N4 bias-corrected, NOT intensity-normalised), masked by
brainmask.mgz > 0, is warped to the same 1 mm grid (linear), and DIPY's
hidden-Markov-random-field classifier fits three classes to it
(TissueClassifierHMRF, the same family as FSL FAST, which is what fMRIPrep's
probseg comes from; fMRIPrep also feeds FAST an N4-corrected, unnormalised T1w).
Each voxel's three values sum to 1 inside the brain and are 0 outside it, where
"brain" is the warped brainmask at or above PROBSEG_BRAIN_FLOOR: with the floor at
0, 0.16% of a pilot session's brain summed below 0.99, all of it the
interpolation fringe; with it at 1.0, none did. They are intensity-based, so they
disagree with the anatomical masks where tissue does not look like its class
(WM-bright deep grey nuclei), and their CSF includes sulcal CSF, which aseg's does
not.

Why not brainmask.mgz's own intensities (the input until 2026-10-26): FastSurfer
normalises white matter to exactly 110, so 13-18% of brain voxels share that one
value. The WM Gaussian can lock onto the spike (its mean converges to 110.0 with
a tiny variance), the rest of the WM goes to "GM" and the GM to "CSF". It did in
32 of 60 audited sessions (scripts/audit_func_outputs.py): CSF_probseg held
~1,300 ml, WM_probseg ~60. Seeding the fit from the dseg did not stop it either.
orig_nu has no spike, but DIPY's start is set by the image maximum, so a few
very bright voxels can collapse it the same way; the fit's input is therefore
clipped at WM_MAX_RATIO times the dseg's WM (anchor_maximum).

Every probseg is also checked against the dseg before it is written (see
probseg_agreement): a class whose argmax agrees with its dseg class on fewer than
PROBSEG_MIN_AGREEMENT of that class's voxels means the fit collapsed, and then no
probseg is written at all. The dseg and masks are written either way.

These are anatomical tissue masks, NOT the aCompCor noise ROIs: aCompCor takes
only cerebral WM (2, 41) and the ventricles proper (4, 14, 15, 43), eroded, in
native BOLD space (preproc.py compute_confounds). They are on the 1 mm grid, not
the BOLD's 2 mm one, so resample before applying one to the MNI BOLD.

Usage:
  python tissue_masks.py --aseg aseg.auto.mgz --template MNI_res-01.nii \\
      --t1w2mni-affine affine.mat --t1w2mni-warp warp.nii.gz \\
      [--brainmask brainmask.mgz --orig-nu orig_nu.mgz] \\
      --outdir out --subj sub-X --session ses-00A
"""

from __future__ import annotations

import argparse
import functools
import json
import subprocess
import tempfile
from datetime import UTC, datetime
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
# Minimum share of each dseg class's voxels whose probseg argmax is that class.
# The two segmentations differ by construction (intensity vs anatomy), and sound
# fits agreed 0.68-0.91 per class across the audited sessions; collapsed ones
# scored 0.0-0.44 on WM. Half sits between the two with room on both sides.
PROBSEG_MIN_AGREEMENT = 0.5
# The fit's input is clipped at this multiple of the dseg's median WM intensity,
# so DIPY's start (3/4 of the maximum for the brightest class) lands on WM (see
# anchor_maximum). orig_nu alone still collapsed on 3 of 40 validation
# sessions, each with a maximum near 1.9 x WM.
WM_MAX_RATIO = 4.0 / 3.0
# Recorded in TissueQC.probseg_input, so a row says which fit produced it.
PROBSEG_INPUT = "orig_nu"
# TissueQC.schema_version as this module writes it; must equal the dataclass
# default in src/metrics/schemas.py (tests/metrics/test_tissue_qc_schema_sync.py).
TISSUE_QC_SCHEMA_VERSION = "1.0"

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


def skull_strip(orig_nu: Path, brainmask: Path, out: Path) -> None:
    """orig_nu.mgz times (brainmask.mgz > 0), both in conformed space, as float32 NIfTI."""
    nu, bm = nib.load(orig_nu), nib.load(brainmask)
    if nu.shape != bm.shape or not np.allclose(nu.affine, bm.affine, atol=1e-4):
        raise ValueError(
            f"orig_nu {nu.shape} and brainmask {bm.shape} are not on one grid; "
            "they come from different FastSurfer runs"
        )
    brain = np.asanyarray(nu.dataobj).astype(np.float32) * (np.asanyarray(bm.dataobj) > 0)
    nib.save(nib.Nifti1Image(brain, nu.affine), out)


def anchor_maximum(t1: np.ndarray, brain: np.ndarray, dseg: np.ndarray) -> np.ndarray:
    """Clip the fit's input at WM_MAX_RATIO times the dseg's median WM intensity.

    DIPY's HMRF starts from class means at 0, 1/4, 1/2 and 3/4 of the image
    maximum (background, then the three tissues, in its [0, 1] rescaling), so
    the maximum alone decides where the fit starts. At a maximum near 4/3 of
    WM they land on CSF, GM and WM. A few bright non-brain voxels inside the
    mask (vessels, fat) raise it: at 208 with WM near 110 they land at 52/104/
    156, GM is nearer the "CSF" start than the "GM" one, WM takes the "GM"
    class and the "WM" class keeps only the bright tail. That is the collapse
    every rejected session showed. Clipping at WM_MAX_RATIO x WM puts the
    maximum where the start is right. Few voxels sit above it, so it makes no
    spike worth the name; an image already below it is left alone.
    """
    wm = brain & (dseg == CLASS_INDEX["WM"])
    if not wm.any():
        return t1
    ceiling = np.float32(WM_MAX_RATIO * np.median(t1[wm]))
    return np.minimum(t1, ceiling)


def probseg_agreement(pve: np.ndarray, dseg: np.ndarray) -> dict[str, float]:
    """Per dseg class present: share of its voxels whose probseg argmax is that class."""
    winner = np.asarray(PVE_ORDER)[np.argmax(pve, axis=-1)]
    return {
        name: float(np.mean(winner[dseg == index] == name))
        for name, index in CLASS_INDEX.items()
        if (dseg == index).any()
    }


def write_probseg(
    t1_mni: Path,
    outdir: Path,
    prefix: str,
    *,
    brain_mni: Path | None = None,
    dseg: np.ndarray | None = None,
    record: dict | None = None,
) -> list[Path]:
    """Write label-{GM,WM,CSF}_probseg for one MNI T1w brain; return their paths.

    `brain_mni`, when given, is the image whose voxels at or above
    PROBSEG_BRAIN_FLOOR are the brain (the warped brainmask); otherwise `t1_mni`
    itself is. `dseg`, when given, gates the write: a fit that disagrees with it
    (probseg_agreement below PROBSEG_MIN_AGREEMENT on any class) writes nothing
    and returns [], because a probseg whose classes collapsed looks valid in
    every other respect (it sums to 1, it covers the brain). `record`, when
    given, receives the fit's TissueQC fields, written or rejected alike.
    """
    img = nib.load(t1_mni)
    t1 = np.asanyarray(img.dataobj).astype(np.float32)
    support = t1 if brain_mni is None else np.asanyarray(nib.load(brain_mni).dataobj)
    brain = support >= PROBSEG_BRAIN_FLOOR
    if not brain.any():
        raise ValueError("the warped brain is empty")
    t1[~brain] = 0.0
    if dseg is not None:
        t1 = anchor_maximum(t1, brain, dseg)
    pve = hmrf_pve(t1)
    # DIPY fills background voxels with noise, which leaves tissue probability
    # up to ~0.98 outside the brain; nothing outside it is tissue.
    pve[~brain] = 0.0
    agree = probseg_agreement(pve, dseg) if dseg is not None else {}
    if record is not None:
        voxel_mm3 = _voxel_mm3(img)
        record.update(
            probseg_input=PROBSEG_INPUT,
            probseg_seed=PROBSEG_SEED,
            probseg_written=False,
            **{
                f"probseg_{name.lower()}_vol_mm3": round(float(pve[..., i].sum()) * voxel_mm3, 1)
                for i, name in enumerate(PVE_ORDER)
            },
            **{f"agreement_{name.lower()}": _round(agree.get(name)) for name in CLASS_INDEX},
        )
    if dseg is not None:
        shown = ", ".join(f"{k} {v:.3f}" for k, v in agree.items())
        if min(agree.values(), default=0.0) < PROBSEG_MIN_AGREEMENT:
            print(
                f"  probseg REJECTED, not written: agreement with the dseg {shown} "
                f"(minimum {PROBSEG_MIN_AGREEMENT})",
                flush=True,
            )
            return []
        print(f"  probseg agreement with the dseg: {shown}", flush=True)
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
    if record is not None:
        record["probseg_written"] = True
    return out


def _voxel_mm3(img) -> float:
    return float(abs(np.linalg.det(img.affine[:3, :3])))


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def dseg_volumes(dseg_path: Path) -> dict[str, float]:
    """TissueQC's dseg_{gm,wm,csf}_vol_mm3, from the written dseg."""
    img = nib.load(dseg_path)
    dseg = np.asanyarray(img.dataobj)
    voxel_mm3 = _voxel_mm3(img)
    return {
        f"dseg_{name.lower()}_vol_mm3": round(int((dseg == index).sum()) * voxel_mm3, 1)
        for name, index in CLASS_INDEX.items()
    }


def write_tissue_qc(record: dict, qc_out: Path, prefix: str) -> Path:
    """One compact JSON line (Athena's JsonSerDe reads one object per line)."""
    qc_out.mkdir(parents=True, exist_ok=True)
    path = qc_out / f"{prefix}_tissue_qc.json"
    path.write_text(json.dumps(record) + "\n")
    return path


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
        "--brainmask",
        type=Path,
        help="FastSurfer brainmask.mgz: the brain extent for the DIPY probseg maps",
    )
    p.add_argument(
        "--orig-nu",
        type=Path,
        help="FastSurfer orig_nu.mgz: the intensities the probseg is fitted to",
    )
    p.add_argument("--outdir", type=Path, required=True)
    p.add_argument("--subj", required=True)
    p.add_argument("--session", required=True)
    p.add_argument(
        "--qc-out", type=Path, help="write the session's TissueQC record into this directory"
    )
    p.add_argument(
        "--source",
        default="func-preproc",
        choices=("func-preproc", "probseg-backfill"),
        help="what produced the record (TissueQC.source)",
    )
    p.add_argument("--image-tag", default="", help="TissueQC.image_tag")
    args = p.parse_args(argv)
    if bool(args.brainmask) != bool(args.orig_nu):
        p.error("--brainmask and --orig-nu go together (the probseg needs both)")
    prefix = f"{args.subj}_{args.session}"
    # Field order is the Glue column order (docs/metrics_data_dictionary.md).
    record = {
        "subject": args.subj,
        "session": args.session,
        "pipeline": "cloudpipe_minproc",
        "image_tag": args.image_tag,
        "source": args.source,
        "schema_version": TISSUE_QC_SCHEMA_VERSION,
        "completed_at": "",
        **dict.fromkeys(f"dseg_{n.lower()}_vol_mm3" for n in CLASS_INDEX),
        # None, not 0.0, when no probseg was fitted: 0.0 is a real volume.
        "probseg_input": None,
        "probseg_seed": None,
        "probseg_written": False,
        **dict.fromkeys(f"probseg_{n.lower()}_vol_mm3" for n in CLASS_INDEX),
        **dict.fromkeys(f"agreement_{n.lower()}" for n in CLASS_INDEX),
    }
    with tempfile.TemporaryDirectory() as tmp:
        aseg_mni = Path(tmp) / "aseg_mni.nii.gz"
        warp_aseg(args.aseg, args.template, args.t1w2mni_affine, args.t1w2mni_warp, aseg_mni)
        dseg_path = write_masks(aseg_mni, args.outdir, prefix)[0]
        record.update(dseg_volumes(dseg_path))
        if args.brainmask:
            transforms = (args.template, args.t1w2mni_affine, args.t1w2mni_warp)
            brain_native = Path(tmp) / "orig_nu_brain.nii.gz"
            skull_strip(args.orig_nu, args.brainmask, brain_native)
            t1_mni = Path(tmp) / "orig_nu_brain_mni.nii.gz"
            warp_t1w(brain_native, *transforms, t1_mni)
            brain_mni = Path(tmp) / "brainmask_mni.nii.gz"
            warp_t1w(args.brainmask, *transforms, brain_mni)
            write_probseg(
                t1_mni,
                args.outdir,
                prefix,
                brain_mni=brain_mni,
                dseg=np.asanyarray(nib.load(dseg_path).dataobj),
                record=record,
            )
    if args.qc_out:
        record["completed_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_tissue_qc(record, args.qc_out, prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
