################################################################################
# Staging test bed for the Globus ingress
#
# A second High Assurance S3 storage gateway and collection live on the SAME GCS
# endpoint as production (they are created in Globus, not here — see the
# `simplify-globus-ingress` OpenSpec change). Everything they can touch is
# confined to one S3 prefix, so a change can be proven against real ABCD data
# and a real HA gateway without production being able to notice.
#
# This file owns only the AWS side of that: the S3 permissions granted to the IAM
# user whose access key the staging gateway registers, and the two (empty)
# Secrets Manager containers the operator CLI writes into. Values are never set
# here — a secret string set by Terraform is a secret in Terraform state.
#
# Confinement rests on two independent things, deliberately:
#   1. the staging collection is rooted at <bucket>/<staging prefix>, so Globus
#      cannot form a path outside it, and
#   2. the named IAM user's S3 permissions stop at that prefix, so a collection
#      rooted anywhere else would fail on the first object.
#
# (2) only holds if `globus_staging_iam_user_name` names a user that has no other
# S3 policy. Pointing it at the production gateway's user would leave (1) as the
# only barrier — see that variable's description.
################################################################################

locals {
  staging_gateway_name = "${var.globus_collection_name}-staging"
  staging_prefix       = trim(var.globus_staging_prefix, "/")

  # An IAM user cannot be created in this account: an organization SCP denies
  # iam:CreateUser outright, for any name. So the user is named, not made.
  staging_iam_user       = trim(var.globus_staging_iam_user_name, " ")
  staging_iam_user_count = var.globus_staging_enabled && local.staging_iam_user != "" ? 1 : 0

  # Secrets Manager names. Kept parallel to the production pair so the operator
  # CLI can derive both from one environment table:
  #   globus/refresh-token           <-> globus/refresh-token-staging
  #   globus/s3-gateway/<gateway>    <-> globus/s3-gateway/<gateway>-staging
  staging_token_secret_name      = "globus/refresh-token-staging"
  staging_credential_secret_name = "globus/s3-gateway/${local.staging_gateway_name}"
}

################################################################################
# S3 permissions for the staging gateway's IAM user
#
# A user, not a role: the GCS S3 connector supports no IAM role or instance
# profile (https://docs.globus.org/premium-storage-connectors/v5/aws-s3/), so a
# static access key is the only option Globus offers. The key itself is created
# by `globus rotate-s3-key --env staging`, never by Terraform.
#
# The user is looked up, never created. An organization SCP in the management
# account explicitly denies iam:CreateUser here, which AdministratorAccess cannot
# override, so a `resource "aws_iam_user"` fails the apply no matter what it is
# named. Getting a new user provisioned is a request to whoever administers the
# organization; until then `globus_staging_iam_user_name` stays empty and this
# whole block does nothing.
#
# The lookup is a data source so a typo is a plan-time error naming the user,
# rather than a NoSuchEntity thrown by PutUserPolicy part-way through an apply.
################################################################################

data "aws_iam_user" "globus_staging" {
  count     = local.staging_iam_user_count
  user_name = local.staging_iam_user
}

data "aws_iam_policy_document" "globus_staging_s3" {
  # Object-level access, confined to the staging prefix. The production gateway's
  # IAM user has the same actions across the whole bucket; this one cannot read
  # or write a single object outside the prefix, including mmps_mproc/.
  statement {
    sid    = "StagingObjectsWithinPrefix"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = [
      "arn:${var.partition}:s3:::${var.globus_s3_bucket}/${local.staging_prefix}/*",
    ]
  }

  # Listing is bucket-scoped in IAM, so the prefix restriction has to be a
  # condition. The staging collection is rooted at the prefix, so every listing
  # Globus issues through it carries that prefix — but if a listing ever arrives
  # with an empty prefix (e.g. a client browsing the gateway root), it is denied
  # rather than enumerating the bucket. Task 1.7/T1 is where this is proven
  # against the real connector; relax the condition only with that evidence.
  statement {
    sid    = "StagingListWithinPrefix"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:ListBucketMultipartUploads",
    ]
    resources = ["arn:${var.partition}:s3:::${var.globus_s3_bucket}"]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        "${local.staging_prefix}/*",
        local.staging_prefix,
      ]
    }
  }

  statement {
    sid    = "StagingBucketLocation"
    effect = "Allow"
    actions = [
      "s3:GetBucketLocation",
    ]
    resources = ["arn:${var.partition}:s3:::${var.globus_s3_bucket}"]
  }

  # The S3 connector documents s3:ListAllMyBuckets on `*` so it can populate a
  # root listing. It cannot be resource-scoped by AWS, and it reveals only
  # bucket names.
  statement {
    sid       = "StagingListAllMyBuckets"
    effect    = "Allow"
    actions   = ["s3:ListAllMyBuckets"]
    resources = ["*"]
  }
}

# An inline policy, so it travels with the user and is removed with it. Note that
# this is additive: if the named user already carries another S3 policy, this
# grant widens what it can reach rather than narrowing it.
resource "aws_iam_user_policy" "globus_staging_s3" {
  count  = local.staging_iam_user_count
  name   = "${var.name}-globus-staging-s3"
  user   = data.aws_iam_user.globus_staging[0].user_name
  policy = data.aws_iam_policy_document.globus_staging_s3.json
}

################################################################################
# Secrets Manager containers (values written by the operator CLI, not Terraform)
#
# recovery_window_in_days = 0 so the staging pair can be deleted and recreated
# without waiting out a recovery window — staging is disposable by design. The
# production equivalents keep the default window.
################################################################################

resource "aws_secretsmanager_secret" "globus_staging_s3_credential" {
  count                   = var.globus_staging_enabled ? 1 : 0
  name                    = local.staging_credential_secret_name
  description             = "AWS access key registered with the ${local.staging_gateway_name} Globus S3 storage gateway. Written by `globus rotate-s3-key --env staging`."
  recovery_window_in_days = 0

  tags = {
    Name = local.staging_credential_secret_name
  }
}

resource "aws_secretsmanager_secret" "globus_staging_refresh_token" {
  count                   = var.globus_staging_enabled ? 1 : 0
  name                    = local.staging_token_secret_name
  description             = "Globus refresh token for the staging collection. Written by `globus login --env staging`."
  recovery_window_in_days = 0

  tags = {
    Name = local.staging_token_secret_name
  }
}

################################################################################
# The GCS instance reads the staging S3 credential while reconciling the staging
# gateway. It never reads the refresh token — that one is for the transfer
# client (Argo pods and the probe), not for the server.
################################################################################

data "aws_iam_policy_document" "globus_staging_secret_read" {
  count = var.globus_staging_enabled ? 1 : 0

  statement {
    sid       = "ReadStagingGatewayCredential"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.globus_staging_s3_credential[0].arn]
  }
}

resource "aws_iam_role_policy" "globus_staging_secret_read" {
  count  = var.globus_staging_enabled ? 1 : 0
  name   = "${var.name}-globus-staging-secret-read"
  role   = aws_iam_role.globus.name
  policy = data.aws_iam_policy_document.globus_staging_secret_read[0].json
}
