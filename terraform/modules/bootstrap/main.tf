################################################################################
# The buckets that must exist before the stack's first apply.
#
# Apply this root ONCE, before anything else — from a COPY OUTSIDE YOUR CLONE,
# never from here. Its state is a local file (see versions.tf), and applied in
# place that file lands inside a git checkout, where `git clean -fdx` or deleting
# the clone to start again takes the only record of the three buckets that
# outlive every cluster. `pixi run cloudpipe setup` prints the command, which
# copies this directory to <your deployment root>/bootstrap and applies it there.
#
# It creates nothing else: there is no `module` block here, so the worst an
# accidental apply can do is make buckets.
#
# WHY THIS IS A SEPARATE ROOT, with its own local state (see versions.tf):
#
#   * It creates the bucket the stack's S3 backend lives in, so it cannot use
#     that backend itself.
#   * Everything here outlives the cluster. `cleanup.sh` destroys the stack and
#     never touches this state, so no teardown path can delete a deployment's
#     data or its record of what was processed.
#
# The stack ADOPTS these buckets by name — `globus_s3_destination_bucket` and
# `metrics_bucket` — and declares none of them. That is what lets a deployment
# point at buckets it already has: set `create_data_bucket = false` and name it.
#
# Deleting any of these buckets is deliberately not something Terraform will do:
# `prevent_destroy` stops the plan, and no `force_destroy` means S3 refuses a
# non-empty bucket anyway. `cleanup.sh` ends by naming them and the `aws s3 rb`
# that removes them, for someone who means it.
################################################################################

# The state bucket's policy was `aws_s3_bucket_policy.terraform_state` before the
# three buckets shared one `for_each`. For a state that holds the old address, this
# block is what turns a destroy-and-recreate — a window in which the bucket holding
# every secret in the stack's state has no transport Deny on it — into a rename.
#
# For THIS deployment it is a no-op, and that is worth knowing rather than
# assuming: the policy resource was added in October and never applied, so the
# address was never in state and the live bucket has no policy at all (verified
# 2026-10-09: `get-bucket-policy` returns NoSuchBucketPolicy). The first apply of
# this root therefore CREATES that policy. Keep the block anyway — it costs
# nothing, and a deployment that did apply the old layout needs it.
moved {
  from = aws_s3_bucket_policy.terraform_state
  to   = aws_s3_bucket_policy.deny_insecure_transport["state"]
}

# ---------------------------------------------------------------------------
# Terraform state
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "terraform_state" {
  bucket = var.state_bucket

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# Imaging data
# ---------------------------------------------------------------------------

# The bucket the pipeline reads its input from and writes its derivatives to —
# the stack's `globus_s3_destination_bucket`. The stack configures it (access
# logging, lifecycle and Intelligent-Tiering rules, and the IAM policies scoped
# to its ARN) and does not create it, which is the split design D4 describes.
#
# NO VERSIONING, and not as an oversight: the contents are recomputable, which
# is the basis of this deployment's H8 risk acceptance, and the operator's
# decision (2026-10-08) is that versioning here stays off permanently because
# versioning hundreds of GB of churn-prone derivatives is too expensive. That is
# also why this bucket's policy carries no `DeleteObjectVersion` Deny: there are
# no versions for one to protect, and a statement that cannot fire reads as
# protection that is not there.
resource "aws_s3_bucket" "data" {
  count  = var.create_data_bucket ? 1 : 0
  bucket = var.data_bucket

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  count  = var.create_data_bucket ? 1 : 0
  bucket = aws_s3_bucket.data[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  count  = var.create_data_bucket ? 1 : 0
  bucket = aws_s3_bucket.data[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# ---------------------------------------------------------------------------
# Metrics — the run of record for pipeline QC
# ---------------------------------------------------------------------------

# Moved here from terraform/modules/stack/metrics_bucket.tf (design D7). It
# records what was processed, so losing it with the cluster would be wrong — and
# a bucket named after the deployment that the deployment itself creates cannot
# survive a teardown and then be installed over: the second apply fails with
# BucketAlreadyOwnedByYou.
#
# VERSIONED, unlike the data bucket. A metric record is overwritten in place
# when a unit is reprocessed, so the prior value exists only as a noncurrent
# version — that is the entire reason this bucket is separate from the data
# bucket, versioning being a bucket-level switch. Versioning the data bucket to
# reach it would put the most churn-prone data in the account under version
# retention.
#
# Sizing, so nobody requotes the stale figure that was in the old header: 32.4 GB
# across 2.56M objects, measured 2026-10-07. The old note said "~1.8 GB at full
# ABCD scale", which is wrong by more than an order of magnitude. The conclusion
# it supported still holds — a few dollars a month in Standard, and lifecycle
# transitions are billed per object, which is the wrong shape for 2.5M small
# ones — so there is deliberately no lifecycle configuration here either.
resource "aws_s3_bucket" "metrics" {
  bucket = var.metrics_bucket

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "metrics" {
  bucket = aws_s3_bucket.metrics.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "metrics" {
  bucket = aws_s3_bucket.metrics.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# SSE-S3 rather than KMS: metrics are many small objects, and per-object KMS
# requests would dominate the cost of a bucket that otherwise costs cents. This
# matches the Athena query-results encryption in modules/metrics/athena.tf.
resource "aws_s3_bucket_server_side_encryption_configuration" "metrics" {
  bucket = aws_s3_bucket.metrics.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ---------------------------------------------------------------------------
# Encryption in transit, for every bucket this root makes
# ---------------------------------------------------------------------------

# Encryption at rest is configured above; this is encryption in transit, a
# separate obligation. The state bucket holds the whole stack's state — secrets
# in plain text, the Cloudflare IdP client secret and the VPN CA key among them —
# and the data bucket holds the objects whose access CloudTrail data events
# record for the DUA, so a plain-HTTP read is the thing to refuse on all of them.
#
# The Deny cannot lock Terraform out of its own state: the S3 backend and the
# AWS SDKs speak HTTPS, so `aws:SecureTransport` is true on every request they
# make. The condition tests for the key being explicitly `false` rather than
# "not true", because the key is absent from some signed requests and a Deny on
# a missing key would reject them.
#
# S3 allows exactly one policy per bucket, so any future statement MUST be
# folded into these documents rather than added as a second
# `aws_s3_bucket_policy`.
locals {
  # Bucket NAMES, keyed by role. The names rather than the resources' `arn`
  # attributes, so that the one bucket this root may not create — an adopted data
  # bucket — would read identically if it were ever added here; `depends_on`
  # below is what orders the policy after the bucket, since the reference is no
  # longer implicit.
  policied_buckets = merge(
    {
      state   = var.state_bucket
      metrics = var.metrics_bucket
    },
    var.create_data_bucket ? { data = var.data_bucket } : {},
  )
}

data "aws_iam_policy_document" "deny_insecure_transport" {
  for_each = local.policied_buckets

  statement {
    sid    = "DenyUnencryptedTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      "arn:aws:s3:::${each.value}",
      "arn:aws:s3:::${each.value}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "deny_insecure_transport" {
  for_each = local.policied_buckets

  bucket = each.value
  policy = data.aws_iam_policy_document.deny_insecure_transport[each.key].json

  # block_public_policy rejects a policy that grants public access while it is
  # being evaluated. A Deny with Principal "*" is not a public grant, so this is
  # belt and braces rather than a requirement — the same ordering the stack uses
  # for its own log buckets.
  depends_on = [
    aws_s3_bucket.terraform_state,
    aws_s3_bucket.metrics,
    aws_s3_bucket.data,
    aws_s3_bucket_public_access_block.terraform_state,
    aws_s3_bucket_public_access_block.metrics,
    aws_s3_bucket_public_access_block.data,
  ]
}
