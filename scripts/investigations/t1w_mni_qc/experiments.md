# Phase-5 re-run experiments — T1w→MNI failing cases

**Mechanism:** standalone debug GPU pod (`rerun-job.yaml`, Tesla T4) on `gpu-nodepool`,
inputs copied in via `kubectl cp` (no in-cluster S3). Ran `fst1w_to_mni.py` directly,
one knob per invocation. NOT an Argo workflow.

**Case:** sub-3NGLDPCG ses-00A — worst batch fail (dice 0.747). Representative of the
single failure category (global under-coverage; all 29 fails are dice-only, jac=0).

## Registration tuning (with FastSurfer brainmask)

| knob changed | dice | jac_neg | centroid_mm | verdict |
|---|---|---|---|---|
| **baseline (defaults)** | 0.7471 | 0.000000 | 5.15 | fail |
| --affine-iterations 400 300 200 100 | 0.7471 | 0.000000 | 5.15 | fail |
| --learning-rate 0.25 | 0.7471 | 0.000000 | 5.15 | fail |
| --syn-iterations 200 150 100 | 0.7471 | 0.000000 | 5.15 | fail |
| combined (all three above) | 0.7471 | 0.000013 | 5.15 | fail |
| --affine-scales 16 8 4 2 1 (coarser pyramid) | 0.7471 | 0.000000 | 5.15 | fail |
| **no brainmask** (register raw orig.mgz) | 1.0000 | 0.000000 | 2.15 | pass* |

\* The no-brainmask "pass" is a **metric artifact**: a full head fills every template-brain
voxel with nonzero intensity → intensity-Dice trivially → 1.0. See honest metrics below.

## Honest alignment metrics (offline, warped resampled to template grid)

Computed with `registration_qc.mutual_information` + Pearson corr within template brain mask,
and coverage = fill_holes(warped>0) ∩ template_brain / template_brain.

| run | intensityDice | MI | corr | coverage |
|---|---|---|---|---|
| baseline (mask) | 0.747 | 0.065 | 0.209 | 0.596 |
| noMask | 1.000 | 0.071 | 0.216 | 1.000 |
| PASS control (sub-MRTV76E4, for reference) | 0.935 | 0.118 | 0.364 | 0.878 |

## Conclusions

1. **Registration alignment is NOT improved by any optimizer tuning** — dice bit-identical
   (0.7471) across affine iterations, learning rate, SyN iterations, and a coarser affine
   pyramid. The FireANTs affine+SyN is fully converged for this subject; the failure is not
   an under-iteration / bad-schedule problem.
2. **Removing the brainmask does not improve alignment** (MI 0.065→0.071, corr 0.209→0.216
   — essentially flat); it only inflates the intensity-Dice by filling the template footprint.
3. **The dice verdict metric is the dominant driver of the failure**, not registration
   quality: it measures intensity>0 coverage (swings 0.747→1.0 by toggling the mask while MI
   stays flat), so it conflates skull-strip/coverage with alignment.
4. There is a **mild, real alignment deficit** for the failing subject (MI 0.065 vs the pass
   control's 0.118) that FireANTs cannot tune away — plausibly related to the anomalously small
   FastSurfer brainmask (1.12M vox vs pass 1.80M) and/or head pose. This is a secondary effect,
   not the "catastrophic failure" the fail verdict implies.

## Follow-up: center-of-mass / moments-initialized affine (CPU, SimpleITK)

Prompted by the observation that failing warped T1w do not conform to the MNI brain.
Hypothesis: FireANTs affine starts from identity and settles in a local optimum
(under-scaled) for subjects whose native pose/scale sits far from MNI. Test: same class
of registration (affine + Mattes MI, 3-level pyramid) but initialized with SimpleITK
`CenteredTransformInitializer(..., MOMENTS)` (aligns intensity center of mass + principal
axes). Metrics computed the same way as above (warped resampled to template grid).

| subject | FireANTs dice (identity) | COM/moments dice | MI | coverage |
|---|---|---|---|---|
| sub-3NGLDPCG | 0.747 | 0.996 | 0.104 | 0.992 |
| sub-3J5VT13N | 0.748 | 0.987 | 0.103 | 0.975 |
| sub-8P7ZETEX | 0.773 | 0.994 | 0.092 | 0.988 |
| sub-91NNHKF0 | 0.784 | 0.991 | 0.091 | 0.982 |

**Result: decisive.** COM/moments initialization conforms every failing brain to the
template (dice ~0.99, coverage ~0.98), vs FireANTs' ~0.75 dice / ~0.60 coverage. This
corrects the earlier "primarily a metric artifact" reading: the failures are GENUINE
registration failures caused by identity-initialized affine converging to a local
minimum, and they are fixable by a center-of-mass/moments initialization. When the
registration conforms the brain, the existing dice metric correctly reports ~0.99 (pass).
Visual proof: artifacts/overlays/sub-3NGLDPCG_ses-00A_COMinit_conform_align.png

---

# FINAL validated recipe (2026-07-03) — supersedes the COM-init reading above

The COM-init conclusion above was **superseded**: COM init changes only translation,
not scale, so on the actual `fst1w_to_mni.py` (FireANTs affine) it did NOT fix the
failures. The real root causes and the shipped fix:

1. **`save_moved_images` no-ops the SyN warp** (byte-identical output) — the deformable
   QC image was affine-only. Fix: apply the warp via `get_warped_coordinates` + `grid_sample`.
2. **Wrong template resolution** (2mm → 1mm `MNI152NLin2009cAsym_res-01_T1w_brain.nii`).
3. **Shipped recipe:** SimpleITK Mattes-MI affine (physical-shift scale, flattened to a
   plain AffineTransform `.mat`) → pre-resample onto the template grid → FireANTs SyN
   (lr 0.25, iters [100,100,100], cc_kernel_size 5) → warp via `get_warped_coordinates`.

## Offline validation — 10 cached subjects (1mm, shipped image + default args)

Full 29-fail set unavailable: S3 `derivatives/` + `metrics/registration/` were cleared
(bucket reset). 10 cached subjects span all original verdict classes.

| orig | subject | mask_dice | lncc | jac_neg | centroid | verdict\* |
|------|---------|-----------|------|---------|----------|-----------|
| fail | sub-3J5VT13N | 0.981 | 0.777 | 0.00038 | 0.69 | pass |
| fail | sub-8P7ZETEX | 0.980 | 0.795 | 0.00030 | 1.00 | pass |
| fail | sub-91NNHKF0 | 0.976 | 0.799 | 0.00043 | 0.62 | pass |
| fail | sub-3NGLDPCG | 0.982 | 0.808 | 0.00025 | 0.78 | pass |
| pass | sub-481L0PYY | 0.980 | 0.801 | 0.00026 | 0.57 | pass |
| pass | sub-90DJ18JF | 0.985 | 0.787 | 0.00066 | 0.24 | warn (jac) |
| pass | sub-MRTV76E4 | 0.984 | 0.836 | 0.00028 | 0.08 | pass |
| warn | sub-086U18RD | 0.979 | 0.785 | 0.00047 | 0.38 | pass |
| warn | sub-107UCJ69 | 0.984 | 0.796 | 0.00035 | 0.28 | pass |
| warn | sub-10RNF3BK | 0.983 | 0.819 | 0.00052 | 0.40 | warn (jac) |

\* under recalibrated lncc gate (warn 0.75 / fail 0.65). Raw run under lncc 0.85 marked all "warn".

- **All 4 fails recovered** (mask_dice 0.72 → ~0.98); zero fails remain; no genuine regressions.
- **lncc recalibrated (Part E):** individual T1w vs MNI *average* template caps
  diffeomorphically at lncc ~0.83 (mean 0.80, sd 0.017); 0.85 warn was above the ceiling.
  New warn 0.75 / fail 0.65 (broken registrations scored lncc ~0.19). Other metrics unchanged.
- **End-to-end shipped-artifact check:** worst fail sub-3NGLDPCG, default args → verdict **pass**
  (mask_dice 0.982, lncc 0.808, jac 0.00025, centroid 0.78).

## Two-transform composition check — preproc.py order is CORRECT (no change needed)

SimpleITK compose of the flat affine `.mat` + FireANTs warp, applied to the native
brainmask onto a 2mm grid, vs the 2mm template brain:

| composition | 2mm mask_dice |
|---|---|
| **affine(warp(x))** — warp applied first, then affine | **0.978** ✓ correct |
| warp(affine(x)) — affine applied first | 0.947 |

The correct pullback is `affine(warp(x))`. Per the official ANTsPy convention
([ANTs transform concepts](https://github.com/ANTsX/ANTsPy/wiki/ANTs-transform-concepts-and-file-formats)):
the **forward displacement field displaces a fixed-space point toward moving space _before_
applying the affine**, and "for forward transforms, the warp field comes first." So resampling
computes `moving_point = affine(warp_displace(fixed_point)) = affine(warp(x))`, with
`transformlist = [Warp, Affine]` → **`-t warp -t affine`**.

That is exactly what `images/afni/preproc.py:298-307` already does
(`-t t1w2mni_warp -t t1w2mni_affine -t bold2t1w_affine` →
`bold2t1w(t1w2mni_affine(t1w2mni_warp(x)))`, warp applied first). **No preproc.py change is
required.** The new pre-resample warp is a standard fixed-space forward displacement, so it
composes with the affine identically to a standard SyN warp.

Confirmed independently by a synthetic real-ANTs test (afni image, ANTs 2.6.5, non-commuting
z-only affines): `antsApplyTransforms -t A -t B` → `B(A(x))`, i.e. the first-listed `-t` is
applied first — consistent with the ANTsPy convention above. (An earlier draft of this note
wrongly claimed the order had to be reversed, based on the mistaken belief that
antsApplyTransforms applies the last-listed transform first.)
