locals {
  name   = var.name
  region = var.region

  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  # Derive the VPC DNS resolver from the VPC CIDR (always base address + 2 in AWS).
  # Use this instead of hardcoding 10.0.0.2 so changes to var.vpc_cidr propagate automatically.
  vpc_dns_resolver = cidrhost(var.vpc_cidr, 2)

  # Service URLs derived from var.domain.
  # Change var.domain once and all hostnames update together.
  argocd_url   = "argocd.${var.domain}"
  argo_url     = "argo.${var.domain}"
  prefect_url  = "prefect.${var.domain}"
  grafana_url  = "grafana.${var.domain}"
  kubecost_url = "kubecost.${var.domain}"
  vpn_url      = "vpn.${var.domain}"
}
