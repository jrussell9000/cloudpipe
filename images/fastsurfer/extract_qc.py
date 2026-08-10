"""
extract_qc.py — Extract AnatQC metrics from FastSurfer/FreeSurfer outputs.

Reads plain-text stats files produced at the end of FastSurfer parcellation:
  stats/aseg.stats       — global volumes (eTIV, cortex, WM, subcortical GM)
  stats/lh.aparc.stats   — left hemisphere thickness, surface area
  stats/rh.aparc.stats   — right hemisphere thickness, surface area

...and volumes for approximate mriqc-style T1w image-quality metrics:
  mri/orig.mgz       — conformed raw T1w intensities
  mri/brainmask.mgz  — foreground/background mask
  mri/aseg.auto.mgz  — tissue labels (WM/GM masks)

WM/GM SNR is *not* computed here. It moved to the separate `fsqc-metrics` step
(`wm_snr_orig/norm`, `gm_snr_orig/norm` in the `fsqc_qc` record), which reads the
bias-corrected `norm.mgz`, uses a broader WM label set, and erodes tissue
boundaries — all of which this file's version did not. `schema_version` 1.2
marks the removal of `snr_gm`/`snr_wm`.

Usage:
  python extract_qc.py \
    --subjects-dir /opt/freesurfer/subjects \
    --subj sub-NDARABC123 \
    --ses ses-00A \
    --pipeline cloudpipe_minproc
"""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import nibabel as nib
import numpy as np

# Format: # Measure <long_key>, <short_key>, <description>, <value>, <unit>
_MEASURE_RE = re.compile(r"^# Measure\s+\S+,\s+(\S+),\s+[^,]+,\s+([\d.eE+\-]+),\s+\S+")

# FreeSurfer aseg labels: left/right cerebral white matter and cortex.
_WM_LABELS = (2, 41)
_GM_LABELS = (3, 42)


def _t1w_iqms(orig_path: Path, brainmask_path: Path, aseg_path: Path) -> dict[str, float]:
    """Approximate mriqc-style IQMs from the raw T1w and FastSurfer's own tissue masks.

    Not a port of mriqc's implementation (no air-mask refinement, no
    head-mask erosion) — close enough to flag outliers, not to compare
    against mriqc-reported values.
    """
    if not (orig_path.exists() and brainmask_path.exists() and aseg_path.exists()):
        return {}

    orig_nii = nib.load(str(orig_path))
    img = np.asarray(orig_nii.dataobj, dtype=np.float64)
    brainmask = np.asarray(nib.load(str(brainmask_path)).dataobj) > 0
    aseg = np.asarray(nib.load(str(aseg_path)).dataobj)

    wm_mask = np.isin(aseg, _WM_LABELS)
    gm_mask = np.isin(aseg, _GM_LABELS)
    bg_mask = ~brainmask

    if not (wm_mask.any() and gm_mask.any() and bg_mask.any()):
        return {}

    fwhm = _fwhm(img, orig_nii.header.get_zooms()[:3])

    wm_vals, gm_vals, bg_vals = img[wm_mask], img[gm_mask], img[bg_mask]
    mu_wm, sigma_wm = float(wm_vals.mean()), float(wm_vals.std())
    mu_gm, sigma_gm = float(gm_vals.mean()), float(gm_vals.std())
    mu_diff = abs(mu_wm - mu_gm)

    denom = np.sqrt(sigma_wm**2 + sigma_gm**2)
    cnr = mu_diff / denom if denom else 0.0
    cjv = (sigma_wm + sigma_gm) / mu_diff if mu_diff else 0.0
    # WM/GM SNR is deliberately absent: fsqc computes it correctly (bias-corrected
    # norm.mgz, broader WM label set, boundary erosion) and emits it as
    # wm_snr_norm/gm_snr_norm in the fsqc_qc record. mu_wm/sigma_* stay because
    # cnr, cjv, and wm2max still need them.

    fg_energy = float((img[brainmask] ** 2).mean())
    bg_energy = float((bg_vals**2).mean())
    fber = fg_energy / bg_energy if bg_energy else 0.0

    positive = img[img > 0]
    wm2max = mu_wm / float(np.percentile(positive, 99.95)) if positive.size else 0.0

    flat = img.ravel()
    b_max = np.sqrt((flat**2).sum())
    n_vox = flat.size
    efc_max = n_vox * (1.0 / np.sqrt(n_vox)) * np.log(1.0 / np.sqrt(n_vox))
    efc = (
        float((1.0 / efc_max) * np.sum((flat / b_max) * np.log((flat + 1e-16) / b_max)))
        if b_max
        else 0.0
    )

    return {
        "efc": efc,
        "fber": fber,
        "cnr": cnr,
        "cjv": cjv,
        "wm2max": wm2max,
        "fwhm_x_mm": fwhm[0],
        "fwhm_y_mm": fwhm[1],
        "fwhm_z_mm": fwhm[2],
        "fwhm_avg_mm": sum(fwhm) / 3.0,
    }


def _fwhm(img: np.ndarray, voxel_sizes: tuple[float, float, float]) -> tuple[float, float, float]:
    """Per-axis FWHM (mm) via Forman's closed-form smoothness estimator.

    Coarser than mriqc/AFNI's ACF-fit FWHM (assumes a single stationary
    Gaussian ACF) but needs only adjacent-voxel differences — no AFNI
    dependency in this image.
    """
    var_img = float(img.var())
    if not var_img:
        return (0.0, 0.0, 0.0)

    out = []
    for axis, voxel_size in enumerate(voxel_sizes):
        ratio = 0.5 * float(np.diff(img, axis=axis).var()) / var_img
        if ratio >= 1.0:
            out.append(0.0)
            continue
        sigma = np.sqrt(-1.0 / (4.0 * np.log(1.0 - ratio)))
        out.append(float(sigma * np.sqrt(8.0 * np.log(2.0)) * voxel_size))
    return tuple(out)  # type: ignore[return-value]


def _parse_measures(stats_path: Path) -> dict[str, float]:
    """Return {short_key: float_value} for all # Measure lines."""
    measures: dict[str, float] = {}
    for line in stats_path.read_text().splitlines():
        m = _MEASURE_RE.match(line)
        if m:
            try:
                measures[m.group(1)] = float(m.group(2))
            except ValueError:
                pass
    return measures


def _aparc_stats(subj_dir: Path, hemi: str) -> Path:
    """Return aparc stats path, falling back to DKT-mapped stats when --fsaparc was not run."""
    primary = subj_dir / "stats" / f"{hemi}.aparc.stats"
    fallback = subj_dir / "stats" / f"{hemi}.aparc.DKTatlas.mapped.stats"
    return primary if primary.exists() else fallback


def extract_anat_qc(subjects_dir: Path, subj: str, ses: str, pipeline: str) -> dict:
    subj_dir = subjects_dir / ses
    aseg_path = subj_dir / "stats" / "aseg.stats"
    lh_path = _aparc_stats(subj_dir, "lh")
    rh_path = _aparc_stats(subj_dir, "rh")

    for p in (aseg_path, lh_path, rh_path):
        if not p.exists():
            print(f"  WARNING: {p} not found — QC will have zeros for its fields", flush=True)

    aseg = _parse_measures(aseg_path) if aseg_path.exists() else {}
    lh = _parse_measures(lh_path) if lh_path.exists() else {}
    rh = _parse_measures(rh_path) if rh_path.exists() else {}

    # WM volume: sum lh + rh white matter measures (both naming conventions)
    lh_wm = aseg.get("lhCerebralWhiteMatterVol", aseg.get("CerebralWhiteMatterVol", 0.0))
    rh_wm = aseg.get("rhCerebralWhiteMatterVol", 0.0)
    wm_vol = lh_wm + rh_wm if rh_wm > 0 else lh_wm

    mri_dir = subj_dir / "mri"
    iqms = _t1w_iqms(mri_dir / "orig.mgz", mri_dir / "brainmask.mgz", mri_dir / "aseg.auto.mgz")
    if not iqms:
        print(
            f"  WARNING: {mri_dir} missing orig/brainmask/aseg — T1w IQMs will be zero", flush=True
        )

    return {
        "schema_version": "1.2",
        "pipeline": pipeline,
        "subject": subj,
        "session": ses,
        "efc": iqms.get("efc", 0.0),
        "fber": iqms.get("fber", 0.0),
        "cnr": iqms.get("cnr", 0.0),
        "cjv": iqms.get("cjv", 0.0),
        "wm2max": iqms.get("wm2max", 0.0),
        "fwhm_x_mm": iqms.get("fwhm_x_mm", 0.0),
        "fwhm_y_mm": iqms.get("fwhm_y_mm", 0.0),
        "fwhm_z_mm": iqms.get("fwhm_z_mm", 0.0),
        "fwhm_avg_mm": iqms.get("fwhm_avg_mm", 0.0),
        "etiv_mm3": aseg.get("eTIV", 0.0),
        "total_brain_vol_mm3": aseg.get("BrainSegVolNotVent", aseg.get("BrainSegVol", 0.0)),
        "lh_cortex_vol_mm3": aseg.get("lhCortexVol", 0.0),
        "rh_cortex_vol_mm3": aseg.get("rhCortexVol", 0.0),
        "wm_vol_mm3": wm_vol,
        "subcort_gm_vol_mm3": aseg.get("SubCortGrayVol", 0.0),
        "lh_mean_thickness_mm": lh.get("MeanThickness", 0.0),
        "rh_mean_thickness_mm": rh.get("MeanThickness", 0.0),
        "lh_surface_area_mm2": lh.get("WhiteSurfArea", 0.0),
        "rh_surface_area_mm2": rh.get("WhiteSurfArea", 0.0),
        "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Extract AnatQC metrics from FastSurfer stats files")
    p.add_argument("--subjects-dir", required=True, type=Path)
    p.add_argument("--subj", required=True)
    p.add_argument("--ses", required=True)
    p.add_argument("--pipeline", default="cloudpipe_minproc")
    args = p.parse_args()

    qc = extract_anat_qc(args.subjects_dir, args.subj, args.ses, args.pipeline)

    out_path = Path(f"/tmp/{args.subj}_{args.ses}_anat_qc.json")
    out_path.write_text(json.dumps(qc))
    print(f"  Anat QC: {out_path}", flush=True)

    # Summarize key fields to stdout for Argo logs
    print(
        f"  eTIV={qc['etiv_mm3']:.0f} mm³  "
        f"brain={qc['total_brain_vol_mm3']:.0f} mm³  "
        f"lh_thickness={qc['lh_mean_thickness_mm']:.3f} mm  "
        f"rh_thickness={qc['rh_mean_thickness_mm']:.3f} mm",
        flush=True,
    )
    print(
        f"  cnr={qc['cnr']:.3f}  cjv={qc['cjv']:.3f}  efc={qc['efc']:.3f}  "
        f"fber={qc['fber']:.3f}  "
        f"wm2max={qc['wm2max']:.3f}  fwhm_avg={qc['fwhm_avg_mm']:.3f} mm",
        flush=True,
    )


if __name__ == "__main__":
    main()
