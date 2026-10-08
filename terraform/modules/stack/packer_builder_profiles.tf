################################################################################
# Static instance profiles for the Packer builders (#640)
#
# These replace Packer's `temporary_iam_instance_profile_policy_document`, and
# the point is what that lets us DELETE from the CI role: the whole `PackerIAM`
# statement in ecr.tf — iam:CreateRole, iam:PutRolePolicy, iam:PassRole on
# role/packer-* — which together were a privilege-escalation path.
#
# The path, for the record, because the obvious fix does not close it. The
# original finding was that `iam:AttachRolePolicy` with no `iam:PolicyARN`
# condition let a main-branch job attach AdministratorAccess to a packer-* role
# and launch an instance with it. Conditioning that action would not have been
# enough: `iam:PutRolePolicy` grants an arbitrary INLINE policy, which can
# express exactly the same privileges, and Packer REQUIRES PutRolePolicy —
# creating the temporary profile is how it applies that policy document. So the
# escalation survives any allowlist on AttachRolePolicy.
#
# Requiring an `iam:PermissionsBoundary` on CreateRole, the other suggestion,
# breaks the builder: Packer's amazon plugin has no option to set a boundary on
# the role it creates, so the condition denies the CreateRole and fails the bake.
#
# Pre-creating the profile removes the need for Packer to write IAM at all,
# which is why it is the fix rather than a narrowing.
#
# TWO profiles, not one shared superset: the fastsurfer builder pulls container
# images from the private registry and the globus builder does not, so a single
# role would hand the globus builder ECR access it never uses. That is the same
# over-grant this issue is about, one level down.
#
# Both policies are copied verbatim from what the templates previously requested
# in `temporary_iam_instance_profile_policy_document`, so the builders' runtime
# permissions are unchanged — only who creates them is.
################################################################################

data "aws_iam_policy_document" "packer_builder_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# Session Manager, needed by every builder: both templates reach the instance
# over SSM rather than SSH on a public IP, so without this the build cannot
# connect at all.
data "aws_iam_policy_document" "packer_builder_ssm" {
  statement {
    sid    = "SessionManagerMessaging"
    effect = "Allow"
    actions = [
      "ssm:UpdateInstanceInformation",
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
      "ec2messages:AcknowledgeMessage",
      "ec2messages:DeleteMessage",
      "ec2messages:FailMessage",
      "ec2messages:GetEndpoint",
      "ec2messages:GetMessages",
      "ec2messages:SendReply",
    ]
    # Resource "*" is not laziness: none of these actions is resource-scopable.
    # ssmmessages and ec2messages have no resource types at all, and the control
    # on them is which principal holds them — hence a dedicated role per builder.
    resources = ["*"]
  }
}

# ── fastsurfer builder: SSM + read-only pulls from the private registry ───────

data "aws_iam_policy_document" "packer_builder_gpu" {
  source_policy_documents = [data.aws_iam_policy_document.packer_builder_ssm.json]

  statement {
    sid    = "PullBakedImages"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    # Scoped to this deployment's repositories, unlike the `*` the temporary
    # policy used. The bake pulls fastsurfer and fireants; the prefix covers
    # both without naming them, so a renamed image does not fail the bake.
    resources = ["arn:aws:ecr:${var.region}:${data.aws_caller_identity.current.account_id}:repository/cloudpipe/*"]
  }

  statement {
    sid    = "EcrAuthToken"
    effect = "Allow"
    # GetAuthorizationToken takes no resource — it is the registry-wide login
    # call, and the pull statement above is what bounds what the token can read.
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
}

resource "aws_iam_role" "packer_builder_gpu" {
  name               = "packer-builder-gpu"
  description        = "Instance profile for the fastsurfer/fireants AMI bake (SSM + private ECR pulls)"
  assume_role_policy = data.aws_iam_policy_document.packer_builder_assume.json
}

resource "aws_iam_role_policy" "packer_builder_gpu" {
  name   = "packer-builder-gpu"
  role   = aws_iam_role.packer_builder_gpu.name
  policy = data.aws_iam_policy_document.packer_builder_gpu.json
}

resource "aws_iam_instance_profile" "packer_builder_gpu" {
  name = "packer-builder-gpu"
  role = aws_iam_role.packer_builder_gpu.name
}

# ── globus builder: SSM only ──────────────────────────────────────────────────

resource "aws_iam_role" "packer_builder_globus" {
  name               = "packer-builder-globus"
  description        = "Instance profile for the Globus GCS AMI bake (SSM only)"
  assume_role_policy = data.aws_iam_policy_document.packer_builder_assume.json
}

resource "aws_iam_role_policy" "packer_builder_globus" {
  name   = "packer-builder-globus"
  role   = aws_iam_role.packer_builder_globus.name
  policy = data.aws_iam_policy_document.packer_builder_ssm.json
}

resource "aws_iam_instance_profile" "packer_builder_globus" {
  name = "packer-builder-globus"
  role = aws_iam_role.packer_builder_globus.name
}
