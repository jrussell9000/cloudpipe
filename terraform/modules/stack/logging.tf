resource "aws_s3_bucket" "logs" {
  bucket = var.log_bucket
  region = var.region
}

# Bucket policy — required for AWS services to write to the log bucket
resource "aws_s3_bucket_policy" "logs" {
  bucket = aws_s3_bucket.logs.id
  policy = data.aws_iam_policy_document.logs_bucket_policy.json
}

# Log bucket access policies
data "aws_iam_policy_document" "logs_bucket_policy" {
  # VPC Flow Logs
  #
  # Delivery requires BOTH statements below. Without the AclCheck statement the
  # flow log is created and reports ACTIVE, but every delivery fails with
  # DeliverLogsErrorMessage="Access error" and no objects are ever written —
  # a silent failure that hid a broken flow log for months. See
  # https://docs.aws.amazon.com/vpc/latest/userguide/flow-logs-s3-permissions.html
  #
  # SourceAccount/SourceArn guard against the confused-deputy problem (AWS best
  # practice); the source ARN is the wildcard logs-service ARN, not this VPC's.
  statement {
    sid     = "AWSLogDeliveryWrite"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    resources = ["${aws_s3_bucket.logs.arn}/vpc-flow-logs/*"]
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:logs:${var.region}:${local.account_id}:*"]
    }
  }

  # VPC Flow Logs — bucket-level ACL check (required; see note above)
  statement {
    sid     = "AWSLogDeliveryAclCheck"
    effect  = "Allow"
    actions = ["s3:GetBucketAcl"]
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    resources = [aws_s3_bucket.logs.arn]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:logs:${var.region}:${local.account_id}:*"]
    }
  }

  # S3 server access logs and ALB access logs are NOT delivered here — see
  # aws_s3_bucket.access_logs below. Both producers require an SSE-S3
  # destination and this bucket is SSE-KMS.
  #
  # An "S3AccessLogDelivery" statement used to grant logging.s3.amazonaws.com
  # PutObject on ${arn}/s3-access-logs/*, and an "ALBAccessLogDelivery"
  # statement granted the regional ELB account below. Neither ever delivered an
  # object. The bucket policy was only ever half the requirement: the CMK policy
  # also has to grant the writer, and for these two producers it cannot. Both
  # statements are removed rather than left in place, because a grant that reads
  # as "the destination is ready" when the destination can never work is what
  # made this look remediated for four months.

  # CloudTrail
  statement {
    sid     = "CloudTrailWrite"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    resources = ["${aws_s3_bucket.logs.arn}/cloudtrail/*"]
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }

  # CloudTrail bucket ACL check
  statement {
    sid     = "CloudTrailAclCheck"
    effect  = "Allow"
    actions = ["s3:GetBucketAcl"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    resources = [aws_s3_bucket.logs.arn]
  }

  # AU-9 (Protection of Audit Information)
  #
  # Versioning above keeps the bytes after a DeleteObject; this keeps them after
  # a determined delete. Erasing a specific version needs DeleteObjectVersion,
  # and turning versioning off to make future deletes permanent needs
  # PutBucketVersioning — deny both to everything but the account root.
  #
  # This does NOT interfere with the lifecycle rules above: S3 performs
  # lifecycle expiration itself and is not evaluated against the bucket policy.
  # It does mean `terraform destroy` cannot empty this bucket, which is the
  # intended behaviour for an audit trail.
  #
  # Service principals carry no aws:PrincipalArn, so the negated condition
  # matches them too. That is harmless: they only ever PutObject.
  statement {
    sid    = "DenyAuditLogTampering"
    effect = "Deny"
    actions = [
      "s3:DeleteObjectVersion",
      "s3:PutBucketVersioning",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    resources = [
      aws_s3_bucket.logs.arn,
      "${aws_s3_bucket.logs.arn}/*",
    ]
    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::${local.account_id}:root"]
    }
  }
}

# Encryption
#
# Must be a CUSTOMER-MANAGED key. VPC Flow Logs cannot deliver to a bucket
# encrypted with the AWS-managed aws/s3 key — AWS requires a customer managed
# key ARN, and the aws/s3 key policy cannot be edited to grant the delivery
# service. Using the AWS-managed key is what silently broke flow log delivery.
# https://docs.aws.amazon.com/vpc/latest/userguide/flow-logs-s3-cmk-policy.html
data "aws_iam_policy_document" "logs_kms" {
  statement {
    sid     = "EnableRootAccess"
    effect  = "Allow"
    actions = ["kms:*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
    resources = ["*"]
  }

  # VPC Flow Logs delivery
  statement {
    sid    = "AllowVPCFlowLogsDelivery"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }

  # CloudTrail — already delivering to this bucket today under the AWS-managed
  # key. Without this grant, switching to the CMK would break it.
  statement {
    sid    = "AllowCloudTrail"
    effect = "Allow"
    actions = [
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    resources = ["*"]
    condition {
      test     = "StringLike"
      variable = "kms:EncryptionContext:aws:cloudtrail:arn"
      values   = ["arn:aws:cloudtrail:*:${local.account_id}:trail/*"]
    }
  }
}

resource "aws_kms_key" "logs" {
  description             = "CMK for the ${var.log_bucket} log bucket (VPC flow logs + CloudTrail)"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.logs_kms.json
}

resource "aws_kms_alias" "logs" {
  name          = "alias/${var.name}-logs"
  target_key_id = aws_kms_key.logs.key_id
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
      # Must be the key ARN, not the key ID — a bare key ID produces a
      # "LogDestination undeliverable" error when creating the flow log.
      kms_master_key_id = aws_kms_key.logs.arn
    }
    # S3 Bucket Keys cut KMS request charges by up to 99%. Flow logs write many
    # small objects, so without this the per-object KMS calls are a real cost.
    bucket_key_enabled = true
  }
}

# Block all public access
resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning — AU-9 (Protection of Audit Information), NOT backup.
#
# Without it, any principal with write access to this bucket can erase the
# record of what it did. With it, a DeleteObject leaves a delete marker and the
# real bytes survive as a noncurrent version, and erasing those needs the
# separate DeleteObjectVersion permission that the bucket policy below denies.
#
# The H8 risk acceptance (no versioning) covers the data bucket on the grounds that its
# contents are recomputable. Audit logs are not recomputable, and this bucket is
# ~24 GB rather than tens of TiB, so that argument does not carry over — which
# the acceptance itself says under "Note on scope".
resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Lifecycle — per-class retention (NIST 800-171 3.3.1)
#
# Two deliberate changes from the previous blanket "Glacier at 90d, expire at
# 3y" rule:
#
# 1. NO Glacier transition. Transitions are billed per object ($0.05/1000), and
#    this bucket holds ~294k objects in ~24 GB — flow logs and CloudTrail both
#    write many small files. Transitioning that population costs ~$14.70 to save
#    ~$0.46/month, a ~32-month payback. Glacier pays off on few-large-objects,
#    which is the opposite of a log bucket. Expiring earlier is strictly cheaper
#    than archiving longer here.
# 2. Retention is split by log class instead of applied uniformly. Three years
#    of flow logs is the single largest line item and the least useful one.
resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  # CloudTrail — the accountability record. Object-level access to the data bucket is
  # what the ABCD DUA audit obligation actually rests on, so this is the one
  # class that keeps a full year.
  rule {
    id     = "cloudtrail-retention"
    status = "Enabled"
    filter {
      prefix = "cloudtrail/"
    }
    expiration {
      days = var.audit_log_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_log_version_retention_days
    }
  }

  # VPC flow logs — packet-level detail, high object count, supplementary to the
  # CloudTrail record above.
  rule {
    id     = "flow-log-retention"
    status = "Enabled"
    filter {
      prefix = "vpc-flow-logs/"
    }
    expiration {
      days = var.flow_log_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_log_version_retention_days
    }
  }

  # Backstop. Versioning is on now, so anything written under a prefix not
  # matched above would otherwise accumulate noncurrent versions forever.
  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_log_version_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
    # Clean up delete markers whose last real version has already expired,
    # otherwise every expired log leaves a zero-byte marker behind forever.
    expiration {
      expired_object_delete_marker = true
    }
  }
}

################################################################################
# Access-log bucket — ALB access logs + S3 server access logs
################################################################################
#
# Why a second bucket rather than another prefix on aws_s3_bucket.logs:
#
#   * ALB access logs are written by a regional AWS-owned ACCOUNT principal
#     (arn:aws:iam::033677994240:root in this region), not by a service principal.
#     AWS supports only SSE-S3 on that destination, and there is no way to grant
#     an account principal usable access to the flow-log CMK. Enabling ALB logs
#     against the SSE-KMS bucket yields access_logs.s3.enabled=true with zero
#     objects delivered — the same silent failure documented for flow logs at
#     the top of this file.
#   * S3 server access logging hit exactly that wall in production. The
#     `aws_s3_bucket_logging.finops` resource has pointed at
#     cloudpipe-logging/finops/ since April; the prefix does not exist and never
#     received an object, because the CMK policy grants delivery.logs and
#     cloudtrail but not logging.s3.amazonaws.com.
#
# The split is also the cheaper arrangement. These two classes are many tiny
# objects, so SSE-S3 avoids a per-object KMS charge that bucket keys only
# partially amortise, and they can carry a much shorter retention than the
# CloudTrail record without weakening it.
resource "aws_s3_bucket" "access_logs" {
  bucket = var.access_log_bucket
  region = var.region
}

resource "aws_s3_bucket_server_side_encryption_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "access_logs" {
  bucket                  = aws_s3_bucket.access_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ALB access logging requires the bucket to accept the ACL the delivery account
# sets. Ownership enforced means ACLs are ignored entirely, which is the
# supported configuration for ALB logs today, but S3 server access logging
# delivers as the log-delivery group and needs the bucket to tolerate that —
# BucketOwnerEnforced is correct for both, since modern server access logging
# uses the logging.s3.amazonaws.com service principal rather than the legacy
# LogDelivery ACL grant.
resource "aws_s3_bucket_ownership_controls" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_policy" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id
  policy = data.aws_iam_policy_document.access_logs_bucket_policy.json

  depends_on = [aws_s3_bucket_public_access_block.access_logs]
}

data "aws_iam_policy_document" "access_logs_bucket_policy" {
  # ALB access logs. The regional ELB account ID is fixed per region:
  # https://docs.aws.amazon.com/elasticloadbalancing/latest/application/enable-access-logging.html
  # ALB writes to <prefix>/AWSLogs/<account-id>/elasticloadbalancing/..., so one
  # wildcard statement covers every per-ingress prefix.
  statement {
    sid     = "ALBAccessLogDelivery"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::033677994240:root"]
    }
    resources = ["${aws_s3_bucket.access_logs.arn}/*/AWSLogs/${local.account_id}/*"]
  }

  # S3 server access logs. SourceAccount guards the confused-deputy problem; the
  # source ARNs are the buckets whose logging is enabled below.
  statement {
    sid     = "S3ServerAccessLogDelivery"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }
    resources = ["${aws_s3_bucket.access_logs.arn}/s3-access-logs/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:s3:::*"]
    }
  }
}

# Short, flat retention. No storage-class transition for the same per-object
# reason as the master bucket, and more sharply here: access logs are the
# highest-object-count, lowest-value class in the system.
resource "aws_s3_bucket_lifecycle_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"
    filter {}
    expiration {
      days = var.access_log_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

################################################################################
# S3 server access logging (H2)
################################################################################

# The ABCD data bucket. CloudTrail data events already record every
# GetObject/PutObject here with the calling principal, which is what the DUA
# audit obligation requires; server access logs add the request-level detail
# CloudTrail omits (referrer, user agent, HTTP status, turnaround time) and are
# far cheaper per request at pipeline volume.
#
# The bucket predates Terraform, but s3_lifecycle.tf already manages
# configuration on it by name, so this follows the same pattern.
resource "aws_s3_bucket_logging" "data" {
  bucket        = var.globus_s3_destination_bucket
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "s3-access-logs/${var.globus_s3_destination_bucket}/"

  depends_on = [aws_s3_bucket_policy.access_logs]
}

# Repointed off the SSE-KMS bucket, where it has silently delivered nothing
# since April.
resource "aws_s3_bucket_logging" "finops" {
  bucket        = "${var.name}-finops"
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "s3-access-logs/${var.name}-finops/"

  depends_on = [module.finops, aws_s3_bucket_policy.access_logs]
}

# AU-9: who read or deleted the audit trail is itself audit information. The
# access-log bucket is deliberately not logged to itself — that recurses, and
# each delivery would generate a further record.
resource "aws_s3_bucket_logging" "logs" {
  bucket        = aws_s3_bucket.logs.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "s3-access-logs/${var.log_bucket}/"

  depends_on = [aws_s3_bucket_policy.access_logs]
}

# Container log groups are deliberately absent.
#
# fluent-bit is disabled at the addon (`containerLogs.enabled = false` in
# eks.tf), so nothing writes to /aws/containerinsights/${var.name}/{application,
# argo-workflows} any more. The durable pod-log corpus is
# s3://<bucket>/logs/{workflow}/{pod}/main.log, written by Argo's
# `archiveLogs: true` — see docs/observability.md.
#
# An `aws_cloudwatch_log_group.container_insights` resource used to declare the
# argo-workflows group here at a 90-day retention so that it
# had a retention policy and a CMK; the `application` group was never in
# Terraform at all because fluent-bit auto-created it (auto_create_group true),
# which is why it accumulated with NO expiry. Both are removed rather than
# retained-and-empty. The historical data is not deleted by this change: destroy
# only removes the Terraform-managed group, and the auto-created one is
# untouched and can be dropped by hand once it is no longer wanted.
#
# Do not re-add these to get searchable logs. Query the S3 archive instead; it
# holds the same bytes and is already paid for.

resource "aws_cloudtrail" "this" {
  name                          = "${var.name}-cloudtrail"
  s3_bucket_name                = aws_s3_bucket.logs.id
  s3_key_prefix                 = "cloudtrail"
  include_global_service_events = true
  is_multi_region_trail         = true
  enable_log_file_validation    = true
  # Add CloudTrail auditing for globus destination bucket
  event_selector {
    read_write_type           = "All"
    include_management_events = true
    data_resource {
      type   = "AWS::S3::Object"
      values = ["arn:aws:s3:::${var.globus_s3_destination_bucket}/"]
    }
  }
}
