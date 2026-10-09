################################################################################
# Forget the typed Kubernetes twins WITHOUT deleting them (#719)
#
# The five objects below moved from `kubernetes_*_v1` to `kubectl_manifest`,
# because the typed resources issue a CREATE and fail outright when ArgoCD has
# already synced the object from gitops/apps/cluster-config:
#
#   Error: roles.rbac.authorization.k8s.io
#   "external-secrets-cert-controller-patch" already exists
#
# A resource type change cannot be expressed with `moved` — Terraform sees a
# different resource, so a plain apply plans destroy-then-create. That plan is
# NOT safe to run:
#
#   * destroying `ebs-sc` deletes the cluster's DEFAULT StorageClass, so every
#     PVC created in the window goes Pending;
#   * destroying the cert-controller RBAC reinstates the 403 Forbidden -> 500
#     healthz loop those objects exist to prevent, which stops every
#     ExternalSecret from being admitted.
#
# `removed` with `destroy = false` drops them from state and leaves the live
# objects alone, so the `kubectl_manifest` resources adopt what is already
# there. Declarative on purpose: the alternative is five `terraform state rm`
# commands run by hand before the apply, which is unreviewable and
# unforgiving — forget one and the apply deletes a live object.
#
# These blocks are safe to delete once applied: `removed` is a no-op when
# neither address is in state. Leave them for one release so a stale checkout
# does not re-plan the destroy, then clean up. Unlike the `moved` blocks in
# ../../moved_to_stack.tf, these are NOT permanent.
################################################################################

removed {
  from = kubernetes_storage_class_v1.ebs_csi_encrypted_gp3_storage_class

  lifecycle {
    destroy = false
  }
}

removed {
  from = module.addons.kubernetes_cluster_role_v1.external_secrets_cert_controller_patch

  lifecycle {
    destroy = false
  }
}

removed {
  from = module.addons.kubernetes_cluster_role_binding_v1.external_secrets_cert_controller_patch_binding

  lifecycle {
    destroy = false
  }
}

removed {
  from = module.addons.kubernetes_role_v1.external_secrets_cert_controller_patch

  lifecycle {
    destroy = false
  }
}

removed {
  from = module.addons.kubernetes_role_binding_v1.external_secrets_cert_controller_patch

  lifecycle {
    destroy = false
  }
}
