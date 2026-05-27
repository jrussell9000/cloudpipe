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
was computed against the original NIfTI header geometry.  The composite warp
precomputation and scipy coordinate building both use nibabel qform/sform-derived
affines to account for oblique geometry, so no explicit deoblique step is needed.

Stages:
  0. Slice timing correction                            (AFNI: 3dTshift)
  1. Extract 3D BOLD reference (mean of NSS frames)     (nibabel)
  2. Precompute composite displacement field            (ANTs: antsApplyTransforms)
  3. Warp unmasked BOLD → MNI (cubic B-spline)         (scipy: map_coordinates)
  4. Warp brain mask → MNI (nearest-neighbour)         (scipy: map_coordinates)
  5. Apply MNI-space mask to MNI-space BOLD             (AFNI: 3dcalc)
  6. Confound estimation

Masking is applied after warping rather than before to avoid Gibbs ringing
artifacts at the cortical boundary.  Applying a hard binary mask in native
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
  --bids-sidecar     BIDS JSON sidecar for the BOLD run (SliceTiming used for STC if present;
                     falls back to NIfTI header slice timing fields)
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
import contextlib
import gzip
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import nibabel as nib
import numpy as np
import pandas as pd
from scipy.ndimage import binary_erosion, map_coordinates
from sklearn.decomposition import PCA


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"\n>>> {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.run([str(c) for c in cmd], check=check)


def _mem_gb() -> float:
    """Current cgroup RSS in GB (v2 then v1 then process RSS)."""
    for _p in ('/sys/fs/cgroup/memory.current',
               '/sys/fs/cgroup/memory/memory.usage_in_bytes'):
        try:
            return int(Path(_p).read_text()) / 2 ** 30
        except (FileNotFoundError, ValueError):
            pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2 ** 20


@contextlib.contextmanager
def timed_stage(name: str, _timings: dict | None = None):
    t0 = time.monotonic()
    yield
    elapsed = time.monotonic() - t0
    print(f"  [timing] {name}: {elapsed:.1f}s ({elapsed / 60:.1f}m)", flush=True)
    if _timings is not None:
        _timings[name] = round(elapsed, 1)


# ---------------------------------------------------------------------------
# Stage 0: Slice timing correction
# ---------------------------------------------------------------------------

def apply_stc(bold: Path, bids_sidecar: Path, outdir: Path, prefix: str) -> Path:
    """Apply slice timing correction using AFNI 3dTshift.

    Slice timing offsets (in seconds, one per slice) are resolved in priority
    order:
      1. SliceTiming field in the BIDS JSON sidecar.
      2. NIfTI header fields (slice_code, slice_duration, slice_start,
         slice_end), read via nibabel's get_slice_times().
      3. If neither source is available, STC is skipped and the input bold
         path is returned unchanged.

    STC must run in native space before any spatial resampling — it cannot be
    applied retroactively after the MNI warp.
    """
    img  = nib.load(bold)
    hdr  = img.header

    with open(bids_sidecar) as f:
        meta = json.load(f)

    if 'SliceTiming' in meta:
        slice_times = meta['SliceTiming']
        print(f"  Using SliceTiming from BIDS sidecar ({len(slice_times)} slices).", flush=True)
    else:
        try:
            slice_times = list(hdr.get_slice_times())
            print(f"  SliceTiming absent from sidecar; using NIfTI header "
                  f"(slice_code={int(hdr['slice_code'])}, "
                  f"{len(slice_times)} slices).", flush=True)
        except nib.spatialimages.HeaderDataError:
            print("  No SliceTiming in sidecar or NIfTI header — skipping STC.", flush=True)
            return bold

    timing_path = Path('/tmp/slice_timing.txt')
    timing_path.write_text('\n'.join(str(t) for t in slice_times))

    tr  = float(nib.load(bold).header.get_zooms()[3])
    out = outdir / f"{prefix}_desc-stc_bold.nii.gz"
    run([
        '3dTshift',
        '-TR', f'{tr}s',
        '-tpattern', f'@{timing_path}',
        '-prefix', str(out),
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
    data     = np.asarray(img.dataobj[..., :nss_frames], dtype=np.float32)
    ref_data = data.mean(axis=-1)
    ref_path = outdir / f"{prefix}_desc-boldref.nii.gz"
    nib.save(nib.Nifti1Image(ref_data, img.affine, img.header), str(ref_path))
    return ref_path


# ---------------------------------------------------------------------------
# Warp helpers (used by Stages 3, 4, and 6)
# ---------------------------------------------------------------------------

def _read_itk_affine(path: Path) -> np.ndarray:
    """Read an ANTs/ITK affine (.mat/.txt) and return a 4×4 RAS-to-RAS matrix.

    ITK stores affines as LPS-to-LPS: x_out = M @ x_in + t.
    Flipping x and y on both sides converts to RAS-to-RAS.
    """
    text = Path(path).read_text()
    m = re.search(r'Parameters:\s*([\d.\-e ]+)', text)
    if m is None:
        raise ValueError(f"No Parameters line in {path}")
    params = list(map(float, m.group(1).split()))
    if len(params) != 12:
        raise ValueError(f"Expected 12 parameters, got {len(params)} in {path}")
    M_lps = np.array(params[:9]).reshape(3, 3)
    t_lps = np.array(params[9:12])
    flip  = np.diag([-1., -1., 1.])          # RAS ↔ LPS
    mat   = np.eye(4)
    mat[:3, :3] = flip @ M_lps @ flip
    mat[:3,  3] = flip @ t_lps
    return mat


def _build_warp_coords(composite_warp: Path, moving_affine: np.ndarray,
                       mni_img: nib.Nifti1Image) -> np.ndarray:
    """Load an ANTs composite displacement field and return moving-space voxel
    coordinates for each MNI output voxel as an array of shape (3, X, Y, Z).

    ANTs displacement fields store, for each reference-space voxel, the
    displacement from reference physical space to moving physical space in
    LPS mm (ITK convention).  Negating x and y converts to RAS.

    The returned coordinate array is suitable as the ``coordinates`` argument
    to scipy.ndimage.map_coordinates.
    """
    warp_img  = nib.load(composite_warp)
    warp_data = np.asarray(warp_img.dataobj, dtype=np.float64)
    if warp_data.ndim == 5:
        warp_data = warp_data[:, :, :, 0, :]   # (X, Y, Z, 3)

    # LPS → RAS: negate x and y displacement components
    warp_data[..., 0] *= -1
    warp_data[..., 1] *= -1

    mni_shape   = mni_img.shape[:3]
    mni_affine  = mni_img.affine
    inv_mov_aff = np.linalg.inv(moving_affine)

    i, j, k = np.mgrid[:mni_shape[0], :mni_shape[1], :mni_shape[2]]
    n       = i.size
    vox_hom = np.ones((4, n), dtype=np.float64)
    vox_hom[0] = i.ravel()
    vox_hom[1] = j.ravel()
    vox_hom[2] = k.ravel()
    del i, j, k

    mni_ras = (mni_affine @ vox_hom)[:3]                       # (3, N)
    del vox_hom
    disp    = warp_data.reshape(-1, 3).T                        # (3, N), RAS mm

    mov_ras_hom      = np.ones((4, n), dtype=np.float64)
    np.add(mni_ras, disp, out=mov_ras_hom[:3])     # avoids a ~0.17 GB intermediate
    del mni_ras, disp, warp_data   # disp is a view of warp_data; delete both together

    mov_vox = (inv_mov_aff @ mov_ras_hom)[:3]                  # (3, N)
    del mov_ras_hom

    return mov_vox.reshape(3, *mni_shape)


# ---------------------------------------------------------------------------
# Stage 2: Precompute composite displacement field
# ---------------------------------------------------------------------------

def precompute_composite_warp(
        bold_ref: Path, mni_template: Path, bold2t1w_affine: Path,
        t1w2mni_affine: Path, t1w2mni_warp: Path, outdir: Path) -> Path:
    """Collapse the bold→T1w affine + T1w→MNI affine + T1w→MNI warp into a
    single displacement field.

    Evaluated once per run (fast 3D operation).  The resulting field is consumed
    by _build_warp_coords, which converts it to a scipy-ready voxel-coordinate
    array shared across all frames.

    The [output,1] flag tells ANTs to write a displacement field rather than
    a warped image.  Transform order (ANTs last-to-first):
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
# Stage 3: Warp unmasked BOLD → MNI (scipy map_coordinates)
# ---------------------------------------------------------------------------

def apply_transforms(bold: Path, mni_template: Path,
                     composite_warp: Path, outdir: Path, prefix: str,
                     threads: int) -> tuple[Path, np.ndarray, np.ndarray]:
    """Warp the unmasked 4D BOLD to MNI space using the precomputed composite
    displacement field.

    The BOLD is intentionally left unmasked at this stage so that interpolation
    does not encounter the steep native-space brain boundary, which would produce
    ringing artefacts along the cortical ribbon.  Masking is applied in MNI space
    afterwards (Stage 5) once interpolation is complete.

    The warp coordinate map (_build_warp_coords) is built once and shared across
    all threads.  Each thread calls scipy.ndimage.map_coordinates for one frame;
    scipy's C extension releases the GIL so threads achieve real CPU parallelism.

    out_data is float16 (~1.6 GB for a 400-frame run at 2mm MNI resolution) rather
    than float32 (~3.2 GB), halving the Stage 3 memory peak.  scipy works internally
    in float64; the per-frame result is cast to float32 then narrowed to float16 on
    assignment.  Downstream tools (FSL, AFNI, FreeSurfer) promote float16 NIfTI to
    float32 on read; precision loss (~3.3 sig figs) is negligible for BOLD data.

    coords is returned to the caller so Stage 4 (warp_mask_to_mni) can reuse it
    without a second _build_warp_coords call (BOLD and brainmask share the same
    native-space affine).

    Peak RAM:  bold_data (1.28 GB) + coords (0.07 GB) + out_data (1.6 GB float16)
               ≈ 3 GB  vs. ~14.1 GB for 1mm float32.
    """
    mni_img   = nib.load(mni_template)
    bold_img  = nib.load(bold)
    n_frames  = bold_img.shape[3]
    bold_data = np.asarray(bold_img.dataobj, dtype=np.float32)

    coords    = _build_warp_coords(composite_warp, bold_img.affine, mni_img)

    mni_shape = mni_img.shape[:3]
    n_voxels  = int(np.prod(mni_shape))
    print(f"  Warping {n_frames} frames ({threads} threads, cubic B-spline) "
          f"| MNI {mni_shape} | mem: {_mem_gb():.2f} GB", flush=True)

    # Allocate time-first (n_frames, x, y, z) so each thread writes a single
    # contiguous 16 MB block (out_data[t]).  The alternative time-last layout
    # (x, y, z, n_frames) causes out_data[..., t] writes to touch one element
    # per 4 KB page across the full 6 GB array — a scattered page-fault storm
    # with 6 concurrent threads that exhausts the cgroup memory limit.
    out_data = np.empty((n_frames, *mni_shape), dtype=np.float16)

    # tSNR via Welford's online algorithm (avoids reloading 6 GB for mean/std).
    mean_acc = np.zeros(mni_shape, dtype=np.float64)
    M2_acc   = np.zeros(mni_shape, dtype=np.float64)
    wf_count = [0]
    wf_lock  = threading.Lock()

    def _warp_frame(t: int) -> None:
        frame_f32 = map_coordinates(
            bold_data[..., t].astype(np.float64), coords,
            order=3, mode='constant', cval=0.0,
        ).astype(np.float32)
        out_data[t] = frame_f32  # contiguous 16 MB write; no page-fault storm
        with wf_lock:
            wf_count[0] += 1
            delta        = frame_f32 - mean_acc
            mean_acc[:] += delta / wf_count[0]
            M2_acc[:]   += delta * (frame_f32 - mean_acc)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(_warp_frame, range(n_frames)))

    with np.errstate(divide="ignore", invalid="ignore"):
        _std     = np.sqrt(M2_acc / n_frames).astype(np.float32)
        tsnr_map = np.where(_std > 0, mean_acc.astype(np.float32) / _std,
                            0.0).astype(np.float32)
    del bold_data, mean_acc, M2_acc, _std

    # Write NIfTI manually to avoid a second 6 GB transpose copy.
    # out_data is (n_frames, x, y, z) C-order; NIfTI expects each 3D volume
    # stored in Fortran order (x-fastest).  np.asfortranarray converts each
    # 16 MB frame; frames are written in time order so the on-disk layout
    # matches (x, y, z, t) Fortran-order — identical to what nibabel would write.
    out_hdr = mni_img.header.copy()
    # nibabel's set_data_dtype rejects float16 in some versions; set the NIfTI1
    # datatype/bitpix fields directly (512 = DT_FLOAT16, bitpix = 16).
    out_hdr.structarr['datatype'] = 512
    out_hdr.structarr['bitpix']   = 16
    out_hdr.set_data_shape((*mni_shape, n_frames))
    out_hdr['vox_offset'] = 352.0

    out_nii = outdir / f"{prefix}_space-MNI152NLin2009cAsym_desc-unmasked_bold.nii"
    with open(str(out_nii), 'wb') as _f:
        _f.write(bytes(np.array(out_hdr.structarr).tobytes()))  # 348-byte header
        _f.write(b'\x00' * 4)                                   # pad to vox_offset=352
        for t in range(n_frames):
            _f.write(np.asfortranarray(out_data[t]).tobytes())  # 16 MB per frame
    del out_data

    # Compress .nii → .nii.gz and remove the uncompressed file (~6 GB on disk).
    # pigz (parallel gzip) compresses in ~5-10s vs ~60-90s for Python's single-
    # threaded gzip; fall back to gzip if pigz is not in PATH.
    out = outdir / f"{prefix}_space-MNI152NLin2009cAsym_desc-unmasked_bold.nii.gz"
    if shutil.which('pigz'):
        run(['pigz', '-p', str(threads), '-1', str(out_nii)])
        # pigz writes {out_nii}.gz in-place and removes the original
    else:
        with open(str(out_nii), 'rb') as _src, gzip.open(str(out), 'wb', compresslevel=1) as _dst:
            shutil.copyfileobj(_src, _dst)
        out_nii.unlink()

    return out, tsnr_map, coords


# ---------------------------------------------------------------------------
# Stage 4: Warp brain mask → MNI (scipy map_coordinates)
# ---------------------------------------------------------------------------

def warp_mask_to_mni(brainmask: Path, mni_template: Path,
                     composite_warp: Path, outdir: Path, prefix: str,
                     coords: "np.ndarray | None" = None) -> Path:
    """Warp the native-space brain mask to MNI space using nearest-neighbour
    interpolation (order=0) to preserve binary 0/1 values.

    The same composite warp used for the BOLD is applied here — the mask
    lives in native BOLD space, so the transform chain is identical.

    coords can be passed from apply_transforms (Stage 3) to avoid recomputing
    _build_warp_coords; the BOLD and brainmask share the same native-space affine.
    """
    mni_img   = nib.load(mni_template)
    mask_img  = nib.load(brainmask)
    mask_data = np.asarray(mask_img.dataobj, dtype=np.float32)

    if coords is None:
        coords = _build_warp_coords(composite_warp, mask_img.affine, mni_img)
    warped = map_coordinates(mask_data, coords, order=0, mode='constant', cval=0.0)

    out = outdir / f"{prefix}_space-MNI152NLin2009cAsym_brainmask.nii.gz"
    nib.save(nib.Nifti1Image((warped > 0.5).astype(np.uint8), mni_img.affine, mni_img.header),
             str(out))
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

    # --- Load BOLD metadata and brain mask ---
    bold_img  = nib.load(bold)
    n_frames  = bold_img.shape[3]
    tr        = float(bold_img.header.get_zooms()[3])
    mask_data = nib.load(brainmask).get_fdata() > 0

    # --- aCompCor tissue masks: precompute before loading bold_data so that
    #     bold_data can be freed immediately after ROI extraction. ---
    aseg_img  = nib.load(aseg)
    aseg_data = np.round(aseg_img.get_fdata()).astype(int)

    wm_arr  = np.isin(aseg_data, [2, 41]).astype(np.uint8)
    csf_arr = np.isin(aseg_data, [4, 14, 15, 43]).astype(np.uint8)
    # Erode WM by 1 voxel to reduce partial-volume contamination at GM boundary
    wm_arr  = binary_erosion(wm_arr, iterations=1).astype(np.uint8)

    # Precompute the BOLD voxel → T1w voxel coordinate map for tissue mask warping.
    # bold2t1w maps BOLD_RAS → T1w_RAS; applying it forward to each BOLD voxel
    # gives the corresponding T1w sampling location (no inversion needed).
    bold2t1w_ras = _read_itk_affine(bold2t1w_affine)
    bold_ref_img = nib.load(bold_ref)
    M_bold_to_t1w_vox = np.linalg.inv(aseg_img.affine) @ bold2t1w_ras @ bold_ref_img.affine
    bold_shape = bold_ref_img.shape[:3]
    gi, gj, gk = np.mgrid[:bold_shape[0], :bold_shape[1], :bold_shape[2]]
    vox_hom = np.ones((4, gi.size))
    vox_hom[0], vox_hom[1], vox_hom[2] = gi.ravel(), gj.ravel(), gk.ravel()
    t1w_coords = (M_bold_to_t1w_vox @ vox_hom)[:3].reshape(3, *bold_shape)
    del gi, gj, gk, vox_hom

    warped_wm  = map_coordinates(wm_arr.astype(np.float32),  t1w_coords,
                                 order=0, mode='constant', cval=0.0) > 0.5
    warped_csf = map_coordinates(csf_arr.astype(np.float32), t1w_coords,
                                 order=0, mode='constant', cval=0.0) > 0.5
    del wm_arr, csf_arr, t1w_coords

    # --- Load BOLD, extract all ROIs, free immediately (~1.28 GB) ---
    # All boolean masks are ready; boolean indexing produces copies, so
    # bold_data can be released as soon as extraction is complete.
    bold_data = bold_img.get_fdata(dtype=np.float32)               # (X, Y, Z, T)
    brain_ts  = bold_data[mask_data].T                             # (T, n_brain)
    wm_ts     = bold_data[warped_wm].T   if warped_wm.any()  else None
    csf_ts    = bold_data[warped_csf].T  if warped_csf.any() else None
    del bold_data

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
    n_cosines = int(np.floor(2 * n_frames * tr / 128.0))
    t_idx = np.arange(n_frames)
    cosine_cols: dict = {
        f'cosine_{k:02d}': np.sqrt(2 / n_frames) * np.cos(np.pi / n_frames * k * (t_idx + 0.5))
        for k in range(1, n_cosines + 1)
    }

    # --- aCompCor: WM and CSF components (from pre-extracted ROI arrays) ---
    comp_cor_cols: dict = {}
    for tissue, ts in [('wm', wm_ts), ('csf', csf_ts)]:
        n_vox  = 0 if ts is None else ts.shape[1]
        if n_vox == 0:
            print(
                f"WARNING: no {tissue.upper()} voxels in BOLD space after warping "
                f"— skipping {tissue} CompCor",
                flush=True,
            )
            continue

        roi_ts = ts - ts.mean(axis=0)  # voxelwise demean

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
        't_indx': np.arange(n_frames),
        **nss_outlier_cols,
        **mp_dict,
        'framewise_displacement': fd,
        'dvars': dvars,
        'global_signal': global_signal,
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
# QC summary
# ---------------------------------------------------------------------------

def compute_func_qc_summary(
    confounds: "pd.DataFrame",
    bold_img: "nib.Nifti1Image",
    mni_bold: Path,
    mask_mni: Path,
    stage_timings: dict,
    total_runtime_s: float,
    peak_memory_gb: float,
    args: "argparse.Namespace",
    tsnr_map: "np.ndarray | None" = None,
) -> dict:
    """Compute a per-run QC summary dict from pipeline outputs.

    Called after confound estimation; inputs are already in memory/on disk.
    Returns a dict matching the FuncQC schema in tools/metrics/schemas.py.
    """
    from datetime import datetime, timezone

    fd = confounds["framewise_displacement"].dropna().values
    dvars = confounds["dvars"].dropna().values

    # Temporal SNR: median over brain voxels of (mean / std across time).
    # tsnr_map is pre-computed in apply_transforms to avoid reloading the
    # full 4D MNI BOLD (~12 GB) after it has already been freed from memory.
    mask_data = nib.load(mask_mni).get_fdata() > 0
    if tsnr_map is None:
        bold_data = nib.load(mni_bold).get_fdata(dtype=np.float32)
        with np.errstate(divide="ignore", invalid="ignore"):
            tsnr_map = np.where(
                bold_data.std(axis=-1) > 0,
                bold_data.mean(axis=-1) / bold_data.std(axis=-1),
                0.0,
            )
    tsnr_median = float(np.median(tsnr_map[mask_data])) if mask_data.any() else 0.0

    # Count confound regressor columns by prefix
    cols = list(confounds.columns)
    n_acompcor_wm  = sum(1 for c in cols if c.startswith("a_comp_cor_wm_"))
    n_acompcor_csf = sum(1 for c in cols if c.startswith("a_comp_cor_csf_"))
    n_tcompcor     = sum(1 for c in cols if c.startswith("t_comp_cor_"))
    n_cosines      = sum(1 for c in cols if c.startswith("cosine_"))

    n_frames = int(bold_img.shape[-1])
    tr = float(bold_img.header.get_zooms()[3])

    n_above_0p2 = int((fd > 0.2).sum())
    n_above_0p5 = int((fd > 0.5).sum())

    return {
        "schema_version": "1.0",
        "pipeline": getattr(args, "pipeline", "cloudpipe_minproc"),
        "image_tag": getattr(args, "image_tag", ""),
        "subject": args.subj,
        "session": args.session,
        "task": args.task,
        "run": args.run,
        "n_frames": n_frames,
        "n_nss_frames": args.nss_frames,
        "tr_seconds": round(tr, 4),
        "mean_fd": round(float(fd.mean()), 4) if fd.size else 0.0,
        "median_fd": round(float(np.median(fd)), 4) if fd.size else 0.0,
        "max_fd": round(float(fd.max()), 4) if fd.size else 0.0,
        "n_fd_above_0p2": n_above_0p2,
        "n_fd_above_0p5": n_above_0p5,
        "pct_fd_above_0p5": round(100.0 * n_above_0p5 / len(fd), 2) if fd.size else 0.0,
        "mean_dvars": round(float(dvars.mean()), 4) if dvars.size else 0.0,
        "mean_global_signal": round(
            float(confounds["global_signal"].mean()) if "global_signal" in confounds else 0.0, 4
        ),
        "tsnr_median": round(tsnr_median, 2),
        "n_acompcor_wm": n_acompcor_wm,
        "n_acompcor_csf": n_acompcor_csf,
        "n_tcompcor": n_tcompcor,
        "n_cosines": n_cosines,
        "stage_timings_s": stage_timings,
        "total_runtime_s": round(total_runtime_s, 1),
        "peak_memory_gb": round(peak_memory_gb, 2),
        "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BOLD preprocessing: STC → composite warp → scipy MNI warp → mask → confounds"
    )
    p.add_argument("--bold", required=True, type=Path)
    p.add_argument("--bids-sidecar", required=True, type=Path,
                   help="BIDS JSON sidecar for the BOLD run (SliceTiming used for STC)")
    p.add_argument("--brainmask", required=True, type=Path)
    p.add_argument("--mni-template", required=True, type=Path)
    p.add_argument("--bold2t1w-affine", required=True, type=Path)
    p.add_argument("--t1w2mni-affine", required=True, type=Path)
    p.add_argument("--t1w2mni-warp", required=True, type=Path)
    p.add_argument("--outdir", required=True, type=Path)
    p.add_argument("--subj", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--aseg", required=True, type=Path,
                   help="aseg.mgz from FastSurfer subjects dir (for WM/CSF masks)")
    p.add_argument("--motion-file", required=True, type=Path,
                   help="ABCD 3dvolreg motion parameter TSV")
    p.add_argument("--nss-frames", required=True, type=int,
                   help="Number of non-steady-state frames at the start of the run")
    p.add_argument("--pipeline", default="cloudpipe_minproc",
                   help="Pipeline name written into QC JSON provenance")
    p.add_argument("--image-tag", default="",
                   help="Docker image tag written into QC JSON provenance")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    os.environ["ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS"] = str(args.threads)

    prefix = f"{args.subj}_{args.session}_{args.task}_{args.run}"

    print(f"=== preproc: {prefix} ===", flush=True)
    pipeline_start = time.monotonic()
    _timings: dict = {}

    print("\n--- Stage 0: Slice Timing Correction ---", flush=True)
    with timed_stage("stc", _timings):
        bold = apply_stc(args.bold, args.bids_sidecar, args.outdir, prefix)

    print(f"\n--- Stage 1: Extract BOLD reference (mean of {args.nss_frames} NSS frames) ---", flush=True)
    with timed_stage("boldref", _timings):
        bold_ref = extract_bold_ref(bold, args.outdir, prefix, args.nss_frames)

    print("\n--- Stage 2: Precompute composite warp (bold→T1w→MNI) ---", flush=True)
    with timed_stage("composite_warp", _timings):
        composite_warp = precompute_composite_warp(
            bold_ref=bold_ref,
            mni_template=args.mni_template,
            bold2t1w_affine=args.bold2t1w_affine,
            t1w2mni_affine=args.t1w2mni_affine,
            t1w2mni_warp=args.t1w2mni_warp,
            outdir=args.outdir,
        )

    print("\n--- Stage 3: Warp unmasked BOLD → MNI (cubic B-spline) ---", flush=True)
    with timed_stage("4d_warp", _timings):
        bold_mni_unmasked, tsnr_map, warp_coords = apply_transforms(
            bold=bold,
            mni_template=args.mni_template,
            composite_warp=composite_warp,
            outdir=args.outdir,
            prefix=prefix,
            threads=args.threads,
        )

    print("\n--- Stage 4: Warp brain mask → MNI (NearestNeighbor) ---", flush=True)
    with timed_stage("mask_warp", _timings):
        mask_mni = warp_mask_to_mni(
            brainmask=args.brainmask,
            mni_template=args.mni_template,
            composite_warp=composite_warp,
            outdir=args.outdir,
            prefix=prefix,
            coords=warp_coords,
        )
    del warp_coords

    composite_warp.unlink()  # ~200 MB; not needed after Stage 4

    print("\n--- Stage 5: Apply MNI mask ---", flush=True)
    with timed_stage("masking", _timings):
        mni_bold = apply_mni_mask(bold_mni_unmasked, mask_mni, args.outdir, prefix)
        bold_mni_unmasked.unlink()

    print("\n--- Stage 6: Confound Estimation ---", flush=True)
    with timed_stage("confounds", _timings):
        confounds_tsv = compute_confounds(
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

    if bold != args.bold:
        bold.unlink()  # STC'd BOLD (~1.28 GB); keep original input, remove intermediate

    total = time.monotonic() - pipeline_start
    print(f"\n=== preproc complete: {prefix} ({total:.1f}s / {total / 60:.1f}m) ===", flush=True)
    print(f"  MNI BOLD : {mni_bold}", flush=True)

    # Cgroup memory.peak covers all processes in the container (Python + AFNI subprocesses).
    # Try cgroupv2 first, then cgroupv1, then fall back to process-only RSS.
    for _cg in ('/sys/fs/cgroup/memory.peak', '/sys/fs/cgroup/memory/memory.max_usage_in_bytes'):
        try:
            peak_gb = int(Path(_cg).read_text()) / 2 ** 30
            print(f"  Peak memory: {peak_gb:.2f} GB (cgroup, includes all subprocesses)", flush=True)
            break
        except (FileNotFoundError, ValueError):
            pass
    else:
        peak_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2 ** 20
        print(f"  Peak memory: {peak_gb:.2f} GB (process RSS only — cgroup unavailable)", flush=True)

    confounds_df = pd.read_csv(confounds_tsv, sep='\t', na_values='n/a')
    qc = compute_func_qc_summary(
        confounds=confounds_df,
        bold_img=nib.load(bold),
        mni_bold=mni_bold,
        mask_mni=mask_mni,
        stage_timings=_timings,
        total_runtime_s=total,
        peak_memory_gb=peak_gb,
        args=args,
        tsnr_map=tsnr_map,
    )
    qc_path = Path(f"/tmp/{prefix}_qc.json")
    qc_path.write_text(json.dumps(qc))
    print(f"  QC summary: {qc_path}", flush=True)


if __name__ == "__main__":
    main()
