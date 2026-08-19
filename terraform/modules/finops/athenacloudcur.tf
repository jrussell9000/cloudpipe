# ------------------------------------------------------------------------------
# Single finops bucket — CUR reports (athena/ prefix), Athena query results
# (query-results/ prefix), and Kubecost federated store share this bucket.
# ------------------------------------------------------------------------------
resource "aws_s3_bucket" "finops" {
  bucket        = local.finops_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "finops" {
  bucket = aws_s3_bucket.finops.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Public Access Block, not just the private ACL below. The two are different
# controls: the ACL governs the grants that exist now, while this makes a future
# public grant impossible to create at all — including one added by hand in the
# console, or via a bucket policy, neither of which the ACL constrains. This
# bucket holds Cost and Usage Report data (account IDs and spend) under athena/,
# Athena query results under query-results/, and the Kubecost federated store,
# so "nothing grants public access today" is not a control.
resource "aws_s3_bucket_public_access_block" "finops" {
  bucket                  = aws_s3_bucket.finops.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "finops" {
  bucket = aws_s3_bucket.finops.id
  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_acl" "finops" {
  depends_on = [aws_s3_bucket_ownership_controls.finops]
  bucket     = aws_s3_bucket.finops.id
  acl        = "private"
}

# ------------------------------------------------------------------------------
# Athena and Glue
# ------------------------------------------------------------------------------
resource "aws_athena_database" "athena_cur_database" {
  name          = "athena_cur_database"
  bucket        = aws_s3_bucket.finops.id
  force_destroy = true
}

resource "aws_athena_workgroup" "cur_athena_workgroup" {
  name = var.athena_workgroup
  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    result_configuration {
      output_location = "s3://${aws_s3_bucket.finops.id}/query-results/"
    }
  }
}

resource "aws_glue_crawler" "cur_report_crawler" {
  database_name = aws_athena_database.athena_cur_database.name
  schedule      = "cron(0 0/12 * * ? *)"
  name          = "cur_report_crawler"
  # Reference, not the literal "crawler-service-role". The role is declared in
  # this same module (iam.tf, aws_iam_role.crawler-service-role), so a string
  # literal builds no dependency edge — on a cold apply the crawler can be
  # created before the role exists, and a rename would leave this pointing at a
  # name that no longer does, with no plan diff to say so.
  role = aws_iam_role.crawler-service-role.name
  configuration = jsonencode(
    {
      Grouping = {
        TableGroupingPolicy = "CombineCompatibleSchemas"
      }
      CrawlerOutput = {
        Partitions = { AddOrUpdateBehavior = "InheritFromTable" }
      }
      Version = 1
    }
  )
  s3_target {
    path = "s3://${aws_s3_bucket.finops.id}/athena/${var.root_name}-cur-report/${var.root_name}-cur-report"
  }
}

# CUR Report Definition — delivers to the athena/ prefix of the finops bucket.
# Must use the us-east-1 (billing) provider; delivery target is <YOUR_AWS_REGION>.
resource "aws_cur_report_definition" "cur_report" {
  provider = aws.billing

  report_name                = "${var.root_name}-cur-report"
  time_unit                  = var.time_unit
  format                     = "Parquet"
  compression                = "Parquet"
  additional_schema_elements = ["RESOURCES", "SPLIT_COST_ALLOCATION_DATA"]

  s3_bucket = aws_s3_bucket.finops.id
  s3_region = var.region
  s3_prefix = "athena"

  additional_artifacts = ["ATHENA"]
  report_versioning    = "OVERWRITE_REPORT"

  depends_on = [
    aws_s3_bucket.finops,
    aws_iam_role_policy_attachment.cur-report-s3-access
  ]
}
