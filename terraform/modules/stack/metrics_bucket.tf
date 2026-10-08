################################################################################
# CloudPipe metrics bucket — the run of record for pipeline QC.
#
# Separate from the data bucket for one reason: versioning is a bucket-level
# switch in S3. The metrics corpus is ~3.7 MB today and ~1.8 GB at full ABCD
# scale (~150 KB/subject), so versioning it costs pennies. Versioning the data
# bucket to reach it would place ~374 GB of derivatives — the most churn-prone
# data in the account, deleted and rewritten on every reprocess — under version
# retention, which at full scale is tens of TB.
#
# The split is also the dev/production boundary. Every metric write is a bare
# put_object with no merge step, so the S3 key IS the record's primary key and a
# stray delete is silent. Keeping metrics in a bucket that the test-batch flush
# has no reason to address makes that structural rather than a convention.
################################################################################

resource "aws_s3_bucket" "metrics" {
  bucket = "${var.name}-metrics"
  region = var.region
}

# The point of the whole exercise. A metric record is overwritten in place on
# reprocessing, so without versioning the prior value is simply gone.
resource "aws_s3_bucket_versioning" "metrics" {
  bucket = aws_s3_bucket.metrics.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "metrics" {
  bucket                  = aws_s3_bucket.metrics.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# SSE-S3 rather than KMS: metrics are many small objects, and per-object KMS
# requests would dominate the cost of a bucket that otherwise costs cents.
# Matches the Athena query-results encryption in modules/metrics/athena.tf.
resource "aws_s3_bucket_server_side_encryption_configuration" "metrics" {
  bucket = aws_s3_bucket.metrics.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Reject anything that did not arrive over TLS.
#
# Encryption at rest is configured above; this is encryption in transit, which
# is a separate obligation and was not enforced anywhere in this account. The
# condition is the negative form on purpose: `aws:SecureTransport` is absent
# from some signed requests, and `Deny` when it is explicitly `false` fails
# closed on plain HTTP without denying a request that simply omits the key.
#
# This bucket had NO policy before (verified 2026-10-07: NoSuchBucketPolicy), so
# this CREATES rather than replaces. S3 allows exactly one policy per bucket —
# any future statement MUST be folded into the document below, never added as a
# second aws_s3_bucket_policy. The same warning is on the data bucket's policy in
# ../../abcd_v7_metrics_retire.tf, where it was not hypothetical.
data "aws_iam_policy_document" "metrics_bucket_policy" {
  statement {
    sid    = "DenyUnencryptedTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.metrics.arn,
      "${aws_s3_bucket.metrics.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "metrics" {
  bucket = aws_s3_bucket.metrics.id
  policy = data.aws_iam_policy_document.metrics_bucket_policy.json

  # block_public_policy rejects a policy that grants public access while it is
  # being evaluated; ordering after it is the pattern used for the access-log
  # bucket. A Deny with Principal "*" is not a public grant, so this is belt and
  # braces rather than a requirement.
  depends_on = [aws_s3_bucket_public_access_block.metrics]
}

# Deliberately NO lifecycle configuration.
#
# Metrics stay in Standard: Glue crawlers and Athena read them synchronously, and
# the volume never justifies tiering. Noncurrent versions are retained
# indefinitely — an overwrite being recoverable is the entire reason this bucket
# exists.
#
# The size note at the top of this file said "~3.7 MB today and ~1.8 GB at full
# ABCD scale". Measured 2026-10-07: 32.4 GB across 2.56M objects, so that
# estimate is stale by more than an order of magnitude and should not be quoted.
# The conclusion still holds — 32 GB in Standard is a few dollars a month, and
# transitions are billed per object, which is the wrong shape for 2.5M small
# objects — but it holds on different numbers than the ones written down.
