#################################################################################
# EKS/K8s Addons
#################################################################################

module "addons" {
  source = "../addons"

  cluster_name = module.eks.cluster_name
  vpc_id       = module.vpc.vpc_id
  region       = var.region
  eks_cluster  = module.eks

  # Null in port-forward mode: cert-manager's only use for a hosted zone is the
  # ACME DNS-01 solver, and without a domain there is no public certificate to
  # issue.
  route53_zone_arn = one(data.aws_route53_zone.brc[*].arn)
}

# The amazon-cloudwatch-observability pod identity is gone with the addon it
# bound. It granted the `cloudwatch-agent` service account in the
# `amazon-cloudwatch` namespace permission to publish ContainerInsights
# metrics; with the addon removed there is no such service account to bind.
# See the removal rationale in eks.tf (`addons` block).


# EBS CSI DRIVER
# Required by Kubecost — Helm release managed by ArgoCD (gitops/apps/aws-ebs-csi-driver/)
# Pod Identity Association keeps the IAM binding in Terraform
module "aws_ebs_csi_pod_identity" {
  source                    = "terraform-aws-modules/eks-pod-identity/aws"
  name                      = "aws-ebs-csi-pod-identity"
  attach_aws_ebs_csi_policy = true
  aws_ebs_csi_kms_arns      = ["arn:aws:kms:*:*:key/*"]
  associations = {
    cloudpipe = {
      cluster_name    = "${var.name}"
      namespace       = "aws-ebs-csi-driver"
      service_account = "ebs-csi-controller-sa"
    }
  }
}

# EXTERNAL DNS
# Helm release managed by ArgoCD (gitops/apps/external-dns/)
# Pod Identity Association keeps the IAM binding in Terraform; SA is created by Helm
#
# Published mode only — external-dns exists to write the UI hostnames into the
# hosted zone, and port-forward mode publishes no hostnames. Note that the
# ArgoCD ApplicationSet deploys every directory under gitops/apps, so switching
# this binding off does not stop the deployment itself; excluding the app is
# task 3.2 of openspec/changes/optional-domain-and-cognito-auth.
module "external_dns_pod_identity" {
  count = local.publish_uis ? 1 : 0

  source = "terraform-aws-modules/eks-pod-identity/aws"

  name                          = "external-dns"
  attach_external_dns_policy    = true
  external_dns_hosted_zone_arns = [data.aws_route53_zone.brc[0].arn]

  associations = {
    cloudpipe = {
      cluster_name    = module.eks.cluster_name
      namespace       = "external-dns"
      service_account = "external-dns"
    }
  }
}
