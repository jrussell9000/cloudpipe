# 002 — SynthMorph over bbregister for BOLD→T1w registration

**Status**: Accepted (see [ADR 012](012-abcd-matrix-rejected.md) for a rejected alternative)

> **Note (ADR 012)**: The ABCD-supplied `registration_matrix_T1` was evaluated
> as a way to skip BOLD→T1w registration entirely. Under every coordinate
> interpretation tested it scored worse on mutual information than applying no
> matrix at all, so it was rejected. SynthMorph rigid (this ADR) is used
> unconditionally. The original context below speculated the matrix was
> "incompatible with the FastSurfer conformed T1w space"; the real finding is
> simpler — no usable interpretation of the matrix improves alignment. See
> ADR 012 for the evidence.

## Context (original)

Each BOLD run must be registered to the subject's T1w anatomy to enable transformation to MNI space. The ABCD minimally preprocessed data ships with an fMRI-to-T1w registration matrix; at the time of this decision it was believed this matrix was referenced to the original DICOM-space T1w and incompatible with the FastSurfer conformed T1w space used in the downstream pipeline. A new BOLD→T1w registration step was therefore considered required.

The standard tool for this in FreeSurfer-based pipelines is `bbregister`, a surface-based registration that exploits the gray-white matter boundary contrast in T1-weighted images. It produces accurate registrations but requires an iterative optimization that typically runs for 5–10 minutes per BOLD run and depends on FreeSurfer surface data (which must be accessible during the registration).

bbregister also makes a T1-weighted assumption: it expects standard T1w-like contrast at the gray-white boundary. ABCD task fMRI includes multiple contrast types and acquisition parameters across the lifespan cohort; any deviation from expected contrast degrades registration quality without a clear error signal.

## Decision

Use `mri_synthmorph` from the SynthMorph toolbox (Hoffmann et al. 2023) for BOLD→T1w registration. SynthMorph is a contrast-agnostic deep learning registration model trained on synthetic data covering a wide range of contrasts and resolutions. Registration is a single neural network forward pass (~5 seconds on CPU, faster on GPU) rather than an iterative optimization.

The `bold_to_t1w.py` script:
1. Extracts the BOLD reference volume (first non-steady-state frame, controlled by `nss-frames`)
2. Calls `mri_synthmorph` to produce an LTA transform (FreeSurfer format)
3. Converts the LTA to an ANTs/ITK affine text file (RAS→LPS coordinate flip) for use in `antsApplyTransforms`
4. Produces a brain mask in BOLD space from the FreeSurfer `brainmask.mgz`

Step 3 is **hand-rolled in numpy rather than shelled out to `lta_convert --outitk`**, which segfaults with `munmap_chunk` on SynthMorph LTAs. The conversion is `A_lps = D · R · D`, `t_lps = D · t` with `D = diag(-1, -1, 1)`, and the result is applied **as-is, with no inversion**.

All FreeSurfer CLI dependencies are replaced with Python equivalents (`nibabel`, `numpy`, `scipy`) to stay compatible with the lightweight `freesurfer/synthmorph` container without requiring a full FreeSurfer installation.

## Consequences

- Per-run registration time drops from ~5–10 minutes to ~5 seconds
- Contrast-agnostic: works equally well across rest, nback, SST, and MID BOLD acquisitions
- The bold-to-t1w step runs on `cpu-heavy-nodepool` (no GPU required in practice, though SynthMorph can use one). It requests 3G/2 CPU with a 12G limit — a deliberately wide gap, because real peak memory is **host-dependent**: 8.67 G on a c6i.4xlarge (Ice Lake) versus 4.25 G on a c5.4xlarge. `TF_ENABLE_ONEDNN_OPTS=0` is set to suppress the oneDNN allocation path responsible for that spread
- The LTA→ITK conversion introduces a fixed RAS-to-LPS coordinate flip — any consumer of the ITK transform must account for this convention. The transform is **not** inverted in the process; a prior version of this pipeline had the direction wrong (fixed 2026-07-23, `cd33678`), which is worth knowing when reading pre-fix QC numbers
- A SynthMorph+BBR two-stage variant was implemented and then **removed** in `61ccff7`: ABCD's 2.4 mm EPI lacks the gray/white contrast `bbregister` needs. The `dice`, `bbr_cost`, `bbr_converged` and `bbr_init_used` QC fields are dead as a result, retained only to deserialize historical records
- bbregister surface-based accuracy is not verified against SynthMorph for this cohort. QC is now quantitative rather than purely visual: `nmi_gain` (NMI over the same session's identity baseline) is the gated metric, with a bound of simply `> 0`. Note the scale — a *good* cross-contrast EPI↔T1w registration scores `nmi` ≈ 1.019 against an identity baseline of ≈ 1.011, so **~1.02 is a good score, not a failure**. Gate calibration found no absolute bound supportable (best candidate AUC 0.83 against a 0.95 bar)
- SynthMorph is called with `-m rigid` (6 DOF), not `-m affine` (12 DOF). ABCD minimally preprocessed BOLD has already undergone SE-fieldmap-based susceptibility distortion correction upstream; the residual misalignment is a head-position difference only, which is a rigid-body problem. Affine would absorb residual EPI artifacts as artificial scaling/shearing of the brain and propagate them through the pipeline. Residual nonlinear misregistration is handled by the T1w→MNI SyN warp applied in the same `antsApplyTransforms` call during functional preprocessing
