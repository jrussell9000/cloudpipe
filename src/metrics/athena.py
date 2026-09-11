"""
Athena query helpers for cloudpipe metrics.

Requires: boto3, pandas
Install: pip install boto3 pandas

Usage
-----
    from metrics.athena import CloudpipeMetrics

    m = CloudpipeMetrics(bucket="my-cloudpipe-bucket")
    df = m.func_qc(session="ses-00A")
    print(df[["subject", "mean_fd", "tsnr_median"]].describe())

    costs = m.costs()
    runs  = m.workflow_runs(status="Failed")

    # Where the money goes, by pipeline component:
    print(m.step_costs(date_from="2026-07-01")[
        ["step", "total_cost_usd", "pct_of_total", "mean_cpu_efficiency"]
    ])
"""

from __future__ import annotations

import time
from typing import Any

_POLL_INTERVAL = 2.0  # seconds between Athena status polls
_MAX_WAIT_S = 300  # give up after 5 minutes

# Column lists for the tables that have a metrics/compacted/ counterpart,
# excluding schema_version (a data column on the raw table, but a partition
# key — not a data column — on the *_compacted table; see
# terraform/modules/metrics/main.tf). UNION ALL requires identical column
# order and count on both sides, so _query(compacted=True) selects these
# explicit lists rather than SELECT * on either side. Keep in sync with the
# `columns` blocks of the corresponding raw/*_compacted Glue tables.
_UNION_COLUMNS = {
    "func_preproc": [
        "subject",
        "session",
        "task",
        "run",
        "n_frames",
        "n_nss_frames",
        "tr_seconds",
        "mean_fd",
        "median_fd",
        "max_fd",
        "n_fd_above_0p2",
        "n_fd_above_0p5",
        "pct_fd_above_0p5",
        "mean_dvars",
        "dvars_std",
        "mean_global_signal",
        "tsnr_median",
        "gcor",
        "aor",
        "aqi",
        "n_acompcor_wm",
        "n_acompcor_csf",
        "n_tcompcor",
        "n_cosines",
        "stage_timings_s",
        "total_runtime_s",
        "peak_memory_gb",
        "container_peak_memory_gb",
        "pipeline",
        "image_tag",
        "completed_at",
        # Grayordinate QC. NULL on volumetric-only runs and on every record
        # written before the surface-func rollout, which is fine on both halves
        # of the UNION: the raw JsonSerDe yields NULL for a missing key, and a
        # Parquet file that lacks a Glue-declared column reads as NULL rather
        # than failing the scan (verified 2026-08-08 for fsqc_qc's all-null
        # columns).
        "surf_L_n_vertices",
        "surf_L_coverage_frac",
        "surf_L_nan_frac",
        "surf_L_tsnr_median",
        "surf_R_n_vertices",
        "surf_R_coverage_frac",
        "surf_R_nan_frac",
        "surf_R_tsnr_median",
        "subcort_n_voxels",
        "subcort_n_structures",
        "subcort_space",
    ],
    # The whole table, unlike anat_qc below — every column here is either run
    # identity, provenance, or a surface metric, and there is no subset to
    # curate. `emit` is load-bearing rather than decorative: "grayordinate"
    # means this record is the ONLY copy of that run's surface QC (the short
    # path leaves FuncQC untouched), so a caller cannot know whether a join to
    # func_preproc is possible without it.
    "surface_sample": [
        "subject",
        "session",
        "task",
        "run",
        "pipeline",
        "image_tag",
        "emit",
        "stage_timings_s",
        "total_runtime_s",
        "peak_memory_gb",
        "container_peak_memory_gb",
        "completed_at",
        "surf_L_n_vertices",
        "surf_L_coverage_frac",
        "surf_L_nan_frac",
        "surf_L_tsnr_median",
        "surf_R_n_vertices",
        "surf_R_coverage_frac",
        "surf_R_nan_frac",
        "surf_R_tsnr_median",
        "subcort_n_voxels",
        "subcort_n_structures",
        "subcort_space",
    ],
    "anat_qc": [
        "subject",
        "session",
        "etiv_mm3",
        "total_brain_vol_mm3",
        "lh_cortex_vol_mm3",
        "rh_cortex_vol_mm3",
        "wm_vol_mm3",
        "subcort_gm_vol_mm3",
        "lh_mean_thickness_mm",
        "rh_mean_thickness_mm",
        "lh_surface_area_mm2",
        "rh_surface_area_mm2",
        "pipeline",
        "completed_at",
    ],
    # Every fsqc column, unlike anat_qc above, which lists a curated subset.
    # There is no equivalent subset to curate here: the whole table is QC
    # metrics, and the *_status codes are what tell a NULL metric that was never
    # computed from one that was computed as absent — dropping them would make
    # the metrics they qualify uninterpretable.
    #
    # Every metric column is nullable, and several are null on every row today
    # (holes_*, defects_*, topo_*, n_outlier_sample_*). Filter on IS NOT NULL,
    # never on `> 0` the way `nmi` is filtered: 0.0 is a legitimate measurement
    # for rot_tal_* and n_outlier_norms. Verified 2026-08-08 that a Parquet file
    # whose column is entirely null (Arrow infers type `null`, stored as
    # int32(Null)) still reads as NULL against the `double` declared in Glue —
    # it does not fail the scan the way a malformed JSON line would.
    "fsqc_qc": [
        "subject",
        "session",
        "wm_snr_orig",
        "gm_snr_orig",
        "wm_snr_norm",
        "gm_snr_norm",
        "cc_size",
        "holes_lh",
        "holes_rh",
        "defects_lh",
        "defects_rh",
        "topo_lh",
        "topo_rh",
        "con_snr_lh",
        "con_snr_rh",
        "rot_tal_x",
        "rot_tal_y",
        "rot_tal_z",
        "n_outlier_norms",
        "n_outlier_sample_nonpar",
        "n_outlier_sample_param",
        "hypothalamus_whole_left_mm3",
        "hypothalamus_whole_right_mm3",
        "metrics_status",
        "outlier_status",
        "hippocampus_status",
        "hypothalamus_status",
        "fsqc_version",
        "pipeline",
        "completed_at",
    ],
    "workflow_runs": [
        "workflow_name",
        "subject",
        "status",
        "started_at",
        "finished_at",
        "total_duration_s",
        "pending_duration_s",
        "message",
        "failed_step",
        "failure_category",
        "pipeline",
        "batch_label",
        "completed_at",
    ],
    # The ONE table whose two Glue declarations are not raw+schema_version.
    # `registration_compacted` also declares four BBR-era columns the raw
    # `registration` table never had: dice, bbr_cost, bbr_converged,
    # bbr_init_used (all retired with BBR in 61ccff7). This list is therefore
    # the INTERSECTION, not the compacted table's column list — selecting a
    # compacted-only name here makes the raw half of the UNION fail with
    # COLUMN_NOT_FOUND, taking the whole query down. Do not "resync" these four
    # back in to match the compacted table; they are dead fields no emitter
    # writes, and their historic values are still reachable by querying
    # registration_compacted directly.
    "registration": [
        "subject",
        "session",
        "registration_type",
        "method",
        "nmi",
        "mi",
        "rigid_disp_mean_mm",
        "rigid_disp_max_mm",
        "rigid_rot_deg",
        "mhd_mm",
        "nmi_identity",
        "nmi_gain",
        "seg_bbr_contrast",
        "seg_bbr_contrast_identity",
        "ngf",
        "ngf_identity",
        "mask_dice",
        "lncc",
        "verdict",
        "jac_det_min",
        "jac_det_max",
        "jac_det_mean",
        "jac_det_std",
        "jac_det_frac_negative",
        "log_jac_mean",
        "log_jac_std",
        "log_jac_p01",
        "log_jac_p99",
        "log_jac_min",
        "log_jac_max",
        "log_jac_frac_beyond_1p5",
        "log_jac_frac_beyond_3",
        "ice_mean_mm",
        "ice_p95_mm",
        "ice_p99_mm",
        "ice_max_mm",
        "centroid_displacement_mm",
        "task",
        "run",
        "pipeline",
        "completed_at",
        # Schema 2.7, t1w_to_mni only: RANDOM-rescue provenance
        # (fst1w_to_mni.py::rescue_provenance). NULL on earlier and bold_to_t1w rows.
        "sampling_strategy",
        "rescue_ticket",
        "sampling_seed",
        "itk_threads",
        "attempts_run",
        "none_lncc",
        "none_jac_det_frac_negative",
    ],
    "costs": [
        "date",
        "workflow_name",
        "subject",
        "total_cost_usd",
        "cpu_cost_usd",
        "memory_cost_usd",
        "gpu_cost_usd",
        "total_adjustment_usd",
        "scrape_age_days",
        "pipeline",
        "completed_at",
    ],
    "pod_costs": [
        "date",
        "workflow_name",
        "pod",
        "step",
        "phase",
        "subject",
        "session",
        "total_cost_usd",
        "cpu_cost_usd",
        "memory_cost_usd",
        "gpu_cost_usd",
        "pv_cost_usd",
        "network_cost_usd",
        "total_adjustment_usd",
        "runtime_minutes",
        "cpu_core_hours",
        "ram_gb_hours",
        "gpu_hours",
        "cpu_efficiency",
        "ram_efficiency",
        "node",
        "node_instance_type",
        "node_capacity_type",
        "node_effective_usd_per_hour",
        "node_ondemand_usd_per_hour",
        "scrape_age_days",
        "pipeline",
        "completed_at",
    ],
}


# ---------------------------------------------------------------------------
# Combined anatomical (T1w-derived) QC view
# ---------------------------------------------------------------------------
#
# anat_qc and fsqc_qc are two halves of one thing: both are derived from the
# same T1w image, both sit at subject×session grain, and neither is complete
# on its own (WM/GM SNR left anat_qc at schema 1.2 and lives only in fsqc_qc;
# volumes and thickness live only in anat_qc). anatomical_qc() joins them into
# the single view an operator actually wants, and these lists are shared with
# the DuckDB twin so the two engines cannot drift apart.
#
# holes_*, defects_*, and topo_* are deliberately NOT carried over: they are
# null on every row this pipeline writes (FastSurfer emits no surf/[lr]h.orig
# .nofix), so six always-null columns would be pure noise in a joined view.
# fsqc_qc() still returns them for the FreeSurfer-based run that would fill
# them in.
_ANAT_JOIN_COLUMNS = (
    "efc",
    "fber",
    "cnr",
    "cjv",
    "wm2max",
    "fwhm_x_mm",
    "fwhm_y_mm",
    "fwhm_z_mm",
    "fwhm_avg_mm",
    "etiv_mm3",
    "total_brain_vol_mm3",
    "lh_cortex_vol_mm3",
    "rh_cortex_vol_mm3",
    "wm_vol_mm3",
    "subcort_gm_vol_mm3",
    "lh_mean_thickness_mm",
    "rh_mean_thickness_mm",
    "lh_surface_area_mm2",
    "rh_surface_area_mm2",
)

_FSQC_JOIN_COLUMNS = (
    "wm_snr_orig",
    "gm_snr_orig",
    "wm_snr_norm",
    "gm_snr_norm",
    "cc_size",
    "con_snr_lh",
    "con_snr_rh",
    "rot_tal_x",
    "rot_tal_y",
    "rot_tal_z",
    "n_outlier_norms",
    "n_outlier_sample_nonpar",
    "n_outlier_sample_param",
    "hypothalamus_whole_left_mm3",
    "hypothalamus_whole_right_mm3",
    "metrics_status",
    "outlier_status",
    "hippocampus_status",
    "hypothalamus_status",
    "fsqc_version",
)


def anatomical_join_sql(
    anat_source: str,
    fsqc_source: str,
    where_both: str = "",
    where_window: str = "",
) -> str:
    """Build the anat_qc ⋈ fsqc_qc SQL, given each side's table expression.

    `anat_source`/`fsqc_source` are whatever the engine reads from — a Glue
    table name for Athena, a read_json(...) call for DuckDB.

    The query separates two questions that a plain join conflates: WHICH SCANS
    are in scope, and WHICH RECORD describes each of them.

    `where_both` holds the predicates that identify a scan (subject, session)
    and applies everywhere. `where_window` holds the dt= bounds and applies
    ONLY to scan selection — a scan is in scope if EITHER table has a record in
    the window, and it is then annotated with each table's most recent record
    for it, whenever that was written.

    That split is measured, not assumed. fsqc-metrics runs on every workflow
    while anat_qc is written only when FastSurfer actually reprocesses, so a
    batch reusing existing derivatives writes fsqc rows under today's dt= and
    leaves its anat rows under the original run's. Filtering both tables by the
    window therefore returns the fsqc half with the anat half blank — verified
    against the 2026-08-09 pilot on a dt=2026-08-09 window: 0 of 23 rows paired
    that way, 23 of 23 paired this way. Filtering neither is equally wrong in
    the other direction: it drags the whole 271-scan corpus into a batch query.

    Selecting scans from EITHER side (rather than driving off fsqc alone, as
    the Grafana panel does) is what keeps an anat-only scan visible — the case
    where FastSurfer ran and fsqc did not record. NULLs on one side of the
    returned frame are the signal, not a defect.

    Each side is reduced to one row per subject×session (most recent
    completed_at) before it is attached. Reprocessing writes a second record
    under a new dt= — the live corpus holds 962 anat_qc rows for 271
    subject×session, up to 9 per scan — so joining raw would fan rows out by
    the product of both sides' re-run counts, the multiplicity trap
    join_subject() documents.
    """
    where_scan = " AND ".join(w for w in (where_both.removeprefix("WHERE "), where_window) if w)
    where_scan = f"WHERE {where_scan}" if where_scan else ""
    anat_cols = ", ".join(_ANAT_JOIN_COLUMNS)
    fsqc_cols = ", ".join(_FSQC_JOIN_COLUMNS)
    select_anat = ", ".join(f"a.{c}" for c in _ANAT_JOIN_COLUMNS)
    select_fsqc = ", ".join(f"f.{c}" for c in _FSQC_JOIN_COLUMNS)
    return f"""
    WITH anat AS (
        SELECT subject, session, pipeline, completed_at, {anat_cols},
               ROW_NUMBER() OVER (
                   PARTITION BY subject, session ORDER BY completed_at DESC
               ) AS rn
        FROM {anat_source}
        {where_both}
    ),
    fsqc AS (
        SELECT subject, session, pipeline, completed_at, {fsqc_cols},
               ROW_NUMBER() OVER (
                   PARTITION BY subject, session ORDER BY completed_at DESC
               ) AS rn
        FROM {fsqc_source}
        {where_both}
    ),
    scans AS (
        SELECT subject, session FROM {anat_source} {where_scan}
        UNION
        SELECT subject, session FROM {fsqc_source} {where_scan}
    )
    SELECT
        s.subject,
        s.session,
        {select_anat},
        {select_fsqc},
        COALESCE(a.pipeline, f.pipeline) AS pipeline,
        a.completed_at AS anat_completed_at,
        f.completed_at AS fsqc_completed_at
    FROM scans s
    LEFT JOIN (SELECT * FROM anat WHERE rn = 1) a
           ON a.subject = s.subject AND a.session = s.session
    LEFT JOIN (SELECT * FROM fsqc WHERE rn = 1) f
           ON f.subject = s.subject AND f.session = s.session
    ORDER BY 1, 2
    """


def sql_in_list(values: list[str]) -> str:
    """Render a list of strings as a SQL IN(...) body, quotes escaped."""
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def cost_scope_clause(
    subjects: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    extra: list[str] | None = None,
) -> str:
    """Build the WHERE clause that scopes cost records to one batch.

    `metrics/costs/` accumulates across batches and is keyed by workflow, not
    subject, so both a subject list and a scrape-date window are needed to
    isolate a single batch's costs.

    CostAllocation.s3_key partitions by `dt=<date>`, and `date` is exactly the
    partition value, so a `date` bound is also added as a `dt` bound — the
    `date` predicate alone filters the JSON body but gives Athena nothing to
    prune the `dt=` partitions on.

    Shared with `metrics/pod-costs/`, which is partitioned and dated the same
    way and carries the same `subject` column.
    """
    where = list(extra or [])
    if subjects:
        where.append(f"subject IN ({sql_in_list(subjects)})")
    if date_from:
        where.append(f"date >= '{date_from}'")
        where.append(f"dt >= '{date_from}'")
    if date_to:
        where.append(f"date <= '{date_to}'")
        where.append(f"dt <= '{date_to}'")
    return ("WHERE " + " AND ".join(where)) if where else ""


# Athena's GetQueryResults API hands every value back as a string in
# `VarCharValue`, regardless of the column's real type. The declared type lives
# separately, in ResultSetMetadata.ColumnInfo[].Type — mapped here.
#
# Integers map to pandas' nullable "Int64" rather than numpy int64 so that a
# NULL does not silently promote the column to float and print a frame count as
# `1437.0`. `decimal` maps to float: the cost columns are the only decimals in
# this schema and are read to the cent, so Decimal exactness buys nothing.
#
# date/timestamp are deliberately absent and stay strings, matching the DuckDB
# twin (which returns `date` and `completed_at` as strings too). Callers slice
# them positionally — export_batch_metrics.analyze_costs() does `str(v)[:10]` —
# so parsing them here would be a silent behaviour change between engines, not
# an improvement.
_ATHENA_NUMERIC_TYPES = {
    "tinyint": "Int64",
    "smallint": "Int64",
    "integer": "Int64",
    "int": "Int64",
    "bigint": "Int64",
    "real": "float64",
    "float": "float64",
    "double": "float64",
    "decimal": "float64",
}


def _cast_athena_types(df, col_types: dict[str, str]):
    """Cast a string-valued Athena result frame to its declared column types.

    Without this every column is an object-dtype string, so `.sum()` on a cost
    column concatenates decimal literals instead of adding them — which is how
    `export_batch_metrics.py` came to die with
    `could not convert string to float: '0.335430.23187...'` while the DuckDB
    engine returned the same query correctly.

    Unknown or complex types (array/row/map/json/varbinary) are left as strings
    rather than guessed at: this is a read path, and a wrong cast would corrupt
    values more quietly than no cast at all.
    """
    import pandas as pd

    for name, athena_type in col_types.items():
        if name not in df.columns:
            continue
        if athena_type == "boolean":
            df[name] = df[name].map({"true": True, "false": False}).astype("boolean")
            continue
        target = _ATHENA_NUMERIC_TYPES.get(athena_type)
        if target is None:
            continue
        # errors="coerce" so one unparseable value becomes NULL instead of
        # failing the whole read; Athena should never emit one, but a partially
        # malformed JSON record reaching a `double` column has precedent here.
        numeric = pd.to_numeric(df[name], errors="coerce")
        df[name] = numeric.astype(target) if target == "float64" else numeric.round().astype(target)
    return df


# Glue/Hive column names are case-insensitive and stored LOWERCASE — declaring
# `surf_L_n_vertices` in a table yields `surf_l_n_vertices`, and that lowercased
# catalog name is the Label Athena returns. The values still arrive: the openx
# JsonSerDe matches the mixed-case JSON keys regardless. Only the name differs.
#
# DuckDB infers its column names from the JSON keys, so without this the two
# engines return identical data under different names, and
# `df["surf_L_coverage_frac"]` works on one engine and KeyErrors on the other.
# The mixed-case names are the documented ones (docs/metrics_data_dictionary.md)
# and what the emitter writes, so Athena is the side that gets corrected.
#
# Derived from _UNION_COLUMNS rather than hand-listed: any future mixed-case
# field is covered as soon as it reaches that list, which the five-place schema
# convention already requires. Today it is the eight FuncQC surf_L_*/surf_R_*
# columns; subcort_* need no entry because they are already lowercase.
_CANONICAL_CASE = {
    col.lower(): col for cols in _UNION_COLUMNS.values() for col in cols if col != col.lower()
}


def _restore_column_case(df):
    """Undo Glue's lowercasing of mixed-case column names."""
    renames = {c: _CANONICAL_CASE[c] for c in df.columns if c in _CANONICAL_CASE}
    return df.rename(columns=renames) if renames else df


class CloudpipeMetrics:
    """Query the cloudpipe_metrics Athena database."""

    def __init__(
        self,
        bucket: str,
        region: str = "<YOUR_AWS_REGION>",
        workgroup: str = "cloudpipe_metrics_workgroup",
        output_prefix: str = "grafana-query-results",
        finops_bucket: str | None = None,
    ):
        """
        Parameters
        ----------
        bucket:
            Main cloudpipe S3 bucket (where metrics/ lives).
        region:
            AWS region.
        workgroup:
            Athena workgroup for cloudpipe_metrics queries.
        output_prefix:
            S3 prefix in finops_bucket for Athena query results.
        finops_bucket:
            Bucket for query results; defaults to `bucket` if not set.
        """
        import boto3

        self._athena = boto3.client("athena", region_name=region)
        self._workgroup = workgroup
        self._results_bucket = finops_bucket or bucket
        self._results_prefix = output_prefix

    # ------------------------------------------------------------------
    # Public query methods
    # ------------------------------------------------------------------

    def func_qc(
        self,
        dt_from: str | None = None,
        dt_to: str | None = None,
        compacted: bool = False,
        **filters: Any,
    ):
        """Return functional QC metrics as a DataFrame.

        Keyword args are appended as WHERE clauses (exact match).
        Example: func_qc(session="ses-00A", task="task-rest")

        dt_from/dt_to (inclusive, YYYY-MM-DD) scope the query to the `dt=`
        partition range, which Athena can prune on — pass these whenever the
        write-date window is known, rather than relying only on a
        completed_at/recorded_at filter that gives Athena nothing to prune.

        compacted: if True, reads today's raw JSON unioned with all prior
        compacted Parquet days (see _query's docstring) instead of only the
        raw table. Off by default so existing callers/dashboards are
        unaffected until explicitly opted in.
        """
        return self._query("func_preproc", filters, dt_from, dt_to, compacted)

    def surface_qc(
        self,
        dt_from: str | None = None,
        dt_to: str | None = None,
        compacted: bool = False,
        **filters: Any,
    ):
        """Return grayordinate (surface) QC metrics as a DataFrame.

        Same per-BOLD-run grain as func_qc(), and this is the table to prefer
        for surface questions: it is written by BOTH of preproc.py's
        grayordinate paths, whereas func_qc's surf_*/subcort_* copy is absent
        on the `--emit grayordinate` short path and stale whenever a run's
        surfaces were recomputed without its volumetric output.

        Filter or read `emit` to know which case you have: "both" means
        func_qc carries the same values for that run, "grayordinate" means
        this record is the only copy.

        Not the same row count as func_qc() over the same window — only runs
        that actually produced surfaces appear here, and a run reprocessed for
        surfaces alone appears under the dt of that later pass.
        """
        return self._query("surface_sample", filters, dt_from, dt_to, compacted)

    def anat_qc(
        self,
        dt_from: str | None = None,
        dt_to: str | None = None,
        compacted: bool = False,
        **filters: Any,
    ):
        """Return anatomical QC metrics as a DataFrame."""
        return self._query("anat_qc", filters, dt_from, dt_to, compacted)

    def fsqc_qc(
        self,
        dt_from: str | None = None,
        dt_to: str | None = None,
        compacted: bool = False,
        **filters: Any,
    ):
        """Return fsqc anatomical QC metrics as a DataFrame.

        Same subject+session grain as anat_qc(), and complementary to it rather
        than overlapping: WM/GM SNR lives here (wm_snr_norm/gm_snr_norm) since
        anat_qc dropped snr_wm/snr_gm at schema 1.2, while the volume and
        thickness fields live only there. Join on subject+session.

        Every metric is nullable — filter with IS NOT NULL, not `> 0`, and read
        the relevant *_status column alongside it (0 = the module ran clean,
        non-zero = it degraded, NULL = it never reported).
        """
        return self._query("fsqc_qc", filters, dt_from, dt_to, compacted)

    def anatomical_qc(
        self,
        dt_from: str | None = None,
        dt_to: str | None = None,
        **filters: Any,
    ):
        """Return anat_qc and fsqc_qc as ONE per-subject×session anatomical view.

        Both tables describe the same T1w image at the same grain, and neither
        is complete alone: WM/GM SNR (wm_snr_norm/gm_snr_norm) left anat_qc at
        schema 1.2 and lives only in fsqc_qc, while volumes, thickness, and the
        mriqc-style IQMs live only in anat_qc. This is the accessor to reach
        for when the question is "how good is this subject's anatomical?";
        anat_qc()/fsqc_qc() remain for single-table work.

        Row count is the number of distinct subject×session pairs present in
        EITHER table over the dt window — never a multiple of it, since each
        side is deduplicated to its most recent record first.

        dt_from/dt_to select WHICH SCANS to return — a scan is in scope if
        either table wrote a record in the window — but do NOT restrict which
        record describes them: each scan carries its most recent record from
        each table, whenever that was written. See anatomical_join_sql for the
        measurement behind that (filtering both tables by the window left 0 of
        23 pilot rows paired; this leaves 23 of 23). Keyword filters (subject=,
        session=) apply to the records themselves, on both sides.

        A NULL anat half is therefore meaningful: it means no anat_qc record
        exists for that scan at all, not merely that it was written on another
        day. `anat_completed_at` tells you when the surviving half was computed,
        and it can legitimately be weeks before `fsqc_completed_at`.

        Every fsqc-side metric is nullable — filter on IS NOT NULL, not `> 0`
        (0.0 is real for rot_tal_* and n_outlier_norms), and read the relevant
        *_status column alongside it.

        No `compacted` option, unlike the single-table accessors: anat_qc's
        _UNION_COLUMNS entry is a curated subset that omits the IQM columns, so
        a compacted-mode join would quietly return fewer columns than a raw one.
        """
        both = [f"{k} = '{v}'" if isinstance(v, str) else f"{k} = {v}" for k, v in filters.items()]
        window = []
        if dt_from:
            window.append(f"dt >= '{dt_from}'")
        if dt_to:
            window.append(f"dt <= '{dt_to}'")
        return self._run_sql(
            anatomical_join_sql(
                "cloudpipe_metrics.anat_qc",
                "cloudpipe_metrics.fsqc_qc",
                ("WHERE " + " AND ".join(both)) if both else "",
                " AND ".join(window),
            )
        )

    def workflow_runs(
        self,
        dt_from: str | None = None,
        dt_to: str | None = None,
        compacted: bool = False,
        **filters: Any,
    ):
        """Return workflow run summaries as a DataFrame."""
        return self._query("workflow_runs", filters, dt_from, dt_to, compacted)

    def registration_qc(
        self,
        dt_from: str | None = None,
        dt_to: str | None = None,
        compacted: bool = False,
        **filters: Any,
    ):
        """Return registration QC metrics as a DataFrame.

        Use registration_type= to filter to one step:
          registration_qc(registration_type="t1w_to_mni")
          registration_qc(registration_type="bold_to_t1w")
        """
        return self._query("registration", filters, dt_from, dt_to, compacted)

    def costs(
        self,
        dt_from: str | None = None,
        dt_to: str | None = None,
        compacted: bool = False,
        **filters: Any,
    ):
        """Return raw cost allocation records as a DataFrame.

        Grain is one row per Argo workflow per scrape date — NOT one row per
        subject. A subject processed over two UTC days, or reprocessed after a
        failure, has several rows. For per-subject totals use subject_costs().

        `dt` equals the `date` field for cost records, so dt_from/dt_to and
        date_from/date_to (in subject_costs) are interchangeable here.
        """
        return self._query("costs", filters, dt_from, dt_to, compacted)

    def subject_costs(
        self,
        subjects: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ):
        """Return one row per subject with costs summed over workflow-day records.

        The `metrics/costs/` prefix accumulates across batches, so scope by the
        batch subject list and the batch's scrape-date window. See the DuckDB
        twin in metrics/duckdb_query.py for parameter details, including what
        total_adjustment_usd and max_scrape_age_days (schema 1.2+) mean.
        """
        return self._run_sql(f"""
            SELECT
                subject,
                COUNT(*)                       AS n_records,
                COUNT(DISTINCT workflow_name)  AS n_workflows,
                SUM(total_cost_usd)            AS total_cost_usd,
                SUM(cpu_cost_usd)              AS cpu_cost_usd,
                SUM(memory_cost_usd)           AS memory_cost_usd,
                SUM(gpu_cost_usd)              AS gpu_cost_usd,
                SUM(total_adjustment_usd)      AS total_adjustment_usd,
                MAX(scrape_age_days)           AS max_scrape_age_days,
                MIN(date)                      AS first_date,
                MAX(date)                      AS last_date
            FROM cloudpipe_metrics.costs
            {cost_scope_clause(subjects, date_from, date_to)}
            GROUP BY subject
            ORDER BY total_cost_usd DESC
        """)

    def pod_costs(
        self,
        dt_from: str | None = None,
        dt_to: str | None = None,
        compacted: bool = False,
        **filters: Any,
    ):
        """Return raw per-pod cost records as a DataFrame.

        Grain is one row per pod per report date — the finest cost grain
        available, and one grain below costs(). Summing total_cost_usd over a
        (date, workflow_name) reproduces that workflow's costs() row.

        Filter to one component with step=:
          pod_costs(step="bold-preprocessing")
          pod_costs(phase="functional", dt_from="2026-07-01")
        """
        return self._query("pod_costs", filters, dt_from, dt_to, compacted)

    def step_costs(
        self,
        subjects: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ):
        """Return the cost distribution across workflow components.

        One row per pipeline step, with cost, share of total, per-pod spread,
        and request efficiency. This is the answer to "where does the money
        go" — read total_cost_usd for size, p95 vs median for variance, and
        cpu/ram efficiency for whether that cost is reducible by right-sizing
        rather than by faster code.

        n_pods is the pod count, not the subject count: a subject with eight
        BOLD runs contributes eight bold-to-t1w pods. That is deliberate —
        mean_cost_usd is then the per-unit-of-work cost, which is what scales
        when the batch grows.

        Pods whose template sets no `cloudpipe.io/step` label group under ''
        rather than being dropped, so the step rows always sum to the true
        total; a large '' row means a template is missing its label, not that
        cost is unattributable.
        """
        return self._run_sql(f"""
            SELECT
                step,
                phase,
                COUNT(*)                                   AS n_pods,
                COUNT(DISTINCT subject)                    AS n_subjects,
                SUM(total_cost_usd)                        AS total_cost_usd,
                SUM(total_cost_usd) * 100.0
                    / SUM(SUM(total_cost_usd)) OVER ()     AS pct_of_total,
                AVG(total_cost_usd)                        AS mean_cost_usd,
                APPROX_PERCENTILE(total_cost_usd, 0.5)     AS median_cost_usd,
                APPROX_PERCENTILE(total_cost_usd, 0.95)    AS p95_cost_usd,
                MAX(total_cost_usd)                        AS max_cost_usd,
                SUM(cpu_cost_usd)                          AS cpu_cost_usd,
                SUM(memory_cost_usd)                       AS memory_cost_usd,
                SUM(gpu_cost_usd)                          AS gpu_cost_usd,
                SUM(pv_cost_usd)                           AS pv_cost_usd,
                AVG(runtime_minutes)                       AS mean_runtime_minutes,
                AVG(cpu_efficiency)                        AS mean_cpu_efficiency,
                AVG(ram_efficiency)                        AS mean_ram_efficiency
            FROM cloudpipe_metrics.pod_costs
            {cost_scope_clause(subjects, date_from, date_to)}
            GROUP BY step, phase
            ORDER BY total_cost_usd DESC
        """)

    def spot_savings(
        self,
        subjects: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        group_by: str | None = None,
    ):
        """Return what this scope actually cost vs. what it would have cost on demand.

        Every Karpenter nodepool is spot-only (ADR 007), so the dollar columns
        in `pod_costs` are spot dollars end to end and there is no on-demand
        figure anywhere to compare them against. This rebuilds one by scaling
        each pod's COMPUTE cost by its node's
        `node_ondemand_usd_per_hour / node_effective_usd_per_hour`.

        pv_cost_usd and network_cost_usd pass through unscaled: EBS and data
        transfer are not priced by capacity type, and multiplying them would
        inflate the counterfactual by the spot discount on storage that never
        existed.

        THE ANSWER IS 'ON DEMAND AT LIST PRICE'. node_ondemand_usd_per_hour is
        AWS's public rate; any savings plan or RI the account holds would make
        real on-demand spend lower, so read this as the conservative upper
        bound on what abandoning spot would cost.

        COVERAGE IS PART OF THE ANSWER, NOT A FOOTNOTE. Rows whose rates are
        NULL — nodes Kubecost had no asset for, instance types the Pricing API
        would not price, and every row written before schema 1.1 — cannot be
        scaled and are excluded from `ondemand_cost_usd`. Summing an
        unrestricted total against them would silently understate. So
        `priced_cost_usd` (the actual cost of only the rows that COULD be
        scaled) is what `ondemand_cost_usd` is comparable against, and
        `coverage_frac` says how much of the scope that is. A coverage_frac
        well below 1.0 makes the savings figure a sample, not a total.

        When NOTHING in the scope could be priced, `coverage_frac` is 0.0 while
        the dollar columns are NULL. That asymmetry is deliberate: the fraction
        has to stay comparable so a `< threshold` guard fires, whereas a 0 in
        `savings_usd` would read as a measured "no savings" instead of "not
        computable".

        DRIFT BEHAVES BACKWARDS FROM EVERY OTHER COLUMN HERE, so read it
        carefully. Reconciliation inflates a day+1 read by some factor k
        (median ~1.5) — and it inflates the pod's cost and the NODE's effective
        rate by the same k, because both come from the same unreconciled
        Kubecost pass. The list price does not move. So in

            ondemand = cost * (list / effective) = (k*cost_s) * (mult_s / k)

        the k cancels: `ondemand_cost_usd` is the most drift-resistant number in
        this table, usable at day+1. `savings_usd` and the ratio are NOT — only
        their actual-cost side carries the inflation, so at day+1 both
        UNDERSTATE the real gap by roughly k. Wait for scrape_age_days = 3
        before quoting a savings multiple.

        group_by, when given, breaks the result out by one of `step`, `phase`,
        `subject`, `node_instance_type`, or `date` — e.g. to see which
        component leans hardest on the spot discount.
        """
        allowed = {"step", "phase", "subject", "node_instance_type", "date"}
        if group_by is not None and group_by not in allowed:
            raise ValueError(f"group_by must be one of {sorted(allowed)}, got {group_by!r}")

        select_group = f"{group_by}," if group_by else ""
        group_clause = f"GROUP BY {group_by} ORDER BY {group_by}" if group_by else ""

        return self._run_sql(f"""
            WITH scoped AS (
                SELECT
                    *,
                    -- Guarded against 0 as well as NULL: a zero effective rate
                    -- would divide by zero, and a zero on-demand rate is a
                    -- pricing miss that got stored rather than a free instance.
                    CASE
                        WHEN node_effective_usd_per_hour > 0
                         AND node_ondemand_usd_per_hour > 0
                        THEN node_ondemand_usd_per_hour / node_effective_usd_per_hour
                    END AS multiplier
                FROM cloudpipe_metrics.pod_costs
                {cost_scope_clause(subjects, date_from, date_to)}
            )
            SELECT
                {select_group}
                COUNT(*)                                       AS n_pods,
                SUM(total_cost_usd)                            AS actual_cost_usd,
                SUM(CASE WHEN multiplier IS NOT NULL
                         THEN total_cost_usd END)              AS priced_cost_usd,
                SUM(CASE WHEN multiplier IS NOT NULL
                         THEN (cpu_cost_usd + memory_cost_usd + gpu_cost_usd) * multiplier
                              + pv_cost_usd + network_cost_usd END)
                                                               AS ondemand_cost_usd,
                SUM(CASE WHEN multiplier IS NOT NULL
                         THEN (cpu_cost_usd + memory_cost_usd + gpu_cost_usd) * multiplier
                              + pv_cost_usd + network_cost_usd END)
                    - SUM(CASE WHEN multiplier IS NOT NULL
                               THEN total_cost_usd END)        AS savings_usd,
                -- COALESCE, so a scope with NOTHING priced reads 0.0 rather
                -- than NULL. SUM(CASE ...) over zero matching rows is NULL,
                -- and NULL/x propagates to NaN in pandas — where every
                -- comparison is False, so the obvious guard
                -- `if coverage_frac < 0.9: warn` would silently NOT fire on
                -- the worst possible coverage. The dollar columns above stay
                -- NULL on purpose: a 0 there would read as a real "$0 saved"
                -- rather than "not computable".
                COALESCE(SUM(CASE WHEN multiplier IS NOT NULL
                                  THEN total_cost_usd END), 0)
                    / NULLIF(SUM(total_cost_usd), 0)           AS coverage_frac,
                AVG(multiplier)                                AS mean_multiplier
            FROM scoped
            {group_clause}
        """)

    def run_costs(
        self,
        subjects: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ):
        """Return an EVEN SPLIT of pod cost across the (task, run)s it processed.

        `bold-to-t1w`, `bold-preprocessing`, and `surface-resample` each run as
        ONE pod per session, looping over every BOLD run internally — Kubecost
        bills at pod granularity, so there is no measured cost below session
        grain. This divides each such pod's cost evenly across the runs its
        `step_outcomes` rows say it processed. It is a MODELED split, not a
        measured one: two runs of very different length are charged equally.
        Do not use this to compare specific runs' cost; use it to compare
        subjects/sessions once step_costs() has already pointed at one of the
        three run-scoped steps as the thing worth investigating.

        Subject-scoped steps (e.g. long-segmentation) never appear here —
        step_outcomes gives them task="na"/run="na", which this excludes.

        Returned `step` values are the `cloudpipe.io/step` pod-cost vocabulary
        (`bold-to-t1w`, `bold-preprocessing`, `surface-resample`), NOT the
        step_outcomes canonical vocabulary — the two differ for func-preproc.
        The `bold-preprocessing` pod writes TWO step_outcomes rows per run
        (`func-preproc` for its volumetric output, `surface-sample` for its
        grayordinate output) from one execution, so joining on step_outcomes'
        own step names would attribute that one pod's cost twice. This maps
        step_outcomes' `func-preproc` rows to the pod-cost step
        `bold-preprocessing` and drops `surface-sample` rows entirely — that
        derivative's cost is the same dollars already counted under
        `bold-preprocessing`, not an additional cost.

        A step retried within the same workflow can produce more than one pod
        for the same (workflow_name, step, subject, session); their costs are
        summed before splitting, so a retry's extra cost is folded into the
        per-run figure rather than duplicating rows.

        Inherits the same day+1 reconciliation caveat as pod_costs() — the
        dollar total being split is provisional until scrape_age_days reaches
        3 (see step_costs()'s docstring and kubecost_drift_probe.py).
        """
        pod_scope = cost_scope_clause(subjects, date_from, date_to)
        sql = f"""
        WITH pod_cost_by_key AS (
            SELECT
                workflow_name, step, subject, session,
                SUM(total_cost_usd)   AS total_cost_usd,
                SUM(cpu_cost_usd)     AS cpu_cost_usd,
                SUM(memory_cost_usd)  AS memory_cost_usd,
                SUM(gpu_cost_usd)     AS gpu_cost_usd,
                MAX(date)             AS date,
                MAX(scrape_age_days)  AS scrape_age_days
            FROM cloudpipe_metrics.pod_costs
            {pod_scope}
            GROUP BY workflow_name, step, subject, session
        ),
        run_outcomes AS (
            -- Map step_outcomes' canonical step names onto the pod_costs
            -- vocabulary. surface-sample is dropped: it is a second derivative
            -- of the SAME bold-preprocessing pod as func-preproc, not a
            -- separately-costed step (see docstring).
            SELECT
                workflow_name,
                CASE step WHEN 'func-preproc' THEN 'bold-preprocessing' ELSE step END AS step,
                subject, session, task, run
            FROM cloudpipe_metrics.step_outcomes
            WHERE task <> 'na' AND run <> 'na' AND step <> 'surface-sample'
        ),
        run_counts AS (
            SELECT workflow_name, step, subject, session, COUNT(*) AS n_runs_in_pod
            FROM run_outcomes
            GROUP BY workflow_name, step, subject, session
        )
        SELECT
            so.workflow_name, so.step, so.subject, so.session, so.task, so.run,
            pc.total_cost_usd  / rc.n_runs_in_pod  AS total_cost_usd,
            pc.cpu_cost_usd    / rc.n_runs_in_pod  AS cpu_cost_usd,
            pc.memory_cost_usd / rc.n_runs_in_pod  AS memory_cost_usd,
            pc.gpu_cost_usd    / rc.n_runs_in_pod  AS gpu_cost_usd,
            rc.n_runs_in_pod,
            pc.date, pc.scrape_age_days
        FROM run_outcomes so
        JOIN run_counts rc
          ON so.workflow_name = rc.workflow_name AND so.step = rc.step
         AND so.subject = rc.subject AND so.session = rc.session
        JOIN pod_cost_by_key pc
          ON pc.workflow_name = rc.workflow_name AND pc.step = rc.step
         AND pc.subject = rc.subject AND pc.session = rc.session
        ORDER BY so.subject, so.session, so.task, so.run
        """
        return self._run_sql(sql)

    def join_subject(self, subject: str):
        """Return per-BOLD-run QC for one subject, with its run-of-record
        workflow status, duration, and total cost attached.

        Grain is one row per run (subject, session, task, run) — the row count
        equals the subject's BOLD-run count, never a multiple of it.

        The three tables sit at three grains: func_preproc is per-run,
        workflow_runs is per-workflow (a reprocessed subject has several), and
        costs is per-workflow-per-scrape-date (a midnight-spanning workflow has
        several). Joining them naively on `subject` / `workflow_name` fans the
        run rows out by the product of those multiplicities, so any SUM over
        the result overstates cost and duration. This query avoids that by
        summing costs to workflow grain first, then reducing workflow_runs to
        the subject's most recent run (the run of record) before the join, so
        each run row is annotated exactly once.

        `status`, `total_duration_s`, and `total_cost_usd` are workflow-grain
        values repeated on every run row for the subject. They describe the
        whole workflow, not the individual run, so do NOT SUM them across the
        returned rows — read any single row for the subject-level figure.
        """
        sql = f"""
        WITH cost_by_workflow AS (
            SELECT workflow_name, SUM(total_cost_usd) AS total_cost_usd
            FROM cloudpipe_metrics.costs
            GROUP BY workflow_name
        ),
        workflow_of_record AS (
            SELECT
                w.subject, w.status, w.total_duration_s, c.total_cost_usd,
                ROW_NUMBER() OVER (
                    PARTITION BY w.subject ORDER BY w.completed_at DESC
                ) AS rn
            FROM cloudpipe_metrics.workflow_runs w
            LEFT JOIN cost_by_workflow c ON w.workflow_name = c.workflow_name
            WHERE w.subject = '{subject}'
        )
        SELECT
            f.session, f.task, f.run,
            f.mean_fd, f.tsnr_median, f.pct_fd_above_0p5, f.total_runtime_s,
            a.lh_mean_thickness_mm, a.rh_mean_thickness_mm, a.total_brain_vol_mm3,
            w.status, w.total_duration_s,
            w.total_cost_usd
        FROM cloudpipe_metrics.func_preproc f
        LEFT JOIN cloudpipe_metrics.anat_qc a
               ON f.subject = a.subject AND f.session = a.session
        LEFT JOIN workflow_of_record w
               ON w.subject = f.subject AND w.rn = 1
        WHERE f.subject = '{subject}'
        ORDER BY f.session, f.task, f.run
        """
        return self._run_sql(sql)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _query(
        self,
        table: str,
        filters: dict[str, Any],
        dt_from: str | None = None,
        dt_to: str | None = None,
        compacted: bool = False,
    ):
        where_clauses = [
            f"{k} = '{v}'" if isinstance(v, str) else f"{k} = {v}" for k, v in filters.items()
        ]
        if dt_from:
            where_clauses.append(f"dt >= '{dt_from}'")
        if dt_to:
            where_clauses.append(f"dt <= '{dt_to}'")
        extra_where = " AND ".join(where_clauses)

        if not compacted:
            where = f"WHERE {extra_where}" if extra_where else ""
            sql = f"SELECT * FROM cloudpipe_metrics.{table} {where} ORDER BY completed_at DESC"
            return self._run_sql(sql)

        # Today's raw JSON unioned with all prior compacted Parquet days.
        # Never the same dt on both sides (raw is restricted to dt =
        # CURRENT_DATE, compacted to dt < CURRENT_DATE — compaction itself
        # never touches today, see src/metrics/compactor.py), so this never
        # double-counts a record. Explicit column lists (not SELECT *) are
        # required: the *_compacted table has one fewer column than the raw
        # table (schema_version is a partition key there, a data column
        # here), so positional UNION ALL needs identical column lists on
        # both sides regardless of each table's own column order.
        cols = _UNION_COLUMNS.get(table)
        if cols is None:
            raise ValueError(f"No compacted counterpart registered for table {table!r}")
        col_list = ", ".join(cols)
        # `dt` is declared `string` on every table (all nine), so a bare
        # `dt = CURRENT_DATE` is varchar = date and Athena rejects the WHOLE
        # query with TYPE_MISMATCH — this made compacted=True unusable for every
        # table, not just registration. Same varchar/date class of bug that broke
        # six Grafana dashboards.
        #
        # Format the DATE down to varchar rather than casting `dt` up to DATE
        # (which is what the Grafana panels do, because there both sides come
        # from Grafana's macros). Comparing the string partition as a string is
        # the more direct reading; a measured A/B on the live table showed
        # identical DataScannedInBytes for both forms, so this is not a
        # pruning optimisation — do not cite it as one. `<` stays correct
        # because YYYY-MM-DD sorts lexicographically.
        today = "date_format(CURRENT_DATE, '%Y-%m-%d')"
        raw_where = " AND ".join([f"dt = {today}", *where_clauses])
        compacted_where = " AND ".join([f"dt < {today}", *where_clauses])
        sql = f"""
            SELECT {col_list} FROM cloudpipe_metrics.{table} WHERE {raw_where}
            UNION ALL
            SELECT {col_list} FROM cloudpipe_metrics.{table}_compacted WHERE {compacted_where}
            ORDER BY completed_at DESC
        """
        return self._run_sql(sql)

    def _run_sql(self, sql: str):
        import pandas as pd

        output_location = f"s3://{self._results_bucket}/{self._results_prefix}/"
        resp = self._athena.start_query_execution(
            QueryString=sql,
            WorkGroup=self._workgroup,
            ResultConfiguration={"OutputLocation": output_location},
        )
        qid = resp["QueryExecutionId"]

        elapsed = 0.0
        while elapsed < _MAX_WAIT_S:
            status = self._athena.get_query_execution(QueryExecutionId=qid)
            state = status["QueryExecution"]["Status"]["State"]
            if state == "SUCCEEDED":
                break
            if state in {"FAILED", "CANCELLED"}:
                reason = status["QueryExecution"]["Status"].get("StateChangeReason", "")
                raise RuntimeError(f"Athena query {qid} {state}: {reason}")
            time.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL
        else:
            raise TimeoutError(f"Athena query {qid} did not complete within {_MAX_WAIT_S}s")

        # Paginate results
        rows: list[dict] = []
        paginator = self._athena.get_paginator("get_query_results")
        columns: list[str] = []
        col_types: dict[str, str] = {}
        for page in paginator.paginate(QueryExecutionId=qid):
            result = page["ResultSet"]
            if not columns:
                info = result["ResultSetMetadata"]["ColumnInfo"]
                columns = [c["Label"] for c in info]
                col_types = {c["Label"]: c["Type"] for c in info}
            for row in result["Rows"]:
                values = [d.get("VarCharValue", None) for d in row["Data"]]
                if values != columns:  # skip header row
                    # strict=True: Athena returns one Data entry per column. If that
                    # ever failed, a plain zip would silently drop trailing columns and
                    # hand back a DataFrame with missing values rather than an error.
                    rows.append(dict(zip(columns, values, strict=True)))

        df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=pd.Index(columns))
        # Cast first, rename second: col_types is keyed on the labels Athena
        # returned, so restoring the case before casting would leave the numeric
        # columns as strings.
        return _restore_column_case(_cast_athena_types(df, col_types))
