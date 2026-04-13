#!/usr/bin/env python3
"""
bold_to_t1w.py — BOLD → T1w registration via SynthMorph

Replaces bbregister with mri_synthmorph for GPU-accelerated, contrast-agnostic
affine registration. SynthMorph performs a single neural network forward pass
(~5s on GPU) rather than iterative surface-based optimisation (~5-10min for
bbregister), giving a 50-100x throughput improvement.

All FreeSurfer CLI dependencies (bbregister, lta_convert, mri_convert,
mri_vol2vol) are replaced with Python equivalents (nibabel, numpy, scipy)
for compatibility with the lightweight freesurfer/synthmorph container.

mri_synthmorph outputs a FreeSurfer LTA file. The LTA (type 1, RAS-to-RAS) is
converted to an ANTs/ITK affine .mat by flipping from RAS to LPS coordinates
(the ITK convention) — the same operation that lta_convert --outitk performs.

Inputs (via CLI args):
  --bold         4D BOLD NIfTI
  --nss-frames   Number of NSS frames to skip (takes precedence over --json)
  --json         BIDS sidecar JSON (optional; used to skip NSS volumes)
  --sbref        SBRef NIfTI (optional; takes precedence over BOLD frame)
  --subjects-dir FreeSurfer/FastSurfer subjects directory (SUBJECTS_DIR)
  --subject      Subject ID as it appears under --subjects-dir
  --out-dir      Output directory
  --prefix       Output filename prefix

Outputs written to --out-dir:
  <prefix>_desc-bold2t1w_ref.nii.gz       reference volume used (QC)
  <prefix>_desc-bold2t1w.lta              SynthMorph LTA transform
  <prefix>_desc-bold2t1w_warped.nii.gz    BOLD ref warped to T1w space (QC)
  <prefix>_desc-bold2t1w_itk.txt          ANTs/ITK affine transform for
                                           use in antsApplyTransforms
  <prefix>_desc-bold2t1w_brainmask.nii.gz binary brain mask in BOLD space
"""

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
from scipy import ndimage

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def run(cmd: list) -> None:
    log.info(f">>> {' '.join(str(c) for c in cmd)}")
    subprocess.run([str(c) for c in cmd], check=True)


def get_first_steady_state_frame(nss_frames: int | None,
                                  json_sidecar: Path | None) -> int:
    if nss_frames is not None:
        log.info(f"Using --nss-frames override: skipping {nss_frames} NSS volume(s); "
                 f"using frame {nss_frames} as reference")
        return nss_frames
    if json_sidecar is not None and json_sidecar.exists():
        with open(json_sidecar) as f:
            meta = json.load(f)
        nss = meta.get("NumberOfVolumesDiscardedByScanner", 0) + \
              meta.get("NumberOfVolumesDiscardedByUser", 0)
        if nss:
            log.info(f"Sidecar: skipping {nss} NSS volume(s); using frame {nss} as reference")
        return nss
    return 0


def build_reference(bold: Path, sbref: Path | None, nss_frames: int | None,
                    json_sidecar: Path | None, out: Path) -> Path:
    """
    Produce a 3D reference volume for SynthMorph.

    Uses SBRef if provided. Otherwise extracts the first steady-state BOLD
    frame using nibabel (replaces mri_convert --frame).
    """
    if sbref is not None and sbref.exists():
        log.info(f"Using SBRef as reference: {sbref}")
        img = nib.load(str(sbref))
        nib.save(img, str(out))
        return out

    frame_idx = get_first_steady_state_frame(nss_frames, json_sidecar)
    log.info(f"No SBRef — extracting frame {frame_idx} from BOLD")
    bold_img = nib.load(str(bold))
    data = np.asarray(bold_img.dataobj)[..., frame_idx]
    ref_img = nib.Nifti1Image(data, bold_img.affine, bold_img.header)
    ref_img.header.set_data_shape(data.shape)
    nib.save(ref_img, str(out))
    return out


def register(ref: Path, subjects_dir: Path, subject: str,
             lta: Path, warped: Path) -> None:
    """
    Run mri_synthmorph affine registration.

    Moving image: BOLD reference (ref)
    Fixed image:  T1w (orig.mgz from FastSurfer output)
    Output:       LTA transform (BOLD RAS → T1w RAS)

    mri_synthmorph uses a contrast-agnostic neural network so no special
    initialisation or cost function tuning is needed for the EPI/T1w contrast
    mismatch. GPU is used automatically when available.

    The warped QC image is produced by apply_lta (replaces bbregister --o).
    """
    t1w = subjects_dir / subject / "mri" / "orig.mgz"
    log.info(f"Running mri_synthmorph: {ref} → {t1w}")
    run([
        "mri_synthmorph", "register",
        "-m", "affine",
        "-t", lta,
        str(ref), str(t1w),
    ])
    apply_lta(ref, lta, t1w, warped, order=1)
    log.info(f"Warped QC image saved: {warped}")


def parse_lta_matrix(lta_path: Path) -> np.ndarray:
    """
    Read the 4x4 RAS-to-RAS matrix from a FreeSurfer LTA file.

    A type-1 LTA stores the matrix after a line containing '1 4 4'
    (one transform, 4 rows, 4 cols).
    """
    with open(lta_path) as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        if line.strip() == '1 4 4':
            rows = []
            for j in range(1, 5):
                rows.append([float(x) for x in lines[i + j].split()])
            return np.array(rows)
    raise ValueError(f"Could not parse 4x4 matrix from {lta_path}")


def apply_lta(moving_path: Path, lta_path: Path, reference_path: Path,
              out_path: Path, order: int = 1) -> None:
    """
    Resample moving_path into the space of reference_path using the LTA.

    The LTA encodes moving_RAS → reference_RAS (type 1, RAS-to-RAS).
    For resampling into reference space we need the inverse mapping:
      v_mov = A_mov⁻¹ @ T⁻¹ @ A_ref @ v_ref

    order=1 (trilinear) for intensity images, order=0 (nearest) for masks.
    Replaces mri_vol2vol.
    """
    T = parse_lta_matrix(lta_path)
    T_inv = np.linalg.inv(T)

    mov_img = nib.load(str(moving_path))
    ref_img = nib.load(str(reference_path))
    mov_data = np.asarray(mov_img.dataobj)
    if mov_data.ndim == 4:
        mov_data = mov_data[..., 0]

    # M maps reference voxel coords → moving voxel coords
    M = np.linalg.inv(mov_img.affine) @ T_inv @ ref_img.affine

    shape = ref_img.shape[:3]
    i, j, k = np.mgrid[:shape[0], :shape[1], :shape[2]]
    vox = np.ones((4, i.size))
    vox[0], vox[1], vox[2] = i.ravel(), j.ravel(), k.ravel()

    mov_coords = (M @ vox)[:3]
    resampled = ndimage.map_coordinates(
        mov_data.astype(np.float32), mov_coords,
        order=order, mode='constant', cval=0,
    ).reshape(shape)

    out_img = nib.Nifti1Image(resampled, ref_img.affine, ref_img.header)
    out_img.header.set_data_shape(shape)
    nib.save(out_img, str(out_path))


def convert_to_itk(lta_path: Path, itk_path: Path) -> None:
    """
    Convert a FreeSurfer LTA (RAS-to-RAS) to an ANTs/ITK affine transform.

    ITK uses LPS coordinates (x and y axes negated relative to RAS). The
    conversion applies the RAS↔LPS flip on both sides of the matrix:
      M_lps = ras2lps @ M_ras @ ras2lps
    where ras2lps = diag([-1, -1, 1, 1]) is its own inverse.

    The ITK MatrixOffsetTransformBase stores the 3×3 linear part and the
    translation vector separately. FixedParameters (centre of rotation) are
    zero. This produces the same output as lta_convert --outitk.
    """
    M_ras = parse_lta_matrix(lta_path)
    ras2lps = np.diag([-1., -1., 1., 1.])
    M_lps = ras2lps @ M_ras @ ras2lps

    matrix = M_lps[:3, :3]
    translation = M_lps[:3, 3]

    params = (
        ' '.join(f'{v:.10f}' for v in matrix.flatten()) + ' ' +
        ' '.join(f'{v:.10f}' for v in translation)
    )

    with open(itk_path, 'w') as f:
        f.write('#Insight Transform File V1.0\n')
        f.write('#Transform 0\n')
        f.write('Transform: MatrixOffsetTransformBase_double_3_3\n')
        f.write(f'Parameters: {params}\n')
        f.write('FixedParameters: 0.0 0.0 0.0\n')

    log.info(f"ANTs/ITK transform saved: {itk_path}")


def mask_to_bold(subjects_dir: Path, subject: str,
                 ref: Path, lta: Path, out: Path) -> Path:
    """
    Warp the FastSurfer brain mask into BOLD space.

    The LTA encodes BOLD_RAS → T1w_RAS. To bring brainmask.mgz (T1w space)
    into BOLD space, we apply the forward transform: for each BOLD voxel,
    look up the corresponding T1w location and sample the mask there:
      v_mask = A_mask⁻¹ @ T @ A_bold @ v_bold

    Nearest-neighbour interpolation (order=0) preserves binary values.
    Replaces mri_vol2vol --inv --nearest.
    """
    brainmask_mgz = subjects_dir / subject / "mri" / "brainmask.mgz"
    T = parse_lta_matrix(lta)

    ref_img = nib.load(str(ref))
    mask_img = nib.load(str(brainmask_mgz))
    mask_data = np.asarray(mask_img.dataobj)

    # M maps BOLD voxel coords → brainmask voxel coords
    M = np.linalg.inv(mask_img.affine) @ T @ ref_img.affine

    shape = ref_img.shape[:3]
    i, j, k = np.mgrid[:shape[0], :shape[1], :shape[2]]
    vox = np.ones((4, i.size))
    vox[0], vox[1], vox[2] = i.ravel(), j.ravel(), k.ravel()

    mask_coords = (M @ vox)[:3]
    sampled = ndimage.map_coordinates(
        mask_data.astype(np.float32), mask_coords,
        order=0, mode='constant', cval=0,
    ).reshape(shape)

    out_img = nib.Nifti1Image((sampled > 0).astype(np.uint8), ref_img.affine, ref_img.header)
    out_img.header.set_data_shape(shape)
    nib.save(out_img, str(out))
    log.info(f"BOLD brain mask saved: {out}")
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BOLD → T1w registration via SynthMorph (GPU-accelerated)"
    )
    p.add_argument("--bold",         required=True,  type=Path)
    p.add_argument("--nss-frames",   required=False, type=int,  default=None,
                   help="Number of NSS frames to skip; takes precedence over --json")
    p.add_argument("--json",         required=False, type=Path, default=None,
                   help="BIDS sidecar JSON for NSS volume detection")
    p.add_argument("--sbref",        required=False, type=Path, default=None)
    p.add_argument("--subjects-dir", required=True,  type=Path)
    p.add_argument("--subject",      required=True)
    p.add_argument("--out-dir",      required=True,  type=Path)
    p.add_argument("--prefix",       required=True)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    pfx = args.out_dir / args.prefix

    log.info(f"=== bold_to_t1w (synthmorph): {args.prefix} ===")
    log.info(f"SUBJECTS_DIR : {args.subjects_dir}")
    log.info(f"Subject      : {args.subject}")

    ref       = Path(str(pfx) + "_desc-bold2t1w_ref.nii.gz")
    lta       = Path(str(pfx) + "_desc-bold2t1w.lta")
    warped    = Path(str(pfx) + "_desc-bold2t1w_warped.nii.gz")
    itk_mat   = Path(str(pfx) + "_desc-bold2t1w_itk.txt")
    bold_mask = Path(str(pfx) + "_desc-bold2t1w_brainmask.nii.gz")

    build_reference(args.bold, args.sbref, args.nss_frames, args.json, ref)
    register(ref, args.subjects_dir, args.subject, lta, warped)
    convert_to_itk(lta, itk_mat)
    mask_to_bold(args.subjects_dir, args.subject, ref, lta, bold_mask)

    log.info("=== bold_to_t1w complete ===")
    log.info(f"  Reference    : {ref}")
    log.info(f"  LTA transform: {lta}")
    log.info(f"  Warped ref   : {warped}  (QC)")
    log.info(f"  ITK transform: {itk_mat}")
    log.info(f"  Brain mask   : {bold_mask}")


if __name__ == "__main__":
    main()
