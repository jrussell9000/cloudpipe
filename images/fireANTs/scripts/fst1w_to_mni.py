#!/usr/bin/env python3
"""
t1w_to_mni.py — T1w → MNI152NLin2009cAsym registration via FireANTs SyN (GPU)

Accepts either a NIfTI or an MGZ file for --t1w and --brainmask. MGZ files
(e.g. orig.mgz, brainmask.mgz from FastSurfer) are read directly via nibabel,
eliminating the need for an mri_convert initContainer and a separate fastsurfer
image pull.

When --brainmask is supplied, it is applied to the T1w before registration to
remove skull signal that would otherwise bias the SyN optimizer.

Outputs (all written to --out-dir):
  <prefix>_affine.mat          affine transform (T1w → MNI)
  <prefix>_warp.nii.gz         SyN forward warp field (T1w → MNI)
  <prefix>_warped.nii.gz       T1w warped to MNI (for QC)
"""

import argparse
import logging
import sys
from pathlib import Path

import json

import nibabel as nib
import numpy as np
import SimpleITK as sitk
import torch

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--t1w', required=True,
                   help='T1w image in native space (NIfTI or MGZ)')
    p.add_argument('--template', required=True,
                   help='MNI152NLin2009cAsym skull-stripped template NIfTI')
    p.add_argument('--out-dir', required=True,
                   help='Output directory')
    p.add_argument('--prefix', required=True,
                   help='Output filename prefix, e.g. sub-001_ses-00A')
    p.add_argument('--brainmask', default=None,
                   help='Brain mask (NIfTI or MGZ, e.g. FastSurfer brainmask.mgz). '
                        'Applied to T1w before registration; improves alignment by removing skull signal.')
    p.add_argument('--subj', default='',
                   help='Subject ID written into RegistrationQC metrics JSON')
    p.add_argument('--ses', default='',
                   help='Session label written into RegistrationQC metrics JSON')
    p.add_argument('--pipeline', default='cloudpipe_minproc',
                   help='Pipeline name written into RegistrationQC metrics JSON')

    # Registration tuning
    p.add_argument('--affine-scales', nargs='+', type=int, default=[8, 4, 2, 1])
    p.add_argument('--affine-iterations', nargs='+', type=int, default=[200, 150, 100, 50])
    p.add_argument('--syn-scales', nargs='+', type=int, default=[4, 2, 1])
    p.add_argument('--syn-iterations', nargs='+', type=int, default=[100, 70, 50])
    p.add_argument('--learning-rate', type=float, default=0.1,
                   help='optimizer_lr passed to AffineRegistration and SyNRegistration')
    p.add_argument('--device', default='cuda' if torch.cuda.is_available() else 'cpu')
    return p.parse_args()


def _ncc(a: np.ndarray, b: np.ndarray, mask: np.ndarray | None = None) -> float:
    """Normalized cross-correlation between two arrays, optionally within a mask."""
    if mask is not None:
        a, b = a[mask], b[mask]
    a = a.ravel().astype(np.float64)
    b = b.ravel().astype(np.float64)
    a -= a.mean()
    b -= b.mean()
    denom = np.sqrt((a ** 2).sum() * (b ** 2).sum())
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0


def _dice(a: np.ndarray, b: np.ndarray) -> float:
    """Dice coefficient between binary brain masks derived by thresholding at > 0."""
    ma = (a > 0).ravel()
    mb = (b > 0).ravel()
    denom = float(ma.sum() + mb.sum())
    return float(2 * (ma & mb).sum() / denom) if denom > 0 else 0.0


def _jacobian_stats(warp_path: str) -> dict:
    """Jacobian determinant statistics of a displacement field (ANTs NIfTI format)."""
    img = nib.load(warp_path)
    d = img.get_fdata(dtype=np.float32)
    if d.ndim == 5:
        d = d[:, :, :, 0, :]           # (X, Y, Z, 1, 3) → (X, Y, Z, 3)
    vox = np.abs(img.header.get_zooms()[:3]).tolist()

    ux, uy, uz = d[..., 0], d[..., 1], d[..., 2]

    j11 = 1 + np.gradient(ux, vox[0], axis=0)
    j12 =     np.gradient(ux, vox[1], axis=1)
    j13 =     np.gradient(ux, vox[2], axis=2)
    j21 =     np.gradient(uy, vox[0], axis=0)
    j22 = 1 + np.gradient(uy, vox[1], axis=1)
    j23 =     np.gradient(uy, vox[2], axis=2)
    j31 =     np.gradient(uz, vox[0], axis=0)
    j32 =     np.gradient(uz, vox[1], axis=1)
    j33 = 1 + np.gradient(uz, vox[2], axis=2)

    det = (j11 * (j22 * j33 - j32 * j23)
           - j12 * (j21 * j33 - j31 * j23)
           + j13 * (j21 * j32 - j31 * j22))

    return {
        'jac_det_min':           float(det.min()),
        'jac_det_max':           float(det.max()),
        'jac_det_mean':          float(det.mean()),
        'jac_det_std':           float(det.std()),
        'jac_det_frac_negative': float((det < 0).mean()),
    }


def to_nifti(src: str, dest: str) -> None:
    """Load any nibabel-supported image (NIfTI, MGZ, …) and save as NIfTI."""
    img = nib.load(src)
    nib.save(nib.Nifti1Image(np.asarray(img.dataobj, dtype=np.float32),
                             img.affine, img.header), dest)
    log.info(f'Converted {src} → {dest}')


def main():
    args = parse_args()

    # Write all registration outputs to a staging directory first.
    # Only on successful completion do we move them to the real out_dir.
    # This prevents Argo from uploading a partial artifact tarball when the
    # step fails — Argo collects output artifacts regardless of exit code.
    out_dir = Path(args.out_dir)
    staging = out_dir.parent / (out_dir.name + '.staging')
    staging.mkdir(parents=True, exist_ok=True)
    pfx_staging = staging / args.prefix

    log.info(f'Device:    {args.device}')
    log.info(f'Out dir:   {out_dir}  (staging: {staging})')
    log.info(f'T1w:       {args.t1w}')
    log.info(f'Template:  {args.template}')
    log.info(f'Brainmask: {args.brainmask or "not provided — using raw T1w"}')

    if args.device == 'cuda' and not torch.cuda.is_available():
        log.error('CUDA requested but not available')
        sys.exit(1)

    # Convert T1w to NIfTI if needed (e.g. orig.mgz from FastSurfer).
    t1w_path = args.t1w
    if not args.t1w.endswith(('.nii', '.nii.gz')):
        t1w_nii = str(pfx_staging) + '_T1w_orig.nii.gz'
        to_nifti(args.t1w, t1w_nii)
        t1w_path = t1w_nii

    # Apply brainmask to T1w before registration if provided.
    # Masking removes skull signal that would otherwise bias the optimizer,
    # particularly during the SyN stage near the brain boundary.
    if args.brainmask is not None:
        t1w_img  = nib.load(t1w_path)
        mask_img = nib.load(args.brainmask)   # nibabel reads MGZ natively
        masked   = t1w_img.get_fdata(dtype=np.float32) * (mask_img.get_fdata(dtype=np.float32) > 0)
        t1w_masked_path = str(pfx_staging) + '_T1w_brain.nii.gz'
        nib.save(nib.Nifti1Image(masked, t1w_img.affine, t1w_img.header), t1w_masked_path)
        log.info(f'Skull-stripped T1w saved: {t1w_masked_path}')
        t1w_path = t1w_masked_path

    # FireANTs convention: fixed = target space (MNI), moving = source (T1w)
    from fireants.io.image import Image, BatchedImages
    from fireants.registration.affine import AffineRegistration
    from fireants.registration.syn import SyNRegistration

    fixed  = BatchedImages([Image(sitk.ReadImage(args.template), device=args.device)])
    moving = BatchedImages([Image(sitk.ReadImage(t1w_path),      device=args.device)])

    # ------------------------------------------------------------------
    # Stage 1: Affine
    # ------------------------------------------------------------------
    log.info('Stage 1: affine registration')
    affine_reg = AffineRegistration(
        fixed_images=fixed,
        moving_images=moving,
        scales=args.affine_scales,
        iterations=args.affine_iterations,
        optimizer_lr=args.learning_rate,
    )
    affine_reg.optimize()

    affine_path = str(pfx_staging) + '_affine.mat'
    affine_reg.save_as_ants_transforms([affine_path])
    log.info(f'Affine saved: {affine_path}')

    # ------------------------------------------------------------------
    # Stage 2: SyN deformable, warm-started from affine
    # ------------------------------------------------------------------
    log.info('Stage 2: SyN deformable registration')
    syn_reg = SyNRegistration(
        fixed_images=fixed,
        moving_images=moving,
        scales=args.syn_scales,
        iterations=args.syn_iterations,
        optimizer_lr=args.learning_rate,
        init_affine=affine_reg.get_affine_matrix(),
    )
    syn_reg.optimize()

    warp_path   = str(pfx_staging) + '_warp.nii.gz'
    warped_path = str(pfx_staging) + '_warped.nii.gz'

    syn_reg.save_as_ants_transforms([warp_path])
    syn_reg.save_moved_images(moving, [warped_path])

    # ------------------------------------------------------------------
    # QC metrics
    # ------------------------------------------------------------------
    from datetime import datetime, timezone

    log.info('Computing QC metrics')
    template_sitk = sitk.ReadImage(args.template)
    warped_sitk   = sitk.ReadImage(warped_path)
    # FireANTs save_moved_images can output at the moving image voxel count
    # rather than the fixed (template) grid. Resample to template space so
    # NCC/Dice comparisons use matching arrays.
    if warped_sitk.GetSize() != template_sitk.GetSize():
        log.info(f'QC resampling: warped {warped_sitk.GetSize()} → template {template_sitk.GetSize()}')
        resampler = sitk.ResampleImageFilter()
        resampler.SetReferenceImage(template_sitk)
        resampler.SetInterpolator(sitk.sitkLinear)
        warped_sitk = resampler.Execute(warped_sitk)
    # sitk.GetArrayFromImage uses (z, y, x) order; consistent for both arrays
    template_data = sitk.GetArrayFromImage(template_sitk).astype(np.float32)
    warped_data   = sitk.GetArrayFromImage(warped_sitk).astype(np.float32)
    brain_mask    = template_data > 0

    qc = {
        'schema_version':    '1.0',
        'pipeline':          args.pipeline,
        'subject':           args.subj,
        'session':           args.ses,
        'registration_type': 't1w_to_mni',
        'ncc':               _ncc(template_data, warped_data, mask=brain_mask),
        'dice':              _dice(template_data, warped_data),
        **_jacobian_stats(warp_path),
        'task':              '',
        'run':               '',
        'completed_at':      datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    qc_path = str(pfx_staging) + '_qc.json'
    with open(qc_path, 'w') as fh:
        json.dump(qc, fh)
    log.info(f'QC: ncc={qc["ncc"]:.4f}  dice={qc["dice"]:.4f}  '
             f'jac_frac_neg={qc["jac_det_frac_negative"]:.6f}')

    # Verify all required outputs exist before promoting to the real output dir.
    for p in [affine_path, warp_path, qc_path]:
        if not Path(p).exists():
            log.error(f'Expected output not found: {p}')
            sys.exit(1)

    # Atomically promote: rename staging → out_dir.
    # out_dir may already exist (retry); remove it first.
    import shutil
    if out_dir.exists():
        shutil.rmtree(out_dir)
    staging.rename(out_dir)
    pfx = out_dir / args.prefix

    # Write flat metrics JSON to /tmp for Argo to upload as a separate artifact.
    if args.subj and args.ses:
        metrics_path = Path(f'/tmp/{args.subj}_{args.ses}_t1w_to_mni_reg_qc.json')
        metrics_path.write_text(json.dumps(qc))
        log.info(f'Registration QC metrics: {metrics_path}')

    log.info(f'Warp:       {out_dir / (args.prefix + "_warp.nii.gz")}')
    log.info(f'Warped T1w: {out_dir / (args.prefix + "_warped.nii.gz")}  (QC only)')
    log.info('Done.')


if __name__ == '__main__':
    main()
