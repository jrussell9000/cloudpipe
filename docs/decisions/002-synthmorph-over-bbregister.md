# 002 — SynthMorph over bbregister for BOLD→T1w registration

**Status**: Accepted

## Context

Each BOLD run must be registered to the subject's T1w anatomy to enable transformation to MNI space. The ABCD minimally preprocessed data ships with an fMRI-to-T1w registration matrix, but this matrix is referenced to the original DICOM-space T1w and is not compatible with the FastSurfer conformed T1w space used in the downstream pipeline. A new BOLD→T1w registration step is required.

The standard tool for this in FreeSurfer-based pipelines is `bbregister`, a surface-based registration that exploits the gray-white matter boundary contrast in T1-weighted images. It produces accurate registrations but requires an iterative optimization that typically runs for 5–10 minutes per BOLD run and depends on FreeSurfer surface data (which must be accessible during the registration).

bbregister also makes a T1-weighted assumption: it expects standard T1w-like contrast at the gray-white boundary. ABCD task fMRI includes multiple contrast types and acquisition parameters across the lifespan cohort; any deviation from expected contrast degrades registration quality without a clear error signal.

## Decision

Use `mri_synthmorph` from the SynthMorph toolbox (Hoffmann et al. 2023) for BOLD→T1w registration. SynthMorph is a contrast-agnostic deep learning registration model trained on synthetic data covering a wide range of contrasts and resolutions. Registration is a single neural network forward pass (~5 seconds on CPU, faster on GPU) rather than an iterative optimization.

The `bold_to_t1w.py` script:
1. Extracts the BOLD reference volume (first non-steady-state frame, controlled by `nss-frames`)
2. Calls `mri_synthmorph` to produce an LTA transform (FreeSurfer format)
3. Converts the LTA to an ANTs/ITK `.mat` affine (RAS→LPS coordinate flip) for use in `antsApplyTransforms`
4. Produces a brain mask in BOLD space from the FreeSurfer `brainmask.mgz`

All FreeSurfer CLI dependencies are replaced with Python equivalents (`nibabel`, `numpy`, `scipy`) to stay compatible with the lightweight `freesurfer/synthmorph` container without requiring a full FreeSurfer installation.

## Consequences

- Per-run registration time drops from ~5–10 minutes to ~5 seconds
- Contrast-agnostic: works equally well across rest, nback, SST, and MID BOLD acquisitions
- The bold-to-t1w step runs on `cpu-heavy-nodepool` (no GPU required in practice, though SynthMorph can use one)
- The LTA→ITK conversion step introduces a fixed RAS-to-LPS coordinate flip — any consumer of the ITK transform must account for this convention
- bbregister surface-based accuracy is not verified against SynthMorph for this cohort; visual QC of `_desc-bold2t1w_warped.nii.gz` is the primary QC mechanism
- SynthMorph performs affine-only registration (no nonlinear warp for BOLD→T1w); residual nonlinear misregistration is handled by the T1w→MNI SyN warp applied in the same `antsApplyTransforms` call during functional preprocessing
