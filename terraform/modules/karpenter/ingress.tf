################################################################################
# The ingress node pool (openspec add-https-ingress-mode, group 3)
################################################################################
# Where `ingress-mode=https` pods run: nodes with public IPv4 addresses, in the
# ingress subnets and behind the ingress-node security group that the stack
# module creates (ingress_network.tf). The node class is the ONLY one allowed
# `associatePublicIPAddress: true`; tests/test_ingress_node_isolation.py holds
# every class to that, and checks everything below.
#
# Its own node IAM role (design D9, operator decision 2026-10-12). An ingress
# node parses internet responses and has a public address, so what a compromise
# of one yields should be less than what a general node holds. Of the shared
# Karpenter node role's six policies it keeps three:
#
#   AmazonEKSWorkerNodePolicy           joining the cluster
#   AmazonEKS_CNI_Policy                aws-node has no Pod Identity, so the CNI
#                                       acts with the node role
#   AmazonEC2ContainerRegistryPullOnly  pulling images
#
# and drops AmazonSSMManagedInstanceCore (no SSM session path to these nodes),
# CloudWatchAgentServerPolicy (no CloudWatch agent runs in the cluster) and
# AmazonEKSVPCResourceController (security groups for pods are off).

locals {
  ingress_node_policies = [
    "AmazonEKSWorkerNodePolicy",
    "AmazonEKS_CNI_Policy",
    "AmazonEC2ContainerRegistryPullOnly",
  ]
}

data "aws_partition" "current" {}

data "aws_iam_policy_document" "ingress_node_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ingress_node" {
  name               = "${var.cluster_name}-ingress-node"
  description        = "Karpenter ingress nodes (public IPs): worker, CNI and ECR pull only"
  assume_role_policy = data.aws_iam_policy_document.ingress_node_assume.json
}

resource "aws_iam_role_policy_attachment" "ingress_node" {
  for_each = toset(local.ingress_node_policies)

  role       = aws_iam_role.ingress_node.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/${each.key}"
}

# The shared node role gets its access entry from module.karpenter_infra. This
# one needs its own, or its nodes launch and never join.
resource "aws_eks_access_entry" "ingress_node" {
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.ingress_node.arn
  type          = "EC2_LINUX"
}

resource "kubectl_manifest" "ingress-nodeclass" {
  server_side_apply = true
  force_conflicts   = true
  yaml_body = templatefile("${path.module}/helm-values/ingress-nodeclass.yaml",
    {
      ingress_node_iam_role_name = aws_iam_role.ingress_node.name
      subnet_ids                 = var.ingress_subnet_ids
      security_group_id          = var.ingress_security_group_id
  })

  depends_on = [
    helm_release.karpenter,
    aws_eks_access_entry.ingress_node,
    aws_iam_role_policy_attachment.ingress_node,
  ]
}

resource "kubectl_manifest" "ingress-nodepool" {
  server_side_apply = true
  force_conflicts   = true
  yaml_body         = file("${path.module}/helm-values/ingress-nodepool.yaml")
  depends_on = [
    kubectl_manifest.ingress-nodeclass
  ]
}
