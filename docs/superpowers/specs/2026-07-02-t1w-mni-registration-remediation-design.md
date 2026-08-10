# T1w→MNI Registration + QC Remediation — Design

**Date:** 2026-07-02 (revised 2026-07-03 after offline GPU validation)
**Status:** Revised — registration approach replaced; QC composite retained
**Pipeline:** `cloudpipe_minproc`
**Precursor:** [investigation findings](../../investigations/2026-07-02-t1w-mni-registration-failures.md)

## Revision note (2026-07-03)

The original design proposed a **center-of-mass init** for the FireANTs affine (Part A).
Offline GPU validation **disproved** it: COM init changes only translation, and the failures
are a **scale** problem, so it left the worst fail unchanged (mask_dice 0.72). Deeper
investigation on the pinned image (fireants 1.5.0) established three compounding root causes
and a validated fix, described below. **Part B (composite QC) was validated and is unchanged.**

## Context — root causes (validated on GPU pod `comfix-validate`)

1. **`save_moved_images` does not apply the SyN warp.** Its output is byte-identical to the
   input (`sumabsdiff == 0.0`). The production script uses it to write the warped QC image, so
   the deformable stage's QC was silently **affine-only**. The warp must be applied via
   `get_warped_coordinates(fixed, moving)` + `torch.nn.functional.grid_sample`. `save_as_ants_transforms`
   (the displacement field written for downstream) is unaffected and correct.
2. **Wrong template resolution.** The pipeline registered against a **2mm** MNI template
   (`nNLin2009cAsym_T1w_brain_res-2_RAI.nii`). The moving ABCD T1w is 1mm; matched to a **1mm**
   template the metrics are sharper and the registration scores higher. Correct template:
   `s3://<YOUR_S3_BUCKET>/config/MNI152NLin2009cAsym_res-01_T1w_brain.nii` (193×229×193, 1mm iso, origin
   [-96,-132,-78], RAS, brain-extracted).
3. **FireANTs `AffineRegistration` handoff is lossy for the deformable warm-start.** The
   library's own `MomentsRegistration(perform_scaling=True)` is *correct* (finds effective
   scale ~0.82, mask_dice 0.947 alone), but the all-FireANTs native chain
   (moments→affine→SyN via `init_affine`) plateaus at mask_dice 0.937 / lncc 0.39 because SyN
   warm-started in native space produces a weak warp. **SimpleITK produces a faithful,
   savable affine and, via pre-resampling, gives SyN matched-resolution gradients.**

## Goal

1. Make T1w→MNI registrations conform to the template for the failing subjects, without
   regressing current passes.
2. Replace the single intensity-dice QC gate with a robust boolean composite (overlap +
   texture + deformation + geometry).
3. Prove both offline on the batch's failing sessions plus a pass/warn regression sample
   before rebuilding the image or re-running the pipeline.

**Out of scope (separate follow-up):** rebuilding the `fireants` image, submitting the
production workflow, and re-running/invalidating existing derivatives. Rollout is planned
after offline validation passes.

## Part A — Registration changes (`images/fireANTs/scripts/fst1w_to_mni.py`)

Replace the FireANTs-affine + `save_moved_images` flow with the **validated recipe**
(worst fail sub-3NGLDPCG: mask_dice 0.72 → **0.987**, lncc 0.19 → **0.885**):

1. **Scale-finding affine in SimpleITK.** On the masked moving brain (orig × brainmask) and the
   brain-extracted 1mm template:
   - init: `CenteredTransformInitializer(..., MOMENTS)`
   - metric: `SetMetricAsMattesMutualInformation(50)`, random sampling 0.1 (fixed seed)
   - optimizer: gradient descent, `SetOptimizerScalesFromPhysicalShift()` (this is what finds
     global scale robustly across FOV/neck variance), shrink `[4,2,1]`, smooth `[2,1,0]`
   - **Save the affine as an ITK/ANTs `.mat`** (`sitk.WriteTransform`) — a genuine ANTs
     transform for downstream.
2. **Pre-resample** the masked moving brain and the brainmask onto the template grid via the
   affine (linear for the brain, nearest-neighbour for the mask). This yields a pre-aligned
   pair on the template's 1mm grid.
3. **SyN in FireANTs** on the pre-aligned pair, `loss_type='cc'` (local NCC, `cc_kernel_size=5–7`),
   identity init. Save the warp via `save_as_ants_transforms` (real displacement field, ~mm).
4. **Apply the warp via `get_warped_coordinates(fixed, moving_pre) + grid_sample`** to produce
   the warped T1w and warped brainmask used for QC — **never `save_moved_images`**.

**Transform output = two-transform stack.** Downstream applies `-t <warp> -t <affine.mat>`
(ITK composes right-to-left: affine then warp). No physical↔normalized-grid bridge is needed.

**API facts (verified against fireants 1.5.0 in the pinned image):**
- `SyNRegistration(..., loss_type='cc', cc_kernel_size=N).get_warped_coordinates(f, m)` returns
  `(1, D, H, W, 3)` normalized sampling coords for `F.affine_grid`-style `grid_sample`.
- `save_as_ants_transforms([path])` writes a 3-component displacement field in template space.
- SimpleITK in the fireants image cannot read `.mgz` — convert mgz→nifti via nibabel first
  (as the existing `to_nifti` helper does).

## Part B — QC overhaul (`images/shared/registration_qc.py` + the QC block) — VALIDATED, UNCHANGED

Boolean composite. `verdict()` returns **fail** if any fail condition trips, else **warn** if
any warn condition trips, else **pass**:

| metric | computation | warn | fail |
|---|---|---|---|
| `mask_dice` | warp the FastSurfer brainmask into template space (nearest-neighbour); Dice against the MNI template brain | < 0.93 | < 0.90 |
| `lncc` | local normalized cross-correlation (window radius 4) between warped T1w and template, within the template brain mask | < 0.85 | < 0.75 |
| `jac_det_frac_negative` | fraction of negative Jacobian determinants in the SyN warp | > 0.0005 | > 0.001 |
| `centroid_displacement_mm` | intensity-weighted centroid shift, warped vs template, within the brain mask | > 1.0 | > 2.0 |

- **Dropped from the gate:** intensity-`>0` `dice` and MI (MI remains an affine cost only).
- The warped brainmask and warped T1w feeding `mask_dice` / `lncc` now come from the
  `get_warped_coordinates` path (Part A item 4), not `save_moved_images`.
- Retain raw metric values in the QC JSON; `schema_version` bumped (keys `dice`→`mask_dice`,
  add `lncc`).

## Part C — Template + output resolution (workflow config)

- **Registration/QC against the 1mm template.** Update `registration-workflow-template.yaml`
  to load `MNI152NLin2009cAsym_res-01_T1w_brain.nii` from S3 `config/` and pass it as the
  template.
- **Final BOLD stays 2mm.** `functional-preprocessing-workflow-template.yaml` applies the
  T1w→MNI transform stack with a **2mm reference grid** (`antsApplyTransforms -r <2mm ref>`),
  so functional output resolution and storage are unchanged. The transforms are continuous, so
  the reference grid sets the output resolution independently of the 1mm registration.

## Part D — Offline validation (GPU pod, no pipeline submission)

- Run the **reworked** `fst1w_to_mni.py` against the **1mm** template on:
  - all failing sessions' inputs (orig.mgz + brainmask.mgz from S3), and
  - a regression sample of passing and warning sessions (≥5 each).
- Compute all four metrics before (existing S3 outputs) and after.
- **Acceptance criteria:**
  1. Every fail **conforms** (clears fail thresholds; target mask_dice ≥ 0.93, lncc ≥ 0.85) and
     receives a non-fail verdict.
  2. **No regression:** every sampled current pass remains pass; no sampled session degrades to
     fail.
  3. Any session missing (1) is documented with metrics and a hypothesis.
- Mechanism: debug GPU pod (`gpu-nodepool`), inputs copied via `kubectl cp`.

## Part E — Threshold confirmation

Adopt the Part-B thresholds, then **confirm against the post-fix 1mm distribution**. The
validated ceiling is high (worst fail reaches mask_dice 0.987 / lncc 0.885), so 0.93/0.90 and
0.85/0.75 are expected to hold; adjust only if the fixed-registration distribution says
otherwise, and record the rationale.

## Files

- `images/fireANTs/scripts/fst1w_to_mni.py` — SimpleITK scale-finding affine (save `.mat`),
  pre-resample, FireANTs SyN, warp via `get_warped_coordinates`, warp the brainmask for QC.
  **Remove the COM-init helpers and all `save_moved_images` calls.**
- `images/shared/registration_qc.py` — validated `lncc` + `mask_dice`, threshold table,
  composite `verdict()`, `schema_version` bump. (Already implemented; retained.)
- `argo/workflows/cloudpipe_minproc/registration-workflow-template.yaml` — 1mm template key +
  path; emit both transforms (affine `.mat` + warp).
- `argo/workflows/cloudpipe_minproc/functional-preprocessing-workflow-template.yaml` — apply
  the two-transform stack with a 2mm reference grid.
- `tests/images/registration_qc/test_registration_qc.py` — composite verdict boundary cases.
- Validation harness under `scripts/investigations/t1w_mni_qc/`.

## Risks

- **`save_moved_images` used elsewhere.** Any other caller (e.g. `images/freesurfer/bold_to_t1w.py`)
  may have the same silent no-op — flag and audit as follow-up.
- **Two-transform composition downstream.** The affine `.mat` + warp order and reference grid
  in `antsApplyTransforms` must be validated end-to-end (warp original moving → template).
- **lncc sensitivity.** lncc is sensitive to resampling/intensity; Part E keeps thresholds
  honest against the post-fix distribution.
- **Coupling** — registration, QC, and template change ship together; they cannot be validated
  independently.
