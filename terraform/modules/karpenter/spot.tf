# The EC2 Spot service-linked role, AWSServiceRoleForEC2Spot.
#
# EC2 creates it on an account's first Spot launch, as the caller. A new account
# has never launched Spot, so that caller is the Karpenter controller, which may
# not create roles: every launch failed with "AuthFailure.ServiceLinkedRoleCreation
# NotPermitted" inside an UnfulfillableCapacity fleet error, and Karpenter reported
# it as "nodepool requirements filtered out all available instance types"
# (second-account rerun, 2026-10-14). The reference account has had the role
# since 2025-01-02, which is why nothing caught it.
#
# A grant rather than an `aws_iam_service_linked_role` resource: the role is
# per account and outlives any one deployment, so Terraform creating it would
# fail with "already exists" in every account that has it, the reference one
# included, and destroying it would break Spot for anything else in the account.
# The grant creates nothing, and EC2 uses it at most once.
#
# A separate policy, like controller_passes_ingress_role, because the module's
# controller policy is at its size limit (main.tf).
data "aws_iam_policy_document" "controller_creates_spot_role" {
  statement {
    sid       = "CreateSpotServiceLinkedRole"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["arn:${data.aws_partition.current.partition}:iam::*:role/aws-service-role/spot.amazonaws.com/AWSServiceRoleForEC2Spot"]

    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values   = ["spot.amazonaws.com"]
    }
  }
}

resource "aws_iam_policy" "controller_creates_spot_role" {
  name        = "${var.cluster_name}-karpenter-create-spot-role"
  description = "Lets the Karpenter controller create the EC2 Spot service-linked role on an account's first Spot launch, and nothing else"
  policy      = data.aws_iam_policy_document.controller_creates_spot_role.json
}

resource "aws_iam_role_policy_attachment" "controller_creates_spot_role" {
  role       = module.karpenter_infra.iam_role_name
  policy_arn = aws_iam_policy.controller_creates_spot_role.arn
}
