"""
preproc.py — BOLD preprocessing pipeline: warp → mask → MNI-space output.

Upstream state of input data (ABCD minimal preprocessing, Hagler et al. 2019):
  The BOLD volumes delivered to this script have already undergone, in order:
  1. Within-scan head motion correction — AFNI 3dvolreg, each frame registered
     to frame 0; motion time courses are provided separately.
  2. B0 distortion correction — reversing-polarity spin-echo fieldmaps;
     displacement field estimated from SE calibration scans, adjusted for
     between-scan head motion, applied to the gradient-echo BOLD series.
  3. Gradient nonlinearity correction — Jovicich et al. (2006).
  4. Between-scan motion correction — each scan cubic-interpolation resampled
     into alignment with the session's middle reference scan (rigid, first
     frame to first frame).
  5. fMRI→T1w registration matrix computed (not applied) — SE fieldmaps
     registered to T1w via mutual information with atlas-based pre-alignment;
     a rigid-body matrix is provided per scan but the data remain in native
     space at 2.4 mm isotropic resolution.

  Slice timing correction is NOT part of ABCD minimal preprocessing and is
  not applied anywhere in the current cloudpipe pipeline.  It must be inserted
  as a native-space step before this script if desired; it cannot be applied
  after Stage 3 (MNI warp).

Deobliquing is intentionally omitted: the BOLD→T1w affine (from SynthMorph)
was computed against the original NIfTI header geometry.  ANTs reads the
NIfTI qform/sform to account for oblique geometry correctly during
antsApplyTransforms, so no explicit deoblique step is needed.

Stages:
  0. Slice timing correction                            (AFNI: 3dTshift)
  1. Extract 3D BOLD reference (mean of NSS frames)     (nibabel)
  2. Precompute composite displacement field            (ANTs: antsApplyTransforms)
  3. Warp unmasked BOLD → MNI (LanczosWindowedSinc)    (ANTs: antsApplyTransforms)
  4. Warp brain mask → MNI (NearestNeighbor)           (ANTs: antsApplyTransforms)
  5. Apply MNI-space mask to MNI-space BOLD             (AFNI: 3dcalc)
  6. Confound estimation

Masking is applied after warping rather than before to avoid Gibbs ringing
artefacts at the cortical boundary.  Applying a hard binary mask in native
space creates a steep edge that LanczosWindowedSinc interpolation treats as
high-frequency content, producing ringing ripples just inside/outside the
cortex after warping.  Instead the unmasked BOLD is warped with Lanczos, then
the mask is warped separately with NearestNeighbor (preserving binary values),
and the final 3dcalc step zeroes non-brain voxels in MNI space.

Registration inputs (bold2t1w affine, t1w2mni affine + warp) are produced
upstream by bold_to_t1w.py and fst1w_to_mni.py respectively.
The brain mask is produced upstream by bold_to_t1w.py.

Inputs (via CLI args):
  --bold             Minimally preprocessed 4D BOLD .nii.gz (native space)
  --bids-sidecar     BIDS JSON sidecar for the BOLD run (SliceTiming field used for STC)
  --brainmask        Brain mask in native BOLD space (from bold_to_t1w.py)
  --mni-template     MNI152NLin2009cAsym reference NIfTI (for output grid)
  --bold2t1w-affine  BOLD→T1w affine transform (.mat, from bold_to_t1w.py)
  --t1w2mni-affine   T1w→MNI affine transform (.mat, from fst1w_to_mni.py)
  --t1w2mni-warp     T1w→MNI warp field (.nii.gz, from fst1w_to_mni.py)
  --outdir           Output directory
  --subj             Subject ID  (e.g. NDARABC123)
  --session          Session label (e.g. ses-00A)
  --run              Run label (e.g. run-01)
  --task             Task label (e.g. task-rest)
  --threads          CPU threads for ANTs (default: 8)
  --nss-frames       Number of non-steady-state frames at the start of the run
  --aseg             aseg.mgz from FastSurfer (for WM/CSF masks in aCompCor)
  --motion-file      ABCD 3dvolreg motion parameter TSV

Outputs written to --outdir:
  <prefix>_desc-boldref.nii.gz                          mean NSS reference volume
  <prefix>_space-MNI152NLin2009cAsym_brainmask.nii.gz  MNI-space brain mask
  <prefix>_space-MNI152NLin2009cAsym_bold.nii.gz       final masked MNI BOLD
  <prefix>_desc-confounds_timeseries.tsv                confound regressors
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import pandas as pd
from scipy.ndimage import binary_erosion
from sklearn.decomposition import PCA


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"\n>>> {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.run([str(c) for c in cmd], check=check)


# ---------------------------------------------------------------------------
# Stage 0: Slice timing correction
# ---------------------------------------------------------------------------

def apply_stc(bold: Path, bids_sidecar: Path, outdir: Path, prefix: str) -> Path:
    """Apply slice timing correction using AFNI 3dTshift.

    Slice timing offsets are extracted from the SliceTiming field of the BIDS
    JSON sidecar (values in seconds, one per slice).  TR is read from the BOLD
    NIfTI header.  The corrected volume is written to outdir and returned.

    STC must run in native space before any spatial resampling — it cannot be
    applied retroactively after the MNI warp.
    """
    with open(bids_sidecar) as f:
        meta = json.load(f)
    timing_path = Path('/tmp/slice_timing.txt')
    timing_path.write_text('\n'.join(str(t) for t in meta['SliceTiming']))

    tr  = float(nib.load(bold).header.get_zooms()[3])
    out = outdir / f"{prefix}_desc-stc_bold.nii.gz"
    run([
        '3dTshift',
        '-TR',       f'{tr}s',
        '-tpattern', f'@{timing_path}',
        '-prefix',   str(out),
        str(bold),
    ])
    return out


# ---------------------------------------------------------------------------
# Stage 1: Extract 3D BOLD reference (first frame)
# ---------------------------------------------------------------------------

def extract_bold_ref(bold: Path, outdir: Path, prefix: str,
                     nss_frames: int) -> Path:
    """Compute the mean of the non-steady-state frames as a 3D BOLD reference.

    NSS frames have elevated T1 contrast (magnetisation not yet at steady
    state), which improves boundary sharpness for the composite warp geometry
    input and for tissue-mask warping in confound estimation.

    Used as the -i geometry input when precomputing the composite warp and
    reused in confound estimation for tissue-mask warping.
    """
    img      = nib.load(bold)
    data     = img.get_fdata(dtype=np.float32)          # (X, Y, Z, T)
    ref_data = data[..., :nss_frames].mean(axis=-1)     # mean over NSS frames
    ref_path = outdir / f"{prefix}_desc-boldref.nii.gz"
    nib.save(nib.Nifti1Image(ref_data, img.affine, img.header), str(ref_path))
    return ref_path


# ---------------------------------------------------------------------------
# Stage 3: Precompute composite displacement field
# ---------------------------------------------------------------------------

def precompute_composite_warp(
    bold_ref: Path, mni_template: Path,
    bold2t1w_affine: Path, t1w2mni_affine: Path, t1w2mni_warp: Path,
    outdir: Path,
) -> Path:
    """Collapse the bold→T1w affine + T1w→MNI affine + T1w→MNI warp into a
    single displacement field.

    Evaluated once per run (fast 3D operation).  apply_transforms then applies
    this single field to every frame of the 4D BOLD, eliminating per-voxel
    evaluation of the full 3-transform chain.

    The [output,1] flag tells ANTs to write a displacement field rather than
    a warped image.  Transform order matches apply_transforms (ANTs last-to-first):
      -t t1w2mni_warp    ← applied third
      -t t1w2mni_affine  ← applied second
      -t bold2t1w_affine ← applied first
    """
    composite = outdir / "composite_warp.nii.gz"
    run([
        "antsApplyTransforms",
        "-d", "3",
        "-i", bold_ref,
        "-r", mni_template,
        "-o", f"[{composite},1]",
        "-t", t1w2mni_warp,
        "-t", t1w2mni_affine,
        "-t", bold2t1w_affine,
    ])
    return composite


# ---------------------------------------------------------------------------
# Stage 3: Warp unmasked BOLD → MNI (antsApplyTransforms)
# ---------------------------------------------------------------------------

def apply_transforms(bold: Path, mni_template: Path,
                     composite_warp: Path, outdir: Path, prefix: str,
                     threads: int) -> Path:
    """Warp the unmasked 4D BOLD to MNI space using the precomputed composite
    displacement field.

    The BOLD is intentionally left unmasked at this stage so that
    LanczosWindowedSinc interpolation does not encounter the steep
    native-space brain boundary, which would produce Gibbs ringing artefacts
    along the cortical ribbon.  Masking is applied in MNI space afterwards
    (Stage 5) once interpolation is complete.
    """
    out = outdir / f"{prefix}_space-MNI152NLin2009cAsym_desc-unmasked_bold.nii.gz"
    run([
        "antsApplyTransforms",
        "-d", "3",
        "-e", "3",
        "-i", bold,
        "-r", mni_template,
        "-o", out,
        "-t", composite_warp,
        "--interpolation", "LanczosWindowedSinc",
        "--default-value", "0",
        "--float", "1",
    ])
    return out


# ---------------------------------------------------------------------------
# Stage 4: Warp brain mask → MNI (NearestNeighbor)
# ---------------------------------------------------------------------------

def warp_mask_to_mni(brainmask: Path, mni_template: Path,
                     composite_warp: Path, outdir: Path, prefix: str) -> Path:
    """Warp the native-space brain mask to MNI space using NearestNeighbor
    interpolation to preserve binary 0/1 values.

    The same composite warp used for the BOLD is applied here — the mask
    lives in native BOLD space, so the transform chain is identical.
    """
    out = outdir / f"{prefix}_space-MNI152NLin2009cAsym_brainmask.nii.gz"
    run([
        "antsApplyTransforms",
        "-d", "3",
        "-i", brainmask,
        "-r", mni_template,
        "-o", out,
        "-t", composite_warp,
        "--interpolation", "NearestNeighbor",
        "--default-value", "0",
    ])
    return out


# ---------------------------------------------------------------------------
# Stage 5: Apply MNI-space mask to MNI-space BOLD (3dcalc)
# ---------------------------------------------------------------------------

def apply_mni_mask(bold_mni: Path, mask_mni: Path,
                   outdir: Path, prefix: str) -> Path:
    """Zero non-brain voxels in MNI space after warping is complete.

    Masking here rather than before the warp prevents LanczosWindowedSinc
    from ringing across the sharp native-space brain boundary.
    """
    out = outdir / f"{prefix}_space-MNI152NLin2009cAsym_bold.nii.gz"
    run([
        "3dcalc",
        "-a", bold_mni,
        "-b", mask_mni,
        "-expr", "a*step(b)",
        "-prefix", out,
    ])
    return out


# ---------------------------------------------------------------------------
# Stage 6: Confound estimation
# ---------------------------------------------------------------------------

def compute_confounds(
    bold: Path,
    brainmask: Path,
    aseg: Path,
    bold2t1w_affine: Path,
    motion_file: Path,
    outdir: Path,
    prefix: str,
    bold_ref: Path,
    nss_frames: int,
) -> Path:
    """
    Estimate confound regressors and write a BIDS-style TSV.

    Confounds produced:
      - Motion parameters (pass-through from ABCD 3dvolreg, columns:
        rot_z/x/y in degrees, trans_z/x/y in mm)
      - Motion parameter derivatives (first temporal difference; NaN frame 0)
      - Motion parameter powers (squares)
      - Motion parameter derivative powers (squares of derivatives)
      - Framewise displacement (FD) derived from motion parameters,
        converting rotations to mm assuming a 50 mm head radius
      - DVARS (RMS of temporal derivative of masked BOLD signal)
      - Global signal (mean over brain mask)
      - aCompCor: PCA components from eroded WM and CSF masks derived from
        FastSurfer aseg.mgz, warped to BOLD native space via the inverse of
        the bold2t1w affine transform; retains the minimum of 5 components
        or the number needed to explain ≥50% of variance (fMRIPrep default)
      - tCompCor: 5 PCA components from the top 2% of brain voxels ranked
        by temporal standard deviation (data-driven noise ROI)
      - Cosine (DCT) regressors: discrete cosine basis for implicit high-pass
        filtering at 1/128 Hz; number of regressors = floor(2·T·TR/128)
      - Non-steady-state outlier indicators: one binary column per NSS frame
        (non_steady_state_outlier_00 … _NN), for explicit GLM exclusion

    WM mask is eroded by 1 voxel before warping to reduce partial-volume
    contamination from grey matter at the tissue boundary.

    The first frame of FD, DVARS, and motion derivatives is set to n/a.

    Output: {outdir}/{prefix}_desc-confounds_timeseries.tsv
    """

    # --- Motion parameters, derivatives, powers, and FD ---
    motion = pd.read_csv(motion_file, sep='\t')
    mp_cols = ['rot_z', 'rot_x', 'rot_y', 'trans_z', 'trans_x', 'trans_y']
    mp      = motion[mp_cols].values                                    # (T, 6)
    mp_deriv  = np.vstack([np.full((1, 6), np.nan), np.diff(mp, axis=0)])  # (T, 6)
    mp_power  = mp ** 2                                                 # (T, 6)
    mp_d_pow  = mp_deriv ** 2                                           # (T, 6)

    rot_mm   = mp[:, :3] * (np.pi / 180.0) * 50.0
    trans_mm = mp[:, 3:]
    fd_raw = np.abs(np.diff(np.hstack([rot_mm, trans_mm]), axis=0)).sum(axis=1)
    fd = np.concatenate([[np.nan], fd_raw])

    # --- Load BOLD and brain mask ---
    bold_img  = nib.load(bold)
    bold_data = bold_img.get_fdata(dtype=np.float32)   # (X, Y, Z, T)
    mask_data = nib.load(brainmask).get_fdata() > 0

    brain_ts  = bold_data[mask_data].T        # (T, voxels)
    n_frames  = brain_ts.shape[0]

    # --- DVARS ---
    diff  = np.diff(brain_ts, axis=0)
    dvars = np.concatenate([[np.nan], np.sqrt((diff ** 2).mean(axis=1))])

    # --- Global signal ---
    global_signal = brain_ts.mean(axis=1)

    # --- tCompCor: top-2% temporal-SD voxels ---
    temporal_std  = brain_ts.std(axis=0)                           # (voxels,)
    sd_threshold  = np.percentile(temporal_std, 98)
    high_var_ts   = brain_ts[:, temporal_std >= sd_threshold]      # (T, n_hv)
    high_var_ts   = high_var_ts - high_var_ts.mean(axis=0)         # voxelwise demean
    n_tcc = min(5, high_var_ts.shape[1], n_frames)
    t_comp_cor_cols: dict = {}
    if n_tcc > 0:
        t_comps = PCA(n_components=n_tcc).fit_transform(high_var_ts)   # (T, n_tcc)
        for i in range(n_tcc):
            t_comp_cor_cols[f't_comp_cor_{i:02d}'] = t_comps[:, i]

    # --- Cosine (DCT) regressors for implicit high-pass filtering at 1/128 Hz ---
    # TR is read from the BOLD header (assumed to be stored in seconds).
    tr = float(bold_img.header.get_zooms()[3])
    n_cosines = int(np.floor(2 * n_frames * tr / 128.0))
    t_idx = np.arange(n_frames)
    cosine_cols: dict = {
        f'cosine_{k:02d}': np.sqrt(2 / n_frames) * np.cos(np.pi / n_frames * k * (t_idx + 0.5))
        for k in range(1, n_cosines + 1)
    }

    # --- aCompCor: WM and CSF components ---
    aseg_img  = nib.load(aseg)
    aseg_data = np.round(aseg_img.get_fdata()).astype(int)

    wm_arr  = np.isin(aseg_data, [2, 41]).astype(np.uint8)
    csf_arr = np.isin(aseg_data, [4, 14, 15, 43]).astype(np.uint8)
    # Erode WM by 1 voxel to reduce partial-volume contamination at GM boundary
    wm_arr  = binary_erosion(wm_arr, iterations=1).astype(np.uint8)

    comp_cor_cols: dict = {}
    for tissue, mask_arr in [('wm', wm_arr), ('csf', csf_arr)]:
        t1w_path  = Path(f'/tmp/{tissue}_mask_t1w.nii.gz')
        bold_path = Path(f'/tmp/{tissue}_mask_bold.nii.gz')

        nib.save(nib.Nifti1Image(mask_arr, aseg_img.affine), str(t1w_path))

        # Warp mask from T1w conformed space → BOLD native space.
        # bold2t1w_affine maps BOLD→T1w; invert it (the ,1 flag) to get T1w→BOLD.
        run([
            'antsApplyTransforms', '-d', '3',
            '-i', str(t1w_path),
            '-r', str(bold_ref),
            '-o', str(bold_path),
            '-t', f'[{bold2t1w_affine},1]',
            '--interpolation', 'NearestNeighbor',
        ])

        warped = nib.load(bold_path).get_fdata() > 0.5
        n_vox  = int(warped.sum())
        if n_vox == 0:
            print(
                f"WARNING: no {tissue.upper()} voxels in BOLD space after warping "
                f"— skipping {tissue} CompCor",
                flush=True,
            )
            continue

        roi_ts = bold_data[warped].T           # (T, voxels)
        roi_ts = roi_ts - roi_ts.mean(axis=0)  # voxelwise demean

        # Fit up to 5 components, then trim to whichever is fewer: 5 or the
        # minimum number of components needed to explain ≥50% of variance.
        max_comp = min(5, n_vox, n_frames)
        pca      = PCA(n_components=max_comp).fit(roi_ts)
        cumvar   = np.cumsum(pca.explained_variance_ratio_)
        n_comp   = min(int(np.searchsorted(cumvar, 0.50)) + 1, max_comp)
        comps    = pca.transform(roi_ts)[:, :n_comp]           # (T, n_comp)
        print(
            f"  aCompCor {tissue.upper()}: {n_comp} components "
            f"({cumvar[n_comp - 1]:.1%} variance explained)",
            flush=True,
        )
        for i in range(n_comp):
            comp_cor_cols[f'a_comp_cor_{tissue}_{i:02d}'] = comps[:, i]

    # --- Non-steady-state outlier indicators ---
    nss_outlier_cols = {
        f'non_steady_state_outlier_{i:02d}': (np.arange(n_frames) == i).astype(np.float32)
        for i in range(nss_frames)
    }

    # --- Assemble and write TSV ---
    mp_dict: dict = {}
    for i, col in enumerate(mp_cols):
        mp_dict[col]                          = mp[:, i]
        mp_dict[f'{col}_derivative1']         = mp_deriv[:, i]
        mp_dict[f'{col}_power2']              = mp_power[:, i]
        mp_dict[f'{col}_derivative1_power2']  = mp_d_pow[:, i]

    confounds = pd.DataFrame({
        't_indx':                 np.arange(n_frames),
        **nss_outlier_cols,
        **mp_dict,
        'framewise_displacement': fd,
        'dvars':                  dvars,
        'global_signal':          global_signal,
        **comp_cor_cols,
        **t_comp_cor_cols,
        **cosine_cols,
    })

    out_tsv = outdir / f'{prefix}_desc-confounds_timeseries.tsv'
    confounds.to_csv(out_tsv, sep='\t', index=False, na_rep='n/a')
    print(
        f"  Confounds: {out_tsv.name} "
        f"({confounds.shape[1]} columns × {confounds.shape[0]} frames)",
        flush=True,
    )
    return out_tsv


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BOLD preprocessing: deoblique → mask → antsApplyTransforms → MNI"
    )
    p.add_argument("--bold",            required=True,  type=Path)
    p.add_argument("--bids-sidecar",    required=True,  type=Path,
                   help="BIDS JSON sidecar for the BOLD run (SliceTiming used for STC)")
    p.add_argument("--brainmask",       required=True,  type=Path)
    p.add_argument("--mni-template",    required=True,  type=Path)
    p.add_argument("--bold2t1w-affine", required=True,  type=Path)
    p.add_argument("--t1w2mni-affine",  required=True,  type=Path)
    p.add_argument("--t1w2mni-warp",    required=True,  type=Path)
    p.add_argument("--outdir",          required=True,  type=Path)
    p.add_argument("--subj",            required=True)
    p.add_argument("--session",         required=True)
    p.add_argument("--run",             required=True)
    p.add_argument("--task",            required=True)
    p.add_argument("--threads",         type=int, default=8)
    p.add_argument("--aseg",            required=True, type=Path,
                   help="aseg.mgz from FastSurfer subjects dir (for WM/CSF masks)")
    p.add_argument("--motion-file",     required=True, type=Path,
                   help="ABCD 3dvolreg motion parameter TSV")
    p.add_argument("--nss-frames",      required=True, type=int,
                   help="Number of non-steady-state frames at the start of the run")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    os.environ["ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS"] = str(args.threads)

    prefix = f"{args.subj}_{args.session}_{args.task}_{args.run}"

    print(f"=== preproc: {prefix} ===", flush=True)

    print("\n--- Stage 0: Slice Timing Correction ---", flush=True)
    bold = apply_stc(args.bold, args.bids_sidecar, args.outdir, prefix)

    print(f"\n--- Stage 1: Extract BOLD reference (mean of {args.nss_frames} NSS frames) ---", flush=True)
    bold_ref = extract_bold_ref(bold, args.outdir, prefix, args.nss_frames)

    print("\n--- Stage 2: Precompute composite warp (bold→T1w→MNI) ---", flush=True)
    composite_warp = precompute_composite_warp(
        bold_ref=bold_ref,
        mni_template=args.mni_template,
        bold2t1w_affine=args.bold2t1w_affine,
        t1w2mni_affine=args.t1w2mni_affine,
        t1w2mni_warp=args.t1w2mni_warp,
        outdir=args.outdir,
    )

    print("\n--- Stage 3: Warp unmasked BOLD → MNI (LanczosWindowedSinc) ---", flush=True)
    bold_mni_unmasked = apply_transforms(
        bold=bold,
        mni_template=args.mni_template,
        composite_warp=composite_warp,
        outdir=args.outdir,
        prefix=prefix,
        threads=args.threads,
    )

    print("\n--- Stage 4: Warp brain mask → MNI (NearestNeighbor) ---", flush=True)
    mask_mni = warp_mask_to_mni(
        brainmask=args.brainmask,
        mni_template=args.mni_template,
        composite_warp=composite_warp,
        outdir=args.outdir,
        prefix=prefix,
    )

    print("\n--- Stage 5: Apply MNI mask ---", flush=True)
    mni_bold = apply_mni_mask(bold_mni_unmasked, mask_mni, args.outdir, prefix)
    bold_mni_unmasked.unlink()

    print("\n--- Stage 6: Confound Estimation ---", flush=True)
    compute_confounds(
        bold=bold,
        brainmask=args.brainmask,
        aseg=args.aseg,
        bold2t1w_affine=args.bold2t1w_affine,
        motion_file=args.motion_file,
        outdir=args.outdir,
        prefix=prefix,
        bold_ref=bold_ref,
        nss_frames=args.nss_frames,
    )

    print(f"\n=== preproc complete: {prefix} ===", flush=True)
    print(f"  MNI BOLD : {mni_bold}", flush=True)


if __name__ == "__main__":
    main()
