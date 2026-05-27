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
