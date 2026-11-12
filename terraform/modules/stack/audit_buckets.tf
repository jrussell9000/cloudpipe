# ---------------------------------------------------------------------------
# Reprocess-audit bucket pairs (openspec/changes/add-reprocess-audit, task 1.1)
# ---------------------------------------------------------------------------
#
# The audit re-runs the production `cloudpipe` template, fresh, on a sample of
# subjects, into buckets of its own, so nothing it writes reaches production
# derivatives, metrics or cost tables (design D1). Two pairs, each a data bucket
# and a metrics bucket:
#
#   primary  the main sample, and each noise-floor subject's first run
#   repeat   each noise-floor subject's second run; a second run into the
#            primary pair would find the first run's outputs and skip
#
# Names are the data bucket's plus a role suffix (operator, 2026-11-08):
# <data bucket>-audit-data, -audit-metrics, -audit-repeat-data and
# -audit-repeat-metrics.
#
# The data buckets hold ABCD-derived imaging data, so they carry exactly the
# production data bucket's controls, not lighter ones: public access blocked,
# SSE-S3, TLS-only, server access logging, and S3 data events in the trail
# (logging.tf: CloudTrail data events are the DUA access record). The metrics
# buckets are versioned like the production metrics bucket.
#
# Unlike production's buckets these are disposable. Everything expires 90 days
# after it is written (design: Risks), and teardown (task 7.1) is: empty them,
# then set reprocess_audit = false. prevent_destroy is deliberately absent and
# force_destroy deliberately false, so a toggle can only remove EMPTY buckets.
#
# Off by default, so the CI-validated example root and any outside deployment
# get none of this.

locals {
  audit_buckets = var.reprocess_audit ? {
    primary_data    = "${var.globus_s3_destination_bucket}-audit-data"
    primary_metrics = "${var.globus_s3_destination_bucket}-audit-metrics"
    repeat_data     = "${var.globus_s3_destination_bucket}-audit-repeat-data"
    repeat_metrics  = "${var.globus_s3_destination_bucket}-audit-repeat-metrics"
  } : {}

  audit_data_buckets    = [for k, b in local.audit_buckets : b if endswith(k, "_data")]
  audit_metrics_buckets = [for k, b in local.audit_buckets : b if endswith(k, "_metrics")]

  # What the queue manager and cost scraper read from the Prefect Variable
  # cloudpipe-audit-buckets (src/metrics/audit_pairs.py). Output so the operator
  # sets the variable from Terraform's own names rather than retyping them.
  audit_pairs_variable = var.reprocess_audit ? jsonencode({
    primary = { bucket = local.audit_buckets.primary_data, metrics_bucket = local.audit_buckets.primary_metrics }
    repeat  = { bucket = local.audit_buckets.repeat_data, metrics_bucket = local.audit_buckets.repeat_metrics }
  }) : ""
}

resource "aws_s3_bucket" "audit" {
  for_each = local.audit_buckets

  bucket        = each.value
  force_destroy = false
  tags          = { "cloudpipe.io/purpose" = "reprocess-audit" }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  for_each = aws_s3_bucket.audit

  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  for_each = aws_s3_bucket.audit

  bucket = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Metrics buckets only, as in production: a record overwritten in place
# survives as a noncurrent version. The data buckets stay unversioned, as
# production's does (bootstrap/main.tf: churn-prone, recomputable derivatives).
resource "aws_s3_bucket_versioning" "audit_metrics" {
  for_each = { for k, b in aws_s3_bucket.audit : k => b if endswith(k, "_metrics") }

  bucket = each.value.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "audit" {
  for_each = aws_s3_bucket.audit

  bucket = each.value.id

  rule {
    id     = "expire-audit-90d"
    status = "Enabled"
    filter {}

    expiration {
      days = 90
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.audit_metrics]
}

data "aws_iam_policy_document" "audit_deny_insecure_transport" {
  for_each = aws_s3_bucket.audit

  statement {
    sid    = "DenyUnencryptedTransport"
    effect = "Deny"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions   = ["s3:*"]
    resources = [each.value.arn, "${each.value.arn}/*"]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "audit" {
  for_each = aws_s3_bucket.audit

  bucket = each.value.id
  policy = data.aws_iam_policy_document.audit_deny_insecure_transport[each.key].json

  depends_on = [aws_s3_bucket_public_access_block.audit]
}

resource "aws_s3_bucket_logging" "audit" {
  for_each = aws_s3_bucket.audit

  bucket        = each.value.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "s3-access-logs/${each.value.bucket}/"

  depends_on = [aws_s3_bucket_policy.access_logs]
}

output "audit_pairs_variable" {
  description = "JSON value for the Prefect Variable cloudpipe-audit-buckets (src/metrics/audit_pairs.py), or \"\" when reprocess_audit is off. Set it from this output, not by hand."
  value       = local.audit_pairs_variable
}
