#---------------------------------------------------------------
# Storage Class - EBS (gp3, encrypted, default)
#---------------------------------------------------------------
# Required by Kubecost — also managed in gitops/apps/cluster-config/
resource "kubernetes_storage_class_v1" "ebs_csi_encrypted_gp3_storage_class" {
  metadata {
    name = "ebs-sc"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" : "true"
    }
  }

  storage_provisioner    = "ebs.csi.aws.com"
  reclaim_policy         = "Delete"
  allow_volume_expansion = true
  volume_binding_mode    = "WaitForFirstConsumer"
  parameters = {
    encrypted = true
    type      = "gp3"
  }

  # ArgoCD (gitops/apps/cluster-config) manages the live state and stamps its
  # own tracking-id annotation on every sync; without this, every plan wants
  # to strip that annotation back off. Terraform's copy exists only so the
  # StorageClass is present before ArgoCD is bootstrapped.
  lifecycle {
    ignore_changes = [
      metadata[0].annotations["argocd.argoproj.io/tracking-id"],
    ]
  }
}
