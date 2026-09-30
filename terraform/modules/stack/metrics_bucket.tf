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

# Deliberately NO lifecycle configuration.
#
# Metrics stay in Standard: Glue crawlers and Athena read them synchronously, and
# the volume never justifies tiering. Noncurrent versions are retained
# indefinitely — at this scale the storage is negligible, and an overwrite being
# recoverable is the entire reason this bucket exists.
