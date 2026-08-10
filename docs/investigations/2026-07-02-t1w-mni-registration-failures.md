# T1w→MNI Registration QC Failures — Findings Report

**Date:** 2026-07-02
**Pipeline:** `cloudpipe_minproc`
**Spec:** `docs/superpowers/specs/2026-07-02-t1w-mni-registration-failures-investigation-design.md`
**Toolkit + artifacts:** `scripts/investigations/t1w_mni_qc/`, `docs/investigations/artifacts/`

## 1. Summary

A test batch of **271 sessions** produced **20 pass / 222 warn / 29 fail** on the
T1w→MNI registration QC. **Every one of the 29 failures is driven by `dice` alone**
(0.747–0.818, just under the 0.82 fail threshold). Jacobian folding is **exactly zero
across all 271 sessions**, and centroid displacement **never trips fail** (all 2.4–7.8 mm,
well under the 15 mm threshold).

The headline finding: **these are genuine registration failures, and they are fixable.**
The warped T1w does not conform to the MNI template — it fills only ~60% of the template
brain footprint versus ~88% for a passing case, and mutual information confirms the alignment
itself is materially worse. The cause is the **affine stage being initialized from identity**:
for subjects whose native pose and scale sit far from MNI (smaller, often tilted heads),
the intensity-gradient optimizer settles in a local minimum that never scales the brain up,
and SyN — regularized and diffeomorphic — cannot recover a global-scale deficit. This is why
no amount of optimizer tuning moved the result, yet adding a **center-of-mass / moments
initialization** conforms every sampled failing brain (dice 0.75 → ~0.99, coverage 0.60 →
~0.98). The existing `dice` metric was largely doing its job — a properly conformed brain
scores ~0.99, a clear pass. FastSurfer skull-stripping is not the culprit either; the masks
are clean. **The fix is to initialize the affine registration by center of mass.**

> **Correction (2026-07-02).** An earlier revision of this report framed the failures as
> "primarily a QC-metric validity problem, not broken registrations." Follow-up experiments
> (center-of-mass–initialized re-registration, below) disproved that: the registrations are
> genuinely failing and the fix is in the registration, not the metric. The metric's
> coverage-sensitivity is a real but secondary issue, retained in §4.

## 2. Population picture

Distribution of the three QC metrics across the batch (`artifacts/plots/`):

| verdict | n | dice range | jac_det_frac_negative | centroid_mm |
|---|---|---|---|---|
| pass | 20 | 0.903–0.935 | 0 | — |
| warn | 222 | 0.823–0.924 | 0 (all 222) | 179/222 > 5 mm |
| fail | 29 | 0.747–0.818 | 0 (all 29) | all < 15 (never fails) |

- **`dice` is a single smooth, unimodal continuum 0.75 → 0.94** — a gradient of registration
  quality rather than two discrete populations. The fails are the genuinely worst-registered
  tail (the smallest, most MNI-displaced brains, §4), not an arbitrary cut — but because the
  distribution is continuous, the exact 0.82 line is a tuning choice, and much of the `warn`
  band is registrations that are only mildly under-conformed.
- **Folding is never a factor** (0 across the cohort) and **centroid never reaches its
  fail threshold**. The **fail** verdict is therefore effectively a single-metric (`dice`)
  gate; the pass/warn split is additionally driven by the 5 mm centroid *warn* threshold
  (e.g. sub-BKN88GVE ses-00A warns and ses-02A passes on identical dice 0.903, differing
  only on centroid crossing 5 mm).
- **82% of the cohort lands in `warn`**, almost all because `dice < 0.90` — a strong sign
  the thresholds are miscalibrated against this metric's actual behavior.
- Dice is **near-constant per subject across sessions** (e.g. sub-3NGLDPCG = 0.747 in all
  three sessions), i.e. the effect is systematic and subject-linked, not random per-scan.

## 3. Failure catalogue

29 failing sessions span **11 distinct subjects** (most fail in every session):
sub-3J5VT13N, sub-3NGLDPCG, sub-8P7ZETEX, sub-91NNHKF0, sub-CXKTLU8R, sub-ER3Y13P5,
sub-HLG6NYZU, sub-KDZKAC4L, sub-LDH3YU2R, sub-MKWH6RED, sub-Z4LY1E6P. Full metric rows:
`artifacts/qc_table.csv` (filter `verdict==fail`).

Representative visual inspection (`artifacts/overlays/`, classification in
`artifacts/classification.csv`):

| session | dice | align overlay | finding |
|---|---|---|---|
| sub-3NGLDPCG ses-00A | 0.747 | `*_FAILworst_align.png` | correctly oriented/positioned; brain under-fills template superiorly/anteriorly |
| sub-8P7ZETEX ses-00A | 0.773 | `*_FAIL_align.png` | same under-coverage pattern |
| sub-Z4LY1E6P ses-00A | 0.818 | `*_FAILnear_align.png` | milder under-coverage; prominent lateral ventricles |
| sub-MRTV76E4 ses-00A | 0.935 | `*_PASSctrl_align.png` (control) | brain fills template contour tightly |

All failing overlays show the same signature: **anatomically correct placement, but the
warped brain does not expand to fill the template footprint** — a global under-scaling. No
skull-strip leakage into dura, no folding, no FOV/neck contamination.

## 4. Root causes

**Primary — the affine stage is initialized from identity and converges to a local minimum.**
FireANTs' `AffineRegistration` starts from the identity transform, assuming the moving brain
and the template already roughly overlap in world space, then refines by intensity gradient.
For subjects whose native pose and scale sit far from MNI — smaller, often tilted heads,
conformed into a 256³ space whose world origin differs from MNI's — that start lands the
optimizer in a basin that captures rough position but never climbs to the full scale-up. SyN,
being regularized and diffeomorphic, cannot then recover a global-scale deficit.

Two experiments (case sub-3NGLDPCG ses-00A, `experiments.md`) pin this down:

- **Optimizer tuning does nothing.** Dice is bit-identical (0.7471) across 4× affine
  iterations, 2.5× learning rate, 2× SyN iterations, and a coarser affine pyramid. That is the
  signature of a *local minimum*, not slow convergence — more iterations cannot escape it.
- **Center-of-mass / moments initialization fixes it.** The same class of registration (affine
  + Mattes MI, 3-level pyramid) initialized with `CenteredTransformInitializer(..., MOMENTS)`
  conforms every sampled failing brain:

  | subject | FireANTs dice (identity init) | COM/moments-init dice | coverage |
  |---|---|---|---|
  | sub-3NGLDPCG | 0.747 | **0.996** | 0.992 |
  | sub-3J5VT13N | 0.748 | **0.987** | 0.975 |
  | sub-8P7ZETEX | 0.773 | **0.994** | 0.988 |
  | sub-91NNHKF0 | 0.784 | **0.991** | 0.982 |

  Coverage goes from ~0.60 to ~0.98; the brain fills the template (visual proof:
  `artifacts/overlays/sub-3NGLDPCG_ses-00A_COMinit_conform_align.png`). Critically, the
  existing `dice` metric then reports ~0.99 — so **the metric was largely doing its job**; the
  failures it flagged were real.

**Why it correlates with brain size.** Fail subjects have the smallest FastSurfer brains
(fail ~1162 cc, warn ~1512, pass ~1784; `artifacts/brainmask_volume_by_verdict.csv`), and the
smaller / more-displaced a brain is from MNI, the worse the identity-initialized affine starts —
so the local-minimum failure hits small brains hardest. **This is not a skull-stripping
problem:** multi-slice inspection of failing and passing brainmasks (`artifacts/montages/`)
shows clean, accurate strips in both; no brain is being cut out.

**Secondary — the `dice` metric is coverage-sensitive.** `registration_qc.dice` binarizes at
`>0` within the template brain mask, so it measures intensity *coverage* rather than alignment.
This shows up as the metric being gameable (dropping the brainmask fills the footprint and
sends dice to 1.0 while MI barely moves, 0.065 → 0.071) and as 82% of good registrations
sitting in `warn`. It did not cause the failures — but a metric that measured alignment
directly (MI within the brain mask) would be more robust and less threshold-sensitive. This is
a worthwhile but secondary refinement.

## 5. Recommendations (for a follow-up remediation spec)

1. **Add center-of-mass / moments initialization to the affine stage — the fix.** In
   [`fst1w_to_mni.py`](../../images/fireANTs/scripts/fst1w_to_mni.py), replace the
   identity-initialized affine start with a center-of-mass transform. **FireANTs has no
   built-in center-of-mass initialization**, so compute the moments affine externally with
   SimpleITK `CenteredTransformInitializer(fixed, moving, AffineTransform(3), MOMENTS)` (CPU,
   deterministic, ~1 s — exactly what these experiments ran) and hand it to FireANTs as the
   affine warm-start via `init_affine`, replacing the identity-initialized `AffineRegistration`
   stage (FireANTs already accepts an affine into SyN via `init_affine`, so the plumbing
   exists). The SimpleITK moments-init affine alone already reaches dice ~0.99, so SyN only
   needs to refine on top. Validate across all 29 fails and regression-check the 20 pass /
   222 warn.
2. **Refine the QC metric (secondary).** Make the primary T1w→MNI gate **MI (or NCC) within the
   template brain mask** rather than intensity-`>0` Dice — alignment-sensitive, not gameable by
   coverage. Re-derive after the registration fix, since most current fails should resolve.
3. **Recalibrate warn/fail thresholds** against the post-fix batch distribution. The current
   thresholds place 82% of good registrations in `warn`; expect that to shrink markedly once
   the affine conforms brains and the metric measures alignment.

## 6. Confidence & limitations

- **High confidence — the fix.** Center-of-mass initialization resolved **4 of 4** failing
  subjects tested (dice 0.75 → ~0.99, coverage 0.60 → ~0.98), a uniform result consistent with
  the identity-init local-minimum mechanism. The failures are genuine registration failures.
- **High confidence — population framing:** all-dice-driven failures, zero folding, and the
  borderline continuum are backed by the full 271-session table.
- **To confirm at remediation:** the COM-init result used a SimpleITK affine as a stand-in for
  the pipeline's FireANTs affine, on 4 of the 11 failing subjects; validate the production path
  — SimpleITK moments affine handed to FireANTs via `init_affine` — across all 29 fails (and
  regression-check pass/warn) before rollout.
- **Environment:** matplotlib on the analysis host emits a benign Axes3D warning (duplicate
  install); it does not affect results.
