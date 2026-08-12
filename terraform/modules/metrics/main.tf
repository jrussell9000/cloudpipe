################################################################################
# CloudPipe Metrics — Glue catalog
#
# Every table is declared explicitly in this file, with partition projection.
# There are no crawlers: they were removed on 2026-07-30 after generating 5,227
# junk tables. See the block at the bottom of this file for the full account
# and for what to do instead when a schema gains a column.
################################################################################

# ---------------------------------------------------------------------------
# Glue catalog database
# ---------------------------------------------------------------------------

resource "aws_glue_catalog_database" "metrics" {
  name = "cloudpipe_metrics"
}

# ---------------------------------------------------------------------------
# Raw metric prefixes
# ---------------------------------------------------------------------------

locals {
  # One entry per raw metrics prefix. Each prefix must hold exactly ONE record
  # grain: the explicit tables below merge every schema found under a prefix,
  # so mixing grains yields a table where half the columns are null on every
  # row.
  #
  # Keys are table names, values are S3 locations. Used only to build
  # partition_projection below — every prefix here needs a matching
  # aws_glue_catalog_table resource; nothing discovers them automatically.
  metric_prefixes = {
    func_qc           = "s3://${var.bucket}/metrics/func-preproc/"
    surface_sample    = "s3://${var.bucket}/metrics/surface-sample/"
    anat_qc           = "s3://${var.bucket}/metrics/anat-qc/"
    fsqc_qc           = "s3://${var.bucket}/metrics/fsqc-qc/"
    registration_qc   = "s3://${var.bucket}/metrics/registration/"
    workflow_runs     = "s3://${var.bucket}/metrics/workflow-runs/"
    costs             = "s3://${var.bucket}/metrics/costs/"
    pod_costs         = "s3://${var.bucket}/metrics/pod-costs/"
    step_outcomes     = "s3://${var.bucket}/metrics/step-outcomes/"
    subject_manifests = "s3://${var.bucket}/metrics/subject-manifests/"
  }

  # Partition projection parameters, one map per prefix, merged into each
  # explicit table's `parameters` block. Projection computes dt= partitions
  # from a formula instead of requiring a crawler run or MSCK REPAIR before a
  # newly-written dt= partition becomes queryable. With the crawlers gone this
  # is the ONLY thing making partitions visible — a prefix whose objects are
  # not under dt=YYYY-MM-DD/ is unreachable from Athena entirely.
  partition_projection = {
    for name, path in local.metric_prefixes : name => {
      "projection.enabled"   = "true"
      "projection.dt.type"   = "date"
      "projection.dt.format" = "yyyy-MM-dd"
      # Conservative lower bound predating the earliest real metrics data;
      # confirm against `aws s3 ls s3://<bucket>/metrics/<prefix>/` rather
      # than assuming this is still correct as the corpus ages.
      "projection.dt.range"         = "2026-01-01,NOW"
      "projection.dt.interval"      = "1"
      "projection.dt.interval.unit" = "DAYS"
      "storage.location.template"   = "${path}dt=$${dt}/"
    }
  }
}

# ---------------------------------------------------------------------------
# Compacted Parquet tables — one per raw table, written by the nightly
# metrics-compactor Prefect flow (src/metrics/compactor.py) into
# metrics/compacted/{table}/dt={date}/schema_version={version}/. No crawler
# targets these: the compactor's output columns are known and hand-set here,
# so there's nothing for a crawler to discover. schema_version is a second
# partition key (not a data column) since it's encoded in the S3 path, not
# the JSON body — Athena still returns NULL for any declared column absent
# from a given file, so a per-schema_version Parquet file only needs to
# physically carry the fields that version actually wrote.
#
# "projection.schema_version.values" must be updated by hand whenever a new
# schema_version starts being emitted (unlike dt, which is formula-projected
# and needs no per-day update).
# ---------------------------------------------------------------------------

locals {
  # table_name -> compacted S3 location, keyed to match compactor.py's
  # RAW_PREFIXES table_name keys (not metric_prefixes' names, which differ
  # for func_qc/registration_qc).
  compacted_targets = {
    func_preproc      = "s3://${var.bucket}/metrics/compacted/func_preproc/"
    surface_sample    = "s3://${var.bucket}/metrics/compacted/surface_sample/"
    anat_qc           = "s3://${var.bucket}/metrics/compacted/anat_qc/"
    fsqc_qc           = "s3://${var.bucket}/metrics/compacted/fsqc_qc/"
    workflow_runs     = "s3://${var.bucket}/metrics/compacted/workflow_runs/"
    step_outcomes     = "s3://${var.bucket}/metrics/compacted/step_outcomes/"
    subject_manifests = "s3://${var.bucket}/metrics/compacted/subject_manifests/"
    registration      = "s3://${var.bucket}/metrics/compacted/registration/"
    costs             = "s3://${var.bucket}/metrics/compacted/costs/"
    pod_costs         = "s3://${var.bucket}/metrics/compacted/pod_costs/"
  }

  compacted_partition_projection = {
    for name, path in local.compacted_targets : name => {
      "projection.enabled"             = "true"
      "projection.dt.type"             = "date"
      "projection.dt.format"           = "yyyy-MM-dd"
      "projection.dt.range"            = "2026-01-01,NOW"
      "projection.dt.interval"         = "1"
      "projection.dt.interval.unit"    = "DAYS"
      "projection.schema_version.type" = "enum"
      "storage.location.template"      = "${path}dt=$${dt}/schema_version=$${schema_version}/"
    }
  }
}

resource "aws_glue_catalog_table" "func_preproc_compacted" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "func_preproc_compacted"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.compacted_partition_projection.func_preproc, {
    "classification"                   = "parquet"
    "projection.schema_version.values" = "1.0,1.1"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }
  partition_keys {
    name = "schema_version"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/compacted/func_preproc/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "session"
      type = "string"
    }
    columns {
      name = "task"
      type = "string"
    }
    columns {
      name = "run"
      type = "string"
    }
    columns {
      name = "n_frames"
      type = "int"
    }
    columns {
      name = "n_nss_frames"
      type = "int"
    }
    columns {
      name = "tr_seconds"
      type = "double"
    }
    columns {
      name = "mean_fd"
      type = "double"
    }
    columns {
      name = "median_fd"
      type = "double"
    }
    columns {
      name = "max_fd"
      type = "double"
    }
    columns {
      name = "n_fd_above_0p2"
      type = "int"
    }
    columns {
      name = "n_fd_above_0p5"
      type = "int"
    }
    columns {
      name = "pct_fd_above_0p5"
      type = "double"
    }
    columns {
      name = "mean_dvars"
      type = "double"
    }
    columns {
      name = "dvars_std"
      type = "double"
    }
    columns {
      name = "mean_global_signal"
      type = "double"
    }
    columns {
      name = "tsnr_median"
      type = "double"
    }
    columns {
      name = "gcor"
      type = "double"
    }
    columns {
      name = "aor"
      type = "double"
    }
    columns {
      name = "aqi"
      type = "double"
    }
    columns {
      name = "n_acompcor_wm"
      type = "int"
    }
    columns {
      name = "n_acompcor_csf"
      type = "int"
    }
    columns {
      name = "n_tcompcor"
      type = "int"
    }
    columns {
      name = "n_cosines"
      type = "int"
    }
    columns {
      # Every key preproc.py's timed_stage() emits must be listed here or Athena
      # silently drops it -- 4d_warp (the second-largest stage, ~30 s/run) and
      # grayordinates were missing from 2026-07 until 2026-08-03 and never
      # reached a query or a dashboard.
      #
      # 4d_warp leads with a digit, so SQL must quote it: stage_timings_s."4d_warp".
      # Unquoted, Athena fails the WHOLE query with MALFORMED_QUERY ("mismatched
      # input '.4'"), not just that column -- see the Grafana stage-timing panel.
      name = "stage_timings_s"
      type = "struct<boldref:double,composite_warp:double,4d_warp:double,mask_warp:double,masking:double,confounds:double,grayordinates:double>"
    }
    columns {
      name = "total_runtime_s"
      type = "double"
    }
    columns {
      name = "peak_memory_gb"
      type = "double"
    }
    columns {
      name = "container_peak_memory_gb"
      type = "double"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "image_tag"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
    # Grayordinate QC — see the identical block on the raw `func_preproc` table
    # for why these sit after completed_at, why they were missing, and why the
    # surf_l_/surf_r_ names are lowercase here while the emitter writes
    # surf_L_/surf_R_. Declared on the compacted table too because compactor.py
    # derives each Parquet file's columns from the fields the records actually
    # carry rather than from the schemas.py dataclass, so the surface fields do
    # reach the Parquet — a column undeclared here is invisible, not absent.
    columns {
      name = "surf_l_n_vertices"
      type = "int"
    }
    columns {
      name = "surf_l_coverage_frac"
      type = "double"
    }
    columns {
      name = "surf_l_nan_frac"
      type = "double"
    }
    columns {
      name = "surf_l_tsnr_median"
      type = "double"
    }
    columns {
      name = "surf_r_n_vertices"
      type = "int"
    }
    columns {
      name = "surf_r_coverage_frac"
      type = "double"
    }
    columns {
      name = "surf_r_nan_frac"
      type = "double"
    }
    columns {
      name = "surf_r_tsnr_median"
      type = "double"
    }
    columns {
      name = "subcort_n_voxels"
      type = "int"
    }
    columns {
      name = "subcort_n_structures"
      type = "int"
    }
    columns {
      name = "subcort_space"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "surface_sample_compacted" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "surface_sample_compacted"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.compacted_partition_projection.surface_sample, {
    "classification" = "parquet"
    # 1.0 is the first version the emitter stamps. Records written before
    # 2026-08-11 carry NO schema_version at all, which compactor.py groups
    # under the literal "unknown" — declared here so the pre-fix batch stays
    # readable rather than landing outside the enum and returning zero rows
    # with a successful query status (#241). Do not drop "unknown" until
    # those partitions are gone.
    "projection.schema_version.values" = "unknown,1.0"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }
  partition_keys {
    name = "schema_version"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/compacted/surface_sample/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "session"
      type = "string"
    }
    columns {
      name = "task"
      type = "string"
    }
    columns {
      name = "run"
      type = "string"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "image_tag"
      type = "string"
    }
    columns {
      name = "emit"
      type = "string"
    }
    columns {
      # Same struct as func_preproc's, deliberately: both come from the same
      # timed_stage() dict. On the `grayordinate` short path only boldref and
      # grayordinates are populated and the rest are NULL *within* the struct,
      # since that path skips the warp chain entirely.
      #
      # 4d_warp leads with a digit, so SQL must quote it:
      # stage_timings_s."4d_warp". Unquoted, Athena fails the WHOLE query with
      # MALFORMED_QUERY, not just that column.
      name = "stage_timings_s"
      type = "struct<boldref:double,composite_warp:double,4d_warp:double,mask_warp:double,masking:double,confounds:double,grayordinates:double>"
    }
    columns {
      name = "total_runtime_s"
      type = "double"
    }
    columns {
      name = "peak_memory_gb"
      type = "double"
    }
    columns {
      name = "container_peak_memory_gb"
      type = "double"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
    # surf_l_/surf_r_ are LOWERCASE here while the emitter writes surf_L_/surf_R_
    # — see the long note on the raw `surface_sample` table for why, and why
    # "fixing" the case makes the plan permanently dirty.
    columns {
      name = "surf_l_n_vertices"
      type = "int"
    }
    columns {
      name = "surf_l_coverage_frac"
      type = "double"
    }
    columns {
      name = "surf_l_nan_frac"
      type = "double"
    }
    columns {
      name = "surf_l_tsnr_median"
      type = "double"
    }
    columns {
      name = "surf_r_n_vertices"
      type = "int"
    }
    columns {
      name = "surf_r_coverage_frac"
      type = "double"
    }
    columns {
      name = "surf_r_nan_frac"
      type = "double"
    }
    columns {
      name = "surf_r_tsnr_median"
      type = "double"
    }
    columns {
      name = "subcort_n_voxels"
      type = "int"
    }
    columns {
      name = "subcort_n_structures"
      type = "int"
    }
    columns {
      name = "subcort_space"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "anat_qc_compacted" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "anat_qc_compacted"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.compacted_partition_projection.anat_qc, {
    "classification" = "parquet"
    # AnatQC emits 1.2 (snr_gm/snr_wm moved to FsqcQC at that bump). A version
    # missing from this enum is not a projected partition, so its rows return
    # EMPTY WITH NO ERROR — unlike an undeclared column, which reads as NULL.
    "projection.schema_version.values" = "1.0,1.1,1.2"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }
  partition_keys {
    name = "schema_version"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/compacted/anat_qc/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "session"
      type = "string"
    }
    columns {
      name = "efc"
      type = "double"
    }
    columns {
      name = "fber"
      type = "double"
    }
    columns {
      name = "cnr"
      type = "double"
    }
    columns {
      name = "cjv"
      type = "double"
    }
    columns {
      name = "wm2max"
      type = "double"
    }
    columns {
      name = "fwhm_x_mm"
      type = "double"
    }
    columns {
      name = "fwhm_y_mm"
      type = "double"
    }
    columns {
      name = "fwhm_z_mm"
      type = "double"
    }
    columns {
      name = "fwhm_avg_mm"
      type = "double"
    }
    columns {
      name = "etiv_mm3"
      type = "double"
    }
    columns {
      name = "total_brain_vol_mm3"
      type = "double"
    }
    columns {
      name = "lh_cortex_vol_mm3"
      type = "double"
    }
    columns {
      name = "rh_cortex_vol_mm3"
      type = "double"
    }
    columns {
      name = "wm_vol_mm3"
      type = "double"
    }
    columns {
      name = "subcort_gm_vol_mm3"
      type = "double"
    }
    columns {
      name = "lh_mean_thickness_mm"
      type = "double"
    }
    columns {
      name = "rh_mean_thickness_mm"
      type = "double"
    }
    columns {
      name = "lh_surface_area_mm2"
      type = "double"
    }
    columns {
      name = "rh_surface_area_mm2"
      type = "double"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
  }
}

# fsqc anatomical QC (Deep-MI/fsqc), subject×session grain — complementary to
# anat_qc above, not overlapping: WM/GM SNR lives ONLY here as of anat_qc schema
# 1.2, volumes and thickness live only there. Join on subject+session.
#
# Every metric column is nullable and several are null on every row today
# (holes_*, defects_*, topo_*, n_outlier_sample_*): FastSurfer writes no
# surf/[lr]h.orig.nofix, and sample-based outlier detection needs a reference
# cohort the driver does not pass. Declared regardless, so enabling either later
# needs no schema change. Query with IS NOT NULL, never `> 0` — 0 is a real
# measurement for rot_tal_* and the outlier counts.
#
# Verified 2026-08-08 against a scratch table: an all-null column in the
# compacted Parquet (pyarrow infers Arrow type `null`, stored int32(Null)) reads
# back as NULL against the `double` declared here rather than failing the scan.
resource "aws_glue_catalog_table" "fsqc_qc_compacted" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "fsqc_qc_compacted"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.compacted_partition_projection.fsqc_qc, {
    "classification"                   = "parquet"
    "projection.schema_version.values" = "1.0"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }
  partition_keys {
    name = "schema_version"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/compacted/fsqc_qc/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "session"
      type = "string"
    }
    columns {
      name = "wm_snr_orig"
      type = "double"
    }
    columns {
      name = "gm_snr_orig"
      type = "double"
    }
    columns {
      name = "wm_snr_norm"
      type = "double"
    }
    columns {
      name = "gm_snr_norm"
      type = "double"
    }
    columns {
      name = "cc_size"
      type = "double"
    }
    columns {
      name = "holes_lh"
      type = "double"
    }
    columns {
      name = "holes_rh"
      type = "double"
    }
    columns {
      name = "defects_lh"
      type = "double"
    }
    columns {
      name = "defects_rh"
      type = "double"
    }
    columns {
      name = "topo_lh"
      type = "double"
    }
    columns {
      name = "topo_rh"
      type = "double"
    }
    columns {
      name = "con_snr_lh"
      type = "double"
    }
    columns {
      name = "con_snr_rh"
      type = "double"
    }
    columns {
      name = "rot_tal_x"
      type = "double"
    }
    columns {
      name = "rot_tal_y"
      type = "double"
    }
    columns {
      name = "rot_tal_z"
      type = "double"
    }
    # SINGULAR n_outlier_*, matching what fsqc actually writes into
    # fsqc-results.csv. Its own docstring documents these as n_outliers_*; the
    # plural spelling would make all three columns NULL on every row.
    columns {
      name = "n_outlier_norms"
      type = "double"
    }
    columns {
      name = "n_outlier_sample_nonpar"
      type = "double"
    }
    columns {
      name = "n_outlier_sample_param"
      type = "double"
    }
    columns {
      name = "hypothalamus_whole_left_mm3"
      type = "double"
    }
    columns {
      name = "hypothalamus_whole_right_mm3"
      type = "double"
    }
    # Per-module exit codes from fsqc's status/{session}/status.txt. 0 = ran
    # clean, non-zero = degraded, NULL = never reported — so these must stay
    # nullable, and 0 must not be read as "missing".
    columns {
      name = "metrics_status"
      type = "int"
    }
    columns {
      name = "outlier_status"
      type = "int"
    }
    columns {
      name = "hippocampus_status"
      type = "int"
    }
    columns {
      name = "hypothalamus_status"
      type = "int"
    }
    columns {
      name = "fsqc_version"
      type = "string"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "workflow_runs_compacted" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "workflow_runs_compacted"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.compacted_partition_projection.workflow_runs, {
    "classification"                   = "parquet"
    "projection.schema_version.values" = "1.1"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }
  partition_keys {
    name = "schema_version"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/compacted/workflow_runs/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "workflow_name"
      type = "string"
    }
    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "status"
      type = "string"
    }
    columns {
      name = "started_at"
      type = "string"
    }
    columns {
      name = "finished_at"
      type = "string"
    }
    columns {
      name = "total_duration_s"
      type = "int"
    }
    columns {
      name = "pending_duration_s"
      type = "double"
    }
    columns {
      name = "message"
      type = "string"
    }
    columns {
      name = "failed_step"
      type = "string"
    }
    columns {
      name = "failure_category"
      type = "string"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "step_outcomes_compacted" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "step_outcomes_compacted"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.compacted_partition_projection.step_outcomes, {
    "classification"                   = "parquet"
    "projection.schema_version.values" = "1.0"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }
  partition_keys {
    name = "schema_version"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/compacted/step_outcomes/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "workflow_name"
      type = "string"
    }
    columns {
      name = "step"
      type = "string"
    }
    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "session"
      type = "string"
    }
    columns {
      name = "task"
      type = "string"
    }
    columns {
      name = "run"
      type = "string"
    }
    columns {
      name = "status"
      type = "string"
    }
    columns {
      name = "failure_category"
      type = "string"
    }
    columns {
      name = "failure_reason"
      type = "string"
    }
    columns {
      name = "upstream_failed_step"
      type = "string"
    }
    columns {
      name = "outputs_verified"
      type = "array<string>"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "recorded_at"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "subject_manifests_compacted" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "subject_manifests_compacted"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.compacted_partition_projection.subject_manifests, {
    "classification"                   = "parquet"
    "projection.schema_version.values" = "1.0"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }
  partition_keys {
    name = "schema_version"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/compacted/subject_manifests/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "workflow_name"
      type = "string"
    }
    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "overall_status"
      type = "string"
    }
    columns {
      name = "steps"
      type = "array<struct<step:string,session:string,task:string,run:string,status:string,failure_category:string,failure_reason:string>>"
    }
    columns {
      name = "outputs_available"
      type = "array<string>"
    }
    columns {
      name = "failed_steps"
      type = "array<string>"
    }
    columns {
      name = "skipped_steps"
      type = "array<string>"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "pod_costs_compacted" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "pod_costs_compacted"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.compacted_partition_projection.pod_costs, {
    "classification"                   = "parquet"
    "projection.schema_version.values" = "1.0"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }
  partition_keys {
    name = "schema_version"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/compacted/pod_costs/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "date"
      type = "string"
    }
    columns {
      name = "workflow_name"
      type = "string"
    }
    columns {
      name = "pod"
      type = "string"
    }
    columns {
      name = "step"
      type = "string"
    }
    columns {
      name = "phase"
      type = "string"
    }
    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "session"
      type = "string"
    }
    columns {
      name = "total_cost_usd"
      type = "double"
    }
    columns {
      name = "cpu_cost_usd"
      type = "double"
    }
    columns {
      name = "memory_cost_usd"
      type = "double"
    }
    columns {
      name = "gpu_cost_usd"
      type = "double"
    }
    columns {
      name = "pv_cost_usd"
      type = "double"
    }
    columns {
      name = "network_cost_usd"
      type = "double"
    }
    columns {
      name = "total_adjustment_usd"
      type = "double"
    }
    columns {
      name = "runtime_minutes"
      type = "double"
    }
    columns {
      name = "cpu_core_hours"
      type = "double"
    }
    columns {
      name = "ram_gb_hours"
      type = "double"
    }
    columns {
      name = "gpu_hours"
      type = "double"
    }
    columns {
      name = "cpu_efficiency"
      type = "double"
    }
    columns {
      name = "ram_efficiency"
      type = "double"
    }
    columns {
      name = "node"
      type = "string"
    }
    columns {
      name = "node_instance_type"
      type = "string"
    }
    columns {
      name = "scrape_age_days"
      type = "int"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "costs_compacted" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "costs_compacted"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.compacted_partition_projection.costs, {
    "classification" = "parquet"
    # Known drift: 1.0 records lack workflow_name (see CostAllocation
    # docstring in src/metrics/schemas.py). Athena returns NULL for
    # workflow_name on schema_version=1.0 partitions — the same behavior
    # duckdb_query.py already relies on for the raw costs table via
    # union_by_name=true.
    "projection.schema_version.values" = "1.0,1.1,1.2"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }
  partition_keys {
    name = "schema_version"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/compacted/costs/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "date"
      type = "string"
    }
    columns {
      name = "workflow_name"
      type = "string"
    }
    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "total_cost_usd"
      type = "double"
    }
    columns {
      name = "cpu_cost_usd"
      type = "double"
    }
    columns {
      name = "memory_cost_usd"
      type = "double"
    }
    columns {
      name = "gpu_cost_usd"
      type = "double"
    }
    columns {
      # Schema 1.2+ only — absent from 1.0/1.1 partitions, same as
      # workflow_name is absent from 1.0.
      name = "total_adjustment_usd"
      type = "double"
    }
    columns {
      name = "scrape_age_days"
      type = "int"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "registration_compacted" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "registration_compacted"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.compacted_partition_projection.registration, {
    "classification" = "parquet"
    # Every schema_version RegistrationQC has ever emitted (src/metrics/schemas.py).
    # Unlike dt, this list is NOT formula-projected — update it by hand whenever
    # a new schema_version starts being emitted, in lockstep with the column
    # list below (which must track RegistrationQC's docstring as source of
    # truth, since there is no crawler to auto-discover new columns here).
    # 2.4 was MISSING here until schema 2.5 was added — bold_to_t1w.py started
    # emitting 2.4 without this list (or the column list below, or athena.py's
    # _UNION_COLUMNS) being updated, so compacted 2.4 records were outside the
    # projected partition range and simply did not appear in query results.
    # Adding a schema_version is a four-place change; all four are listed in
    # RegistrationQC's docstring in src/metrics/schemas.py.
    "projection.schema_version.values" = "1.1,1.2,2.0,2.1,2.2,2.3,2.4,2.5,2.6"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }
  partition_keys {
    name = "schema_version"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/compacted/registration/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    # Superset union across schema_versions 1.1-2.3 (RegistrationQC in
    # src/metrics/schemas.py). Each individual Parquet file only physically
    # contains the fields its own schema_version actually wrote; Athena
    # returns NULL for the rest via name-based column matching.
    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "session"
      type = "string"
    }
    columns {
      name = "registration_type"
      type = "string"
    }
    columns {
      name = "method"
      type = "string"
    }
    columns {
      name = "dice" # dead since schema 2.0; pre-2.0 records only
      type = "double"
    }
    columns {
      name = "nmi" # bold_to_t1w, schema >=2.1
      type = "double"
    }
    columns {
      name = "mi" # superseded by nmi in 2.1; pre-2.1 records only
      type = "double"
    }
    columns {
      name = "rigid_disp_mean_mm" # bold_to_t1w, schema >=2.2
      type = "double"
    }
    columns {
      name = "rigid_disp_max_mm" # bold_to_t1w, schema >=2.2
      type = "double"
    }
    columns {
      name = "rigid_rot_deg" # bold_to_t1w, schema >=2.2
      type = "double"
    }
    columns {
      name = "mhd_mm" # bold_to_t1w, schema >=2.2
      type = "double"
    }
    columns {
      name = "nmi_identity" # bold_to_t1w, schema >=2.4
      type = "double"
    }
    columns {
      name = "nmi_gain" # bold_to_t1w, schema >=2.4 — THE GATED METRIC
      type = "double"
    }
    # Boundary-sensitive alignment, schema >=2.5, recorded-only. The seg_*
    # fields carry a -999.0 "could not compute" sentinel (0.0 is a legitimate
    # contrast), so filter them on `> -999`, not `> 0`.
    #
    # seg_bbr_contrast_gain, seg_ventricle_ratio and ngf_gain were emitted by
    # schema 2.5 only and are DROPPED in 2.6 — see RegistrationQC in
    # src/metrics/schemas.py for why. Both gains stay derivable in SQL from the
    # retained operands, including over historical 2.5 partitions:
    #   seg_bbr_contrast - seg_bbr_contrast_identity
    #   ngf - ngf_identity
    columns {
      name = "seg_bbr_contrast" # bold_to_t1w, schema >=2.5
      type = "double"
    }
    columns {
      name = "seg_bbr_contrast_identity" # bold_to_t1w, schema >=2.5
      type = "double"
    }
    columns {
      name = "ngf" # bold_to_t1w, schema >=2.5
      type = "double"
    }
    columns {
      name = "ngf_identity" # bold_to_t1w, schema >=2.5
      type = "double"
    }
    columns {
      name = "bbr_cost" # dead since schema 1.2; schema 1.2 records only
      type = "double"
    }
    columns {
      name = "bbr_converged" # dead since schema 1.2; schema 1.2 records only
      type = "boolean"
    }
    columns {
      name = "bbr_init_used" # dead since schema 1.2; schema 1.2 records only
      type = "string"
    }
    columns {
      name = "mask_dice" # t1w_to_mni, schema >=2.0
      type = "double"
    }
    columns {
      name = "lncc" # t1w_to_mni, schema >=2.0
      type = "double"
    }
    columns {
      name = "verdict" # both types, thresholds differ by type/version
      type = "string"
    }
    columns {
      name = "jac_det_min"
      type = "double"
    }
    columns {
      name = "jac_det_max"
      type = "double"
    }
    columns {
      name = "jac_det_mean"
      type = "double"
    }
    columns {
      name = "jac_det_std"
      type = "double"
    }
    columns {
      name = "jac_det_frac_negative"
      type = "double"
    }
    columns {
      name = "log_jac_mean" # t1w_to_mni, schema >=2.1
      type = "double"
    }
    columns {
      name = "log_jac_std"
      type = "double"
    }
    columns {
      name = "log_jac_p01"
      type = "double"
    }
    columns {
      name = "log_jac_p99"
      type = "double"
    }
    columns {
      name = "log_jac_min"
      type = "double"
    }
    columns {
      name = "log_jac_max"
      type = "double"
    }
    columns {
      name = "log_jac_frac_beyond_1p5"
      type = "double"
    }
    columns {
      name = "log_jac_frac_beyond_3"
      type = "double"
    }
    columns {
      name = "ice_mean_mm" # t1w_to_mni, schema >=2.1
      type = "double"
    }
    columns {
      name = "ice_p95_mm"
      type = "double"
    }
    columns {
      name = "ice_p99_mm"
      type = "double"
    }
    columns {
      name = "ice_max_mm"
      type = "double"
    }
    columns {
      name = "centroid_displacement_mm" # t1w_to_mni only
      type = "double"
    }
    columns {
      name = "task"
      type = "string"
    }
    columns {
      name = "run"
      type = "string"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
  }
}

# ---------------------------------------------------------------------------
# Explicit table definitions — ensures tables exist before crawlers run
# and before the first metric records are written to S3.
# ---------------------------------------------------------------------------

resource "aws_glue_catalog_table" "anat_qc" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "anat_qc"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.partition_projection.anat_qc, {
    "classification"        = "json"
    "compressionType"       = "none"
    "typeOfData"            = "file"
    "ignore.malformed.json" = "true"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/anat-qc/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        paths                   = "subject,session,efc,fber,cnr,cjv,wm2max,fwhm_x_mm,fwhm_y_mm,fwhm_z_mm,fwhm_avg_mm,etiv_mm3,total_brain_vol_mm3,lh_cortex_vol_mm3,rh_cortex_vol_mm3,wm_vol_mm3,subcort_gm_vol_mm3,lh_mean_thickness_mm,rh_mean_thickness_mm,lh_surface_area_mm2,rh_surface_area_mm2,pipeline,schema_version,completed_at"
        "ignore.malformed.json" = "true"
      }
    }

    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "session"
      type = "string"
    }
    columns {
      name = "efc"
      type = "double"
    }
    columns {
      name = "fber"
      type = "double"
    }
    columns {
      name = "cnr"
      type = "double"
    }
    columns {
      name = "cjv"
      type = "double"
    }
    columns {
      name = "wm2max"
      type = "double"
    }
    columns {
      name = "fwhm_x_mm"
      type = "double"
    }
    columns {
      name = "fwhm_y_mm"
      type = "double"
    }
    columns {
      name = "fwhm_z_mm"
      type = "double"
    }
    columns {
      name = "fwhm_avg_mm"
      type = "double"
    }
    columns {
      name = "etiv_mm3"
      type = "double"
    }
    columns {
      name = "total_brain_vol_mm3"
      type = "double"
    }
    columns {
      name = "lh_cortex_vol_mm3"
      type = "double"
    }
    columns {
      name = "rh_cortex_vol_mm3"
      type = "double"
    }
    columns {
      name = "wm_vol_mm3"
      type = "double"
    }
    columns {
      name = "subcort_gm_vol_mm3"
      type = "double"
    }
    columns {
      name = "lh_mean_thickness_mm"
      type = "double"
    }
    columns {
      name = "rh_mean_thickness_mm"
      type = "double"
    }
    columns {
      name = "lh_surface_area_mm2"
      type = "double"
    }
    columns {
      name = "rh_surface_area_mm2"
      type = "double"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "schema_version"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
  }
}

# Raw counterpart of fsqc_qc_compacted above — see that resource's comment for
# the nullability and n_outlier_* spelling notes, which apply identically here.
# Written by images/fsqc/stage_and_run.py, one JSON object per subject×session.
resource "aws_glue_catalog_table" "fsqc_qc" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "fsqc_qc"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.partition_projection.fsqc_qc, {
    "classification"        = "json"
    "compressionType"       = "none"
    "typeOfData"            = "file"
    "ignore.malformed.json" = "true"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/fsqc-qc/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        paths                   = "subject,session,wm_snr_orig,gm_snr_orig,wm_snr_norm,gm_snr_norm,cc_size,holes_lh,holes_rh,defects_lh,defects_rh,topo_lh,topo_rh,con_snr_lh,con_snr_rh,rot_tal_x,rot_tal_y,rot_tal_z,n_outlier_norms,n_outlier_sample_nonpar,n_outlier_sample_param,hypothalamus_whole_left_mm3,hypothalamus_whole_right_mm3,metrics_status,outlier_status,hippocampus_status,hypothalamus_status,fsqc_version,pipeline,schema_version,completed_at"
        "ignore.malformed.json" = "true"
      }
    }

    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "session"
      type = "string"
    }
    columns {
      name = "wm_snr_orig"
      type = "double"
    }
    columns {
      name = "gm_snr_orig"
      type = "double"
    }
    columns {
      name = "wm_snr_norm"
      type = "double"
    }
    columns {
      name = "gm_snr_norm"
      type = "double"
    }
    columns {
      name = "cc_size"
      type = "double"
    }
    columns {
      name = "holes_lh"
      type = "double"
    }
    columns {
      name = "holes_rh"
      type = "double"
    }
    columns {
      name = "defects_lh"
      type = "double"
    }
    columns {
      name = "defects_rh"
      type = "double"
    }
    columns {
      name = "topo_lh"
      type = "double"
    }
    columns {
      name = "topo_rh"
      type = "double"
    }
    columns {
      name = "con_snr_lh"
      type = "double"
    }
    columns {
      name = "con_snr_rh"
      type = "double"
    }
    columns {
      name = "rot_tal_x"
      type = "double"
    }
    columns {
      name = "rot_tal_y"
      type = "double"
    }
    columns {
      name = "rot_tal_z"
      type = "double"
    }
    columns {
      name = "n_outlier_norms"
      type = "double"
    }
    columns {
      name = "n_outlier_sample_nonpar"
      type = "double"
    }
    columns {
      name = "n_outlier_sample_param"
      type = "double"
    }
    columns {
      name = "hypothalamus_whole_left_mm3"
      type = "double"
    }
    columns {
      name = "hypothalamus_whole_right_mm3"
      type = "double"
    }
    columns {
      name = "metrics_status"
      type = "int"
    }
    columns {
      name = "outlier_status"
      type = "int"
    }
    columns {
      name = "hippocampus_status"
      type = "int"
    }
    columns {
      name = "hypothalamus_status"
      type = "int"
    }
    columns {
      name = "fsqc_version"
      type = "string"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "schema_version"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "costs" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "costs"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.partition_projection.costs, {
    "classification"        = "json"
    "compressionType"       = "none"
    "typeOfData"            = "file"
    "ignore.malformed.json" = "true"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/costs/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        paths                   = "completed_at,cpu_cost_usd,date,gpu_cost_usd,memory_cost_usd,pipeline,scrape_age_days,schema_version,subject,total_adjustment_usd,total_cost_usd,workflow_name"
        "ignore.malformed.json" = "true"
      }
    }

    columns {
      name = "date"
      type = "string"
    }
    columns {
      name = "workflow_name"
      type = "string"
    }
    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "total_cost_usd"
      type = "double"
    }
    columns {
      name = "cpu_cost_usd"
      type = "double"
    }
    columns {
      name = "memory_cost_usd"
      type = "double"
    }
    columns {
      name = "gpu_cost_usd"
      type = "double"
    }
    columns {
      # Schema 1.2+ only — NULL on older records, same as any other
      # ignore.malformed.json-tolerated missing key.
      name = "total_adjustment_usd"
      type = "double"
    }
    columns {
      # Schema 1.2+ only — days between `date` (report date) and when this
      # record was actually scraped. See CostAllocation in schemas.py.
      name = "scrape_age_days"
      type = "int"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "schema_version"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "pod_costs" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "pod_costs"

  table_type = "EXTERNAL_TABLE"

  # One grain below `costs`: one row per pod per report date, carrying the
  # per-template `cloudpipe.io/step` / `cloudpipe.io/phase` pod labels so cost
  # rolls up by pipeline component. Objects here are newline-delimited JSON
  # (all of one workflow's pods in one key) rather than one object per key —
  # TextInputFormat + JsonSerDe already reads one object per line, so that
  # needs no special declaration, but it is why the object count stays flat as
  # pod count grows.
  parameters = merge(local.partition_projection.pod_costs, {
    "classification"        = "json"
    "compressionType"       = "none"
    "typeOfData"            = "file"
    "ignore.malformed.json" = "true"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/pod-costs/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        paths                   = "completed_at,cpu_core_hours,cpu_cost_usd,cpu_efficiency,date,gpu_cost_usd,gpu_hours,memory_cost_usd,network_cost_usd,node,node_instance_type,phase,pipeline,pod,pv_cost_usd,ram_efficiency,ram_gb_hours,runtime_minutes,schema_version,scrape_age_days,session,step,subject,total_adjustment_usd,total_cost_usd,workflow_name"
        "ignore.malformed.json" = "true"
      }
    }

    columns {
      name = "date"
      type = "string"
    }
    columns {
      name = "workflow_name"
      type = "string"
    }
    columns {
      name = "pod"
      type = "string"
    }
    columns {
      # From the per-template `cloudpipe.io/step` pod label. Empty string (not
      # NULL) when a template sets no such label — grouping on it still
      # reconciles to the workflow total.
      name = "step"
      type = "string"
    }
    columns {
      name = "phase"
      type = "string"
    }
    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "session"
      type = "string"
    }
    columns {
      name = "total_cost_usd"
      type = "double"
    }
    columns {
      name = "cpu_cost_usd"
      type = "double"
    }
    columns {
      name = "memory_cost_usd"
      type = "double"
    }
    columns {
      name = "gpu_cost_usd"
      type = "double"
    }
    columns {
      name = "pv_cost_usd"
      type = "double"
    }
    columns {
      name = "network_cost_usd"
      type = "double"
    }
    columns {
      name = "total_adjustment_usd"
      type = "double"
    }
    columns {
      name = "runtime_minutes"
      type = "double"
    }
    columns {
      name = "cpu_core_hours"
      type = "double"
    }
    columns {
      name = "ram_gb_hours"
      type = "double"
    }
    columns {
      name = "gpu_hours"
      type = "double"
    }
    columns {
      # Kubecost usage/request ratio in [0, 1]. Low values on an expensive
      # step are the right-sizing signal.
      name = "cpu_efficiency"
      type = "double"
    }
    columns {
      name = "ram_efficiency"
      type = "double"
    }
    columns {
      name = "node"
      type = "string"
    }
    columns {
      name = "node_instance_type"
      type = "string"
    }
    columns {
      name = "scrape_age_days"
      type = "int"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "schema_version"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "step_outcomes" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "step_outcomes"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.partition_projection.step_outcomes, {
    "classification"        = "json"
    "compressionType"       = "none"
    "typeOfData"            = "file"
    "ignore.malformed.json" = "true"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/step-outcomes/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        paths                   = "failure_category,failure_reason,outputs_verified,pipeline,recorded_at,run,schema_version,session,status,step,subject,task,upstream_failed_step,workflow_name"
        "ignore.malformed.json" = "true"
      }
    }

    columns {
      name = "workflow_name"
      type = "string"
    }
    columns {
      name = "step"
      type = "string"
    }
    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "session"
      type = "string"
    }
    columns {
      name = "task"
      type = "string"
    }
    columns {
      name = "run"
      type = "string"
    }
    columns {
      name = "status"
      type = "string"
    }
    columns {
      name = "failure_category"
      type = "string"
    }
    columns {
      name = "failure_reason"
      type = "string"
    }
    columns {
      name = "upstream_failed_step"
      type = "string"
    }
    columns {
      name = "outputs_verified"
      type = "array<string>"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "schema_version"
      type = "string"
    }
    columns {
      name = "recorded_at"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "subject_manifests" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "subject_manifests"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.partition_projection.subject_manifests, {
    "classification"        = "json"
    "compressionType"       = "none"
    "typeOfData"            = "file"
    "ignore.malformed.json" = "true"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/subject-manifests/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        paths                   = "completed_at,failed_steps,overall_status,outputs_available,pipeline,schema_version,skipped_steps,steps,subject,workflow_name"
        "ignore.malformed.json" = "true"
      }
    }

    columns {
      name = "workflow_name"
      type = "string"
    }
    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "overall_status"
      type = "string"
    }
    columns {
      name = "steps"
      type = "array<struct<step:string,session:string,task:string,run:string,status:string,failure_category:string,failure_reason:string>>"
    }
    columns {
      name = "outputs_available"
      type = "array<string>"
    }
    columns {
      name = "failed_steps"
      type = "array<string>"
    }
    columns {
      name = "skipped_steps"
      type = "array<string>"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "schema_version"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "registration_qc" {
  database_name = aws_glue_catalog_database.metrics.name

  # "registration", not "registration_qc": the crawler derives the table name
  # from the prefix (metrics/registration/), so any other name here produces a
  # second, crawler-owned table at the same location. That is exactly how the
  # stale `registration_qc`/`registration` pair arose. Every consumer — the
  # Grafana dashboards and athena.py — already queries `registration`.
  name = "registration"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.partition_projection.registration_qc, {
    "classification"        = "json"
    "compressionType"       = "none"
    "typeOfData"            = "file"
    "ignore.malformed.json" = "true"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/registration/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        paths                   = "centroid_displacement_mm,completed_at,ice_max_mm,ice_mean_mm,ice_p95_mm,ice_p99_mm,jac_det_frac_negative,jac_det_max,jac_det_mean,jac_det_min,jac_det_std,lncc,log_jac_frac_beyond_1p5,log_jac_frac_beyond_3,log_jac_max,log_jac_mean,log_jac_min,log_jac_p01,log_jac_p99,log_jac_std,mask_dice,method,mhd_mm,mi,ngf,ngf_identity,nmi,nmi_gain,nmi_identity,pipeline,registration_type,rigid_disp_max_mm,rigid_disp_mean_mm,rigid_rot_deg,run,schema_version,seg_bbr_contrast,seg_bbr_contrast_identity,session,subject,task,verdict"
        "ignore.malformed.json" = "true"
      }
    }

    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "session"
      type = "string"
    }
    columns {
      name = "registration_type"
      type = "string"
    }
    columns {
      name = "method"
      type = "string"
    }
    # t1w_to_mni (schema 2.0) quality metrics. `mask_dice` and `lncc` replaced the
    # former `dice`/`ncc` columns, which the emitters no longer write. `verdict` is
    # the composite pass/warn/fail gate from images/shared/registration_qc.py.
    columns {
      name = "mask_dice"
      type = "double"
    }
    columns {
      name = "lncc"
      type = "double"
    }
    columns {
      name = "verdict"
      type = "string"
    }
    # bold_to_t1w quality metrics. This table's column list must track the
    # RegistrationQC docstring in src/metrics/schemas.py the same way
    # registration_compacted's does — there is no crawler to auto-discover new
    # fields, and a field absent here is invisible to Athena even though it is
    # physically present in every JSON record under this prefix. Everything from
    # `nmi` down was emitted from schema 2.1/2.2/2.4/2.5 onward but never added
    # here, so every bold_to_t1w row after 2026-07-23 read as all-NULL to the
    # Grafana dashboards, which were still querying the dead `mi`.
    columns {
      name = "mi" # superseded by nmi in 2.1; pre-2.1 records only
      type = "double"
    }
    columns {
      name = "nmi" # bold_to_t1w, schema >=2.1
      type = "double"
    }
    columns {
      name = "nmi_identity" # bold_to_t1w, schema >=2.4
      type = "double"
    }
    columns {
      name = "nmi_gain" # bold_to_t1w, schema >=2.4 — THE GATED METRIC
      type = "double"
    }
    columns {
      name = "rigid_disp_mean_mm" # bold_to_t1w, schema >=2.2
      type = "double"
    }
    columns {
      name = "rigid_disp_max_mm" # bold_to_t1w, schema >=2.2
      type = "double"
    }
    columns {
      name = "rigid_rot_deg" # bold_to_t1w, schema >=2.2
      type = "double"
    }
    columns {
      name = "mhd_mm" # bold_to_t1w, schema >=2.2
      type = "double"
    }
    # Boundary-sensitive alignment, schema >=2.5, recorded-only. The seg_* fields
    # carry a -999.0 "could not compute" sentinel (0.0 is a legitimate contrast),
    # so filter them on `> -999`, not `> 0`.
    columns {
      name = "seg_bbr_contrast" # bold_to_t1w, schema >=2.5
      type = "double"
    }
    columns {
      name = "seg_bbr_contrast_identity" # bold_to_t1w, schema >=2.5
      type = "double"
    }
    columns {
      name = "ngf" # bold_to_t1w, schema >=2.5
      type = "double"
    }
    columns {
      name = "ngf_identity" # bold_to_t1w, schema >=2.5
      type = "double"
    }
    columns {
      name = "jac_det_min"
      type = "double"
    }
    columns {
      name = "jac_det_max"
      type = "double"
    }
    columns {
      name = "jac_det_mean"
      type = "double"
    }
    columns {
      name = "jac_det_std"
      type = "double"
    }
    columns {
      name = "jac_det_frac_negative"
      type = "double"
    }
    columns {
      name = "log_jac_mean" # t1w_to_mni, schema >=2.1
      type = "double"
    }
    columns {
      name = "log_jac_std"
      type = "double"
    }
    columns {
      name = "log_jac_p01"
      type = "double"
    }
    columns {
      name = "log_jac_p99"
      type = "double"
    }
    columns {
      name = "log_jac_min"
      type = "double"
    }
    columns {
      name = "log_jac_max"
      type = "double"
    }
    columns {
      name = "log_jac_frac_beyond_1p5"
      type = "double"
    }
    columns {
      name = "log_jac_frac_beyond_3"
      type = "double"
    }
    columns {
      name = "ice_mean_mm" # t1w_to_mni, schema >=2.1
      type = "double"
    }
    columns {
      name = "ice_p95_mm"
      type = "double"
    }
    columns {
      name = "ice_p99_mm"
      type = "double"
    }
    columns {
      name = "ice_max_mm"
      type = "double"
    }
    columns {
      name = "centroid_displacement_mm"
      type = "double"
    }
    columns {
      name = "task"
      type = "string"
    }
    columns {
      name = "run"
      type = "string"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "schema_version"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "func_preproc" {
  # Previously crawler-only (no explicit Terraform resource) — this is one of
  # the two tables the doc/reality mismatch referred to (7 crawlers, only 5
  # explicit tables). Needs an explicit resource now because partition
  # projection is a table property the crawler itself never sets.
  database_name = aws_glue_catalog_database.metrics.name
  name          = "func_preproc"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.partition_projection.func_qc, {
    "classification"        = "json"
    "compressionType"       = "none"
    "typeOfData"            = "file"
    "ignore.malformed.json" = "true"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/func-preproc/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        paths                   = "subject,session,task,run,n_frames,n_nss_frames,tr_seconds,mean_fd,median_fd,max_fd,n_fd_above_0p2,n_fd_above_0p5,pct_fd_above_0p5,mean_dvars,dvars_std,mean_global_signal,tsnr_median,gcor,aor,aqi,n_acompcor_wm,n_acompcor_csf,n_tcompcor,n_cosines,stage_timings_s,total_runtime_s,peak_memory_gb,container_peak_memory_gb,pipeline,image_tag,schema_version,completed_at,surf_L_n_vertices,surf_L_coverage_frac,surf_L_nan_frac,surf_L_tsnr_median,surf_R_n_vertices,surf_R_coverage_frac,surf_R_nan_frac,surf_R_tsnr_median,subcort_n_voxels,subcort_n_structures,subcort_space"
        "ignore.malformed.json" = "true"
      }
    }

    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "session"
      type = "string"
    }
    columns {
      name = "task"
      type = "string"
    }
    columns {
      name = "run"
      type = "string"
    }
    columns {
      name = "n_frames"
      type = "int"
    }
    columns {
      name = "n_nss_frames"
      type = "int"
    }
    columns {
      name = "tr_seconds"
      type = "double"
    }
    columns {
      name = "mean_fd"
      type = "double"
    }
    columns {
      name = "median_fd"
      type = "double"
    }
    columns {
      name = "max_fd"
      type = "double"
    }
    columns {
      name = "n_fd_above_0p2"
      type = "int"
    }
    columns {
      name = "n_fd_above_0p5"
      type = "int"
    }
    columns {
      name = "pct_fd_above_0p5"
      type = "double"
    }
    columns {
      name = "mean_dvars"
      type = "double"
    }
    columns {
      name = "dvars_std"
      type = "double"
    }
    columns {
      name = "mean_global_signal"
      type = "double"
    }
    columns {
      name = "tsnr_median"
      type = "double"
    }
    columns {
      name = "gcor"
      type = "double"
    }
    columns {
      name = "aor"
      type = "double"
    }
    columns {
      name = "aqi"
      type = "double"
    }
    columns {
      name = "n_acompcor_wm"
      type = "int"
    }
    columns {
      name = "n_acompcor_csf"
      type = "int"
    }
    columns {
      name = "n_tcompcor"
      type = "int"
    }
    columns {
      name = "n_cosines"
      type = "int"
    }
    # Fixed set of stage names emitted by preproc.py — a dict[str, float] in
    # Python, but the JSON SerDe needs a concrete struct shape.
    columns {
      # Every key preproc.py's timed_stage() emits must be listed here or Athena
      # silently drops it -- 4d_warp (the second-largest stage, ~30 s/run) and
      # grayordinates were missing from 2026-07 until 2026-08-03 and never
      # reached a query or a dashboard.
      #
      # 4d_warp leads with a digit, so SQL must quote it: stage_timings_s."4d_warp".
      # Unquoted, Athena fails the WHOLE query with MALFORMED_QUERY ("mismatched
      # input '.4'"), not just that column -- see the Grafana stage-timing panel.
      name = "stage_timings_s"
      type = "struct<boldref:double,composite_warp:double,4d_warp:double,mask_warp:double,masking:double,confounds:double,grayordinates:double>"
    }
    columns {
      name = "total_runtime_s"
      type = "double"
    }
    columns {
      name = "peak_memory_gb"
      type = "double"
    }
    columns {
      name = "container_peak_memory_gb"
      type = "double"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "image_tag"
      type = "string"
    }
    columns {
      name = "schema_version"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
    # Grayordinate QC. Positioned after completed_at because that is the
    # emitter's own key order: preproc.py computes these separately and folds
    # them in with `qc.update(surf_metrics)` after the volumetric record is
    # already built.
    #
    # Undeclared here from the surface-func rollout until 2026-08-11, so all
    # eleven read as absent through Athena while DuckDB (which infers columns
    # from the JSON itself) returned them — the two engines of
    # export_batch_metrics.py disagreed on eleven columns with no error on
    # either side. Populated on 2,996 of 3,012 runs in the 2026-08-10 batch;
    # NULL on any run that produced no surfaces.
    #
    # `surf_l_`/`surf_r_` are LOWERCASE here even though the emitter writes
    # `surf_L_`/`surf_R_`, and that mismatch is deliberate. Glue stores every
    # column name lowercased, but the AWS provider compares the stored name
    # against this config case-SENSITIVELY, so declaring `surf_L_n_vertices`
    # produces a diff that can never converge: the first apply reported success
    # and the next plan still wanted all eight renamed, on every run forever. A
    # perpetually dirty plan is worse than cosmetic here — a clean plan is how
    # you tell whether a change actually landed, and this module already carries
    # unrelated drift.
    #
    # Nothing downstream needs the mixed case at this layer. Athena identifiers
    # are case-insensitive, the SerDe `paths` parameter below keeps the emitter's
    # mixed case (it is matched against the raw JSON keys, not treated as an
    # identifier), and athena.py::_restore_column_case renames the lowercased
    # labels back to the documented mixed-case names on read.
    columns {
      name = "surf_l_n_vertices"
      type = "int"
    }
    columns {
      name = "surf_l_coverage_frac"
      type = "double"
    }
    columns {
      name = "surf_l_nan_frac"
      type = "double"
    }
    columns {
      name = "surf_l_tsnr_median"
      type = "double"
    }
    columns {
      name = "surf_r_n_vertices"
      type = "int"
    }
    columns {
      name = "surf_r_coverage_frac"
      type = "double"
    }
    columns {
      name = "surf_r_nan_frac"
      type = "double"
    }
    columns {
      name = "surf_r_tsnr_median"
      type = "double"
    }
    columns {
      name = "subcort_n_voxels"
      type = "int"
    }
    columns {
      name = "subcort_n_structures"
      type = "int"
    }
    columns {
      name = "subcort_space"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "surface_sample" {
  # Added 2026-08-11 (#241). The prefix existed and was actively written from
  # the surface-func rollout onward, but its artifact key had no dt= component,
  # so partition projection — the only thing publishing partitions since the
  # crawlers were removed — had nothing to project and Athena could not see the
  # prefix at all. The writer now templates dt=; this table makes it readable.
  #
  # Not redundant with func_preproc's surf_*/subcort_* block, despite the
  # identical field names: preproc.py's `grayordinate` short path writes ONLY
  # here (it leaves FuncQC alone rather than overwriting a real volumetric
  # record with a partial one), so for those runs this table is the sole copy.
  database_name = aws_glue_catalog_database.metrics.name
  name          = "surface_sample"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.partition_projection.surface_sample, {
    "classification"        = "json"
    "compressionType"       = "none"
    "typeOfData"            = "file"
    "ignore.malformed.json" = "true"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/surface-sample/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        # Mixed case is CORRECT here and must not be lowercased to match the
        # `columns` blocks below: this parameter is matched against the raw JSON
        # keys, not treated as a Hive identifier. A name in `columns` but absent
        # from `paths` reads NULL.
        paths                   = "subject,session,task,run,pipeline,image_tag,emit,stage_timings_s,total_runtime_s,peak_memory_gb,container_peak_memory_gb,schema_version,completed_at,surf_L_n_vertices,surf_L_coverage_frac,surf_L_nan_frac,surf_L_tsnr_median,surf_R_n_vertices,surf_R_coverage_frac,surf_R_nan_frac,surf_R_tsnr_median,subcort_n_voxels,subcort_n_structures,subcort_space"
        "ignore.malformed.json" = "true"
      }
    }

    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "session"
      type = "string"
    }
    columns {
      name = "task"
      type = "string"
    }
    columns {
      name = "run"
      type = "string"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "image_tag"
      type = "string"
    }
    columns {
      # Which preproc.py path wrote this record: "both" (full path — the same
      # values are also in that run's func_preproc row) or "grayordinate" (short
      # path — this row is the only copy). The column to read before attempting
      # a join to func_preproc.
      name = "emit"
      type = "string"
    }
    columns {
      # See the compacted table's note: same struct as func_preproc's because
      # both come from the same timed_stage() dict, and on the short path only
      # boldref/grayordinates are populated.
      name = "stage_timings_s"
      type = "struct<boldref:double,composite_warp:double,4d_warp:double,mask_warp:double,masking:double,confounds:double,grayordinates:double>"
    }
    columns {
      name = "total_runtime_s"
      type = "double"
    }
    columns {
      name = "peak_memory_gb"
      type = "double"
    }
    columns {
      name = "container_peak_memory_gb"
      type = "double"
    }
    columns {
      # A data column on this raw table and a PARTITION KEY on
      # surface_sample_compacted — which is why the compacted table's column
      # list is this one minus this field, and why _UNION_COLUMNS omits it.
      name = "schema_version"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
    # `surf_l_`/`surf_r_` are LOWERCASE here even though the emitter writes
    # `surf_L_`/`surf_R_`, and that mismatch is deliberate — the same reasoning
    # as on func_preproc. Glue stores every column name lowercased, but the AWS
    # provider compares the stored name against this config case-SENSITIVELY, so
    # declaring `surf_L_n_vertices` produces a diff that can never converge: the
    # apply reports success and the next plan still wants all eight renamed,
    # forever. Athena identifiers are case-insensitive, the SerDe `paths`
    # parameter above keeps the emitter's mixed case, and
    # athena.py::_restore_column_case renames the lowercased labels back to the
    # documented names on read.
    columns {
      name = "surf_l_n_vertices"
      type = "int"
    }
    columns {
      name = "surf_l_coverage_frac"
      type = "double"
    }
    columns {
      name = "surf_l_nan_frac"
      type = "double"
    }
    columns {
      name = "surf_l_tsnr_median"
      type = "double"
    }
    columns {
      name = "surf_r_n_vertices"
      type = "int"
    }
    columns {
      name = "surf_r_coverage_frac"
      type = "double"
    }
    columns {
      name = "surf_r_nan_frac"
      type = "double"
    }
    columns {
      name = "surf_r_tsnr_median"
      type = "double"
    }
    # Absent, not zero, when the run sampled no subcortex.
    columns {
      name = "subcort_n_voxels"
      type = "int"
    }
    columns {
      name = "subcort_n_structures"
      type = "int"
    }
    columns {
      name = "subcort_space"
      type = "string"
    }
  }
}

resource "aws_glue_catalog_table" "workflow_runs" {
  # Previously crawler-only — see the func_preproc comment above; same reason.
  database_name = aws_glue_catalog_database.metrics.name
  name          = "workflow_runs"

  table_type = "EXTERNAL_TABLE"

  parameters = merge(local.partition_projection.workflow_runs, {
    "classification"        = "json"
    "compressionType"       = "none"
    "typeOfData"            = "file"
    "ignore.malformed.json" = "true"
  })

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/workflow-runs/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        paths                   = "completed_at,failed_step,failure_category,finished_at,message,pending_duration_s,pipeline,schema_version,started_at,status,subject,total_duration_s,workflow_name"
        "ignore.malformed.json" = "true"
      }
    }

    columns {
      name = "workflow_name"
      type = "string"
    }
    columns {
      name = "subject"
      type = "string"
    }
    columns {
      name = "status"
      type = "string"
    }
    columns {
      name = "started_at"
      type = "string"
    }
    columns {
      name = "finished_at"
      type = "string"
    }
    columns {
      name = "total_duration_s"
      type = "int"
    }
    columns {
      name = "pending_duration_s"
      type = "double"
    }
    columns {
      name = "message"
      type = "string"
    }
    columns {
      name = "failed_step"
      type = "string"
    }
    columns {
      name = "failure_category"
      type = "string"
    }
    columns {
      name = "pipeline"
      type = "string"
    }
    columns {
      name = "schema_version"
      type = "string"
    }
    columns {
      name = "completed_at"
      type = "string"
    }
  }
}

# The nightly Glue crawlers were REMOVED on 2026-07-30. They are not coming
# back; do not re-add them without reading this first.
#
# What they cost: 5,227 junk tables in cloudpipe_metrics (5,243 total, of which
# only 16 were real). A crawler groups files into a table by shared folder, so
# every object sitting directly at a prefix root -- rather than inside a dt=
# partition -- became its own table, named after the file. The counts matched
# one-to-one per prefix (step-outcomes: 4,247 loose objects -> 4,248 tables).
# Those loose objects predated the dt= partitioning change; they have since
# been relocated into dt= partitions and the junk tables deleted.
#
# Why removing them is safe rather than merely convenient: every table in this
# module is declared explicitly above, with partition projection. Projection is
# what makes a newly-written dt= partition queryable -- it computes partitions
# from a formula, so nothing needs to discover them. The crawlers' only
# remaining job was schema evolution via CombineCompatibleSchemas, and since
# the `columns` blocks here are hand-maintained anyway, that is now a Terraform
# edit: add the column to the table (and to _UNION_COLUMNS in
# src/metrics/athena.py) in the same change that starts emitting it.
#
# The failure mode is also latent rather than gone -- anything that writes an
# unpartitioned object to a prefix root would recreate it. With no crawler
# there is simply nothing to turn that mistake into 5,000 tables; the object
# just sits there, invisible to dt-scoped queries. See the note in
# src/metrics/duckdb_query.py::_s3_glob.
#
# The IAM role in iam.tf is retained deliberately: it is what a manual,
# one-off `aws glue start-crawler` would assume if a future schema
# investigation ever wants a throwaway crawl against a scratch database.
