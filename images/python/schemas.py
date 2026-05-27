"""
Metric schemas for cloudpipe pipeline observability.

Each dataclass maps to one S3 prefix and one Glue/Athena table:

  FuncQC          → metrics/func-preproc/   → cloudpipe_metrics.func_qc
  AnatQC          → metrics/anat/           → cloudpipe_metrics.anat_qc
  WorkflowRun     → metrics/workflow-runs/  → cloudpipe_metrics.workflow_runs
  CostAllocation  → metrics/costs/          → cloudpipe_metrics.costs

All schemas include schema_version (for forward compatibility) and
completed_at (ISO 8601 UTC), which Athena treats as the sort key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Functional preprocessing QC
# ---------------------------------------------------------------------------

@dataclass
class FuncQC:
    """Per-BOLD-run quality metrics from preproc.py.

    S3 key: metrics/func-preproc/{subject}_{session}_{task}_{run}_qc.json
    """

    subject: str
    session: str
    task: str
    run: str

    # Acquisition parameters
    n_frames: int = 0
    n_nss_frames: int = 0
    tr_seconds: float = 0.0

    # Head motion
    mean_fd: float = 0.0
    median_fd: float = 0.0
    max_fd: float = 0.0
    n_fd_above_0p2: int = 0
    n_fd_above_0p5: int = 0
    pct_fd_above_0p5: float = 0.0

    # Signal quality
    mean_dvars: float = 0.0
    mean_global_signal: float = 0.0
    tsnr_median: float = 0.0

    # Confound regressor counts
    n_acompcor_wm: int = 0
    n_acompcor_csf: int = 0
    n_tcompcor: int = 0
    n_cosines: int = 0

    # Runtime
    stage_timings_s: dict[str, float] = field(default_factory=dict)
    total_runtime_s: float = 0.0
    peak_memory_gb: float = 0.0

    # Provenance
    pipeline: str = "cloudpipe_minproc"
    image_tag: str = ""
    schema_version: str = "1.0"
    completed_at: str = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FuncQC":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def s3_key(subject: str, session: str, task: str, run: str) -> str:
        return f"metrics/func-preproc/{subject}_{session}_{task}_{run}_qc.json"


# ---------------------------------------------------------------------------
# Anatomical (FastSurfer) QC
# ---------------------------------------------------------------------------

@dataclass
class AnatQC:
    """Per-subject×session anatomical quality metrics from FastSurfer.

    S3 key: metrics/anat/{subject}_{session}_anat_qc.json
    """

    subject: str
    session: str

    # Volumes (mm³)
    etiv_mm3: float = 0.0
    total_brain_vol_mm3: float = 0.0
    lh_cortex_vol_mm3: float = 0.0
    rh_cortex_vol_mm3: float = 0.0
    wm_vol_mm3: float = 0.0
    subcort_gm_vol_mm3: float = 0.0

    # Cortical morphometry
    lh_mean_thickness_mm: float = 0.0
    rh_mean_thickness_mm: float = 0.0
    lh_surface_area_mm2: float = 0.0
    rh_surface_area_mm2: float = 0.0

    # Provenance
    pipeline: str = "cloudpipe_minproc"
    schema_version: str = "1.0"
    completed_at: str = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AnatQC":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def s3_key(subject: str, session: str) -> str:
        return f"metrics/anat/{subject}_{session}_anat_qc.json"


# ---------------------------------------------------------------------------
# Workflow run summary
# ---------------------------------------------------------------------------

@dataclass
class WorkflowRun:
    """Per-Argo-workflow run summary, written by the exit handler.

    S3 key: metrics/workflow-runs/{workflow_name}_run_summary.json
    """

    workflow_name: str
    subject: str
    status: str           # Succeeded | Failed | Error

    started_at: str = ""
    finished_at: str = field(default_factory=_now_utc)
    total_duration_s: int = 0
    message: str = ""     # Argo failure message, if any

    pipeline: str = "cloudpipe_minproc"
    schema_version: str = "1.0"
    completed_at: str = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkflowRun":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def s3_key(workflow_name: str) -> str:
        return f"metrics/workflow-runs/{workflow_name}_run_summary.json"


# ---------------------------------------------------------------------------
# Kubecost cost allocation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Registration QC
# ---------------------------------------------------------------------------

@dataclass
class RegistrationQC:
    """Per-registration quality metrics, written by the registration scripts.

    Covers two registration steps:
      registration_type="t1w_to_mni"   — from fst1w_to_mni.py (FireANTs SyN)
      registration_type="bold_to_t1w"  — from bold_to_t1w.py  (SynthMorph)

    S3 keys:
      metrics/registration/{subject}_{session}_t1w_to_mni_reg_qc.json
      metrics/registration/{subject}_{session}_{task}_{run}_bold_to_t1w_reg_qc.json
    """

    subject: str
    session: str
    registration_type: str    # "t1w_to_mni" or "bold_to_t1w"

    # Overlap quality — present for both registration types
    dice: float = 0.0         # Dice coefficient between aligned brain masks

    # T1w→MNI only: image similarity and warp regularity
    ncc: float = 0.0                   # Normalized cross-correlation in MNI brain mask
    jac_det_min: float = 0.0           # Jacobian determinant — min (should be > 0)
    jac_det_max: float = 0.0           # Jacobian determinant — max
    jac_det_mean: float = 0.0          # Jacobian determinant — mean (should be ~1)
    jac_det_std: float = 0.0           # Jacobian determinant — std
    jac_det_frac_negative: float = 0.0 # Fraction of voxels with det < 0 (folding)

    # BOLD→T1w only: task/run identity
    task: str = ""
    run: str = ""

    # Provenance
    pipeline: str = "cloudpipe_minproc"
    schema_version: str = "1.0"
    completed_at: str = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RegistrationQC":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def s3_key(subject: str, session: str, registration_type: str,
               task: str = "", run: str = "") -> str:
        if registration_type == "t1w_to_mni":
            return f"metrics/registration/{subject}_{session}_t1w_to_mni_reg_qc.json"
        return f"metrics/registration/{subject}_{session}_{task}_{run}_bold_to_t1w_reg_qc.json"


# ---------------------------------------------------------------------------
# Kubecost cost allocation
# ---------------------------------------------------------------------------

@dataclass
class CostAllocation:
    """Daily per-subject cost from Kubecost, written by the nightly scraper.

    S3 key: metrics/costs/{date}_cost_allocation.json
    """

    date: str             # YYYY-MM-DD
    subject: str

    total_cost_usd: float = 0.0
    cpu_cost_usd: float = 0.0
    memory_cost_usd: float = 0.0
    gpu_cost_usd: float = 0.0

    pipeline: str = "cloudpipe_minproc"
    schema_version: str = "1.0"
    completed_at: str = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CostAllocation":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def s3_key(date: str, subject: str) -> str:
        return f"metrics/costs/{date}_{subject}_cost_allocation.json"
