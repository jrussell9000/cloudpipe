module "karpenter_infra" {
  source = "terraform-aws-modules/eks/aws//modules/karpenter"

  cluster_name                      = var.cluster_name
  create_pod_identity_association   = true
  node_iam_role_additional_policies = var.node_iam_role_additional_policies
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

resource "kubectl_manifest" "gpu-nodeclass" {
  server_side_apply = true
  force_conflicts   = true
  yaml_body = templatefile("${path.module}/helm-values/gpu-nodeclass.yaml",
    {
      karpenter_node_iam_role_name = module.karpenter_infra.node_iam_role_name
      eks_cluster_name             = var.cluster_name
      fastsurfer_ami_tag           = var.fastsurfer_ami_tag
      fireants_ami_tag             = var.fireants_ami_tag
      eks_version                  = var.eks_version
  })

  depends_on = [
    helm_release.karpenter
  ]
}

resource "kubectl_manifest" "gpu-nodepool" {
  server_side_apply = true
  force_conflicts   = true
  yaml_body         = file("${path.module}/helm-values/gpu-nodepool.yaml")
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
