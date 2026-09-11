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

  Slice timing correction is NOT part of ABCD minimal preprocessing, and is
  deliberately not applied here either.  This script used to open with an STC
  stage, but ABCD ships neither a SliceTiming sidecar field nor the NIfTI
  header slice fields, so the stage detected no timing and returned the input
  untouched on every run ever processed.  It was removed in full rather than
  left as a no-op guarding an AFNI binary (3dTshift) that is not even present
  in this image — see #119 for how that combination fails.  STC would have to
  be reinstated as a native-space step before this script; it cannot be applied
  after Stage 3 (MNI warp).

Deobliquing is intentionally omitted: the BOLD→T1w affine (from SynthMorph)
was computed against the original NIfTI header geometry.  The composite warp
precomputation and scipy coordinate building both use nibabel qform/sform-derived
affines to account for oblique geometry, so no explicit deoblique step is needed.

Stages:
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
  --brainmask        Brain mask in native BOLD space (from bold_to_t1w.py)
  --mni-template     MNI152NLin2009cAsym reference NIfTI (for output grid)
  --bold2t1w-affine  BOLD/T1w rigid affine (.mat, from bold_to_t1w.py). Despite the
                     name, _read_itk_affine returns it as T1w_RAS → BOLD_RAS — ITK
                     stores pullbacks. antsApplyTransforms wants it as-is; numpy
                     callers must pick a direction deliberately.
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

Exit codes:
  0   success — including a Stage 5b downgrade to volumetric-only
  66  a required input does not exist; nothing was computed (EXIT_MISSING_INPUT)
  1   anything else
"""

import argparse
import contextlib
import gzip
import json
import math
import os
import re
import resource
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import NormalDist

import nibabel as nib
import numpy as np
import pandas as pd
from scipy.ndimage import binary_erosion, map_coordinates
from scipy.stats import rankdata
from sklearn.decomposition import PCA

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"\n>>> {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.run([str(c) for c in cmd], check=check)


def _mem_gb() -> float:
    """Current cgroup RSS in GB (v2 then v1 then process RSS)."""
    for _p in ('/sys/fs/cgroup/memory.current', '/sys/fs/cgroup/memory/memory.usage_in_bytes'):
        try:
            return int(Path(_p).read_text()) / 2**30
        except (FileNotFoundError, ValueError):
            pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20


@contextlib.contextmanager
def timed_stage(name: str, _timings: dict | None = None):
    t0 = time.monotonic()
    yield
    elapsed = time.monotonic() - t0
    print(f"  [timing] {name}: {elapsed:.1f}s ({elapsed / 60:.1f}m)", flush=True)
    if _timings is not None:
        _timings[name] = round(elapsed, 1)


# ---------------------------------------------------------------------------
# Stage 1: Extract 3D BOLD reference (first frame)
# ---------------------------------------------------------------------------


def extract_bold_ref(bold: Path, outdir: Path, prefix: str, nss_frames: int) -> Path:
    """Compute the mean of the non-steady-state frames as a 3D BOLD reference.

    NSS frames have elevated T1 contrast (magnetisation not yet at steady
    state), which improves boundary sharpness for the composite warp geometry
    input and for tissue-mask warping in confound estimation.

    Used as the -i geometry input when precomputing the composite warp and
    reused in confound estimation for tissue-mask warping.
    """
    img = nib.load(bold)
    data = np.asarray(img.dataobj[..., :nss_frames], dtype=np.float32)
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
    flip = np.diag([-1.0, -1.0, 1.0])  # RAS ↔ LPS
    mat = np.eye(4)
    mat[:3, :3] = flip @ M_lps @ flip
    mat[:3, 3] = flip @ t_lps
    return mat


def _build_warp_coords(
    composite_warp: Path, moving_affine: np.ndarray, mni_img: nib.Nifti1Image
) -> np.ndarray:
    """Load an ANTs composite displacement field and return moving-space voxel
    coordinates for each MNI output voxel as an array of shape (3, X, Y, Z).

    ANTs displacement fields store, for each reference-space voxel, the
    displacement from reference physical space to moving physical space in
    LPS mm (ITK convention).  Negating x and y converts to RAS.

    The returned coordinate array is suitable as the ``coordinates`` argument
    to scipy.ndimage.map_coordinates.
    """
    warp_img = nib.load(composite_warp)
    warp_data = np.asarray(warp_img.dataobj, dtype=np.float64)
    if warp_data.ndim == 5:
        warp_data = warp_data[:, :, :, 0, :]  # (X, Y, Z, 3)

    # LPS → RAS: negate x and y displacement components
    warp_data[..., 0] *= -1
    warp_data[..., 1] *= -1

    mni_shape = mni_img.shape[:3]
    mni_affine = mni_img.affine
    inv_mov_aff = np.linalg.inv(moving_affine)

    i, j, k = np.mgrid[: mni_shape[0], : mni_shape[1], : mni_shape[2]]
    n = i.size
    vox_hom = np.ones((4, n), dtype=np.float64)
    vox_hom[0] = i.ravel()
    vox_hom[1] = j.ravel()
    vox_hom[2] = k.ravel()
    del i, j, k

    mni_ras = (mni_affine @ vox_hom)[:3]  # (3, N)
    del vox_hom
    disp = warp_data.reshape(-1, 3).T  # (3, N), RAS mm

    mov_ras_hom = np.ones((4, n), dtype=np.float64)
    np.add(mni_ras, disp, out=mov_ras_hom[:3])  # avoids a ~0.17 GB intermediate
    del mni_ras, disp, warp_data  # disp is a view of warp_data; delete both together

    mov_vox = (inv_mov_aff @ mov_ras_hom)[:3]  # (3, N)
    del mov_ras_hom

    return mov_vox.reshape(3, *mni_shape)


# ---------------------------------------------------------------------------
# Stage 2: Precompute composite displacement field
# ---------------------------------------------------------------------------


def precompute_composite_warp(
    bold_ref: Path,
    mni_template: Path,
    bold2t1w_affine: Path,
    t1w2mni_affine: Path,
    t1w2mni_warp: Path,
    outdir: Path,
) -> Path:
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
    run(
        [
            "antsApplyTransforms",
            "-d",
            "3",
            "-i",
            str(bold_ref),
            "-r",
            str(mni_template),
            "-o",
            f"[{composite},1]",
            "-t",
            str(t1w2mni_warp),
            "-t",
            str(t1w2mni_affine),
            "-t",
            str(bold2t1w_affine),
        ]
    )
    return composite


# ---------------------------------------------------------------------------
# Stage 3: Warp unmasked BOLD → MNI (scipy map_coordinates)
# ---------------------------------------------------------------------------


def apply_transforms(
    bold: Path, mni_template: Path, composite_warp: Path, outdir: Path, prefix: str, threads: int
) -> tuple[Path, np.ndarray, np.ndarray]:
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
    mni_img = nib.load(mni_template)
    bold_img = nib.load(bold)
    n_frames = bold_img.shape[3]
    bold_data = np.asarray(bold_img.dataobj, dtype=np.float32)

    coords = _build_warp_coords(composite_warp, bold_img.affine, mni_img)

    mni_shape = mni_img.shape[:3]
    print(
        f"  Warping {n_frames} frames ({threads} threads, cubic B-spline) "
        f"| MNI {mni_shape} | mem: {_mem_gb():.2f} GB",
        flush=True,
    )

    # Allocate time-first (n_frames, x, y, z) so each thread writes a single
    # contiguous 16 MB block (out_data[t]).  The alternative time-last layout
    # (x, y, z, n_frames) causes out_data[..., t] writes to touch one element
    # per 4 KB page across the full 6 GB array — a scattered page-fault storm
    # with 6 concurrent threads that exhausts the cgroup memory limit.
    out_data = np.empty((n_frames, *mni_shape), dtype=np.float16)

    # tSNR via Welford's online algorithm (avoids reloading 6 GB for mean/std).
    mean_acc = np.zeros(mni_shape, dtype=np.float64)
    M2_acc = np.zeros(mni_shape, dtype=np.float64)
    wf_count = [0]
    wf_lock = threading.Lock()

    def _warp_frame(t: int) -> None:
        frame_f32 = map_coordinates(
            bold_data[..., t].astype(np.float64),
            coords,
            order=3,
            mode='constant',
            cval=0.0,
        ).astype(np.float32)
        out_data[t] = frame_f32  # contiguous 16 MB write; no page-fault storm
        with wf_lock:
            wf_count[0] += 1
            delta = frame_f32 - mean_acc
            mean_acc[:] += delta / wf_count[0]
            M2_acc[:] += delta * (frame_f32 - mean_acc)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(_warp_frame, range(n_frames)))

    with np.errstate(divide="ignore", invalid="ignore"):
        _std = np.sqrt(M2_acc / n_frames).astype(np.float32)
        tsnr_map = np.where(_std > 0, mean_acc.astype(np.float32) / _std, 0.0).astype(np.float32)
    # Released by rebinding rather than `del`: these three are captured by the
    # _warp_frame closure above, and `del`ing a closed-over name makes pyflakes
    # (F821) treat every use inside the closure as unbound. Rebinding drops the
    # same reference at the same point, so the arrays are freed identically.
    bold_data = mean_acc = M2_acc = None
    del _std

    # Write NIfTI manually to avoid a second 6 GB transpose copy.
    # out_data is (n_frames, x, y, z) C-order; NIfTI expects each 3D volume
    # stored in Fortran order (x-fastest).  np.asfortranarray converts each
    # 16 MB frame; frames are written in time order so the on-disk layout
    # matches (x, y, z, t) Fortran-order — identical to what nibabel would write.
    out_hdr = mni_img.header.copy()
    # nibabel's set_data_dtype rejects float16 in some versions; set the NIfTI1
    # datatype/bitpix fields directly (512 = DT_FLOAT16, bitpix = 16).
    out_hdr.structarr['datatype'] = 512
    out_hdr.structarr['bitpix'] = 16
    out_hdr.set_data_shape((*mni_shape, n_frames))
    out_hdr['vox_offset'] = 352.0
    # pixdim[4] is the TR, and out_hdr is copied from the MNI *template* — a 3D
    # file whose pixdim[4] is a meaningless 1.0. Without this the output claims
    # a 1 s TR regardless of the sequence, which sails through any plausibility
    # check and silently rescales every frequency-domain analysis reading the
    # header (including the CIFTI -timestep, which is derived from this file).
    out_hdr.structarr['pixdim'][4] = float(bold_img.header.get_zooms()[3])

    out_nii = outdir / f"{prefix}_space-MNI152NLin2009cAsym_desc-unmasked_bold.nii"
    with open(str(out_nii), 'wb') as _f:
        _f.write(bytes(np.array(out_hdr.structarr).tobytes()))  # 348-byte header
        _f.write(b'\x00' * 4)  # pad to vox_offset=352
        for t in range(n_frames):
            _f.write(np.asfortranarray(out_data[t]).tobytes())  # 16 MB per frame
    out_data = None  # rebind, not `del` — closed over by _warp_frame (see above)

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


def warp_mask_to_mni(
    brainmask: Path,
    mni_template: Path,
    composite_warp: Path,
    outdir: Path,
    prefix: str,
    coords: "np.ndarray | None" = None,
) -> Path:
    """Warp the native-space brain mask to MNI space using nearest-neighbour
    interpolation (order=0) to preserve binary 0/1 values.

    The same composite warp used for the BOLD is applied here — the mask
    lives in native BOLD space, so the transform chain is identical.

    coords can be passed from apply_transforms (Stage 3) to avoid recomputing
    _build_warp_coords; the BOLD and brainmask share the same native-space affine.
    """
    mni_img = nib.load(mni_template)
    mask_img = nib.load(brainmask)
    mask_data = np.asarray(mask_img.dataobj, dtype=np.float32)

    if coords is None:
        coords = _build_warp_coords(composite_warp, mask_img.affine, mni_img)
    warped = map_coordinates(mask_data, coords, order=0, mode='constant', cval=0.0)

    out = outdir / f"{prefix}_space-MNI152NLin2009cAsym_brainmask.nii.gz"
    nib.save(
        nib.Nifti1Image((warped > 0.5).astype(np.uint8), mni_img.affine, mni_img.header), str(out)
    )
    return out


# ---------------------------------------------------------------------------
# Stage 5: Apply MNI-space mask to MNI-space BOLD (3dcalc)
# ---------------------------------------------------------------------------


def apply_mni_mask(bold_mni: Path, mask_mni: Path, outdir: Path, prefix: str) -> Path:
    """Zero non-brain voxels in MNI space after warping is complete.

    Masking here rather than before the warp prevents LanczosWindowedSinc
    from ringing across the sharp native-space brain boundary.
    """
    out = outdir / f"{prefix}_space-MNI152NLin2009cAsym_bold.nii.gz"
    run(
        [
            "3dcalc",
            "-a",
            str(bold_mni),
            "-b",
            str(mask_mni),
            "-expr",
            "a*step(b)",
            "-prefix",
            str(out),
        ]
    )
    return out


# ---------------------------------------------------------------------------
# Stage 5b: Grayordinate extraction (cortical surface + subcortical volume)
# ---------------------------------------------------------------------------

# The 19 standard CIFTI subcortical structures, keyed by their FreeSurfer aseg
# label. Structure *names* are the CIFTI vocabulary wb_command expects; the
# integer keys are what we write into the label volume, and the sidecar list
# lets Stage 2 turn it into a proper label volume via -volume-label-import.
CIFTI_SUBCORTICAL: dict[int, str] = {
    26: "ACCUMBENS_LEFT",
    58: "ACCUMBENS_RIGHT",
    18: "AMYGDALA_LEFT",
    54: "AMYGDALA_RIGHT",
    16: "BRAIN_STEM",
    11: "CAUDATE_LEFT",
    50: "CAUDATE_RIGHT",
    8: "CEREBELLUM_LEFT",
    47: "CEREBELLUM_RIGHT",
    28: "DIENCEPHALON_VENTRAL_LEFT",
    60: "DIENCEPHALON_VENTRAL_RIGHT",
    17: "HIPPOCAMPUS_LEFT",
    53: "HIPPOCAMPUS_RIGHT",
    13: "PALLIDUM_LEFT",
    52: "PALLIDUM_RIGHT",
    12: "PUTAMEN_LEFT",
    51: "PUTAMEN_RIGHT",
    10: "THALAMUS_LEFT",
    49: "THALAMUS_RIGHT",
}


def _tkr_to_scanner_ras(img: "nib.freesurfer.mghformat.MGHImage") -> np.ndarray:
    """Return the 4×4 taking FreeSurfer surface (tkr) RAS to scanner RAS.

    FreeSurfer surface vertices are stored in the *tkrRAS* frame of the
    conformed volume, whose origin is the volume centre — not the scanner
    origin the volume's affine describes. Sampling a surface against any
    image without this conversion silently lands the mesh tens of mm off.
    """
    return img.header.get_vox2ras() @ np.linalg.inv(img.header.get_vox2ras_tkr())


# ---------------------------------------------------------------------------
# bold2t1w direction
#
# _read_itk_affine returns the rigid transform as T1w_RAS → BOLD_RAS — ITK
# stores pullbacks, and the `bold2t1w` filename describes the registration, not
# the matrix. The pipeline traverses it both ways, so the two directions live
# here side by side rather than as a comment at each use: a comment cannot be
# tested, and four agreeing comments are what hid this being backwards until
# 2026-07-23. test_bold2t1w_direction.py composes these two and asserts the
# registration cancels, which fails if either one flips.
# ---------------------------------------------------------------------------


def _tkr_ras_to_bold_vox(
    t1w2bold_ras: np.ndarray,
    aseg_img: "nib.freesurfer.mghformat.MGHImage",
    bold_ref_affine: np.ndarray,
) -> np.ndarray:
    """4×4 taking FreeSurfer surface (tkr) RAS to BOLD voxel indices.

    Travels *with* the matrix (T1w out to BOLD), so it is applied as-is.
    """
    return np.linalg.inv(bold_ref_affine) @ t1w2bold_ras @ _tkr_to_scanner_ras(aseg_img)


def _bold_vox_to_aseg_vox(
    t1w2bold_ras: np.ndarray,
    aseg_affine: np.ndarray,
    bold_ref_affine: np.ndarray,
) -> np.ndarray:
    """4×4 taking BOLD voxel indices to aseg (conformed-T1w) voxel indices.

    Travels *against* the matrix (BOLD back to T1w), so it inverts it.
    """
    return np.linalg.inv(aseg_affine) @ np.linalg.inv(t1w2bold_ras) @ bold_ref_affine


def sample_cortical_ribbon(
    bold: Path,
    bold_ref: Path,
    aseg: Path,
    surf_dir: Path,
    bold2t1w_affine: Path,
    outdir: Path,
    prefix: str,
    n_depths: int = 5,
    threads: int = 1,
) -> dict[str, Path]:
    """Sample the native BOLD onto each hemisphere's cortical ribbon.

    For every vertex, `n_depths` points are placed between the white and pial
    surfaces and the BOLD is trilinearly interpolated at each, then averaged.
    Depths are drawn strictly inside the ribbon (endpoints excluded) so that
    samples do not sit exactly on the white or pial boundary, where partial
    volume with WM or CSF is worst.

    Sampling reads the *native* BOLD rather than the MNI output: the surfaces
    already live in the conformed-T1w frame the bold2t1w transform targets, so
    this is a single interpolation from the source data instead of a second
    resampling of data already warped to MNI.

    Returns {"L": path, "R": path} of per-hemisphere `.func.gii`.
    """
    aseg_img = nib.load(aseg)
    bold_img = nib.load(bold)
    bold_ref_img = nib.load(bold_ref)
    n_frames = bold_img.shape[3]

    # tkrRAS → scanner RAS → BOLD RAS → BOLD voxel.
    M = _tkr_ras_to_bold_vox(_read_itk_affine(bold2t1w_affine), aseg_img, bold_ref_img.affine)

    fractions = np.linspace(0.0, 1.0, n_depths + 2)[1:-1]

    coords: dict[str, np.ndarray] = {}
    n_vert: dict[str, int] = {}
    for hemi, fs_hemi in (("L", "lh"), ("R", "rh")):
        white, _ = nib.freesurfer.read_geometry(str(surf_dir / f"{fs_hemi}.white"))
        pial, _ = nib.freesurfer.read_geometry(str(surf_dir / f"{fs_hemi}.pial"))
        if white.shape != pial.shape:
            raise ValueError(
                f"{fs_hemi}: white/pial vertex counts differ ({white.shape[0]} vs {pial.shape[0]})"
            )
        n_vert[hemi] = white.shape[0]

        # (n_depths, n_vert, 3) sample points in tkrRAS, then to BOLD voxels.
        pts = np.stack([white + f * (pial - white) for f in fractions])
        hom = np.ones((4, pts.shape[0] * pts.shape[1]), dtype=np.float64)
        hom[:3] = pts.reshape(-1, 3).T
        coords[hemi] = (M @ hom)[:3]
        del pts, hom
        print(f"  {hemi}: {n_vert[hemi]} vertices × {n_depths} depths", flush=True)

    acc = {h: np.zeros((n_vert[h], n_frames), dtype=np.float32) for h in coords}

    # Read the 4D array in one pass rather than frame by frame. nibabel's
    # ArrayProxy only does true random access into a .nii.gz when indexed_gzip
    # is installed, which it is not here; without it every dataobj[..., t]
    # opens a fresh handle and inflates the stream from byte zero to reach
    # frame t. That is O(n^2) over a run: measured on a 383-frame rest BOLD,
    # per-frame reads cost ~820 s against 5.3 s for one bulk read, and
    # accounted for essentially all of this stage's runtime.
    #
    # The cost is 0.69 GB resident for a 383-frame run. Should indexed_gzip
    # ever land in the image, revisit this — random access would then be cheap
    # and streaming would bound memory for free.
    bold_data = np.asarray(bold_img.dataobj, dtype=np.float32)

    # With the data resident there is no file handle to contend over, so frames
    # parallelise cleanly — map_coordinates releases the GIL, the same property
    # apply_transforms relies on to thread its warp. Each frame owns column t
    # of every accumulator, so the writes are disjoint and need no lock.
    def _sample_frame(t: int) -> None:
        vol = bold_data[..., t]
        for hemi, crd in coords.items():
            s = map_coordinates(vol, crd, order=1, mode="constant", cval=0.0)
            acc[hemi][:, t] = s.reshape(n_depths, n_vert[hemi]).mean(axis=0)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(_sample_frame, range(n_frames)))
    # Rebind, not `del` — both are closed over by _sample_frame (F821); see the
    # note in apply_transforms. Same reference drop, same point in the function.
    coords = bold_data = None

    out: dict[str, Path] = {}
    for hemi, ts in acc.items():
        gii = nib.gifti.GiftiImage(
            meta=nib.gifti.GiftiMetaData(
                {
                    "AnatomicalStructurePrimary": ("CortexLeft" if hemi == "L" else "CortexRight"),
                }
            ),
            darrays=[
                nib.gifti.GiftiDataArray(
                    data=ts[:, t],
                    intent="NIFTI_INTENT_TIME_SERIES",
                    datatype="NIFTI_TYPE_FLOAT32",
                    encoding="GZipBase64Binary",
                )
                for t in range(n_frames)
            ],
        )
        path = outdir / f"{prefix}_hemi-{hemi}_space-fsnative_bold.func.gii"
        nib.save(gii, path)
        out[hemi] = path
        print(f"  wrote {path.name}", flush=True)
    return out


def extract_subcortical(
    mni_bold: Path,
    aseg: Path,
    mni_template: Path,
    t1w2mni_affine: Path,
    t1w2mni_warp: Path,
    outdir: Path,
    prefix: str,
) -> tuple[Path, Path, Path]:
    """Build the CIFTI subcortical block on the MNI BOLD's own grid.

    The aseg is warped from conformed-T1w space to MNI with nearest-neighbour
    interpolation, restricted to the 19 standard CIFTI structures, and used to
    mask the MNI BOLD. Because the BOLD is *already* on that grid this is pure
    indexing — no second interpolation of the timeseries.

    Volume space is MNI152NLin2009cAsym, matching the rest of the pipeline
    rather than the MNI152NLin6Asym grid standard 91282-grayordinate files use;
    see the openspec change `add-surface-func-processing` Decision 2a.

    Returns (bold_path, label_path, label_list_path).
    """
    aseg_mni = outdir / f"{prefix}_desc-asegMNI_dseg.nii.gz"
    run(
        [
            "antsApplyTransforms",
            "-d",
            "3",
            "-i",
            str(aseg),
            "-r",
            str(mni_template),
            "-o",
            str(aseg_mni),
            "-n",
            "NearestNeighbor",
            "-t",
            str(t1w2mni_warp),
            "-t",
            str(t1w2mni_affine),
        ]
    )

    bold_img = nib.load(mni_bold)
    lab_src = np.round(nib.load(aseg_mni).get_fdata()).astype(np.int32)

    labels = np.zeros(lab_src.shape, dtype=np.int16)
    present: dict[int, str] = {}
    for key, (aseg_val, name) in enumerate(sorted(CIFTI_SUBCORTICAL.items()), start=1):
        hit = lab_src == aseg_val
        if hit.any():
            labels[hit] = key
            present[key] = name
        else:
            print(f"  WARNING: no voxels for {name} (aseg {aseg_val})", flush=True)
    del lab_src

    mask = labels > 0
    n_vox = int(mask.sum())
    if n_vox == 0:
        raise ValueError("no subcortical voxels survived the aseg warp")

    data = bold_img.get_fdata(dtype=np.float32)
    data[~mask] = 0.0
    bold_out = outdir / f"{prefix}_space-MNI152NLin2009cAsym_desc-subcort_bold.nii.gz"
    nib.save(nib.Nifti1Image(data, bold_img.affine, bold_img.header), bold_out)
    del data

    label_out = outdir / f"{prefix}_space-MNI152NLin2009cAsym_desc-subcort_dseg.nii.gz"
    nib.save(nib.Nifti1Image(labels, bold_img.affine), label_out)

    # wb_command -volume-label-import format: name line, then "key R G B A".
    # Colours are irrelevant to the dtseries but the importer requires them.
    list_out = outdir / f"{prefix}_desc-subcort_labellist.txt"
    list_out.write_text(
        "".join(f"{name}\n{key} 0 0 0 255\n" for key, name in sorted(present.items()))
    )

    aseg_mni.unlink()  # intermediate only; the label volume supersedes it
    print(f"  {n_vox} subcortical voxels across {len(present)} structures", flush=True)
    return bold_out, label_out, list_out


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
    mp = motion[mp_cols].values  # (T, 6)
    mp_deriv = np.vstack([np.full((1, 6), np.nan), np.diff(mp, axis=0)])  # (T, 6)
    mp_power = mp**2  # (T, 6)
    mp_d_pow = mp_deriv**2  # (T, 6)

    rot_mm = mp[:, :3] * (np.pi / 180.0) * 50.0
    trans_mm = mp[:, 3:]
    fd_raw = np.abs(np.diff(np.hstack([rot_mm, trans_mm]), axis=0)).sum(axis=1)
    fd = np.concatenate([[np.nan], fd_raw])

    # --- Load BOLD metadata and brain mask ---
    bold_img = nib.load(bold)
    n_frames = bold_img.shape[3]
    tr = float(bold_img.header.get_zooms()[3])
    mask_data = nib.load(brainmask).get_fdata() > 0

    # --- aCompCor tissue masks: precompute before loading bold_data so that
    #     bold_data can be freed immediately after ROI extraction. ---
    aseg_img = nib.load(aseg)
    aseg_data = np.round(aseg_img.get_fdata()).astype(int)

    wm_arr = np.isin(aseg_data, [2, 41]).astype(np.uint8)
    csf_arr = np.isin(aseg_data, [4, 14, 15, 43]).astype(np.uint8)
    # Erode WM by 1 voxel to reduce partial-volume contamination at GM boundary
    wm_arr = binary_erosion(wm_arr, iterations=1).astype(np.uint8)

    # Precompute the BOLD voxel → T1w voxel coordinate map for tissue mask warping.
    bold_ref_img = nib.load(bold_ref)
    M_bold_to_t1w_vox = _bold_vox_to_aseg_vox(
        _read_itk_affine(bold2t1w_affine), aseg_img.affine, bold_ref_img.affine
    )
    bold_shape = bold_ref_img.shape[:3]
    gi, gj, gk = np.mgrid[: bold_shape[0], : bold_shape[1], : bold_shape[2]]
    vox_hom = np.ones((4, gi.size))
    vox_hom[0], vox_hom[1], vox_hom[2] = gi.ravel(), gj.ravel(), gk.ravel()
    t1w_coords = (M_bold_to_t1w_vox @ vox_hom)[:3].reshape(3, *bold_shape)
    del gi, gj, gk, vox_hom

    warped_wm = (
        map_coordinates(wm_arr.astype(np.float32), t1w_coords, order=0, mode='constant', cval=0.0)
        > 0.5
    )
    warped_csf = (
        map_coordinates(csf_arr.astype(np.float32), t1w_coords, order=0, mode='constant', cval=0.0)
        > 0.5
    )
    del wm_arr, csf_arr, t1w_coords

    # --- Load BOLD, extract all ROIs, free immediately (~1.28 GB) ---
    # All boolean masks are ready; boolean indexing produces copies, so
    # bold_data can be released as soon as extraction is complete.
    bold_data = bold_img.get_fdata(dtype=np.float32)  # (X, Y, Z, T)
    brain_ts = bold_data[mask_data].T  # (T, n_brain)
    wm_ts = bold_data[warped_wm].T if warped_wm.any() else None
    csf_ts = bold_data[warped_csf].T if warped_csf.any() else None
    del bold_data

    # --- DVARS ---
    diff = np.diff(brain_ts, axis=0)
    dvars = np.concatenate([[np.nan], np.sqrt((diff**2).mean(axis=1))])

    # --- Global signal ---
    global_signal = brain_ts.mean(axis=1)

    # --- tCompCor: top-2% temporal-SD voxels ---
    temporal_std = brain_ts.std(axis=0)  # (voxels,)
    sd_threshold = np.percentile(temporal_std, 98)
    high_var_ts = brain_ts[:, temporal_std >= sd_threshold]  # (T, n_hv)
    high_var_ts = high_var_ts - high_var_ts.mean(axis=0)  # voxelwise demean
    n_tcc = min(5, high_var_ts.shape[1], n_frames)
    t_comp_cor_cols: dict = {}
    if n_tcc > 0:
        t_comps = PCA(n_components=n_tcc).fit_transform(high_var_ts)  # (T, n_tcc)
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
    #
    # An empty tissue mask means the bold→T1w warp is invalid — it does not mean
    # CompCor should be skipped. White matter and the ventricles occupy a large,
    # contiguous share of any whole-brain BOLD, so zero surviving voxels means the
    # BOLD grid was mapped outside the aseg entirely.
    #
    # This previously only warned and continued. The run then exited 0 and wrote a
    # near-empty output that looked valid by filename, so the pod, the driver's
    # own tally, and the Argo workflow all reported success; only downstream
    # output-size verification caught it (sub-WGVKC3KK ses-00A, 2026-07-23: ~7 MB
    # vs ~120 MB for its sibling sessions, which is a mostly-zero volume gzipping
    # down). Similarity metrics do not catch it either — that session scored the
    # HIGHEST bold→T1w mutual information of the three, because MI is computed
    # over surviving voxels and inflates as the overlap collapses.
    #
    # extract_subcortical() already raises on the same underlying breakage ("no
    # subcortical voxels survived the aseg warp"); this makes the confounds path
    # consistent with it. The driver marks a run failed on a non-zero exit, so
    # raising here is what records the run as failed and keeps its output from
    # being treated as valid.
    empty_tissues = [
        tissue for tissue, ts in (('wm', wm_ts), ('csf', csf_ts)) if ts is None or ts.shape[1] == 0
    ]
    if empty_tissues:
        raise ValueError(
            f"no {'/'.join(t.upper() for t in empty_tissues)} voxels in BOLD space "
            "after warping the aseg — the bold→T1w registration for this run is invalid"
        )

    comp_cor_cols: dict = {}
    for tissue, ts in [('wm', wm_ts), ('csf', csf_ts)]:
        n_vox = ts.shape[1]

        roi_ts = ts - ts.mean(axis=0)  # voxelwise demean

        # Fit up to 5 components, then trim to whichever is fewer: 5 or the
        # minimum number of components needed to explain ≥50% of variance.
        max_comp = min(5, n_vox, n_frames)
        pca = PCA(n_components=max_comp).fit(roi_ts)
        cumvar = np.cumsum(pca.explained_variance_ratio_)
        n_comp = min(int(np.searchsorted(cumvar, 0.50)) + 1, max_comp)
        comps = pca.transform(roi_ts)[:, :n_comp]  # (T, n_comp)
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
        mp_dict[col] = mp[:, i]
        mp_dict[f'{col}_derivative1'] = mp_deriv[:, i]
        mp_dict[f'{col}_power2'] = mp_power[:, i]
        mp_dict[f'{col}_derivative1_power2'] = mp_d_pow[:, i]

    confounds = pd.DataFrame(
        {
            't_indx': np.arange(n_frames),
            **nss_outlier_cols,
            **mp_dict,
            'framewise_displacement': fd,
            'dvars': dvars,
            'global_signal': global_signal,
            **comp_cor_cols,
            **t_comp_cor_cols,
            **cosine_cols,
        }
    )

    out_tsv = outdir / f'{prefix}_desc-confounds_timeseries.tsv'
    confounds.to_csv(out_tsv, sep='\t', index=False, na_rep='n/a')
    print(
        f"  Confounds: {out_tsv.name} ({confounds.shape[1]} columns × {confounds.shape[0]} frames)",
        flush=True,
    )
    return out_tsv


# ---------------------------------------------------------------------------
# QC summary
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BOLD image-quality metrics: aor / aqi / gcor
# ---------------------------------------------------------------------------
#
# These three were originally shelled out to AFNI's 3dToutcount, 3dTqual and
# @compute_gcor.  None of the three could ever have run (#119): at the time the
# image took AFNI from conda-forge, whose package ships 71 of AFNI's ~600
# programs, and none of these were among them.  The image now installs AFNI from
# upstream, where all three DO exist, but ships only an allow-list of binaries
# (AFNI_PROGRAMS in the Dockerfile, currently just 3dcalc) — so they are still
# absent, now by choice rather than by accident.  Adding them back would be a
# one-word Dockerfile edit; the numpy implementations below are kept because they
# are exact and avoid three subprocess round-trips per run, not because AFNI's
# are unavailable.  Reimplemented at AFNI's default settings so the values stay
# comparable with AFNI's own and with MRIQC's aor/aqi/gcor, which wrap the same
# programs.  All three take the in-mask voxel x time matrix, loaded once.


def _load_masked_timeseries(mni_bold: Path, mask_mni: Path) -> np.ndarray:
    """Load the masked MNI BOLD as an (n_voxels, n_frames) float32 array.

    Read whole rather than frame by frame: nibabel cannot random-access frames
    inside a gzip stream, so a per-frame loop re-inflates the entire file every
    frame.  The full 4D is transient (~1.7 GB for a 400-frame run on the 2 mm
    MNI grid, as 3dcalc writes it in Stage 5); only the in-mask voxels are kept,
    ~0.4 GB at a 250k-voxel mask.
    """
    mask = np.asanyarray(nib.load(mask_mni).dataobj) > 0
    data = np.asanyarray(nib.load(mni_bold).dataobj)
    ts = data[mask].astype(np.float32)
    del data
    return ts


def _outlier_fraction(ts: np.ndarray) -> np.ndarray:
    """Per-frame outlier fraction, as AFNI ``3dToutcount -fraction``.

    Per voxel: subtract the median, take MAD = median(|residual|), and count a
    frame as an outlier where |residual| > alpha * MAD, for
    alpha = sqrt(pi/2) * the normal deviate with upper-tail probability
    0.001 / n_frames (sqrt(pi/2) * MAD being the MAD's estimate of sigma).
    Voxels with MAD == 0 yield no outliers but still count in the denominator.
    """
    n_vox, n_frames = ts.shape
    alpha = math.sqrt(0.5 * math.pi) * NormalDist().inv_cdf(1.0 - 0.001 / n_frames)

    resid = ts - np.median(ts, axis=1, keepdims=True)
    np.abs(resid, out=resid)
    mad = np.median(resid, axis=1, keepdims=True)
    return np.count_nonzero((mad > 0) & (resid > alpha * mad), axis=0) / n_vox


def _quality_index(ts: np.ndarray) -> np.ndarray:
    """Per-frame quality index, as AFNI ``3dTqual`` (default -spearman).

    1 minus the Spearman correlation between each frame and the median volume,
    over in-mask voxels.  Lower is better.
    """
    ref = rankdata(np.median(ts, axis=1))
    return np.array([1.0 - _corr(rankdata(ts[:, t]), ref) for t in range(ts.shape[1])])


def _corr(x: np.ndarray, y: np.ndarray) -> float:
    """Pearson correlation, 0.0 if either input is constant (AFNI's convention)."""
    x = x - x.mean()
    y = y - y.mean()
    denom = math.sqrt(float(x @ x) * float(y @ y))
    return float(x @ y) / denom if denom > 0 else 0.0


def _gcor(ts: np.ndarray) -> float:
    """Global correlation, as AFNI ``@compute_gcor``.

    Demean and scale every voxel time series to unit *length*, average those
    unit series over the mask, and take the squared length of that average —
    equivalently the mean of all pairwise voxel-timeseries correlations (Saad
    et al. 2013).

    Unit length, not unit variance: AFNI's 3dTnorm divides by the L2 norm, so
    each series contributes with weight 1 to the average.  Dividing by the
    standard deviation instead would scale every series by sqrt(n_frames) and
    the result by n_frames.
    """
    unit = ts - ts.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(unit, axis=1, keepdims=True)
    # A constant voxel demeans to the zero vector, which is already what AFNI
    # normalises it to, so `where` can simply leave those rows alone.
    np.divide(unit, norms, out=unit, where=norms > 0)
    mean_unit = unit.mean(axis=0, dtype=np.float64)
    return float(mean_unit @ mean_unit)


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
    container_peak_memory_gb: float = 0.0,
) -> dict:
    """Compute a per-run QC summary dict from pipeline outputs.

    Called after confound estimation; inputs are already in memory/on disk.
    Returns a dict matching the FuncQC schema in src/metrics/schemas.py.
    """
    from datetime import datetime, timezone

    fd = confounds["framewise_displacement"].dropna().values
    dvars = confounds["dvars"].dropna().values

    # --- Artifact/outlier metrics (AFNI-equivalent, mirroring MRIQC's aor/aqi/gcor) ---
    # Degraded, never fatal: these are descriptive metrics computed after the run's
    # derivatives are already on disk, so a failure here must not cost the run (cf.
    # 4914ddb, which downgraded a Stage 5b failure to volumetric-only rather than
    # discarding a completed MNI BOLD).  On failure the three fields carry their
    # schema default of 0.0 — which is why 0.0 is not a meaningful value for any of
    # them and the log line below is the only way to tell it apart.
    try:
        iqm_ts = _load_masked_timeseries(mni_bold, mask_mni)
        aor = float(np.mean(_outlier_fraction(iqm_ts)))
        aqi = float(np.mean(_quality_index(iqm_ts)))
        gcor = _gcor(iqm_ts)
        del iqm_ts
    except Exception as exc:
        traceback.print_exc()
        print(
            f"  WARNING: BOLD IQMs (aor/aqi/gcor) failed, recording 0.0 for all three "
            f"— run is kept: {type(exc).__name__}: {exc}",
            flush=True,
        )
        aor = aqi = gcor = 0.0

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
    n_acompcor_wm = sum(1 for c in cols if c.startswith("a_comp_cor_wm_"))
    n_acompcor_csf = sum(1 for c in cols if c.startswith("a_comp_cor_csf_"))
    n_tcompcor = sum(1 for c in cols if c.startswith("t_comp_cor_"))
    n_cosines = sum(1 for c in cols if c.startswith("cosine_"))

    n_frames = int(bold_img.shape[-1])
    tr = float(bold_img.header.get_zooms()[3])

    n_above_0p2 = int((fd > 0.2).sum())
    n_above_0p5 = int((fd > 0.5).sum())

    mean_dvars_val = float(dvars.mean()) if dvars.size else 0.0
    mean_global_signal_val = (
        float(confounds["global_signal"].mean()) if "global_signal" in confounds else 0.0
    )
    # Percent-signal-change standardization of DVARS (Power et al. 2012), reusing
    # the mean global signal already computed above rather than a second pass.
    dvars_std = (
        round(mean_dvars_val / mean_global_signal_val * 100, 4) if mean_global_signal_val else 0.0
    )

    return {
        "schema_version": "1.1",
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
        "mean_dvars": round(mean_dvars_val, 4),
        "dvars_std": dvars_std,
        "mean_global_signal": round(mean_global_signal_val, 4),
        "tsnr_median": round(tsnr_median, 2),
        "gcor": round(gcor, 6),
        "aor": round(aor, 6),
        "aqi": round(aqi, 6),
        "n_acompcor_wm": n_acompcor_wm,
        "n_acompcor_csf": n_acompcor_csf,
        "n_tcompcor": n_tcompcor,
        "n_cosines": n_cosines,
        "stage_timings_s": stage_timings,
        "total_runtime_s": round(total_runtime_s, 1),
        "peak_memory_gb": round(peak_memory_gb, 2),
        # Container-lifetime, so on a multi-run session this grows run over run
        # while peak_memory_gb stays flat. Size limits.memory against this one.
        "container_peak_memory_gb": round(container_peak_memory_gb, 2),
        "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _surface_qc(
    surf_paths: dict[str, Path],
    subcort: "tuple[Path, Path, Path] | None",
) -> dict:
    """QC for the grayordinate components.

    Vertex coverage is the fraction of vertices whose timeseries is not
    identically zero — a vertex sampling outside the BOLD field of view reads
    as constant zero, so this is the direct measure of how much cortex the
    acquisition actually covered.
    """
    qc: dict = {}
    for hemi, path in surf_paths.items():
        gii = nib.load(path)
        ts = np.stack([d.data for d in gii.darrays], axis=1)  # (n_vert, T)
        n_vert = ts.shape[0]
        covered = np.any(ts != 0, axis=1)
        with np.errstate(invalid="ignore"):
            tsnr = np.where(ts.std(axis=1) > 0, ts.mean(axis=1) / ts.std(axis=1), 0.0)
        qc[f"surf_{hemi}_n_vertices"] = int(n_vert)
        qc[f"surf_{hemi}_coverage_frac"] = round(float(covered.mean()), 4)
        qc[f"surf_{hemi}_nan_frac"] = round(float(np.isnan(ts).mean()), 6)
        qc[f"surf_{hemi}_tsnr_median"] = (
            round(float(np.median(tsnr[covered])), 3) if covered.any() else 0.0
        )
        del ts

    if subcort is not None:
        labels = nib.load(subcort[1]).get_fdata()
        qc["subcort_n_voxels"] = int((labels > 0).sum())
        qc["subcort_n_structures"] = int(len(np.unique(labels[labels > 0])))
        qc["subcort_space"] = "MNI152NLin2009cAsym"
    return qc


def _surface_qc_envelope(
    args: argparse.Namespace,
    timings: dict,
    total_runtime_s: float,
    peak_memory_gb: float,
    container_peak_memory_gb: float = 0.0,
) -> dict:
    """Provenance wrapper for the metrics/surface-sample/ record.

    Every path that produces grayordinates writes that record, not just the
    short one. A run first processed as --emit grayordinate and later
    reprocessed as --emit both would otherwise leave the earlier file in
    place — describing a different run, from a different image, under the
    current run's name.

    schema_version and completed_at are not decoration. The compactor groups
    raw records by schema_version and writes each group to a
    schema_version=<v>/ partition, which is a *projected* partition key on
    surface_sample_compacted — a record without the field lands under
    schema_version=unknown, outside the enum, and Athena then returns zero
    rows with a successful query status rather than an error (#241). Field
    order here is the Glue tables' column order; keep them in step.
    """
    from datetime import datetime, timezone

    return {
        "subject": args.subj,
        "session": args.session,
        "task": args.task,
        "run": args.run,
        "pipeline": args.pipeline,
        "image_tag": args.image_tag,
        "emit": args.emit,
        "stage_timings_s": {k: round(v, 2) for k, v in timings.items()},
        "total_runtime_s": round(total_runtime_s, 2),
        "peak_memory_gb": round(peak_memory_gb, 3),
        "container_peak_memory_gb": round(container_peak_memory_gb, 2),
        "schema_version": "1.0",
        "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _finish_grayordinate_only(
    args: argparse.Namespace,
    prefix: str,
    surf_paths: dict[str, Path],
    subcort: "tuple[Path, Path, Path] | None",
    timings: dict,
    pipeline_start: float,
) -> None:
    """Tail of the grayordinate-only short path.

    The volumetric QC summary is not produced here: it describes the MNI BOLD
    and confounds, neither of which this path recomputed, and emitting a
    partially-populated copy would overwrite the real one in metrics/.
    """
    total = time.monotonic() - pipeline_start
    peak_gb = _peak_memory_gb()

    qc = {
        **_surface_qc_envelope(args, timings, total, peak_gb, _container_peak_memory_gb()),
        **_surface_qc(surf_paths, subcort),
    }
    qc_path = Path(f"/tmp/{prefix}_surf_qc.json")
    qc_path.write_text(json.dumps(qc))

    print(
        f"\n=== preproc complete (grayordinate only): {prefix} "
        f"({total:.1f}s / {total / 60:.1f}m) ===",
        flush=True,
    )
    print(f"  Peak memory: {peak_gb:.2f} GB", flush=True)
    print(f"  QC summary: {qc_path}", flush=True)


def _peak_memory_gb() -> float:
    """Peak RSS in GB for *this run*.

    Deliberately rusage rather than the cgroup's memory.peak. preproc.py runs
    once per run, but the driver loops a whole session's runs inside one pod,
    and memory.peak is a container-lifetime high-water mark that never resets —
    so run N reports the accumulated maximum over runs 1..N, inflated further by
    page cache from every file written so far. Measured on a six-run session:
    2.51 → 3.12 → 3.80 → 4.44 → 5.07 → 5.59 GB against a 6 G limit, while the
    four task-rest runs were identical in size (383 frames each) and the first
    run matched a single-run pod almost exactly. The climb was the metric, not
    the workload.

    RUSAGE_SELF alone would miss the AFNI and ANTs subprocesses, so this takes
    the max with RUSAGE_CHILDREN — which covers children this process has
    reaped, and resets per invocation as required. The max understates the case
    where parent and child hold their peaks simultaneously; the container figure
    from _container_peak_memory_gb bounds that from above.
    """
    self_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    child_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return max(self_kb, child_kb) / 2**20


def _container_peak_memory_gb() -> float:
    """Container-lifetime peak RSS in GB — cgroup v2, then v1, else 0.

    This is the figure the pod's memory limit actually acts on, so it is the one
    to size `limits.memory` against. It covers every process in the container
    and, on a multi-run session, every run so far — which is what makes it wrong
    for the per-run reading and right for the limit.
    """
    for cg in ('/sys/fs/cgroup/memory.peak', '/sys/fs/cgroup/memory/memory.max_usage_in_bytes'):
        try:
            return int(Path(cg).read_text()) / 2**30
        except (FileNotFoundError, ValueError):
            pass
    return 0.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BOLD preprocessing: composite warp → scipy MNI warp → mask → confounds"
    )
    p.add_argument("--bold", required=True, type=Path)
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
    p.add_argument(
        "--aseg",
        required=True,
        type=Path,
        help="aseg.mgz from FastSurfer subjects dir (for WM/CSF masks)",
    )
    p.add_argument(
        "--motion-file", required=True, type=Path, help="ABCD 3dvolreg motion parameter TSV"
    )
    p.add_argument(
        "--nss-frames",
        required=True,
        type=int,
        help="Number of non-steady-state frames at the start of the run",
    )
    p.add_argument(
        "--pipeline",
        default="cloudpipe_minproc",
        help="Pipeline name written into QC JSON provenance",
    )
    p.add_argument(
        "--image-tag", default="", help="Docker image tag written into QC JSON provenance"
    )
    p.add_argument(
        "--emit",
        default="volumetric",
        choices=["volumetric", "grayordinate", "both"],
        help="Which derivatives to produce. 'grayordinate' skips the "
        "composite warp and 4D MNI warp and reuses an existing "
        "MNI BOLD supplied via --mni-bold-in.",
    )
    p.add_argument(
        "--surf-dir",
        type=Path,
        help="FastSurfer surf/ directory (?h.white, ?h.pial). Required unless --emit volumetric.",
    )
    p.add_argument(
        "--mni-bold-in",
        type=Path,
        help="Existing MNI BOLD for --emit grayordinate. The short "
        "path does not recompute it; the driver recovers it "
        "from the run's existing volumetric derivative.",
    )
    p.add_argument(
        "--ribbon-depths",
        type=int,
        default=5,
        help="Sample points across the cortical ribbon per vertex",
    )
    args = p.parse_args()

    if args.emit in ("grayordinate", "both") and args.surf_dir is None:
        p.error("--surf-dir is required unless --emit volumetric")
    if args.emit == "grayordinate" and args.mni_bold_in is None:
        p.error("--mni-bold-in is required for --emit grayordinate")
    return args


# Exit code for "a required input does not exist". Distinct from 1 (a real
# compute failure) so a run the upstream QC gate already rejected is
# distinguishable at a glance in the log, in the driver, and in Athena. Issue
# #222: `bold-to-t1w` exits 65 on its relative-NMI floor and its driver
# discards the run's output directory, so this run's transform is *supposed* to
# be absent — but antsApplyTransforms discovered that at Stage 2 and the run
# died with a 25-line CalledProcessError traceback indistinguishable from an
# OOM or an ANTs crash.
#
# Deliberately absent from the func-preproc retryStrategy expression (which
# retries "64", "75", "143" only): a missing upstream artifact is not
# transient, and a retry would re-download the same empty prefix eight times
# while spending the session's shared retry budget.
EXIT_MISSING_INPUT = 66


def missing_inputs(args: argparse.Namespace) -> list[str]:
    """Required input paths that do not exist, as `flag: path` strings.

    Which inputs are actually read depends on --emit, and over-checking would
    turn a working configuration into a refusal: the grayordinate short path
    skips Stages 2-5 and Stage 6, so it never opens the brain mask or the
    motion file, and only that path passes --mni-bold-in. The T1w→MNI pair is
    required on every path — the short path still warps the subcortical block
    with it (extract_subcortical).
    """
    required = [
        ("--bold", args.bold),
        ("--mni-template", args.mni_template),
        ("--aseg", args.aseg),
        ("--bold2t1w-affine", args.bold2t1w_affine),
        ("--t1w2mni-affine", args.t1w2mni_affine),
        ("--t1w2mni-warp", args.t1w2mni_warp),
    ]
    if args.emit in ("volumetric", "both"):
        required += [("--brainmask", args.brainmask), ("--motion-file", args.motion_file)]
    if args.emit in ("grayordinate", "both"):
        required.append(("--surf-dir", args.surf_dir))
    if args.emit == "grayordinate":
        required.append(("--mni-bold-in", args.mni_bold_in))
    return [f"{flag}: {path}" for flag, path in required if not Path(path).exists()]


def refuse_if_inputs_missing(args: argparse.Namespace, prefix: str) -> None:
    """Exit EXIT_MISSING_INPUT if any required input is absent, computing nothing.

    Runs before Stage 1 rather than letting the first program that opens the
    file decide: the failure this replaces was antsApplyTransforms exiting 1 at
    Stage 2, which surfaced as a CalledProcessError traceback that reads exactly
    like a crash. See EXIT_MISSING_INPUT.
    """
    absent = missing_inputs(args)
    if not absent:
        return
    for item in absent:
        print(f"[preproc] MISSING INPUT {item}", flush=True)
    hint = ""
    if any(item.startswith("--bold2t1w-affine") for item in absent):
        hint = " (bold-to-t1w rejected this run on its QC floor and discarded the transform)"
    print(
        f"[preproc] SKIPPING {prefix} — required input(s) absent{hint}; "
        f"nothing computed, exiting {EXIT_MISSING_INPUT}",
        flush=True,
    )
    sys.exit(EXIT_MISSING_INPUT)


def main() -> None:
    args = parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    os.environ["ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS"] = str(args.threads)

    prefix = f"{args.subj}_{args.session}_{args.task}_{args.run}"

    print(f"=== preproc: {prefix} ===", flush=True)

    refuse_if_inputs_missing(args, prefix)

    pipeline_start = time.monotonic()
    _timings: dict = {}

    print(
        f"\n--- Stage 1: Extract BOLD reference (mean of {args.nss_frames} NSS frames) ---",
        flush=True,
    )
    with timed_stage("boldref", _timings):
        bold_ref = extract_bold_ref(args.bold, args.outdir, prefix, args.nss_frames)

    # Stages 2-5 produce the volumetric derivative. On a grayordinate-only
    # rerun they are skipped entirely — that warp chain is the expensive part —
    # and the MNI BOLD is taken from the run's existing derivative instead.
    if args.emit == "grayordinate":
        print("\n--- Stages 2-5: SKIPPED (--emit grayordinate) ---", flush=True)
        print(f"  reusing MNI BOLD: {args.mni_bold_in}", flush=True)
        mni_bold = args.mni_bold_in
        mask_mni = None
        tsnr_map = None
    else:
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
                bold=args.bold,
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

    surf_paths: dict[str, Path] = {}
    subcort: tuple[Path, Path, Path] | None = None
    if args.emit in ("grayordinate", "both"):
        print("\n--- Stage 5b: Grayordinate extraction ---", flush=True)
        try:
            with timed_stage("grayordinates", _timings):
                surf_paths = sample_cortical_ribbon(
                    bold=args.bold,
                    bold_ref=bold_ref,
                    aseg=args.aseg,
                    surf_dir=args.surf_dir,
                    bold2t1w_affine=args.bold2t1w_affine,
                    outdir=args.outdir,
                    prefix=prefix,
                    n_depths=args.ribbon_depths,
                    threads=args.threads,
                )
                subcort = extract_subcortical(
                    mni_bold=mni_bold,
                    aseg=args.aseg,
                    mni_template=args.mni_template,
                    t1w2mni_affine=args.t1w2mni_affine,
                    t1w2mni_warp=args.t1w2mni_warp,
                    outdir=args.outdir,
                    prefix=prefix,
                )
        except Exception:
            # On --emit both the volumetric MNI BOLD is already computed (Stage
            # 5) and is a complete derivative on its own. A surface-sampling
            # failure must not discard it — downgrade this run to volumetric
            # only: leave surf_paths empty so the driver writes no components
            # tarball, let Stages 6+ and the volumetric tarball proceed, and let
            # the absent Stage 1 marker be what records the surface-sample
            # failure. The driver keeps func-preproc and surface-sample as
            # independent per-run outcomes, so this reads as "volumetric
            # succeeded, surface failed" rather than a whole-run failure.
            #
            # On --emit grayordinate there is nothing to downgrade to: the
            # volumetric already exists in S3 and Stages 2-5 were skipped, so the
            # run's only product is the surface. Re-raise and fail it.
            if args.emit == "grayordinate":
                raise
            print(
                "[preproc] WARNING: Stage 5b (grayordinate extraction) failed; "
                f"downgrading {prefix} to volumetric-only",
                flush=True,
            )
            traceback.print_exc()
            surf_paths = {}
            subcort = None
            # Purge any partial surface outputs. If sample_cortical_ribbon
            # succeeded and extract_subcortical then failed, the cortical GIFTIs
            # are still on disk; left there, the driver's component glob would
            # find them and tar an INCOMPLETE components set that passes as a
            # success and breaks Stage 2. These globs mirror the driver's.
            outdir = Path(args.outdir)
            for pat in (
                f"{prefix}_hemi-*_space-fsnative_bold.func.gii",
                f"{prefix}_*desc-subcort*",
                f"{prefix}_desc-subcort_labellist.txt",
            ):
                for stale in outdir.glob(pat):
                    stale.unlink(missing_ok=True)

    # Confounds belong to the volumetric derivative. A grayordinate-only rerun
    # already has them in the existing tarball, and recomputing would burn the
    # aCompCor PCA for a file nobody consumes.
    if args.emit == "grayordinate":
        _finish_grayordinate_only(
            args,
            prefix,
            surf_paths,
            subcort,
            _timings,
            pipeline_start,
        )
        return

    print("\n--- Stage 6: Confound Estimation ---", flush=True)
    with timed_stage("confounds", _timings):
        confounds_tsv = compute_confounds(
            bold=args.bold,
            brainmask=args.brainmask,
            aseg=args.aseg,
            bold2t1w_affine=args.bold2t1w_affine,
            motion_file=args.motion_file,
            outdir=args.outdir,
            prefix=prefix,
            bold_ref=bold_ref,
            nss_frames=args.nss_frames,
        )

    total = time.monotonic() - pipeline_start
    print(f"\n=== preproc complete: {prefix} ({total:.1f}s / {total / 60:.1f}m) ===", flush=True)
    print(f"  MNI BOLD : {mni_bold}", flush=True)

    peak_gb = _peak_memory_gb()
    container_gb = _container_peak_memory_gb()
    print(
        f"  Peak memory: {peak_gb:.2f} GB this run, {container_gb:.2f} GB container to date",
        flush=True,
    )

    # QC is descriptive and runs after every derivative is already on disk, so
    # nothing in here is worth a run: the driver reads a non-zero exit as "every
    # derivative this run was asked for is lost" and deletes the work dir. A
    # missing metrics record costs one row in Athena; an exception here would
    # cost the MNI BOLD, the confounds and the surfaces (#119).
    try:
        confounds_df = pd.read_csv(confounds_tsv, sep='\t', na_values='n/a')
        qc = compute_func_qc_summary(
            confounds=confounds_df,
            bold_img=nib.load(args.bold),
            mni_bold=mni_bold,
            mask_mni=mask_mni,
            stage_timings=_timings,
            total_runtime_s=total,
            peak_memory_gb=peak_gb,
            args=args,
            tsnr_map=tsnr_map,
            container_peak_memory_gb=container_gb,
        )
        if surf_paths:
            # Computed once and used twice: folded into the volumetric QC for a
            # single per-run view, and written to metrics/surface-sample/ so that
            # prefix never carries a stale record from an earlier short-path run.
            surf_metrics = _surface_qc(surf_paths, subcort)
            qc.update(surf_metrics)
            Path(f"/tmp/{prefix}_surf_qc.json").write_text(
                json.dumps(
                    {
                        **_surface_qc_envelope(args, _timings, total, peak_gb, container_gb),
                        **surf_metrics,
                    }
                )
            )
        qc_path = Path(f"/tmp/{prefix}_qc.json")
        qc_path.write_text(json.dumps(qc))
        print(f"  QC summary: {qc_path}", flush=True)
    except Exception:
        traceback.print_exc()
        print(
            f"[preproc] WARNING: QC summary failed for {prefix}; no metrics record "
            "will be written for this run. Derivatives are unaffected.",
            flush=True,
        )


if __name__ == "__main__":
    main()
