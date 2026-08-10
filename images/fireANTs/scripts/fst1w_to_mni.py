#!/usr/bin/env python3
"""
t1w_to_mni.py — T1w → MNI152NLin2009cAsym registration via FireANTs SyN (GPU)

# Why are we using FireANTs here instead of SynthMorph?

FireANTs: This is where FireANTs excels. T1w to MNI requires high-precision
alignment of complex, high-resolution cortical folding patterns. Because
FireANTs is an iterative solver, it continuously minimizes the cost function
until convergence. You achieve the gold-standard diffeomorphic precision of
antsRegistration SyN, but it executes in seconds or minutes rather than hours.
For tracking longitudinal developmental morphological changes in a cohort-level
data set this mathematically guaranteed precision is highly desirable.

SynthMorph: SynthMorph is incredibly fast and robust to anatomical variations,
often skipping the need for rigorous skull-stripping or baseline affine
initializations. However, because it is a feed-forward neural network, it can
sometimes produce slightly smoother deformation fields that may under-fit the
ultra-fine, high-frequency details of the cortex compared to a dedicated iterative
diffeomorphic solver.

Winner: FireANTs (but see BOLD to T1w)

Accepts either a NIfTI or an MGZ file for --t1w and --brainmask. MGZ files
(e.g. orig.mgz, brainmask.mgz from FastSurfer) are read directly via nibabel,
eliminating the need for an mri_convert initContainer and a separate fastsurfer
image pull.

When --brainmask is supplied, it is applied to the T1w before registration to
remove skull signal that would otherwise bias the SyN optimizer.

The SyN stage runs fused CUDA kernels (`fireants_fused_ops`, built into this image)
when USE_FFO is true — the default in `fireants.interpolator`. USE_FFO=False selects
the non-fused torch path AND the non-fused loss, i.e. the exact pre-adoption
configuration, so the two arms are comparable from one image. Production runs
USE_FFO=False: the fused arm folds the warp under the library's DEFAULT approximate
gradient, which `resolve_syn_loss` therefore turns off. See #165 and
handoffs/fireants-fused-ops.md for the QC baseline any change here must clear.

Recipe: SimpleITK Mattes-MI affine (recovers global scale) → pre-resample onto the
template grid → FireANTs SyN (local NCC) → warp applied via get_warped_coordinates
(NOT the moved-image save helper, which no-ops the SyN warp). Emits a two-transform stack:
  <prefix>_affine.mat   SimpleITK/ITK affine (T1w → MNI, pullback)
  <prefix>_warp.nii.gz   FireANTs SyN displacement field (template space)
  <prefix>_warped.nii.gz T1w warped to MNI on the template grid (QC only)
"""

import argparse
import json
import logging
import os
import shutil
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import SimpleITK as sitk
from registration_qc import (
    centroid_displacement_mm,
    dice,
    inverse_consistency_error,
    jacobian_stats,
    lncc,
    verdict,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--t1w', required=True, help='T1w image in native space (NIfTI or MGZ)')
    p.add_argument(
        '--template', required=True, help='MNI152NLin2009cAsym skull-stripped template NIfTI'
    )
    p.add_argument('--out-dir', required=True, help='Output directory')
    p.add_argument('--prefix', required=True, help='Output filename prefix, e.g. sub-001_ses-00A')
    p.add_argument(
        '--brainmask',
        default=None,
        help='Brain mask (NIfTI or MGZ, e.g. FastSurfer brainmask.mgz). '
        'Applied to T1w before registration; improves alignment by removing skull signal.',
    )
    p.add_argument('--subj', default='', help='Subject ID written into RegistrationQC metrics JSON')
    p.add_argument(
        '--ses', default='', help='Session label written into RegistrationQC metrics JSON'
    )
    p.add_argument(
        '--pipeline',
        default='cloudpipe_minproc',
        help='Pipeline name written into RegistrationQC metrics JSON',
    )

    # Registration tuning. The affine stage is SimpleITK (self-tuning via
    # SetOptimizerScalesFromPhysicalShift), so only the SyN stage is exposed.
    # Defaults validated offline (10-subject 1mm sample): lr 0.25 + [100,100,100]
    # give lncc ~0.80 with jac_det_frac_negative ~0.0004-0.0013 (diffeomorphic);
    # higher lr / fewer smoothing folds the warp.
    #
    # The gate is jac_det_frac_negative > 0.005 (registration_qc.py). This comment
    # used to cite 0.001 as the bound, which is the value the 2026-07-23
    # recalibration REMOVED for sitting inside the healthy distribution — a normal
    # registration tripped it ~17% of the time. Quoting it here made the fused_ops
    # A/B (2026-08-05) look marginal when it was 2.6-3.8x over the real bound;
    # registration_qc.py is the source of truth for every threshold.
    p.add_argument('--syn-scales', nargs='+', type=int, default=[4, 2, 1])
    p.add_argument('--syn-iterations', nargs='+', type=int, default=[100, 100, 100])
    p.add_argument(
        '--learning-rate', type=float, default=0.25, help='optimizer_lr passed to SyNRegistration'
    )
    # Imported here, not at module scope: torch initialises CUDA on import, and
    # this module is imported by tests that never touch the GPU path (fireants
    # SyN is likewise deferred into main()). Keeping it local also means --help
    # no longer pays for CUDA init.
    import torch

    p.add_argument('--device', default='cuda' if torch.cuda.is_available() else 'cpu')
    return p.parse_args()


def to_nifti(src: str, dest: str) -> None:
    """Load any nibabel-supported image (NIfTI, MGZ, …) and save as NIfTI."""
    img = nib.load(src)
    # Do not pass img.header here — passing an MGHHeader to Nifti1Image causes
    # nibabel to produce incorrect sform/qform codes, which makes SimpleITK
    # ignore the affine and fall back to pixdim scaling with identity direction
    # cosines. Omitting the header lets nibabel generate a correct Nifti1Header
    # with sform_code=2 and qform_code=1 from the affine.
    nib.save(nib.Nifti1Image(np.asarray(img.dataobj, dtype=np.float32), img.affine), dest)
    log.info(f'Converted {src} → {dest}')


def sitk_scale_affine(fixed_sitk: 'sitk.Image', moving_sitk: 'sitk.Image') -> 'sitk.Transform':
    """Mattes-MI affine (fixed→moving pullback) that reliably recovers global scale.

    Moments init + Mattes mutual information + SetOptimizerScalesFromPhysicalShift.
    The physical-shift scaling is what lets the affine find the true global scale
    for small / MNI-displaced brains where FireANTs' own affine under-fits. The
    returned transform maps FIXED (template) points to MOVING (T1w) points, i.e.
    the pullback used by ITK resampling and antsApplyTransforms.

    Returns a flat AffineTransform. ImageRegistrationMethod.Execute yields a
    CompositeTransform wrapping the affine; written as-is that produces a
    CompositeTransform .mat that ITK/antsApplyTransforms and sitk.ReadTransform
    choke on ("parameter list size 3 instead of 0"). Flattening to a single
    AffineTransform emits a clean 12-parameter .mat.
    """
    fixed_sitk = sitk.Cast(fixed_sitk, sitk.sitkFloat32)
    moving_sitk = sitk.Cast(moving_sitk, sitk.sitkFloat32)
    init = sitk.CenteredTransformInitializer(
        fixed_sitk,
        moving_sitk,
        sitk.AffineTransform(3),
        sitk.CenteredTransformInitializerFilter.MOMENTS,
    )
    reg = sitk.ImageRegistrationMethod()
    reg.SetMetricAsMattesMutualInformation(50)
    # NONE (every voxel centre), not RANDOM/REGULAR. Both sampling strategies draw
    # their point set through a thread-partitioned RNG, so the set — and hence where
    # the affine lands — is a function of the ITK thread count, NOT just the seed.
    # `seed=42` buys reproducibility only at a FIXED thread count. When #107 coupled
    # ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS to requests.cpu (2 -> 1), that moved the
    # sampled set and t1w-to-mni QC failures went 1.1% -> 12.3% of sessions (#138).
    #
    # Measured on sub-HGNA569Y/ses-00A (the flipped session), varying only the thread
    # count — RANDOM 0.1: lncc .645/.716/.702/.159/.275 at 1/2/4/8/16 threads. There is
    # no safe constant to pin, so the fix is to remove the sampled set as a variable
    # rather than to freeze the thread count. NONE: lncc .77511/.77507/.77506 at
    # 1/4/8 threads — a 5e-5 spread against RANDOM's 0.49, all passing, jac_neg ~0.0010.
    #
    # REGULAR does NOT work here: it still "randomly perturbs from center" within each
    # sampled voxel (SimpleITK registration overview), and measured WORSE than RANDOM
    # (8.6x lncc spread at 0.2). Residual multithread nondeterminism remains — Mattes MI
    # merges per-thread partial sums, so it is never bit-reproducible — but with NONE
    # that residue is ~2e-5 mm of translation, six orders of magnitude below the
    # ~20 mm the sampled set was contributing.
    #
    # Costs ~+17 s of CPU on a step averaging 0.5 min. Thread count is now purely a
    # performance knob, so #107's cpu: 1 GPU-packing win is kept, not reverted.
    reg.SetMetricSamplingStrategy(reg.NONE)
    reg.SetInterpolator(sitk.sitkLinear)
    reg.SetOptimizerAsGradientDescent(
        learningRate=1.0,
        numberOfIterations=500,
        convergenceMinimumValue=1e-6,
        convergenceWindowSize=10,
    )
    reg.SetOptimizerScalesFromPhysicalShift()
    reg.SetShrinkFactorsPerLevel([4, 2, 1])
    reg.SetSmoothingSigmasPerLevel([2, 1, 0])
    reg.SetInitialTransform(init, inPlace=False)
    final = reg.Execute(fixed_sitk, moving_sitk)
    flat = sitk.CompositeTransform(final)
    flat.FlattenTransform()
    return sitk.AffineTransform(flat.GetNthTransform(0))


def resolve_syn_loss(use_ffo: bool, env: dict) -> tuple[str, dict]:
    """Pick the SyN loss and its params for the arm `use_ffo` selects.

    Returns ``(loss_type, loss_params)`` for SyNRegistration.

    `USE_FFO=False` is the pre-fused_ops configuration exactly: the non-fused 'cc'
    loss, which takes the exact autograd gradient and has no approximation switch.

    `USE_FFO=True` selects 'fusedcc' AND, by default, turns the library's gradient
    approximation OFF. `FusedLocalNormalizedCrossCorrelationLoss.__init__` defaults
    to ``use_ants_gradient=True``, whose backward pass skips a convolution and so
    "ignore[s] interactions from other neighboring pixels" (fusedcc.py's own comment
    calls the other branch "compute correct gradients"). That neighbour coupling is
    the spatial smoothing that keeps a deformation locally consistent, and dropping
    it is the measured cause of the 2026-08-05 A/B failure: `jac_det_frac_negative`
    0.0129-0.0191 against a 0.005 fail bound, i.e. 1.3-1.9% of brain voxels folded,
    while `lncc` went UP by +0.078 (#163, #165). Defaulting it off makes
    'cc' -> 'fusedcc' a like-for-like swap of the GRADIENT as well as the metric —
    which is what the fused arm has to be before it can be considered for adoption.

    FFO_ANTS_GRADIENT=True restores the library default (the approximate gradient).
    It exists so a three-arm A/B — off / fused-approx / fused-exact — runs from ONE
    image on env vars alone, the same property that let #163 avoid a pin commit
    between arms. Do not set it in production.

    `use_ants_gradient` is passed only on the fused arm: `loss_params` reaches
    BOTH branches of abstract.py's `loss_type == 'fusedcc'` block (the fused loss
    and its ImportError fallback to the non-fused one), and the non-fused loss does
    not accept the kwarg. That fallback is already unreachable here — main() exits
    when FFO_AVAILABLE is False — so this only decides whether a broken image fails
    with a TypeError or runs silently unfused. Fail.
    """
    if not use_ffo:
        return 'cc', {}
    approximate = env.get('FFO_ANTS_GRADIENT', 'False').lower() == 'true'
    return 'fusedcc', {'use_ants_gradient': approximate}


def preresample_to_grid(
    moving_sitk: 'sitk.Image',
    reference_sitk: 'sitk.Image',
    transform: 'sitk.Transform',
    interp: str = 'linear',
) -> 'sitk.Image':
    """Resample `moving_sitk` onto `reference_sitk`'s grid through `transform`."""
    rs = sitk.ResampleImageFilter()
    rs.SetReferenceImage(sitk.Cast(reference_sitk, sitk.sitkFloat32))
    rs.SetTransform(transform)
    rs.SetInterpolator(sitk.sitkNearestNeighbor if interp == 'nearest' else sitk.sitkLinear)
    return rs.Execute(sitk.Cast(moving_sitk, sitk.sitkFloat32))


def finalize_outputs(staging, out_dir, qc, required_paths, metrics_path=None):
    """Emit observability outputs, then promote staging -> out_dir ONLY on a non-fail verdict.

    Source-of-truth QC gate (R2): a QC 'fail' must leave NO completion marker in
    out_dir — and therefore none in S3 — so that a resubmit re-registers instead of
    skipping onto a QC-failed transform. `inventory.check_t1w_completion` treats the
    presence of the promoted ``_affine.mat`` as "t1w-to-mni done"; the master gate then
    lets functional-preprocessing run on a 'Skipped' registration. By exiting BEFORE the
    promote on fail, that marker never exists.

    The metrics file lives under /tmp (never out_dir), so it is written from the
    in-memory ``qc`` dict up front and uploaded even on a fail — QC dashboards still
    record the failure without any file acting as a completion marker.

    Exits 65 on a 'fail' verdict (excluded from the Argo retry policy) and 1 if a
    required staged output is missing.
    """
    staging = Path(staging)
    out_dir = Path(out_dir)

    # A missing staged output means the run did not actually produce its results.
    for p in required_paths:
        if not Path(p).exists():
            log.error(f'Expected output not found: {p}')
            sys.exit(1)

    # Observability outputs (from the in-memory qc dict, not from out_dir) — emitted
    # before promotion so they upload even when the verdict is 'fail'.
    if metrics_path is not None:
        Path(metrics_path).write_text(json.dumps(qc))
        log.info(f'Registration QC metrics: {metrics_path}')

    if qc['verdict'] == 'fail':
        log.error(
            'QC verdict: FAIL — registration quality below acceptable thresholds. '
            'Exiting 65 WITHOUT promoting outputs (no completion marker uploaded to S3, '
            'so a resubmit will re-register instead of skipping this session).'
        )
        sys.exit(65)

    # pass / warn: atomically promote staging -> out_dir (out_dir may exist on retry).
    if out_dir.exists():
        shutil.rmtree(out_dir)
    staging.rename(out_dir)
    log.info(f'Promoted outputs to {out_dir}')


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

    import torch

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
        t1w_img = nib.load(t1w_path)
        mask_img = nib.load(args.brainmask)  # nibabel reads MGZ natively
        masked = t1w_img.get_fdata(dtype=np.float32) * (mask_img.get_fdata(dtype=np.float32) > 0)
        t1w_masked_path = str(pfx_staging) + '_T1w_brain.nii.gz'
        nib.save(nib.Nifti1Image(masked, t1w_img.affine, t1w_img.header), t1w_masked_path)
        log.info(f'Skull-stripped T1w saved: {t1w_masked_path}')
        t1w_path = t1w_masked_path

        brainmask_bin_path = str(pfx_staging) + '_brainmask_bin.nii.gz'
        nib.save(
            nib.Nifti1Image(
                (mask_img.get_fdata(dtype=np.float32) > 0).astype(np.float32), t1w_img.affine
            ),
            brainmask_bin_path,
        )

    import torch.nn.functional as Fn
    from fireants.io.image import BatchedImages, Image
    from fireants.registration.syn import SyNRegistration

    template_sitk_in = sitk.Cast(sitk.ReadImage(args.template), sitk.sitkFloat32)
    moving_sitk_in = sitk.Cast(sitk.ReadImage(t1w_path), sitk.sitkFloat32)

    # ------------------------------------------------------------------
    # Stage 1: scale-finding affine in SimpleITK (fixed→moving pullback)
    # ------------------------------------------------------------------
    log.info('Stage 1: SimpleITK Mattes-MI affine (physical-shift scale)')
    affine_tf = sitk_scale_affine(template_sitk_in, moving_sitk_in)
    affine_path = str(pfx_staging) + '_affine.mat'
    sitk.WriteTransform(affine_tf, affine_path)
    log.info(f'Affine saved: {affine_path}')

    # Pre-resample the masked moving brain onto the template grid so SyN sees a
    # well-overlapping, matched-resolution pair (this is what makes the SyN warp
    # meaningful — warm-starting SyN via init_affine in native space under-warps).
    moving_pre_sitk = preresample_to_grid(
        moving_sitk_in, template_sitk_in, affine_tf, interp='linear'
    )
    moving_pre_path = str(pfx_staging) + '_moving_pre.nii.gz'
    sitk.WriteImage(moving_pre_sitk, moving_pre_path)

    # ------------------------------------------------------------------
    # Stage 2: FireANTs SyN on the pre-aligned pair (identity init)
    # ------------------------------------------------------------------
    # fused_ops arm selection, driven entirely by the USE_FFO env var (see the
    # Argo template). `fireants.interpolator` builds its GridSampleDispatcher at
    # IMPORT time from USE_FFO, and that one switch flips the whole SyN inner loop —
    # grid sampling and warp composition, not just the loss. So the loss has to
    # follow the same switch: passing 'fusedcc' under USE_FFO=False would leave the
    # loss fused while the interpolator was not, and the control arm of an A/B has
    # to be the pre-fused_ops configuration exactly.
    #
    # 'cc' -> 'fusedcc' is a like-for-like swap of implementation, not of metric:
    # cc_kernel_type already defaults to 'rectangular' (box), which is also
    # fusedcc's only kernel. cc_kernel_size stays 5. It is NOT like-for-like on the
    # gradient unless use_ants_gradient is turned off — see resolve_syn_loss().
    from fireants.interpolator import FFO_AVAILABLE, USE_FFO

    if USE_FFO and not FFO_AVAILABLE:
        # Assert; do not trust the log. fireants degrades a missing extension to a
        # logged *warning* in two separate places — the interpolator import above,
        # and abstract.py's ImportError catch around 'fusedcc' — so an image whose
        # CUDA extension failed to build yields a slower run that still exits 0 and
        # looks entirely successful.
        log.error(
            'USE_FFO is set but fireants_fused_ops is not importable: the fused CUDA '
            'extension is missing or was built for a different arch/libtorch than '
            'this image provides. Refusing to run silently unfused — set '
            'USE_FFO=False to select the non-fused path deliberately.'
        )
        sys.exit(1)
    loss_type, loss_params = resolve_syn_loss(USE_FFO, os.environ)
    log.info(
        f'fused_ops: USE_FFO={USE_FFO} FFO_AVAILABLE={FFO_AVAILABLE} '
        f'loss_type={loss_type} loss_params={loss_params}'
    )

    log.info('Stage 2: FireANTs SyN (local NCC) on pre-aligned pair')
    fixed = BatchedImages([Image(sitk.ReadImage(args.template), device=args.device)])
    moving_pre = BatchedImages([Image(sitk.ReadImage(moving_pre_path), device=args.device)])
    syn_reg = SyNRegistration(
        fixed_images=fixed,
        moving_images=moving_pre,
        scales=args.syn_scales,
        iterations=args.syn_iterations,
        optimizer_lr=args.learning_rate,
        loss_type=loss_type,
        loss_params=loss_params,
        cc_kernel_size=5,
    )
    syn_reg.optimize()

    warp_path = str(pfx_staging) + '_warp.nii.gz'
    warped_path = str(pfx_staging) + '_warped.nii.gz'
    syn_reg.save_as_ants_transforms([warp_path])

    # Inverse warp, for the inverse-consistency QC check below. SyN optimises to a
    # midpoint space (phi = phi1 o phi2^-1), so the inverse is a first-class product
    # of the fit rather than something we numerically invert — inverting it here
    # would make the QC metric partly a measure of our own inversion error.
    #
    # QC-only, so it must never take the registration down with it: `fireants` is
    # installed unpinned, and if a future version drops or renames save_inverse we
    # want a logged warning and a missing metric, not a failed subject. verdict()
    # ignores threshold keys that are absent, so ICE simply does not gate when this
    # fails — deliberately fail-open, hence the loud log.
    inv_warp_path = str(pfx_staging) + '_inverse_warp.nii.gz'
    try:
        syn_reg.save_as_ants_transforms([inv_warp_path], save_inverse=True)
    except Exception as exc:  # noqa: BLE001 — any failure here must stay non-fatal
        log.warning(
            f'Could not save inverse warp ({exc!r}); inverse-consistency QC will be '
            'skipped and its threshold will not be evaluated for this session.'
        )
        inv_warp_path = None

    # Apply the warp via get_warped_coordinates (the moved-image save helper is a
    # no-op for the SyN warp — it returns the input unchanged). Sample the
    # pre-aligned brain through the warped normalized coordinates.
    coords = syn_reg.get_warped_coordinates(fixed, moving_pre)
    warped_t = Fn.grid_sample(moving_pre(), coords, mode='bilinear', align_corners=True)
    warped_np = warped_t.detach().cpu().numpy()[0, 0]  # (z, y, x) on template grid
    tmpl_ref = sitk.ReadImage(args.template)
    warped_img = sitk.GetImageFromArray(warped_np.astype(np.float32))
    warped_img.CopyInformation(tmpl_ref)
    sitk.WriteImage(warped_img, warped_path)

    # Warp the binary brainmask the same way (nearest via pre-resample, then warp).
    warped_mask_path = str(pfx_staging) + '_warped_mask.nii.gz'
    if args.brainmask is not None:
        mask_pre_sitk = preresample_to_grid(
            sitk.ReadImage(brainmask_bin_path), template_sitk_in, affine_tf, interp='nearest'
        )
        mask_pre_path = str(pfx_staging) + '_mask_pre.nii.gz'
        sitk.WriteImage(mask_pre_sitk, mask_pre_path)
        mask_pre = BatchedImages([Image(sitk.ReadImage(mask_pre_path), device=args.device)])
        mcoords = syn_reg.get_warped_coordinates(fixed, mask_pre)
        warped_mask_t = Fn.grid_sample(mask_pre(), mcoords, mode='nearest', align_corners=True)
        wm_np = warped_mask_t.detach().cpu().numpy()[0, 0]
        wm_img = sitk.GetImageFromArray((wm_np > 0.5).astype(np.float32))
        wm_img.CopyInformation(tmpl_ref)
        sitk.WriteImage(wm_img, warped_mask_path)
        log.info(f'Warped brainmask: {warped_mask_path}')

    # ------------------------------------------------------------------
    # QC metrics
    # ------------------------------------------------------------------
    from datetime import datetime, timezone

    log.info('Computing QC metrics')
    template_sitk = sitk.ReadImage(args.template)
    template_data = sitk.GetArrayFromImage(template_sitk).astype(np.float32)
    warped_data = sitk.GetArrayFromImage(sitk.ReadImage(warped_path)).astype(np.float32)
    brain_mask = template_data > 0

    lncc_val = lncc(template_data, warped_data, brain_mask, radius=4)

    spacing = template_sitk.GetSpacing()  # (sx, sy, sz)
    affine_zyx = np.diag([float(spacing[2]), float(spacing[1]), float(spacing[0]), 1.0])
    centroid_disp = centroid_displacement_mm(template_data, warped_data, affine_zyx, brain_mask)

    qc = {
        # 2.1 adds the log-Jacobian distribution (log_jac_*) AND restricts every
        # Jacobian statistic to brain voxels via the template mask. jac_det_* is
        # therefore not comparable across the 2.0/2.1 boundary: 2.0 values are
        # whole-field and include unconstrained background extrapolation.
        'schema_version': '2.1',
        'pipeline': args.pipeline,
        'subject': args.subj,
        'session': args.ses,
        'registration_type': 't1w_to_mni',
        'lncc': lncc_val,
        # Masked to the template brain, not the whole field: the warp outside the
        # brain is extrapolation, and its extremes would dominate the
        # log_jac_frac_* fractions. Passed as a path (not the SimpleITK
        # `brain_mask` above) so jacobian_stats reads it with nibabel in the
        # warp's own (x, y, z) order — brain_mask is (z, y, x).
        **jacobian_stats(warp_path, mask_path=args.template),
        'centroid_displacement_mm': centroid_disp,
        'task': '',
        'run': '',
        'completed_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    # Inverse consistency: forward∘inverse round-trip residual, in mm. Masked to
    # the template brain for the same reason the Jacobian stats are. Non-fatal for
    # the same reason the inverse save is — see the note there.
    if inv_warp_path is not None:
        try:
            qc.update(inverse_consistency_error(warp_path, inv_warp_path, mask_path=args.template))
        except Exception as exc:  # noqa: BLE001 — QC must not fail the registration
            log.warning(
                f'Inverse-consistency QC failed ({exc!r}); its threshold will not be '
                'evaluated for this session.'
            )

    if args.brainmask is not None:
        warped_mask = sitk.GetArrayFromImage(sitk.ReadImage(warped_mask_path)) > 0.5
        qc['mask_dice'] = dice(brain_mask.astype(np.float32), warped_mask.astype(np.float32))

    qc['verdict'] = verdict(qc)

    qc_path = str(pfx_staging) + '_qc.json'
    with open(qc_path, 'w') as fh:
        json.dump(qc, fh)
    mask_dice_str = f'{qc["mask_dice"]:.4f}' if 'mask_dice' in qc else 'n/a'
    ice_str = f'{qc["ice_mean_mm"]:.3f}/{qc["ice_p95_mm"]:.3f}mm' if 'ice_mean_mm' in qc else 'n/a'
    log.info(
        f'QC: mask_dice={mask_dice_str}  lncc={qc["lncc"]:.4f}  '
        f'jac_frac_neg={qc["jac_det_frac_negative"]:.6f}  '
        f'log_jac=[{qc["log_jac_p01"]:.2f},{qc["log_jac_p99"]:.2f}]p1-99 '
        f'beyond1.5={qc["log_jac_frac_beyond_1p5"]:.4f} '
        f'beyond3={qc["log_jac_frac_beyond_3"]:.6f}  '
        f'ice(mean/p95)={ice_str}  '
        f'centroid={qc["centroid_displacement_mm"]:.2f}mm  verdict={qc["verdict"]}'
    )

    # Emit observability outputs, then promote staging -> out_dir ONLY if QC did not
    # fail. On a 'fail' verdict this exits 65 BEFORE promoting, so no completion marker
    # (_affine.mat) reaches out_dir/S3 and a resubmit re-registers this session.
    metrics_path = (
        Path(f'/tmp/{args.subj}_{args.ses}_t1w_to_mni_reg_qc.json')
        if args.subj and args.ses
        else None
    )
    finalize_outputs(
        staging,
        out_dir,
        qc,
        required_paths=[affine_path, warp_path, qc_path],
        metrics_path=metrics_path,
    )
    log.info(f'Warp:       {out_dir / (args.prefix + "_warp.nii.gz")}')
    log.info(f'Warped T1w: {out_dir / (args.prefix + "_warped.nii.gz")}  (QC only)')
    log.info('Done.')


if __name__ == '__main__':
    main()
