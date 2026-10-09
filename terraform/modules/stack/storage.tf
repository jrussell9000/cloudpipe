#---------------------------------------------------------------
# Storage Class - EBS (gp3, encrypted, default)
#---------------------------------------------------------------
# Required by Kubecost — also managed in gitops/apps/cluster-config/
# `kubectl_manifest`, not `kubernetes_storage_class_v1` (#719). The typed
# resource issues a CREATE and fails with `already exists` when ArgoCD has
# already synced this from gitops/apps/cluster-config — which on a fresh install
# it always has, because install.sh brings ArgoCD up in Phase 5 and applies in
# Phase 6. kubectl_manifest applies instead, so it adopts.
#
# Terraform's copy exists only so the StorageClass is present before ArgoCD is
# bootstrapped; ArgoCD owns the live state afterwards and stamps its tracking-id
# annotation on every sync, which `ignore_fields` keeps out of the diff.
resource "kubectl_manifest" "ebs_csi_encrypted_gp3_storage_class" {
  ignore_fields = ["metadata.annotations.argocd\\.argoproj\\.io/tracking-id"]

  yaml_body = <<-YAML
    apiVersion: storage.k8s.io/v1
    kind: StorageClass
    metadata:
      name: ebs-sc
      annotations:
        storageclass.kubernetes.io/is-default-class: "true"
    provisioner: ebs.csi.aws.com
    reclaimPolicy: Delete
    allowVolumeExpansion: true
    volumeBindingMode: WaitForFirstConsumer
    parameters:
      encrypted: "true"
      type: gp3
  YAML
}
