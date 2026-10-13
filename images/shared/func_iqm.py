"""Functional QC metric computation: the FuncQC record.

Shared by images/afni (preproc.py, which writes the in-pod record) and
src/equivalence (which recomputes the same record from any pipeline's outputs —
openspec change add-fmriprep-equivalence-benchmark, D2). One implementation, so a
difference between two pipelines' records is a pipeline difference and not a
measurement difference. The afni Dockerfile COPYs this file to /app/func_iqm.py.

Pipeline-agnostic by construction: nothing here knows which pipeline produced its
inputs. `func_qc_measures` is the measurement; `compute_func_qc_summary` wraps it
in preproc.py's run envelope (identity, timings, memory).
"""

from __future__ import annotations

import math
import traceback
from pathlib import Path
from statistics import NormalDist
from typing import TYPE_CHECKING

import nibabel as nib
import numpy as np
from scipy.stats import rankdata

if TYPE_CHECKING:  # annotations only: the afni image ships pandas, src/ may not need it
    import argparse

    import pandas as pd

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


# ---------------------------------------------------------------------------
# The FuncQC record
# ---------------------------------------------------------------------------


def tsnr_map_from_file(mni_bold: Path) -> np.ndarray:
    """Voxelwise temporal SNR (mean / population std over time), 0 where std is 0.

    Population std (ddof=0), matching the Welford accumulator preproc.py's
    apply_transforms uses to compute the same map without a second read.
    """
    bold_data = nib.load(mni_bold).get_fdata(dtype=np.float32)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(
            bold_data.std(axis=-1) > 0,
            bold_data.mean(axis=-1) / bold_data.std(axis=-1),
            0.0,
        )


def func_qc_measures(
    confounds: pd.DataFrame,
    n_frames: int,
    tr: float,
    n_nss_frames: int,
    mni_bold: Path,
    mask_mni: Path,
    tsnr_map: np.ndarray | None = None,
) -> dict:
    """The measured fields of a FuncQC record, in schema order.

    Reads only the outputs: the confounds table, the MNI BOLD and its brain mask.
    `tsnr_map` lets preproc.py pass the map it already accumulated during the
    warp; when None it is computed from `mni_bold`.
    """
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
    # preproc.py pre-computes tsnr_map in apply_transforms to avoid reloading the
    # full 4D MNI BOLD (~12 GB) after it has already been freed from memory.
    mask_data = nib.load(mask_mni).get_fdata() > 0
    if tsnr_map is None:
        tsnr_map = tsnr_map_from_file(mni_bold)
    tsnr_median = float(np.median(tsnr_map[mask_data])) if mask_data.any() else 0.0

    # Count confound regressor columns by prefix
    cols = list(confounds.columns)
    n_acompcor_wm = sum(1 for c in cols if c.startswith("a_comp_cor_wm_"))
    n_acompcor_csf = sum(1 for c in cols if c.startswith("a_comp_cor_csf_"))
    n_tcompcor = sum(1 for c in cols if c.startswith("t_comp_cor_"))
    n_cosines = sum(1 for c in cols if c.startswith("cosine_"))

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
        "n_frames": int(n_frames),
        "n_nss_frames": n_nss_frames,
        "tr_seconds": round(float(tr), 4),
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
    }


def compute_func_qc_summary(
    confounds: pd.DataFrame,
    bold_img: nib.Nifti1Image,
    mni_bold: Path,
    mask_mni: Path,
    stage_timings: dict,
    total_runtime_s: float,
    peak_memory_gb: float,
    args: argparse.Namespace,
    tsnr_map: np.ndarray | None = None,
    container_peak_memory_gb: float = 0.0,
) -> dict:
    """Compute a per-run QC summary dict from pipeline outputs.

    Called after confound estimation; inputs are already in memory/on disk.
    Returns a dict matching the FuncQC schema in src/metrics/schemas.py.
    """
    from datetime import datetime, timezone

    measures = func_qc_measures(
        confounds=confounds,
        n_frames=int(bold_img.shape[-1]),
        tr=float(bold_img.header.get_zooms()[3]),
        n_nss_frames=args.nss_frames,
        mni_bold=mni_bold,
        mask_mni=mask_mni,
        tsnr_map=tsnr_map,
    )
    return {
        "schema_version": "1.1",
        "pipeline": getattr(args, "pipeline", "cloudpipe_minproc"),
        "image_tag": getattr(args, "image_tag", ""),
        "subject": args.subj,
        "session": args.session,
        "task": args.task,
        "run": args.run,
        **measures,
        "stage_timings_s": stage_timings,
        "total_runtime_s": round(total_runtime_s, 1),
        "peak_memory_gb": round(peak_memory_gb, 2),
        # Container-lifetime, so on a multi-run session this grows run over run
        # while peak_memory_gb stays flat. Size limits.memory against this one.
        "container_peak_memory_gb": round(container_peak_memory_gb, 2),
        "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
