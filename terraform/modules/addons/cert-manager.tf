# CERTIFICATE MANAGER
# Helm release is managed by ArgoCD (gitops/apps/cert-manager/)
module "cert_manager_pod_identity" {
  source = "terraform-aws-modules/eks-pod-identity/aws"

  name = "cert-manager"

  # No policy without a domain. Its only purpose is the ACME DNS-01 solver, and
  # the upstream module renders the zone list as a statement's resources, so an
  # empty list is a statement with none, which IAM refuses: "MalformedPolicyDocument:
  # Policy statement must contain resources" (second-account rerun, 2026-10-13).
  attach_cert_manager_policy = var.route53_zone_arn != null

  cert_manager_hosted_zone_arns = var.route53_zone_arn == null ? [] : [var.route53_zone_arn]

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
