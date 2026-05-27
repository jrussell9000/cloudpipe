# 003 — Use `orig.mgz` instead of BIDS T1w for T1w→MNI registration

**Status**: Accepted

## Context

The T1w→MNI registration step (`t1w-to-mni-template`) needs a T1w image as its source. Two candidates exist for each session:

1. **BIDS T1w** (`mmps_mproc/{subjID}/{session}/anat/{subjID}_{session}_T1w.nii.gz`) — the original minimally preprocessed T1w in BIDS format, in native scanner space
2. **FreeSurfer `orig.mgz`** — the same T1w as received by FastSurfer, resampled into FreeSurfer's 256×256×256 1mm isotropic conformed space

The BOLD→T1w registration (SynthMorph, ADR 002) produces a transform referenced to the FreeSurfer conformed space: SynthMorph receives the `brainmask.mgz` (also in conformed space) as the fixed image and writes the LTA transform with the conformed volume as its target coordinate frame. The ITK-format transform used in functional preprocessing (`_desc-bold2t1w_itk.txt`) is therefore a BOLD→conformed transform.

If the T1w→MNI registration were run on the native BIDS T1w, the two transforms (BOLD→conformed and conformed→native→MNI) would have a mismatched intermediate space. Concatenating them in `antsApplyTransforms` would require an additional conformed→native resampling step, introducing an extra interpolation and complicating the single-step transform application during functional preprocessing.

## Decision

Pass `orig.mgz` (and `brainmask.mgz` for skull-stripping, both in conformed space) to `fst1w_to_mni.py`. Both files are read via nibabel, eliminating the need for a separate init container or `mri_convert` call.

The T1w→MNI transform is therefore a **conformed→MNI** transform. Combined with the BOLD→conformed ITK affine from SynthMorph, `antsApplyTransforms` can apply both transforms in a single interpolation step during functional preprocessing: `BOLD → conformed → MNI`.

## Consequences

- Functional preprocessing applies BOLD→T1w and T1w→MNI in one `antsApplyTransforms` call with a single interpolation — no intermediate resampling
- The T1w→MNI QC image (`_warped.nii.gz`) is in MNI space but was registered from the conformed T1w, not the BIDS T1w; the conformed image has slightly different FOV and orientation than the original BIDS T1w
- Any downstream analysis that needs to go back to native scanner space must account for the conformed-space origin, not the BIDS-space origin
- FastSurfer must complete successfully before T1w→MNI registration can run (the `orig.mgz` and `brainmask.mgz` come from the FastSurfer tarball); there is no path to run T1w→MNI without FastSurfer
