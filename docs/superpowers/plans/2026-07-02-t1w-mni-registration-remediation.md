# T1w→MNI Registration + QC Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the (disproven) COM-init FireANTs affine with a validated SimpleITK scale-finding affine → pre-resample → FireANTs SyN recipe, apply the SyN warp via `get_warped_coordinates` (never the no-op `save_moved_images`), register against the 1mm MNI template while keeping BOLD output at 2mm, and re-validate offline.

**Architecture:** Testable CPU core in `images/shared/registration_qc.py` (lncc + composite verdict — **already done, Tasks 1–2**) and testable SimpleITK helpers in `fst1w_to_mni.py` (scale-finding affine + pre-resample, both CPU). GPU-only wiring (FireANTs SyN + `get_warped_coordinates`) lives in `main()` and is validated in the offline GPU run. The registration emits a two-transform stack (affine `.mat` + warp field) that downstream `preproc.py` already consumes.

**Tech Stack:** Python 3 (run as `python`), pytest, numpy, SimpleITK (CPU), torch + FireANTs (GPU image only), nibabel, scipy (lazy in `lncc`), kubectl/GPU pod for validation.

## Global Constraints

- **Interpreter:** run Python/pytest as `python` / `python -m pytest` (Homebrew); `python3` lacks deps.
- **Validated recipe (measured; worst fail sub-3NGLDPCG, 1mm template: mask_dice 0.72→0.987, lncc 0.19→0.885):**
  1. SimpleITK affine: `CenteredTransformInitializer(..., MOMENTS)` → `SetMetricAsMattesMutualInformation(50)`, random sampling 0.1 seed 42, `SetOptimizerAsGradientDescent(lr=1.0, iters=500, convergenceMinimumValue=1e-6, convergenceWindowSize=10)`, **`SetOptimizerScalesFromPhysicalShift()`**, shrink `[4,2,1]`, smooth `[2,1,0]`. Save via `sitk.WriteTransform` as `.mat`.
  2. Pre-resample masked moving (linear) and binary brainmask (nearest) onto the template grid via that affine.
  3. FireANTs `SyNRegistration(..., loss_type='cc', cc_kernel_size=5)` on the pre-aligned pair, identity init.
  4. Apply warp via `syn_reg.get_warped_coordinates(fixed, moving_pre)` + `torch.nn.functional.grid_sample` — **never `save_moved_images` (proven no-op: byte-identical output)**.
  5. Save warp via `syn_reg.save_as_ants_transforms([warp_path])` (real 3-component displacement field, template space).
- **FireANTs API (verified against fireants 1.5.0 in the pinned image):** `get_warped_coordinates(fixed, moving)` → `(1,D,H,W,3)` normalized coords for `grid_sample(..., align_corners=True)`; `BatchedImages([Image(sitk_img, device=...)])`; `m()` returns the moving tensor `(1,1,D,H,W)`. SimpleITK in the fireants image **cannot read `.mgz`** — convert via nibabel first.
- **Template:** register against 1mm `MNI152NLin2009cAsym_res-01_T1w_brain.nii` (S3 `config/`, 193×229×193, 1mm, origin [-96,-132,-78], RAS, brain-extracted). **BOLD output stays 2mm** — `preproc.py --mni-template` keeps the 2mm reference grid (separate parameter; no change needed to keep 2mm output).
- **QC composite thresholds (verbatim, already implemented):** `mask_dice` warn<0.93/fail<0.90; `lncc` warn<0.85/fail<0.75; `jac_det_frac_negative` warn>0.0005/fail>0.001; `centroid_displacement_mm` warn>1.0/fail>2.0. Verdict = fail if any fail, else warn if any warn, else pass. `schema_version` = `2.0`, keys `mask_dice`/`lncc`.
- **`registration_qc.py` is shared** by the FireANTs and FreeSurfer images. `lncc` imports scipy lazily; scipy only in the FireANTs Dockerfile.
- **Out of scope:** submitting the production workflow, re-running/invalidating existing derivatives. Rollout is a separate follow-up after offline validation passes.

## File Structure

- `images/shared/registration_qc.py` — **done (Tasks 1–2)**: `lncc()`, composite `verdict()`, thresholds.
- `images/fireANTs/scripts/fst1w_to_mni.py` — **remove** `com_init_rigid` + `_intensity_com_physical`; **add** `sitk_scale_affine()` + `preresample_to_grid()`; rewrite `main()` for the validated recipe.
- `tests/images/fireants/test_fst1w_to_mni.py` — **replace** COM-init tests with sitk-affine + pre-resample tests (CPU).
- `argo/workflows/cloudpipe_minproc/registration-workflow-template.yaml` — 1mm template S3 key + mount + `--template`.
- `scripts/investigations/t1w_mni_qc/` — validation harness (already present) + 1mm run.
- `images/freesurfer/bold_to_t1w.py` — audit for the same `save_moved_images` no-op (Task 7).

---

## Task 1: `lncc` metric — DONE

Already implemented and committed (`lncc()` in `registration_qc.py`, tests green). No action; listed for coverage. Ledger: complete.

## Task 2: Composite `verdict()` + thresholds — DONE

Already implemented and committed (composite gate over `mask_dice`/`lncc`/`jac_det_frac_negative`/`centroid_displacement_mm`, `schema_version` 2.0). No action. Ledger: complete.

---

## Task 3: Replace COM-init helpers with SimpleITK scale-finding affine + pre-resample

**Files:**
- Modify: `images/fireANTs/scripts/fst1w_to_mni.py` (remove `_intensity_com_physical`, `com_init_rigid`; add `sitk_scale_affine`, `preresample_to_grid`)
- Replace: `tests/images/fireants/test_fst1w_to_mni.py`

**Interfaces:**
- Produces: `sitk_scale_affine(fixed_sitk, moving_sitk) -> sitk.Transform` — a Mattes-MI affine (moments init + physical-shift optimizer scaling) mapping fixed→moving (ITK resampling pullback), robust at recovering global scale.
- Produces: `preresample_to_grid(moving_sitk, reference_sitk, transform, interp='linear'|'nearest') -> sitk.Image` — resample `moving_sitk` onto `reference_sitk`'s grid through `transform`.
- Consumed by: Task 4 `main()`.

- [ ] **Step 1: Replace the test file with CPU tests for the new helpers**

Overwrite `tests/images/fireants/test_fst1w_to_mni.py`:

> **Fixture note:** the synthetic phantom must be **asymmetric and textured**. A binary
> sphere is a degenerate Mattes-MI case (≈2-bin histogram → no gradient; the optimizer
> false-converges to a spurious transform) and is rotation-invariant (under-constrains an
> affine). An ellipsoid + smoothed-noise texture mimics real T1w/MNI intensity richness.
> The scale test asserts what the helper *guarantees* — scale recovery within tolerance
> plus an overlap improvement — not a high absolute dice (synthetic texture ceilings
> lower than real brains; high-fidelity alignment is proven on real data in Task 6).

```python
import numpy as np
import SimpleITK as sitk

from fst1w_to_mni import sitk_scale_affine, preresample_to_grid


def _textured_ellipsoid(radii=(30, 24, 20), shape=(96, 96, 96), spacing=(1.0, 1.0, 1.0), seed=7):
    """An asymmetric ellipsoid filled with deterministic smoothed-noise texture.

    Asymmetry constrains pose (a sphere is rotation-invariant and under-constrains
    an affine); the noise texture gives Mattes MI real gradient (a uniform blob is a
    degenerate ~2-bin histogram). This mimics the intensity richness of real T1w/MNI
    data far better than a binary sphere.
    """
    from scipy.ndimage import gaussian_filter
    zz, yy, xx = np.indices(shape).astype(np.float64)
    c = [(s - 1) / 2.0 for s in shape]
    q = ((zz-c[0])/radii[0])**2 + ((yy-c[1])/radii[1])**2 + ((xx-c[2])/radii[2])**2
    rng = np.random.default_rng(seed)
    tex = gaussian_filter(rng.standard_normal(shape), sigma=2.5)
    tex = 140.0 + 70.0 * (tex - tex.mean()) / tex.std()
    arr = np.where(q <= 1.0, tex, 0.0).astype(np.float32)
    img = sitk.GetImageFromArray(arr)
    img.SetSpacing(spacing)
    return img


def _mask_dice(fixed, moving, tf):
    warped = preresample_to_grid(moving, fixed, tf, interp='linear')
    f = sitk.GetArrayFromImage(fixed) > 1e-3
    w = sitk.GetArrayFromImage(warped) > 1e-3
    return 2 * (f & w).sum() / (f.sum() + w.sum())


def test_preresample_lands_on_reference_grid():
    fixed = _textured_ellipsoid()
    moving = _textured_ellipsoid(radii=(15, 12, 10))
    out = preresample_to_grid(moving, fixed, sitk.AffineTransform(3), interp='nearest')
    assert out.GetSize() == fixed.GetSize()
    assert out.GetSpacing() == fixed.GetSpacing()


def test_sitk_scale_affine_recovers_scale():
    # moving = fixed compressed by s>1 about its centre -> a smaller copy, same texture.
    # The affine must recover the inverse scale (~1/s) and improve overlap.
    fixed = _textured_ellipsoid()
    s = 1.25
    sc = sitk.ScaleTransform(3, (s, s, s))
    sc.SetCenter(fixed.TransformContinuousIndexToPhysicalPoint(
        [(d - 1) / 2.0 for d in fixed.GetSize()]))
    moving = sitk.Resample(fixed, fixed, sc, sitk.sitkLinear, 0.0)

    baseline = _mask_dice(fixed, moving, sitk.AffineTransform(3))
    tf = sitk_scale_affine(fixed, moving)
    recovered = _mask_dice(fixed, moving, tf)

    lin = np.array(tf.GetParameters()[:9]).reshape(3, 3)
    eff_scale = float(np.linalg.det(lin) ** (1 / 3))
    assert abs(eff_scale - 1 / s) < 0.06, f"scale not recovered: {eff_scale:.3f} vs {1/s:.3f}"
    assert recovered > baseline + 0.05, f"overlap not improved: {recovered:.3f} vs base {baseline:.3f}"


def test_sitk_scale_affine_returns_transform():
    tf = sitk_scale_affine(_textured_ellipsoid(), _textured_ellipsoid())
    assert isinstance(tf, sitk.Transform) or hasattr(tf, "GetParameters")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/images/fireants/test_fst1w_to_mni.py -v`
Expected: ERROR `ImportError: cannot import name 'sitk_scale_affine'`.

- [ ] **Step 3: Remove the COM-init helpers**

In `images/fireANTs/scripts/fst1w_to_mni.py`, delete `_intensity_com_physical` (lines ~106–123) and `com_init_rigid` (lines ~126–144) entirely.

- [ ] **Step 4: Add the SimpleITK helpers**

Add at module level (after `to_nifti`):

```python
def sitk_scale_affine(fixed_sitk: 'sitk.Image', moving_sitk: 'sitk.Image') -> 'sitk.Transform':
    """Mattes-MI affine (fixed→moving pullback) that reliably recovers global scale.

    Moments init + Mattes mutual information + SetOptimizerScalesFromPhysicalShift.
    The physical-shift scaling is what lets the affine find the true global scale
    for small / MNI-displaced brains where FireANTs' own affine under-fits. The
    returned transform maps FIXED (template) points to MOVING (T1w) points, i.e.
    the pullback used by ITK resampling and antsApplyTransforms.
    """
    fixed_sitk  = sitk.Cast(fixed_sitk,  sitk.sitkFloat32)
    moving_sitk = sitk.Cast(moving_sitk, sitk.sitkFloat32)
    init = sitk.CenteredTransformInitializer(
        fixed_sitk, moving_sitk, sitk.AffineTransform(3),
        sitk.CenteredTransformInitializerFilter.MOMENTS)
    reg = sitk.ImageRegistrationMethod()
    reg.SetMetricAsMattesMutualInformation(50)
    reg.SetMetricSamplingStrategy(reg.RANDOM)
    reg.SetMetricSamplingPercentage(0.1, seed=42)
    reg.SetInterpolator(sitk.sitkLinear)
    reg.SetOptimizerAsGradientDescent(
        learningRate=1.0, numberOfIterations=500,
        convergenceMinimumValue=1e-6, convergenceWindowSize=10)
    reg.SetOptimizerScalesFromPhysicalShift()
    reg.SetShrinkFactorsPerLevel([4, 2, 1])
    reg.SetSmoothingSigmasPerLevel([2, 1, 0])
    reg.SetInitialTransform(init, inPlace=False)
    return reg.Execute(fixed_sitk, moving_sitk)


def preresample_to_grid(moving_sitk: 'sitk.Image', reference_sitk: 'sitk.Image',
                        transform: 'sitk.Transform', interp: str = 'linear') -> 'sitk.Image':
    """Resample `moving_sitk` onto `reference_sitk`'s grid through `transform`."""
    rs = sitk.ResampleImageFilter()
    rs.SetReferenceImage(sitk.Cast(reference_sitk, sitk.sitkFloat32))
    rs.SetTransform(transform)
    rs.SetInterpolator(sitk.sitkNearestNeighbor if interp == 'nearest' else sitk.sitkLinear)
    return rs.Execute(sitk.Cast(moving_sitk, sitk.sitkFloat32))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/images/fireants/test_fst1w_to_mni.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add images/fireANTs/scripts/fst1w_to_mni.py tests/images/fireants/test_fst1w_to_mni.py
git commit -m "feat(reg): SimpleITK scale-finding affine + pre-resample helpers (replace COM-init)"
```

---

## Task 4: Rewrite `main()` for the validated recipe (sitk affine → pre-resample → SyN → get_warped_coordinates)

Integration task (GPU image; validated in Task 6). Edits `main()` and the module docstring.

**Files:**
- Modify: `images/fireANTs/scripts/fst1w_to_mni.py` (`main()`, docstring)

**Interfaces:**
- Consumes: `sitk_scale_affine`, `preresample_to_grid` (Task 3); `lncc`, `dice`, `centroid_displacement_mm`, `jacobian_stats`, `verdict` (registration_qc).
- Produces (unchanged names, so downstream is stable): `<prefix>_affine.mat`, `<prefix>_warp.nii.gz`, `<prefix>_warped.nii.gz`, `<prefix>_qc.json`.

- [ ] **Step 1: Replace the affine + SyN + warp-application block**

In `main()`, replace everything from `# FireANTs convention:` (the `fixed = BatchedImages(...)` construction, the COM-init lines, Stage 1 affine, Stage 2 SyN, and both `save_moved_images` calls — current lines ~192–255) with:

```python
    import torch.nn.functional as Fn
    from fireants.io.image import Image, BatchedImages
    from fireants.registration.syn import SyNRegistration

    template_sitk_in = sitk.Cast(sitk.ReadImage(args.template), sitk.sitkFloat32)
    moving_sitk_in   = sitk.Cast(sitk.ReadImage(t1w_path),      sitk.sitkFloat32)

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
    moving_pre_sitk = preresample_to_grid(moving_sitk_in, template_sitk_in, affine_tf, interp='linear')
    moving_pre_path = str(pfx_staging) + '_moving_pre.nii.gz'
    sitk.WriteImage(moving_pre_sitk, moving_pre_path)

    # ------------------------------------------------------------------
    # Stage 2: FireANTs SyN on the pre-aligned pair (identity init)
    # ------------------------------------------------------------------
    log.info('Stage 2: FireANTs SyN (local NCC) on pre-aligned pair')
    fixed      = BatchedImages([Image(sitk.ReadImage(args.template), device=args.device)])
    moving_pre = BatchedImages([Image(sitk.ReadImage(moving_pre_path), device=args.device)])
    syn_reg = SyNRegistration(
        fixed_images=fixed,
        moving_images=moving_pre,
        scales=args.syn_scales,
        iterations=args.syn_iterations,
        optimizer_lr=args.learning_rate,
        loss_type='cc',
        cc_kernel_size=5,
    )
    syn_reg.optimize()

    warp_path   = str(pfx_staging) + '_warp.nii.gz'
    warped_path = str(pfx_staging) + '_warped.nii.gz'
    syn_reg.save_as_ants_transforms([warp_path])

    # Apply the warp via get_warped_coordinates (save_moved_images is a no-op for
    # the SyN warp — it returns the input unchanged). Sample the pre-aligned brain
    # through the warped normalized coordinates.
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
            sitk.ReadImage(brainmask_bin_path), template_sitk_in, affine_tf, interp='nearest')
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
```

- [ ] **Step 2: Simplify the QC block (warped images are already on the template grid)**

The warped T1w and warped mask are now written on the template grid, so the QC-time resampling guards are no longer needed. Replace the QC block (`template_sitk = sitk.ReadImage(...)` through the `qc['mask_dice'] = ...` line) with:

```python
    template_sitk = sitk.ReadImage(args.template)
    template_data = sitk.GetArrayFromImage(template_sitk).astype(np.float32)
    warped_data   = sitk.GetArrayFromImage(sitk.ReadImage(warped_path)).astype(np.float32)
    brain_mask    = template_data > 0

    lncc_val = lncc(template_data, warped_data, brain_mask, radius=4)

    spacing = template_sitk.GetSpacing()  # (sx, sy, sz)
    affine_zyx = np.diag([float(spacing[2]), float(spacing[1]), float(spacing[0]), 1.0])
    centroid_disp = centroid_displacement_mm(template_data, warped_data, affine_zyx, brain_mask)

    qc = {
        'schema_version': '2.0',
        'pipeline': args.pipeline,
        'subject': args.subj,
        'session': args.ses,
        'registration_type': 't1w_to_mni',
        'lncc': lncc_val,
        **jacobian_stats(warp_path),
        'centroid_displacement_mm': centroid_disp,
        'task': '',
        'run': '',
        'completed_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    if args.brainmask is not None:
        warped_mask = sitk.GetArrayFromImage(sitk.ReadImage(warped_mask_path)) > 0.5
        qc['mask_dice'] = dice(brain_mask.astype(np.float32), warped_mask.astype(np.float32))

    qc['verdict'] = verdict(qc)
```

Keep the existing `from datetime import datetime, timezone` import (move it above this block if the diff requires).

- [ ] **Step 3: Update the module docstring**

In the top docstring, replace the "Outputs" list note and add a one-line description of the recipe:

```
Recipe: SimpleITK Mattes-MI affine (recovers global scale) → pre-resample onto the
template grid → FireANTs SyN (local NCC) → warp applied via get_warped_coordinates
(NOT save_moved_images, which no-ops the SyN warp). Emits a two-transform stack:
  <prefix>_affine.mat   SimpleITK/ITK affine (T1w → MNI, pullback)
  <prefix>_warp.nii.gz   FireANTs SyN displacement field (template space)
  <prefix>_warped.nii.gz T1w warped to MNI on the template grid (QC only)
```

- [ ] **Step 4: Verify import + unit tests still green**

Run: `python -m pytest tests/images/ -q`
Expected: all pass (edits are in `main()` runtime + docstring; helper/QC signatures unchanged; FireANTs imports remain inside `main()`).

- [ ] **Step 5: Commit**

```bash
git add images/fireANTs/scripts/fst1w_to_mni.py
git commit -m "feat(reg): sitk affine + pre-resample + SyN via get_warped_coordinates; drop save_moved_images"
```

---

## Task 5: Wire the 1mm template into the registration workflow

**Files:**
- Modify: `argo/workflows/cloudpipe_minproc/registration-workflow-template.yaml`

**Interfaces:**
- The `t1w-to-n` template step must mount the 1mm template from S3 `config/` and pass it as `--template`. `preproc.py --mni-template` is unchanged (keeps the 2mm output grid).

- [ ] **Step 1: Point the template artifact at the 1mm file**

In `registration-workflow-template.yaml`, in the `t1w-to-n` step, change the S3 input artifact key and mount path from the 2mm RAI file to the 1mm file:

- S3 key: `config/MNI152NLin2009cAsym_res-01_T1w_brain.nii`
- mount path: `/data/MNI152NLin2009cAsym_res-01_T1w_brain.nii`
- `--template /data/MNI152NLin2009cAsym_res-01_T1w_brain.nii`

(Match the exact artifact block style already in the file — same `s3:` / `key:` / `path:` structure as the current `n` artifact.)

- [ ] **Step 2: Confirm func-preproc still targets the 2mm output grid**

Inspect `functional-preprocessing-workflow-template.yaml`: the `--mni-template` / output-grid reference must remain the **2mm** template (unchanged). If and only if it shares the same artifact key as the registration step did, split it so registration=1mm and func-preproc output grid=2mm. Record the finding in the task report (no change if already separate).

- [ ] **Step 3: Lint the workflow templates**

Run the repo's Argo lint job locally if available (`argo lint argo/workflows/cloudpipe_minproc/registration-workflow-template.yaml`) or `yq` to confirm valid YAML:
`yq '.spec.templates[].name' argo/workflows/cloudpipe_minproc/registration-workflow-template.yaml`
Expected: parses; template names listed.

- [ ] **Step 4: Commit**

```bash
git add argo/workflows/cloudpipe_minproc/registration-workflow-template.yaml
git commit -m "feat(reg): register T1w against 1mm MNI template (BOLD output stays 2mm)"
```

---

## Task 6: Rebuild image + offline validation at 1mm (acceptance proof)

Execution task. Requires the reworked FireANTs image and a GPU pod. AWS SSO must be active — **on any auth error, stop and ask the user to `aws sso login`.**

**Files:**
- Append: `scripts/investigations/t1w_mni_qc/experiments.md` (results)

**Interfaces:**
- Consumes: reworked `fst1w_to_mni.py`, the 1mm template, the fail + regression session lists (`validate_comfix.py --emit-list`).

- [ ] **Step 1: Rebuild + push the validation image**

```bash
docker --config <isolated-dockercfg> buildx build --platform linux/amd64 \
  -f images/fireANTs/Dockerfile \
  -t public.ecr.aws/l9e7l1h1/cloudpipe/fireants:comfix-validation --push .
```
(ECR-public login: `aws ecr-public get-login-password --region us-east-1 | docker --config <cfg> login --username AWS --password-stdin public.ecr.aws`. The default credstore is broken → use an isolated `--config` dir containing `{}`.)

- [ ] **Step 2: Launch the GPU pod**

```bash
kubectl -n argo-workflows apply -f <scratch>/comfix-pod.yaml
kubectl -n argo-workflows wait --for=condition=Ready pod/comfix-validate --timeout=600s
```
Copy in the 1mm template once: `kubectl -n argo-workflows cp <scratch>/mni1mm.nii comfix-validate:/work/mni1mm.nii`.

- [ ] **Step 3: Run the reworked script on every fail + regression sample**

For each `subject ses` from `python scripts/investigations/t1w_mni_qc/validate_comfix.py --emit-list`: fetch the FastSurfer inputs from S3, `kubectl cp` orig.mgz + brainmask.mgz in, run `fst1w_to_mni.py --template /work/mni1mm.nii --brainmask ...`, and collect the `_qc.json`. Aggregate into `after.csv` (`subject,session,verdict,mask_dice,lncc,jac_det_frac_negative,centroid_displacement_mm`). Build `before.csv` from the existing S3 QC JSONs (verdict field).

- [ ] **Step 4: End-to-end transform composition check (one subject)**

For one recovered fail, apply the emitted stack to the ORIGINAL moving with a **2mm** reference and confirm it lands on the template:
`antsApplyTransforms -d 3 -i <orig_T1w> -r <2mm template> -t <prefix>_warp.nii.gz -t <prefix>_affine.mat -o /tmp/check.nii.gz`
Confirm `/tmp/check.nii.gz` is 2mm-gridded and brain-aligned (mask_dice vs 2mm template brain > 0.90). This proves the two-transform stack + separate output grid works downstream.

- [ ] **Step 5: Summarize + record**

```bash
python scripts/investigations/t1w_mni_qc/validate_comfix.py --summarize before.csv after.csv
```
Append to `experiments.md`: per-session old→new verdict + new metrics, the composition-check result, and the post-fix metric distribution (Part E threshold confirmation). **Acceptance:** 0 regressions AND 0 unresolved fails (or each exception documented with a hypothesis).

- [ ] **Step 6: Tear down + commit**

```bash
kubectl -n argo-workflows delete pod comfix-validate
git add scripts/investigations/t1w_mni_qc/experiments.md
git commit -m "test(reg): 1mm offline validation — fails recovered, no regressions"
```

---

## Task 7: Flag / audit the `save_moved_images` no-op elsewhere

**Files:**
- Inspect: `images/freesurfer/bold_to_t1w.py` (and any other `save_moved_images` caller)

- [ ] **Step 1: Find all callers**

Run: `rg -n 'save_moved_images' images/`
List every caller.

- [ ] **Step 2: Assess each**

For each caller: does it rely on the returned/saved image reflecting a **deformable** (SyN) warp? Affine-only registrations (e.g. a rigid/affine bold_to_t1w) are unaffected — `save_moved_images` applies the affine correctly; only the SyN warp is dropped. Record the assessment in the task report.

- [ ] **Step 3: Fix or file follow-up**

If a caller depends on a SyN warp being applied, switch it to `get_warped_coordinates` + `grid_sample` (same pattern as Task 4) and note it. If none do, record "no other affected callers" and leave a one-line code comment at the `fst1w_to_mni.py` warp-application site referencing the no-op so future readers don't reintroduce `save_moved_images`.

- [ ] **Step 4: Commit (if any change)**

```bash
git add -A && git commit -m "fix(reg): audit save_moved_images no-op across images"
```

---

## Self-Review Notes

- **Spec coverage:** Part A → Tasks 3 (sitk affine + pre-resample helpers) + 4 (main() recipe, get_warped_coordinates, two-transform output); Part B → Tasks 1–2 (done) + 4 Step 2 (QC block uses the correctly-warped images); Part C → Task 5 (1mm register / 2mm output) + Task 6 Step 4 (end-to-end composition proof); Part D → Task 6; Part E → Task 6 Step 5. save_moved_images risk → Task 7.
- **No stale COM-init:** Task 3 removes both COM helpers and their tests; Task 4 removes both `save_moved_images` calls and the COM-init wiring. After Task 4 no `com_init_rigid` / `save_moved_images` reference remains in `fst1w_to_mni.py`.
- **FireANTs API:** `get_warped_coordinates(fixed, moving)` + `grid_sample(align_corners=True)` and `save_as_ants_transforms` are the verified 1.5.0 calls. `m()` returns `(1,1,D,H,W)`.
- **Type consistency:** `sitk_scale_affine` / `preresample_to_grid` names match across Task 3 (def) and Task 4 (use); output filenames (`_affine.mat`, `_warp.nii.gz`, `_warped.nii.gz`) unchanged so downstream `preproc.py --t1w2mni-affine/--t1w2mni-warp` interface is stable.
- **Downstream unchanged where possible:** `preproc.py` already consumes affine+warp and a separate `--mni-template` output grid, so register-at-1mm/output-at-2mm needs only the registration template swap (Task 5) + the composition proof (Task 6 Step 4).
- **Threshold honesty:** Part E confirmation in Task 6; worst fail reaches 0.987/0.885 so 0.93/0.90 and 0.85/0.75 are expected to hold.
