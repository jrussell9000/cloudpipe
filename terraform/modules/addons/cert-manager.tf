# CERTIFICATE MANAGER
# Helm release is managed by ArgoCD (gitops/apps/cert-manager/)
module "cert_manager_pod_identity" {
  source = "terraform-aws-modules/eks-pod-identity/aws"

  name = "cert-manager"

  attach_cert_manager_policy    = true
  cert_manager_hosted_zone_arns = [var.route53_zone_arn]

  # Pod Identity Associations
  associations = {
    cloudpipe = {
      cluster_name = var.cluster_name
      # These are the default namespace and SA in cert-manager
      namespace       = "cert-manager"
      service_account = "cert-manager"
    }
  }
}
