# T1w→MNI Registration Failure Investigation — Design

**Date:** 2026-07-02
**Status:** Approved (investigation spec; remediation deferred to follow-up)
**Pipeline:** `cloudpipe_minproc`

## Context

A recent test batch of `cloudpipe_minproc` workflows produced several sessions
where the T1w→MNI registration returned a `fail` QC verdict. A `fail` exits the
registration step with code 65 (not retried) and gates functional preprocessing
off for that session, so recurring failures directly cap batch yield.

### How registration and QC work today

- **Registration** — [`images/fireANTs/scripts/fst1w_to_mni.py`](../../../images/fireANTs/scripts/fst1w_to_mni.py):
  FireANTs affine + SyN (GPU). Inputs are FastSurfer conformed-space `orig.mgz`
  (moving) and the `MNI152NLin2009cAsym_T1w_brain_res-2` template (fixed).
  `brainmask.mgz` is applied to the T1w before registration to remove skull
  signal. Default tuning: affine scales `[8,4,2,1]` / iters `[200,150,100,50]`,
  SyN scales `[4,2,1]` / iters `[100,70,50]`, learning rate `0.1`.
- **QC verdict** — [`images/shared/registration_qc.py`](../../../images/shared/registration_qc.py),
  `_T1W_MNI_THRESHOLDS`. Three fixed thresholds drive pass/warn/fail
  (fail dominates warn):

  | Metric | warn | fail | direction |
  |---|---|---|---|
  | `dice` (warped∩template, template brain footprint) | < 0.90 | < 0.82 | below |
  | `jac_det_frac_negative` (folded voxels in SyN warp) | > 0.001 | > 0.01 | above |
  | `centroid_displacement_mm` (intensity-weighted, in template mask) | > 5.0 | > 15.0 | above |

- **Where the evidence lives (per session):**
  - `s3://{bucket}/metrics/registration/{subj}_{ses}_t1w_to_mni_reg_qc.json`
    — flat metrics + verdict (the population dataset for this investigation).
  - `s3://{bucket}/derivatives/registration/{subj}/{ses}/t1w_to_mni/` —
    `*_warped.nii.gz` (T1w in MNI space), `*_T1w_brain.nii.gz`,
    `*_T1w_orig.nii.gz`, `*_warp.nii.gz`, `*_affine.mat`, `*_qc.json`.
  - `s3://{bucket}/derivatives/fastsurfer/{subj}/{subj}_{ses}_templated.tar.gz`
    — contains input `orig.mgz` and `brainmask.mgz` (for skull-strip inspection).
  - Subject-level rollup: [`src/metrics/registration_qc_summary.py`](../../../src/metrics/registration_qc_summary.py).

## Goal & deliverable

A **diagnostic investigation** answering: *why did some T1w→MNI registrations
fail QC, and are those failures true misregistrations, QC-threshold artifacts,
or bad inputs?*

The terminal deliverable is a **findings report** that:
1. Categorizes each failing session by root cause (backed by visual evidence).
2. States, per root cause, whether it is addressable by parameter tuning vs.
   needs an alternative/backup registration method.
3. Flags any QC threshold or metric that is itself miscalibrated (i.e. failing
   registrations that are visually good).

**Out of scope (deferred to a follow-up spec):** any change to
`fst1w_to_mni.py`, the QC thresholds, or the workflow templates; implementing a
backup registration method. This spec only diagnoses and recommends.

## Approach

Metric-first triage → hypothesis-driven experiments. Lead with the cheap,
high-information step (pull the whole-batch metric distribution to learn whether
failures are borderline or catastrophic) before spending GPU on re-runs. The
failure-mode vocabulary (below) is the classification scheme applied once
outputs are visually inspected.

## Phases

### Phase 0 — Locate failures
Enumerate the batch's sessions and their `qc.json` verdicts from
`metrics/registration/`. Produce the definitive list of `fail` (and `warn`)
sessions with subject/session IDs. Record batch size and pass/warn/fail counts.

### Phase 1 — Population table
Pull **all** `t1w_to_mni_reg_qc.json` for the batch (pass + warn + fail) into one
dataframe. Plot distributions of `dice`, `jac_det_frac_negative`,
`centroid_displacement_mm` with the warn/fail thresholds overlaid.
**Key question answered here:** are failures *borderline* (clustered just past a
threshold → QC-calibration problem) or *catastrophic* (in the far tail → real
misregistration)?

### Phase 2 — Categorize by driving metric
For each `fail`, record which threshold(s) tripped and metric co-occurrence
(e.g. dice-only vs. dice+centroid vs. jacobian-only). This partitions failures
into clusters that likely share a root cause.

### Phase 3 — Visual inspection
Headless-generate overlay PNGs for every failing session plus a handful of
passing controls:
- `*_warped.nii.gz` over the MNI template (alignment quality).
- Input `orig.mgz` and `brainmask.mgz` (FastSurfer skull-strip quality).

Classify each failure into the failure-mode vocabulary below.

### Phase 4 — Hypotheses
Map visual categories → candidate root causes; predict which remediation lever
(Phase 5) should recover each.

### Phase 5 — Controlled re-runs (GPU)
For representative cases per category, re-run `fst1w_to_mni.py` varying **one
factor at a time** and record the resulting metrics/verdict:
- affine initialization,
- affine/SyN iterations & scales,
- learning rate,
- brainmask on/off (or an alternative mask),
- template resolution.

Confirms which change (if any) recovers a passing verdict, distinguishing
tuning-fixable failures from those needing a backup method.

### Phase 6 — Findings report
Synthesize into the deliverable report (§Deliverable). One row per failing
session: root-cause tag, visual evidence link, Phase-5 result, recommended
remediation class.

## Failure-mode vocabulary

Each failing session is tagged with one or more:

1. **Skull-strip error** — `brainmask.mgz` clips brain or leaves dura/skull,
   biasing the optimizer.
2. **Gross affine-init failure** — affine stage lands in the wrong pose; SyN
   cannot recover.
3. **Anatomical outlier** — large ventricles / atrophy / incidental finding the
   SyN under- or over-warps.
4. **SyN over-warp / folding** — high `jac_det_frac_negative` with otherwise
   acceptable alignment.
5. **FOV / neck inclusion** — extra inferior anatomy in `orig.mgz` pulls the
   intensity-weighted centroid.
6. **QC false alarm** — registration is visually good; the metric trips due to a
   threshold or metric artifact (e.g. dice on intensity>0 masks, centroid
   sensitivity to FOV).

## Artifacts

Reusable analysis toolkit under a new `scripts/investigations/t1w_mni_qc/`
directory (consistent with the repo convention that `scripts/` holds runnable
helpers):

- `pull_qc.py` — list + download all `*_t1w_to_mni_reg_qc.json` for the batch
  from S3; emit a tidy CSV/dataframe.
- `plot_distributions.py` — Phase-1 distribution plots with thresholds overlaid.
- `make_overlays.py` — headless (nilearn/matplotlib) overlay PNGs for warped↔
  template and orig/brainmask inspection.
- `experiments.md` — Phase-5 one-factor-at-a-time re-run log (case, factor
  changed, before/after metrics, verdict).

**Deliverable report:**
`docs/investigations/2026-07-02-t1w-mni-registration-failures.md`.

## Success criteria

The investigation is **done** when:
- Every `fail` session has a root-cause tag backed by visual evidence.
- Each root cause has a confirmed or ruled-out remediation lever from Phase-5
  experiments.
- The report gives clear recommendations on: (a) parameter changes worth trying,
  (b) whether a backup registration method (e.g. SynthMorph, ANTs affine
  fallback) is warranted and for which failure modes, and (c) any QC threshold
  or metric recalibration.

## Dependencies & risks

- **GPU access** to re-run `fst1w_to_mni.py` for Phase 5 (the FireANTs image
  runs on `gpu-nodepool`). Phases 0–4 need only S3 read + CPU.
- **Batch identity** — Phase 0 assumes the failing batch's subject/session IDs
  are recoverable from `metrics/registration/`. If the batch is not cleanly
  isolable there, Phase 0 expands to reconstruct it from workflow/outcome
  records first.
- Small-N risk: if only a few sessions failed, category clusters may be N=1;
  the report will state confidence accordingly rather than over-generalize.
