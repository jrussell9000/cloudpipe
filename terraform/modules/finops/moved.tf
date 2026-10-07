# The Kubecost Ingress became conditional on var.publish_ui (design D1 of
# openspec/changes/optional-domain-and-cognito-auth), so its address gained an
# index. Without this block Terraform plans a destroy and a create of the same
# Ingress, which removes the host rule from the shared ALB in between.
moved {
  from = kubectl_manifest.kubecost_ingress
  to   = kubectl_manifest.kubecost_ingress[0]
}
