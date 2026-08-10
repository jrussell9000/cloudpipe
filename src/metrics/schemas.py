"""
Metric schemas for cloudpipe pipeline observability.

Each dataclass maps to one S3 prefix and one Glue/Athena table:

  FuncQC          → metrics/func-preproc/   → cloudpipe_metrics.func_preproc
  AnatQC          → metrics/anat-qc/        → cloudpipe_metrics.anat_qc
  FsqcQC          → metrics/fsqc-qc/        → cloudpipe_metrics.fsqc_qc
  WorkflowRun     → metrics/workflow-runs/  → cloudpipe_metrics.workflow_runs
  CostAllocation  → metrics/costs/          → cloudpipe_metrics.costs
  PodCost         → metrics/pod-costs/      → cloudpipe_metrics.pod_costs

The mapping is by convention: the table name is the last path segment with
hyphens replaced by underscores. Each pair is declared by hand in
terraform/modules/metrics/main.tf — a new prefix needs both an entry in
`local.metric_prefixes` and its own aws_glue_catalog_table, since the Glue
crawlers that used to discover them were removed on 2026-07-30. (When crawlers
did the naming, a prefix whose name didn't match its intended table name
produced a duplicate table, which is how `anat`/`anat_qc` and
`registration`/`registration_qc` both came to exist.)

All schemas include schema_version (for forward compatibility) and
completed_at (ISO 8601 UTC), which Athena treats as the sort key.

SERIALIZATION: every record MUST be written as compact, single-line JSON.
Athena reads metrics/ through TextInputFormat + JsonSerDe, which parses one
JSON object per line, so a pretty-printed record is read as N malformed rows
and fails the entire table scan — every other record in that prefix becomes
unreadable, not just the offending one. `to_json()` therefore defaults to
compact output; pass an indent only for human-facing display, never for a
file destined for S3.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Functional preprocessing QC
# ---------------------------------------------------------------------------


@dataclass
class FuncQC:
    """Per-BOLD-run quality metrics from preproc.py.

    S3 key: metrics/func-preproc/dt={dt}/{subject}_{session}_{task}_{run}_qc.json
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
    # DVARS as % signal change (schema 1.1): mean_dvars / mean_global_signal * 100
    dvars_std: float = 0.0
    mean_global_signal: float = 0.0
    tsnr_median: float = 0.0
    # The three below are AFNI's gcor/aor/aqi, computed in-process by preproc.py at
    # AFNI's own definitions (the binaries are absent from the image — see #119).
    # 0.0 on any of them means "not computed": they are descriptive metrics taken
    # after the derivatives are on disk, so a failure downgrades to this default
    # rather than costing the run.
    # global correlation (schema 1.1)
    gcor: float = 0.0
    # AFNI outlier ratio (schema 1.1), mean fraction of outlier voxels/frame
    aor: float = 0.0
    # AFNI quality index (schema 1.1), mean 1-Spearman-correlation-with-median-volume
    aqi: float = 0.0

    # Confound regressor counts
    n_acompcor_wm: int = 0
    n_acompcor_csf: int = 0
    n_tcompcor: int = 0
    n_cosines: int = 0

    # Runtime
    stage_timings_s: dict[str, float] = field(default_factory=dict)
    total_runtime_s: float = 0.0
    peak_memory_gb: float = 0.0  # this run only (rusage, resets per run)
    container_peak_memory_gb: float = 0.0  # container lifetime; size limits.memory on this
    pending_duration_s: float = (
        0.0  # seconds from pod creation to container start (node wait + image pull)
    )

    # Provenance
    pipeline: str = "cloudpipe_minproc"
    image_tag: str = ""
    schema_version: str = "1.1"
    completed_at: str = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FuncQC:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def s3_key(subject: str, session: str, task: str, run: str, dt: str | None = None) -> str:
        dt = dt or _today_utc()
        return f"metrics/func-preproc/dt={dt}/{subject}_{session}_{task}_{run}_qc.json"


# ---------------------------------------------------------------------------
# Anatomical (FastSurfer) QC
# ---------------------------------------------------------------------------


@dataclass
class AnatQC:
    """Per-subject×session anatomical quality metrics from FastSurfer.

    S3 key: metrics/anat-qc/dt={dt}/{subject}_{session}_anat_qc.json
    """

    subject: str
    session: str

    # T1w image quality — computed from orig.mgz using FastSurfer's own
    # tissue masks (brainmask.mgz, aseg.auto.mgz). Approximate, mriqc-style
    # IQMs; not guaranteed to match mriqc's implementation bit-for-bit.
    #
    # snr_gm/snr_wm were removed at schema_version 1.2 — WM/GM SNR now comes
    # from the fsqc_qc table (wm_snr_norm/gm_snr_norm), which computes it
    # against the bias-corrected norm.mgz. Join on subject+session.
    efc: float = 0.0
    fber: float = 0.0
    cnr: float = 0.0
    cjv: float = 0.0
    wm2max: float = 0.0
    fwhm_x_mm: float = 0.0
    fwhm_y_mm: float = 0.0
    fwhm_z_mm: float = 0.0
    fwhm_avg_mm: float = 0.0

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
    schema_version: str = "1.2"
    completed_at: str = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AnatQC:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def s3_key(subject: str, session: str, dt: str | None = None) -> str:
        dt = dt or _today_utc()
        return f"metrics/anat-qc/dt={dt}/{subject}_{session}_anat_qc.json"


# ---------------------------------------------------------------------------
# fsqc anatomical QC
# ---------------------------------------------------------------------------


@dataclass
class FsqcQC:
    """Per-subject×session anatomical QC from Deep-MI/fsqc.

    Written by images/fsqc/stage_and_run.py, one record per session, from a
    single per-subject fsqc invocation over a merged $SUBJECTS_DIR. Note that
    fsqc's own notion of a "subject" is our SESSION: the driver stages one
    directory per session, so `subject` here is the cloudpipe subject and the
    fields below describe one session of it.

    S3 key: metrics/fsqc-qc/dt={dt}/{subject}_{session}_fsqc_qc.json

    EVERY METRIC IS NULLABLE, and that is load-bearing — unlike every other
    schema in this file, which uses 0.0 as its "not computed" default. fsqc
    writes NaN for a metric whose inputs are absent, and the driver maps NaN to
    JSON null rather than 0.0, because 0.0 is a legitimate value for several of
    these (rot_tal_*, the n_outlier_* counts) and a real 0 must stay
    distinguishable from a missing measurement. Filter on IS NOT NULL, never on
    `> 0`.

    Known-always-null on this pipeline: holes_*, defects_*, topo_* (FastSurfer
    writes no surf/[lr]h.orig.nofix and its recon-all.log carries no defect
    counts) and n_outlier_sample_{nonpar,param} (sample-based outlier detection
    needs a reference cohort the driver does not pass). They are declared anyway
    so a future FreeSurfer-based or cohort-wide run needs no schema change.
    """

    subject: str
    session: str

    # Core image-quality metrics, in fsqc-results.csv column order.
    #
    # wm_snr_norm/gm_snr_norm SUPERSEDE AnatQC's removed snr_wm/snr_gm (schema
    # 1.2): fsqc computes them against the bias-corrected norm.mgz with eroded
    # tissue masks and a broader WM label set, so they are not comparable to the
    # old orig.mgz-based figures. Join to anat_qc on subject+session.
    wm_snr_orig: float | None = None
    gm_snr_orig: float | None = None
    wm_snr_norm: float | None = None
    gm_snr_norm: float | None = None
    # Corpus callosum size as a fraction of eTIV — fsqc's proxy for a failed or
    # truncated talairach registration.
    cc_size: float | None = None

    # Surface topology. NULL on this pipeline (see class docstring).
    holes_lh: float | None = None
    holes_rh: float | None = None
    defects_lh: float | None = None
    defects_rh: float | None = None
    topo_lh: float | None = None
    topo_rh: float | None = None

    # White/gray contrast-to-noise from surf/[lr]h.w-g.pct.mgh.
    con_snr_lh: float | None = None
    con_snr_rh: float | None = None

    # Rotation components of talairach.lta, in radians. Large values mean the
    # head was acquired far off the template's orientation; 0.0 is legitimate.
    rot_tal_x: float | None = None
    rot_tal_y: float | None = None
    rot_tal_z: float | None = None

    # Outlier counts from fsqc's --outlier module. n_outlier_norms counts
    # aseg/aparc regions outside fsqc's built-in normative ranges (a count, but
    # emitted as a float by fsqc). The two sample_* fields need a reference
    # cohort and are NULL here.
    #
    # SINGULAR, not plural: fsqc's own docstring documents these as
    # n_outliers_*, but the names it writes into fsqc-results.csv are
    # n_outlier_*. Verified against a real CSV header — the plural spelling
    # would make all three columns read NULL forever.
    n_outlier_norms: float | None = None
    n_outlier_sample_nonpar: float | None = None
    n_outlier_sample_param: float | None = None

    # Hypothalamic subunit volumes (mm³). NOT from fsqc-results.csv — the
    # subregion modules contribute no CSV columns at all, so the driver lifts
    # these from outliers/all.regions.stats. NULL when the hypothalamic
    # segmentation did not cover this session, which is common: the tarball is
    # subject-level but ships empty per-session mri/ dirs.
    hypothalamus_whole_left_mm3: float | None = None
    hypothalamus_whole_right_mm3: float | None = None

    # Per-module exit codes from fsqc's status/{session}/status.txt: 0 = ran
    # clean, non-zero = the module degraded (missing input, NaN output). NULL
    # means the module never reported at all, which is NOT the same as 0 — it is
    # the only thing separating "ran and found nothing" from "never ran", so
    # these must stay nullable ints.
    metrics_status: int | None = None
    outlier_status: int | None = None
    hippocampus_status: int | None = None
    hypothalamus_status: int | None = None

    # Provenance. fsqc_version is pinned by images/fsqc/Dockerfile and recorded
    # per record because fsqc's metric definitions are version-dependent.
    fsqc_version: str = ""
    pipeline: str = "cloudpipe_minproc"
    schema_version: str = "1.0"
    completed_at: str = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FsqcQC:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def s3_key(subject: str, session: str, dt: str | None = None) -> str:
        dt = dt or _today_utc()
        return f"metrics/fsqc-qc/dt={dt}/{subject}_{session}_fsqc_qc.json"


# ---------------------------------------------------------------------------
# Workflow run summary
# ---------------------------------------------------------------------------


@dataclass
class WorkflowRun:
    """Per-Argo-workflow run summary, written by the exit handler.

    S3 key: metrics/workflow-runs/dt={dt}/{workflow_name}__{subject}_run_summary.json
    """

    workflow_name: str
    subject: str
    status: str  # Succeeded | Failed | Error

    started_at: str = ""
    finished_at: str = field(default_factory=_now_utc)
    total_duration_s: int = 0
    # Seconds from workflow submission to the first DAG task running. None when
    # unmeasured — never a 0.0 stand-in (#147); see exit_handler._pending_duration_s.
    pending_duration_s: float | None = None
    message: str = ""  # Argo failure message, if any

    failed_step: str = ""  # canonical name of first failed step; "" on success
    failure_category: str = ""  # taxonomy category of first failure; "" on success

    pipeline: str = "cloudpipe_minproc"
    schema_version: str = "1.1"
    completed_at: str = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WorkflowRun:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def s3_key(workflow_name: str, subject: str, dt: str | None = None) -> str:
        dt = dt or _today_utc()
        return f"metrics/workflow-runs/dt={dt}/{workflow_name}__{subject}_run_summary.json"


# ---------------------------------------------------------------------------
# Step outcome tracking
# ---------------------------------------------------------------------------


@dataclass
class StepOutcome:
    """Per-step, per-scan-unit processing outcome.

    One record per (workflow_name, step, subject, session, task, run).
    Written unconditionally after each substantive pipeline step.

    S3 key: metrics/step-outcomes/dt={dt}/{workflow_name}__{step}__{subject}__{session}__{task}__{run}_outcome.json
    Absent scan dimensions (e.g. task/run for subject-level steps) use the literal "na".
    """

    workflow_name: str
    step: str  # canonical step name from taxonomy
    subject: str
    session: str  # "na" for subject-scoped steps
    task: str  # "na" for session/subject-scoped steps
    run: str  # "na" for session/subject-scoped steps

    status: str  # "succeeded" | "failed" | "skipped"
    failure_category: str = ""  # "infrastructure"|"algorithm"|"data"|"dependency"|"unknown"|""
    failure_reason: str = ""  # raw Argo message
    upstream_failed_step: str = ""  # step name that caused this skip (if skipped)
    outputs_verified: list = field(default_factory=list)  # S3 keys confirmed to exist

    pipeline: str = "cloudpipe_minproc"
    schema_version: str = "1.0"
    recorded_at: str = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StepOutcome:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def s3_key(
        workflow_name: str,
        step: str,
        subject: str,
        session: str = "na",
        task: str = "na",
        run: str = "na",
        dt: str | None = None,
    ) -> str:
        dt = dt or _today_utc()
        return (
            f"metrics/step-outcomes/dt={dt}/"
            f"{workflow_name}__{step}__{subject}__{session}__{task}__{run}_outcome.json"
        )


# ---------------------------------------------------------------------------
# Subject processing manifest
# ---------------------------------------------------------------------------


@dataclass
class StepSummary:
    """Lightweight step entry within a SubjectManifest."""

    step: str
    session: str
    task: str
    run: str
    status: str
    failure_category: str = ""
    failure_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SubjectManifest:
    """Aggregated processing status for one subject, written at workflow exit.

    Assembled from all StepOutcome records emitted during the workflow run.

    S3 key: metrics/subject-manifests/dt={dt}/{workflow_name}__{subject}_manifest.json
    """

    workflow_name: str
    subject: str
    overall_status: str  # "succeeded" | "partial" | "failed"

    steps: list = field(default_factory=list)  # list of StepSummary dicts
    outputs_available: list = field(
        default_factory=list
    )  # union of outputs_verified from succeeded steps
    failed_steps: list = field(default_factory=list)  # canonical names of failed steps
    skipped_steps: list = field(default_factory=list)  # canonical names of skipped steps

    pipeline: str = "cloudpipe_minproc"
    schema_version: str = "1.0"
    completed_at: str = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SubjectManifest:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def s3_key(workflow_name: str, subject: str, dt: str | None = None) -> str:
        dt = dt or _today_utc()
        return f"metrics/subject-manifests/dt={dt}/{workflow_name}__{subject}_manifest.json"


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
      metrics/registration/dt={dt}/{subject}_{session}_t1w_to_mni_reg_qc.json
      metrics/registration/dt={dt}/{subject}_{session}_{task}_{run}_bold_to_t1w_reg_qc.json

    ADDING A FIELD OR A SCHEMA VERSION IS A FOUR-PLACE CHANGE. Miss any one and
    the data is written to S3 but is not queryable, silently — which is exactly
    what happened to schema 2.4: nmi_gain, the metric the bold_to_t1w gate runs
    on, was emitted for weeks while being dropped on ingest.

      1. this dataclass — from_dict() filters to __dataclass_fields__, so an
         unlisted field is discarded on read
      2. src/metrics/athena.py::_UNION_COLUMNS["registration"] — the explicit
         column list used for the raw/compacted UNION
      3. terraform/modules/metrics/main.tf, BOTH the registration and
         registration_compacted `columns` blocks — there are no crawlers, so
         nothing discovers new columns on either table
      4. terraform, the same resource's "projection.schema_version.values" —
         a new schema_version outside that enum is not a projected partition
         and its records return nothing at all

    Step 3 covering the raw table too is a change from 2026-07-30: the raw
    table used to be crawler-discovered, so a new column resolved on its own
    after a nightly crawl. The crawlers were removed (they generated 5,227
    junk tables), so an undeclared column now simply never appears.
    """

    subject: str
    session: str
    registration_type: str  # "t1w_to_mni" or "bold_to_t1w"

    # Registration method — "synthmorph" for bold_to_t1w, "" for t1w_to_mni.
    # NOTE: the "synthmorph+bbr" method was removed in 61ccff7; no emitter
    # writes it any more. See the BBR block below.
    method: str = ""

    # DEAD FIELD for bold_to_t1w — bold_to_t1w.py hardcodes 0.0 (no BOLD brain
    # mask is computed, and EPI↔T1w Dice is not meaningful cross-contrast).
    # Also dead for t1w_to_mni as of schema 2.0, which emits `mask_dice` below.
    # Retained only so pre-2.0 records still deserialize.
    dice: float = 0.0

    # BOLD→T1w only (schema 2.1): Studholme normalized MI in the T1w brain mask,
    # (H_a + H_b) / H_ab. This is the ONLY real quality metric emitted for
    # bold_to_t1w. Ranges 1.0 (independent) to 2.0 (perfectly determined);
    # polarity-independent (unlike NCC); > 0 marks a completed run.
    nmi: float = 0.0

    # SUPERSEDED (schema 2.0 and earlier). Raw mutual information in nats. Replaced
    # by `nmi` in 2.1: raw MI is computed over surviving voxels, so it inflates as
    # brain overlap collapses and a broken registration can out-score a healthy one
    # (2655eea). No live emitter writes it; retained only so pre-2.1 records still
    # deserialize. Do NOT compare `mi` across the 2.0/2.1 boundary — filter on
    # schema_version, or use `nmi`.
    mi: float = 0.0

    # BOLD→T1w only (schema 2.2+): transform-only magnitude of the rigid
    # SynthMorph transform, from registration_qc.rigid_transform_metrics.
    # Present on 2.2+ records; pre-2.2 records omit them and take these defaults,
    # so filter on schema_version before pooling.
    #
    # bold_to_t1w is deliberately rigid (ADR 002): SDC ran upstream, so the true
    # BOLD↔T1w offset is a same-session head-position difference — small by
    # construction. These read no intensities and no segmentation, so they are
    # robust and orthogonal to `nmi`, which a broken registration can still
    # inflate (2655eea). As of schema 2.3 rigid_rot_deg and rigid_disp_max_mm GATE
    # the run via registration_qc._BOLD_T1W_THRESHOLDS (warn > 15, fail > 20; see
    # `verdict` below); rigid_disp_mean_mm is recorded-only. 0.0 on the early-exit
    # failure path.
    rigid_disp_mean_mm: float = 0.0  # mean brain-voxel displacement induced by the transform
    rigid_disp_max_mm: float = 0.0  # worst-case brain-voxel displacement (gated, schema 2.3)
    rigid_rot_deg: float = 0.0  # rotation angle of the rigid transform, degrees (gated, schema 2.3)

    # Modified Hausdorff distance (mm) between the T1w brain surface and an Otsu
    # skull-strip of the warped BOLD, from registration_qc.modified_hausdorff_
    # distance. Boundary-agreement metric, orthogonal to the intensity agreement
    # of `nmi`. Its floor is set by the soft 2.4 mm EPI skull-strip, not the
    # registration, so it is the least trustworthy to gate. -1.0 is the "could not
    # compute" sentinel (empty EPI mask); real MHD is always >= 0.
    mhd_mm: float = -1.0

    # BOLD→T1w only (schema 2.4): the identity baseline and the gain over it.
    # nmi_identity is the NMI of the same BOLD reference resampled with NO
    # transform; nmi_gain is nmi - nmi_identity. nmi_gain is THE GATED METRIC for
    # bold_to_t1w — absolute nmi is not comparable across sessions, since the
    # identity baseline alone spans ~0.004, about the size of the gain itself.
    #
    # NOTE these were emitted by bold_to_t1w.py from schema 2.4 but were never
    # added here or to athena.py's column list, so from_dict() silently dropped
    # them and the gated metric was not queryable. Records written before this
    # fix have the values in their S3 JSON but not in the table; re-ingest that
    # partition if you need the history.
    nmi_identity: float = 0.0
    nmi_gain: float = 0.0

    # BOLD→T1w only (schema 2.5): boundary-sensitive alignment, RECORDED-ONLY.
    # Both families exist because `nmi` pools every brain voxel into a single
    # joint histogram, diluting the alignment signal (~0.13 nats of MI against
    # ~7 nats of joint entropy) until the whole usable range is ~1.011–1.020.
    # These evaluate only at tissue boundaries, where misregistration actually
    # changes something. See docs/nmi-interpretation.md and registration_qc.
    #
    # seg_* come from registration_qc.segmentation_alignment, sampling the warped
    # BOLD through FastSurfer's aseg.auto.mgz: BBR's premise (GM brighter than WM
    # in T2*-weighted EPI) measured rather than optimised. Their sentinel is
    # -999.0, NOT 0.0 — a zero seg_bbr_contrast is a real measurement meaning the
    # tissues are indistinguishable, so it cannot double as "could not compute".
    # Filter these on `> -999` the way nmi is filtered on `> 0`.
    #
    # Phase A (2026-07-30, handoffs/bold-t1w-qc-gate-calibration/RESULTS.md)
    # measured seg_bbr_contrast as the strongest of all seven candidates against a
    # known 2 mm misregistration: AUC 0.830, d' 1.31, Spearman -0.90. It is the
    # metric to build any future relative check on — but NORMALISED WITHIN
    # SESSION, both because that cancels the FOV-prescription nuisance term and
    # because every run in a session shares one aseg.auto.mgz, so segmentation
    # error cancels too. An absolute bound would inherit that dependency.
    seg_bbr_contrast: float = -999.0  # (mean_GM - mean_WM) / mean, in the WM/GM shell
    seg_bbr_contrast_identity: float = -999.0  # same, on the identity-resampled BOLD
    #
    # ngf comes from registration_qc.normalized_gradient_field: squared cosine
    # between BOLD and T1w intensity-gradient directions on T1w edges, weighted
    # by edge strength. In [0, 1], higher better; the square makes it blind to
    # the EPI↔T1w polarity flip, as MI is. 0.0 is both the natural floor and the
    # "no evaluation voxels" sentinel.
    ngf: float = 0.0
    ngf_identity: float = 0.0

    # REMOVED IN SCHEMA 2.6, all three emitted by 2.5 records only:
    #
    #   seg_bbr_contrast_gain, ngf_gain — every _gain variant measured WORSE than
    #     its absolute counterpart in Phase A (0.695 vs 0.830 and 0.693 vs 0.805),
    #     and gain made the between/within-session variance ratio 10x worse for
    #     seg_bbr_contrast (13.9x -> 139.8x). The identity baseline is dominated
    #     by the BOLD-vs-T1w field-of-view prescription, a session-level constant
    #     (69x between/within SD; see registration_qc._BOLD_T1W_THRESHOLDS), so
    #     subtracting it INJECTS the session variance it was meant to cancel.
    #     Both are exactly derivable from the retained operands
    #     (`seg_bbr_contrast - seg_bbr_contrast_identity`, `ngf - ngf_identity`),
    #     for historical 2.5 records too, so nothing is lost by not storing them.
    #
    #     ngf_gain additionally had a broken sentinel: 0.0 meant "could not
    #     compute" while ALSO being the legitimate value for "the transform
    #     bought nothing" — it was exactly 0.0 for all 110 identity anchors in the
    #     sweep. That is the same collision seg_* avoided by choosing -999.0, and
    #     it fired on precisely the degenerate case the gate exists to catch.
    #
    #   seg_ventricle_ratio — recorded on a hunch about a distinct failure family;
    #     Phase A found it uninformative. Spearman -0.158 (next worst -0.73) and
    #     NON-MONOTONE: AUC rose to 0.806 at 10 mm then fell to 0.585 at 20 mm, so
    #     grosser misregistration scored healthier. Not derivable from anything
    #     retained; the values survive in the 2.5 S3 JSON if ever wanted.
    #
    # nmi_gain is deliberately KEPT despite the above: it is the gated metric, and
    # "did the transform buy anything over identity" is inherently relative, so
    # the gain framing is correct there even though it is a poor quality signal.

    # DEAD FIELDS (schema 1.2). BBR was removed from bold_to_t1w.py in 61ccff7
    # ("replace SynthMorph+BBR with SynthMorph-only") because ABCD 2.4 mm EPI
    # lacks the gray/white contrast bbregister needs. Live records emit `null`
    # for all three; the defaults below only describe the historic 1.2 records.
    bbr_cost: float = -1.0
    bbr_converged: bool = False  # False when cost > 0.8, init ignored, or BBR failed
    bbr_init_used: str = ""  # "synthmorph" (--init-reg honored) or "coreg" (fallback)

    # mask_dice / lncc are T1w→MNI only (schema 2.0) — the fields fst1w_to_mni.py
    # actually emits; `dice` above is NOT written.
    mask_dice: float = 0.0  # Dice between the MNI brain mask and the warped T1w mask
    lncc: float = 0.0  # Mean local normalized cross-correlation in the brain mask
    # verdict is emitted by BOTH registration types, against different threshold
    # tables: t1w_to_mni uses _T1W_MNI_THRESHOLDS (schema 2.0), bold_to_t1w uses
    # _BOLD_T1W_THRESHOLDS (schema 2.3, gating rigid_rot_deg / rigid_disp_max_mm).
    # "" means not evaluated (early-exit failure path).
    # "pass" | "fail" from registration_qc.verdict(). "warn" is HISTORICAL only:
    # no table has declared a warn band since 2026-07-30 and verdict() can no
    # longer emit one, but records written before then still carry it, so queries
    # over historical partitions must still handle the value.
    verdict: str = ""

    # T1w→MNI only: warp regularity.
    # As of schema 2.1 every jac_* / log_jac_* field is computed within the
    # template brain mask. In 2.0 and earlier they were whole-field and included
    # unconstrained background extrapolation, so DO NOT compare these across the
    # 2.0/2.1 boundary — filter on schema_version first.
    jac_det_min: float = 0.0  # Jacobian determinant — min (should be > 0)
    jac_det_max: float = 0.0  # Jacobian determinant — max
    jac_det_mean: float = 0.0  # Jacobian determinant — mean (should be ~1)
    jac_det_std: float = 0.0  # Jacobian determinant — std
    jac_det_frac_negative: float = 0.0  # Fraction of voxels with det < 0 (folding)

    # Log-Jacobian distribution (schema 2.1). log(det J) is symmetric about 0 —
    # +0.69 is a doubling of local volume, −0.69 a halving — so expansion and
    # compression are comparable, which raw det J is not. Healthy adult T1w→MNI152
    # keeps the vast majority within ±1.5 (a ~4.5× volume change); mass out near
    # ±3 (~20×) means tissue is being squashed or ballooned to force an intensity
    # match, typically failed skull-stripping or pathology the template cannot fit.
    # Folded voxels (det <= 0) have no logarithm and are excluded from all of
    # these — they are counted by jac_det_frac_negative instead, so the
    # log_jac_frac_* denominators are non-folded voxels only.
    # RECORDED BUT NOT GATED: no log_jac_* field is in _T1W_MNI_THRESHOLDS yet,
    # pending calibration against a measured batch distribution.
    log_jac_mean: float = 0.0
    log_jac_std: float = 0.0
    log_jac_p01: float = 0.0  # 1st percentile — robust lower edge
    log_jac_p99: float = 0.0  # 99th percentile — robust upper edge
    log_jac_min: float = 0.0  # worst single compressing voxel
    log_jac_max: float = 0.0  # worst single expanding voxel
    log_jac_frac_beyond_1p5: float = 0.0  # fraction with |log det J| > 1.5
    log_jac_frac_beyond_3: float = 0.0  # fraction with |log det J| > 3

    # Inverse consistency error (schema 2.1), in mm, within the template brain.
    # Displace a voxel by the forward warp, then by the inverse warp sampled
    # there; a true inverse pair returns to the start, so the residual is the
    # error. Alone among these metrics it reads no image intensities — it tests
    # the transform itself, so it catches a warp that matches intensities well
    # but is not globally invertible (which positive det(J) everywhere does NOT
    # guarantee). Only ice_mean_mm is gated (< 0.5 mm, sub-half-voxel on 1 mm
    # MNI152); the rest are recorded for diagnosis.
    # All four are absent — not zero — when the inverse warp could not be saved
    # or the computation failed; verdict() skips absent keys, so ICE fails open.
    ice_mean_mm: float = 0.0
    ice_p95_mm: float = 0.0
    ice_p99_mm: float = 0.0
    ice_max_mm: float = 0.0
    centroid_displacement_mm: float = 0.0  # T1w→MNI only; mm; one of the three verdict thresholds

    # BOLD→T1w only: task/run identity
    task: str = ""
    run: str = ""

    # Provenance
    pipeline: str = "cloudpipe_minproc"
    schema_version: str = "1.2"
    completed_at: str = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RegistrationQC:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def s3_key(
        subject: str,
        session: str,
        registration_type: str,
        task: str = "",
        run: str = "",
        dt: str | None = None,
    ) -> str:
        dt = dt or _today_utc()
        if registration_type == "t1w_to_mni":
            return f"metrics/registration/dt={dt}/{subject}_{session}_t1w_to_mni_reg_qc.json"
        return (
            f"metrics/registration/dt={dt}/{subject}_{session}_{task}_{run}_bold_to_t1w_reg_qc.json"
        )


# ---------------------------------------------------------------------------
# Kubecost cost allocation
# ---------------------------------------------------------------------------


@dataclass
class CostAllocation:
    """Per-workflow cost from Kubecost, written by the nightly scraper.

    Keyed by (scrape date, workflow name) so records join to WorkflowRun on
    workflow_name while preserving one record per scrape day.

    S3 key: metrics/costs/dt={date}/{date}_{workflow_name}_cost_allocation.json

    The date prefix matters: a workflow that spans the UTC-midnight boundary is
    scraped on two consecutive days. Without the date in the key, the second
    scrape overwrote the first (emit_to_s3 is a plain put, not a merge), silently
    discarding the pre-midnight cost. The bucket has no versioning, so that loss
    was unrecoverable. Keying by date keeps both partial records; sum them per
    workflow to get the full cost.

    total_adjustment_usd and scrape_age_days (schema 1.2) exist because Kubecost
    has no per-allocation "is this reconciled yet" flag — see
    kubecost_drift_probe.py's docstring. total_adjustment_usd is the portion of
    total_cost_usd that reconciliation against cloud billing has already
    contributed for this specific allocation; it's the same *CostAdjustment sum
    the drift probe computes cross-sectionally, just captured per-row instead.
    scrape_age_days is how many days after `date` this record was actually
    scraped, and is what lets a query tell an unreconciled record from a settled
    one. The nightly scraper writes 1; the settled re-scrape overwrites the same
    key at age 3 (kubecost_scraper.SETTLED_AGE_DAYS), where Kubecost's
    reconciliation has converged, so a mature date reads 3 and a record still
    reading 1 is a date whose re-scrape never landed. Day-granular: two reads of
    one report-date on the same day both stamp the same value even when their
    totals differ.
    """

    date: str  # YYYY-MM-DD (scrape date, i.e. day after workflow ran)
    workflow_name: str  # Argo workflow name — join key to WorkflowRun
    subject: str  # extracted from pod label subjectid

    total_cost_usd: float = 0.0
    cpu_cost_usd: float = 0.0
    memory_cost_usd: float = 0.0
    gpu_cost_usd: float = 0.0

    # schema 1.2+
    total_adjustment_usd: float = 0.0
    scrape_age_days: int = 1

    pipeline: str = "cloudpipe_minproc"
    schema_version: str = "1.2"
    completed_at: str = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CostAllocation:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def s3_key(date: str, workflow_name: str) -> str:
        return f"metrics/costs/dt={date}/{date}_{workflow_name}_cost_allocation.json"


@dataclass
class PodCost:
    """Per-pod cost from Kubecost, written by the same nightly scraper.

    A finer grain of the same Kubecost window that produces CostAllocation:
    `costs` answers "what did this workflow cost", `pod_costs` answers "which
    component of it cost that". Summing pod_costs over a (date, workflow_name)
    reproduces that workflow's CostAllocation row, so the two are consistent by
    construction rather than independently scraped.

    It is a SEPARATE prefix, not extra rows in metrics/costs/, because one
    prefix is one table: everything under metrics/costs/ is read as a `costs`
    row regardless of its shape. Mixing pod grain into workflow grain would
    double-count every existing SUM in athena.py — one row per workflow plus
    one row per pod of that same workflow, all summing together. See the
    metric_prefixes comment in terraform/modules/metrics/main.tf.

    S3 key: metrics/pod-costs/dt={date}/{date}_{workflow_name}_pod_costs.json

    That key holds ALL of one workflow's pods as newline-delimited JSON, one
    object per line — unlike every other schema here, where one key is one
    record. Object-per-pod would mean ~10k tiny S3 objects/day at the
    300-concurrent target. Athena's JsonSerDe already parses one object per
    line, so JSONL is free on the read side; compactor.read_raw_records is what
    had to learn about it.

    step/phase/session come from the per-template pod labels
    (`cloudpipe.io/step`, `cloudpipe.io/phase`, `session`) that the workflow
    templates already set, so no template change was needed to get the
    component dimension. Kubecost sanitizes label keys — dots and slashes both
    become underscores — so they arrive as `cloudpipe_io_step` etc.; see
    kubecost_scraper._label().

    The efficiency and *_hours fields come free in the same Allocation
    response. They are what makes this table actionable rather than merely
    descriptive: cost alone ranks components, but cost next to
    cpu_efficiency/ram_efficiency identifies the over-requested ones, which is
    where per-pod cost is actually reducible.

    Cost fields inherit the day+1 scrape drift documented on
    kubecost_drift_probe.py: reliable for the RELATIVE distribution across
    components, not for absolute billed dollars.
    """

    date: str  # YYYY-MM-DD (report date, matching CostAllocation.date)
    workflow_name: str  # Argo workflow name — join key to WorkflowRun / costs
    pod: str  # Kubecost allocation key (Argo pod name)

    # Component dimensions, from per-template pod labels. "" when the pod
    # carries no such label (e.g. the Argo controller's own helper pods).
    step: str = ""
    phase: str = ""
    subject: str = ""
    session: str = ""

    # Cost breakdown. pv/network are included because the master workflow's
    # per-subject volumeClaimTemplate makes pv cost a real, unevenly
    # distributed component rather than noise.
    total_cost_usd: float = 0.0
    cpu_cost_usd: float = 0.0
    memory_cost_usd: float = 0.0
    gpu_cost_usd: float = 0.0
    pv_cost_usd: float = 0.0
    network_cost_usd: float = 0.0
    total_adjustment_usd: float = 0.0

    # Resource consumption and request efficiency. Efficiency is Kubecost's
    # usage/request ratio in [0, 1]; low values on an expensive step are the
    # right-sizing signal.
    runtime_minutes: float = 0.0
    cpu_core_hours: float = 0.0
    ram_gb_hours: float = 0.0
    gpu_hours: float = 0.0
    cpu_efficiency: float = 0.0
    ram_efficiency: float = 0.0

    # Node the pod landed on — instance type drives the rate, so a step whose
    # cost varies without its resource-hours varying is a placement effect.
    node: str = ""
    node_instance_type: str = ""

    scrape_age_days: int = 1
    pipeline: str = "cloudpipe_minproc"
    schema_version: str = "1.0"
    completed_at: str = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PodCost:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def s3_key(date: str, workflow_name: str) -> str:
        return f"metrics/pod-costs/dt={date}/{date}_{workflow_name}_pod_costs.json"
