locals {
  # Rendered into the GPU NodePools' zone requirement. Built here rather than in
  # the YAML so those files stay valid YAML for the tests that parse them, and
  # formatted to the exact bytes the literal list used to have — a whitespace
  # difference would rewrite two NodePool objects for no change in meaning.
  zones_list = "[${join(", ", formatlist("\"%s\"", var.zones))}]"
}

module "karpenter_infra" {
  source = "terraform-aws-modules/eks/aws//modules/karpenter"

  cluster_name                      = var.cluster_name
  create_pod_identity_association   = true
  node_iam_role_additional_policies = var.node_iam_role_additional_policies

  # The controller may pass only the shared node role (AllowPassingInstanceRole).
  # The ingress node class runs under its own role (ingress.tf), so the controller
  # needs one more PassRole — on that role alone, with the same condition. The
  # module appends this to its own policy document; it replaces nothing.
  iam_policy_statements = [
    {
      sid       = "AllowPassingIngressNodeRole"
      actions   = ["iam:PassRole"]
      resources = [aws_iam_role.ingress_node.arn]
      condition = [
        {
          test     = "StringEquals"
          variable = "iam:PassedToService"
          values   = ["ec2.amazonaws.com"]
        }
      ]
    }
  ]
}

# Install Karpenter and modify default configuration
# If this returns an access error (e.g., 403 forbidden) - helm registry logout public.ecr.aws && docker logout public.ecr.aws
resource "helm_release" "karpenter" {
  name             = "karpenter"
  namespace        = "kube-system"
  create_namespace = true
  repository       = "oci://public.ecr.aws/karpenter"
  chart            = "karpenter"
  version          = var.karpenter_version
  wait             = false

  values = [
    templatefile("${path.module}/helm-values/values-karpenter.yaml", {
      clusterName       = var.cluster_name
      clusterEndpoint   = var.cluster_endpoint
      interruptionQueue = module.karpenter_infra.queue_name
    })
  ]
}

# Adding EC2nodeclasses and nodepools
resource "kubectl_manifest" "nodeclass" {
  server_side_apply = true
  force_conflicts   = true
  yaml_body = templatefile("${path.module}/helm-values/nodeclass.yaml",
    {
      karpenter_node_iam_role_name = module.karpenter_infra.node_iam_role_name
      eks_cluster_name             = var.cluster_name
  })

  depends_on = [
    helm_release.karpenter
  ]
}

# Does this account own a pre-baked GPU AMI for the current images? The same three
# tags the node class selects on. `aws_ami_ids` returns an empty list rather than
# failing, which is what lets a deployment with no bake fall through to the
# published AMI or the stock one. Not read with selection = "alias".
data "aws_ami_ids" "gpu_baked" {
  count  = var.gpu_ami_selection == "alias" ? 0 : 1
  owners = ["self"]

  filter {
    name   = "tag:fastsurfer-image-digest"
    values = [var.fastsurfer_ami_digest]
  }
  filter {
    name   = "tag:fireants-image-digest"
    values = [var.fireants_ami_digest]
  }
  filter {
    name   = "tag:eks-version"
    values = [var.eks_version]
  }
  filter {
    name   = "state"
    values = ["available"]
  }
}

data "aws_region" "current" {}

module "gpu_ami" {
  source = "../gpu-ami-choice"

  baked_ami_found   = length(flatten(data.aws_ami_ids.gpu_baked[*].ids)) > 0
  published         = jsondecode(file("${path.module}/published-gpu-amis.json"))
  fastsurfer_digest = var.fastsurfer_ami_digest
  fireants_digest   = var.fireants_ami_digest
  eks_version       = var.eks_version
  region            = data.aws_region.current.region
  selection         = var.gpu_ami_selection
  alias             = var.gpu_ami_alias
}

resource "kubectl_manifest" "gpu-nodeclass" {
  server_side_apply = true
  force_conflicts   = true
  yaml_body = templatefile("${path.module}/helm-values/gpu-nodeclass.yaml",
    {
      karpenter_node_iam_role_name = module.karpenter_infra.node_iam_role_name
      eks_cluster_name             = var.cluster_name
      fastsurfer_ami_digest        = var.fastsurfer_ami_digest
      fireants_ami_digest          = var.fireants_ami_digest
      eks_version                  = var.eks_version
      gpu_ami_kind                 = module.gpu_ami.kind
      # Not coalesce(..., ""): coalesce skips empty strings as well as nulls, so
      # with no published AMI it has nothing to return and fails the plan.
      gpu_ami_id    = module.gpu_ami.ami_id == null ? "" : module.gpu_ami.ami_id
      gpu_ami_alias = var.gpu_ami_alias
  })

  depends_on = [
    helm_release.karpenter
  ]
}

resource "kubectl_manifest" "gpu-nodepool" {
  server_side_apply = true
  force_conflicts   = true
  yaml_body         = templatefile("${path.module}/helm-values/gpu-nodepool.yaml", { zones_list = local.zones_list })
  depends_on = [
    kubectl_manifest.gpu-nodeclass
  ]
}

# NodeOverlay (alpha) that adds nvidia.com/gpu to g6f fractional-GPU instances so
# Karpenter will provision them for GPU pods. Requires settings.featureGates.
# nodeOverlay=true (values-karpenter.yaml); depends on the helm release for the
# NodeOverlay CRD + feature gate, and on the nodepool it augments.
resource "kubectl_manifest" "gpu-nodeoverlay" {
  server_side_apply = true
  force_conflicts   = true
  yaml_body         = file("${path.module}/helm-values/gpu-nodeoverlay.yaml")
  depends_on = [
    kubectl_manifest.gpu-nodepool
  ]
}

# NodeOverlay (alpha) that makes Karpenter model 3 nvidia.com/gpu per node for the
# full-GPU families (g4dn/g5/g6/g6e), matching the device-plugin time-slicing
# replicas (gitops/apps/nvidia-device-plugin/values.yaml). Without it Karpenter
# still models 1 GPU/node and over-launches on a burst. Excludes g6f (its ~5.59 GiB
# slice fits only one FastSurfer pod). Keep this count in sync with
# sharing.timeSlicing.resources[].replicas in that chart's values.yaml —
# tests/argo/test_gpu_step_resources.py fails the build if they diverge.
resource "kubectl_manifest" "gpu-timeslice-nodeoverlay" {
  server_side_apply = true
  force_conflicts   = true
  yaml_body         = file("${path.module}/helm-values/gpu-timeslice-nodeoverlay.yaml")
  depends_on = [
    kubectl_manifest.gpu-nodepool
  ]
}

# NodeOverlay (alpha) that makes Karpenter model 4 nvidia.com/gpu per node for the
# gpu-dense-nodepool types (g5/g6/g6e 2xlarge), matching the device plugin's `dense`
# profile (#374). Weight 10 so it wins over gpu-timeslice-3x, which matches the same
# types by family.
#
# Ordering is gpu-nodepool -> this overlay -> gpu-dense-nodepool. The overlay must not
# land while gpu-nodepool can still launch these types (it would model them at 4 but
# they would advertise 3 there), and the dense pool should not exist before the overlay
# (it would briefly be modelled at 3 and over-launch).
resource "kubectl_manifest" "gpu-dense-timeslice-nodeoverlay" {
  server_side_apply = true
  force_conflicts   = true
  yaml_body         = file("${path.module}/helm-values/gpu-dense-timeslice-nodeoverlay.yaml")
  depends_on = [
    kubectl_manifest.gpu-nodepool
  ]
}

# Drought fallback pool at 4 slices per GPU (#374). No weight, so gpu-nodepool is
# preferred; see the header of gpu-dense-nodepool.yaml.
resource "kubectl_manifest" "gpu-dense-nodepool" {
  server_side_apply = true
  force_conflicts   = true
  yaml_body         = templatefile("${path.module}/helm-values/gpu-dense-nodepool.yaml", { zones_list = local.zones_list })
  depends_on = [
    kubectl_manifest.gpu-nodeclass,
    kubectl_manifest.gpu-dense-timeslice-nodeoverlay
  ]
}

resource "kubectl_manifest" "cpu-light-nodepool" {
  server_side_apply = true
  force_conflicts   = true
  yaml_body         = file("${path.module}/helm-values/cpu-light-nodepool.yaml")
  depends_on = [
    kubectl_manifest.nodeclass
  ]
}

resource "kubectl_manifest" "cpu-heavy-nodepool" {
  server_side_apply = true
  force_conflicts   = true
  yaml_body         = file("${path.module}/helm-values/cpu-heavy-nodepool.yaml")
  depends_on = [
    kubectl_manifest.nodeclass
  ]
}

resource "kubectl_manifest" "first-level-nodepool" {
  server_side_apply = true
  force_conflicts   = true
  yaml_body         = file("${path.module}/helm-values/first-level-nodepool.yaml")
  depends_on = [
    kubectl_manifest.nodeclass
  ]
}
