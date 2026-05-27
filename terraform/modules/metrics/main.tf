################################################################################
# CloudPipe Metrics — Glue catalog + crawlers
#
# Five crawlers keep the Glue catalog in sync with JSON files written to S3
# by pipeline steps.  All share a single IAM role (cloudpipe-metrics-crawler).
# Crawlers run nightly at 01:00 UTC; use the CombineCompatibleSchemas policy
# so new fields don't create duplicate tables.
################################################################################

# ---------------------------------------------------------------------------
# Glue catalog database
# ---------------------------------------------------------------------------

resource "aws_glue_catalog_database" "metrics" {
  name = "cloudpipe_metrics"
}

# ---------------------------------------------------------------------------
# Crawlers — one per S3 prefix / metric type
# ---------------------------------------------------------------------------

locals {
  crawler_targets = {
    func_qc         = "s3://${var.bucket}/metrics/func-preproc/"
    anat_qc         = "s3://${var.bucket}/metrics/anat-qc/"
    registration_qc = "s3://${var.bucket}/metrics/registration/"
    workflow_runs   = "s3://${var.bucket}/metrics/workflow-runs/"
    costs           = "s3://${var.bucket}/metrics/costs/"
  }

  crawler_configuration = jsonencode({
    Grouping = {
      TableGroupingPolicy = "CombineCompatibleSchemas"
    }
    CrawlerOutput = {
      Partitions = { AddOrUpdateBehavior = "InheritFromTable" }
    }
    Version = 1
  })
}

# ---------------------------------------------------------------------------
# Explicit table definitions — ensures tables exist before crawlers run
# and before the first metric records are written to S3.
# ---------------------------------------------------------------------------

resource "aws_glue_catalog_table" "anat_qc" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "anat_qc"

  table_type = "EXTERNAL_TABLE"

  parameters = {
    "classification"        = "json"
    "compressionType"       = "none"
    "typeOfData"            = "file"
    "ignore.malformed.json" = "true"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/anat-qc/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        paths                   = "completed_at,etiv_mm3,lh_cortex_vol_mm3,lh_mean_thickness_mm,lh_surface_area_mm2,pipeline,rh_cortex_vol_mm3,rh_mean_thickness_mm,rh_surface_area_mm2,schema_version,session,subcort_gm_vol_mm3,subject,total_brain_vol_mm3,wm_vol_mm3"
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

resource "aws_glue_catalog_table" "costs" {
  database_name = aws_glue_catalog_database.metrics.name
  name          = "costs"

  table_type = "EXTERNAL_TABLE"

  parameters = {
    "classification"       = "json"
    "compressionType"      = "none"
    "typeOfData"           = "file"
    "ignore.malformed.json" = "true"
  }

  storage_descriptor {
    location      = "s3://${var.bucket}/metrics/costs/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        paths                = "completed_at,cpu_cost_usd,date,gpu_cost_usd,memory_cost_usd,pipeline,schema_version,subject,total_cost_usd"
        "ignore.malformed.json" = "true"
      }
    }

    columns {
      name = "date"
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

resource "aws_glue_crawler" "metrics" {
  for_each = local.crawler_targets

  name          = "cloudpipe-metrics-${replace(each.key, "_", "-")}"
  database_name = aws_glue_catalog_database.metrics.name
  role          = aws_iam_role.metrics_crawler.arn
  schedule      = "cron(0 1 * * ? *)"
  configuration = local.crawler_configuration

  s3_target {
    path = each.value
  }

  tags = var.tags
}
