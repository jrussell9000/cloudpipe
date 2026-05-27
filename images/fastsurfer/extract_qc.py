"""
extract_qc.py — Extract AnatQC metrics from FastSurfer/FreeSurfer stats files.

Reads plain-text stats files produced at the end of FastSurfer parcellation:
  stats/aseg.stats       — global volumes (eTIV, cortex, WM, subcortical GM)
  stats/lh.aparc.stats   — left hemisphere thickness, surface area
  stats/rh.aparc.stats   — right hemisphere thickness, surface area

No neuroimaging library dependencies — all files are plain text.

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


# Format: # Measure <long_key>, <short_key>, <description>, <value>, <unit>
_MEASURE_RE = re.compile(
    r"^# Measure\s+\S+,\s+(\S+),\s+[^,]+,\s+([\d.eE+\-]+),\s+\S+"
)


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
    primary  = subj_dir / "stats" / f"{hemi}.aparc.stats"
    fallback = subj_dir / "stats" / f"{hemi}.aparc.DKTatlas.mapped.stats"
    return primary if primary.exists() else fallback


def extract_anat_qc(subjects_dir: Path, subj: str, ses: str, pipeline: str) -> dict:
    subj_dir = subjects_dir / f"{subj}_{ses}"
    aseg_path = subj_dir / "stats" / "aseg.stats"
    lh_path   = _aparc_stats(subj_dir, "lh")
    rh_path   = _aparc_stats(subj_dir, "rh")

    for p in (aseg_path, lh_path, rh_path):
        if not p.exists():
            print(f"  WARNING: {p} not found — QC will have zeros for its fields", flush=True)

    aseg = _parse_measures(aseg_path) if aseg_path.exists() else {}
    lh   = _parse_measures(lh_path)   if lh_path.exists()   else {}
    rh   = _parse_measures(rh_path)   if rh_path.exists()   else {}

    # WM volume: sum lh + rh white matter measures (both naming conventions)
    lh_wm = aseg.get("lhCerebralWhiteMatterVol", aseg.get("CerebralWhiteMatterVol", 0.0))
    rh_wm = aseg.get("rhCerebralWhiteMatterVol", 0.0)
    wm_vol = lh_wm + rh_wm if rh_wm > 0 else lh_wm

    return {
        "schema_version": "1.0",
        "pipeline": pipeline,
        "subject": subj,
        "session": ses,
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


if __name__ == "__main__":
    main()
