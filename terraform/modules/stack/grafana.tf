################################################################################
# Grafana SSO (Dex OIDC)
#
# Grafana authenticates against the ArgoCD-bundled Dex using its native
# generic_oauth. Two things are delivered into the grafana namespace:
#
#   1. grafana-sso        — the Dex static-client secret (sensitive)
#   2. grafana-oidc-config — non-sensitive values (domain, admin email) that the
#                            Grafana chart reads via envValueFrom and expands in
#                            grafana.ini with $__env{...}. This keeps var.domain
#                            and var.admin_netid as the single source of truth
#                            instead of hardcoding hostnames in the gitops values.
#
# The grafana namespace itself is created by the cluster-addons ApplicationSet
# (gitops/apps/grafana), so it is referenced here as a data source — these
# resources apply in the second bootstrap phase, after ArgoCD has synced.
################################################################################

data "kubernetes_namespace_v1" "grafana" {
  metadata {
    name = var.grafana_namespace
  }
}

resource "kubernetes_secret_v1" "grafana_sso" {
  metadata {
    name      = "grafana-sso"
    namespace = var.grafana_namespace
  }
  data = {
    clientID     = "grafana"
    clientSecret = random_password.grafana_dex_client.result
  }
  depends_on = [data.kubernetes_namespace_v1.grafana]
}

# Grafana's local admin account.
#
# The chart generates `adminPassword` with randAlphaNum when none is supplied,
# so it renders a DIFFERENT value every time. The Deployment carries a
# checksum/secret annotation over that Secret, so each Argo CD sync changed the
# pod template and started another rollout — which then deadlocked, because the
# new pod cannot attach the RWO EBS volume the old one still holds. Found
# 2026-09-17 at Deployment revision 702, with the same pod serving throughout.
#
# Owning the password here makes the rendered Secret stable (with
# admin.existingSecret set, the chart stops templating one at all) and keeps it
# out of git. The value lives in Terraform state, like every other password in
# this file.
#
# The local login form is an escape hatch only; normal access is SSO through
# Dex. Read the password with:
#   kubectl -n grafana get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d
resource "random_password" "grafana_admin" {
  length  = 32
  special = false
}

resource "kubernetes_secret_v1" "grafana_admin" {
  metadata {
    name      = "grafana-admin"
    namespace = var.grafana_namespace
  }
  data = {
    admin-user     = "admin"
    admin-password = random_password.grafana_admin.result
  }
  depends_on = [data.kubernetes_namespace_v1.grafana]
}

# Shared token between Grafana and its image renderer (gitops/apps/grafana).
#
# The renderer drives a headless Chromium to whatever URL a request names, and
# accepts any request carrying its token. Left unset, both sides fall back to
# the well-known default `-`. The chart's NetworkPolicy, which would otherwise
# admit only the Grafana pod, is inert here: the VPC CNI runs with
# --enable-network-policy=false. So the token is the only thing stopping any
# pod in the cluster from using the renderer to fetch internal URLs.
#
# Both Deployments read this Secret at start, so it must exist before Argo CD
# syncs the values that reference it — otherwise the Grafana pod itself stalls
# in CreateContainerConfigError.
resource "random_password" "grafana_renderer_token" {
  length  = 48
  special = false
}

resource "kubernetes_secret_v1" "grafana_renderer_token" {
  metadata {
    name      = "grafana-renderer-token"
    namespace = var.grafana_namespace
  }
  data = {
    token = random_password.grafana_renderer_token.result
  }
  depends_on = [data.kubernetes_namespace_v1.grafana]
}

resource "kubernetes_config_map_v1" "grafana_oidc_config" {
  metadata {
    name      = "grafana-oidc-config"
    namespace = var.grafana_namespace
  }
  data = {
    domain      = var.domain
    admin_email = "${var.admin_netid}@${var.institution_domain}"
  }
  depends_on = [data.kubernetes_namespace_v1.grafana]
}

################################################################################
# Grafana ALB Security Group — UNUSED since Grafana joined the shared internal
# `cloudpipe-ui` ALB (ui_alb.tf). Kept only so plan 012 §5's rollback can
# recreate the per-app ALB; removed in plan 012 §3.4, once no old ALB remains.
#
# Restricts inbound access to the UW-Madison prefix list, matching the pattern
# already used for the ArgoCD and Argo Workflows ALBs (see aws_security_group
# "argocd_lb" in argocd.tf). Grafana's ingress is defined in the GitOps-synced
# gitops/apps/grafana/values.yaml rather than as a Terraform-managed Kubernetes
# resource, so it can't reference this security group's ID directly the way
# ArgoCD's and Argo Workflows' Terraform-managed ingresses do. Instead this SG
# is given an explicit Name tag, and referenced by that name (not ID) from the
# security-groups annotation in values.yaml — the AWS Load Balancer Controller
# accepts either.
################################################################################

resource "aws_security_group" "grafana_lb" {
  name_prefix = "grafana-alb-"
  description = "Controls inbound access to the Grafana ALB."
  vpc_id      = module.vpc.vpc_id
  tags        = { Name = "grafana-alb-sg" }
}

resource "aws_vpc_security_group_ingress_rule" "grafana_lb_https" {
  security_group_id = aws_security_group.grafana_lb.id
  description       = "HTTPS from UW Madison prefix list"
  prefix_list_id    = var.uwmadison_prefix_list_id
  from_port         = 443
  ip_protocol       = "tcp"
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "grafana_lb_http" {
  security_group_id = aws_security_group.grafana_lb.id
  description       = "HTTP from UW Madison prefix list (redirected to HTTPS)"
  prefix_list_id    = var.uwmadison_prefix_list_id
  from_port         = 80
  ip_protocol       = "tcp"
  to_port           = 80
}

# Allow access via the AWS Client VPN too (source-NATs to the VPC CIDR, same
# as the EKS API server rule in vpn.tf) — lets a single VPN connection reach
# both the cluster API and this ALB, without also requiring the UW-Madison VPN.
resource "aws_vpc_security_group_ingress_rule" "grafana_lb_https_client_vpn" {
  security_group_id = aws_security_group.grafana_lb.id
  description       = "HTTPS from AWS Client VPN (source-NATs to VPC CIDR)"
  cidr_ipv4         = var.vpc_cidr
  from_port         = 443
  ip_protocol       = "tcp"
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "grafana_lb_http_client_vpn" {
  security_group_id = aws_security_group.grafana_lb.id
  description       = "HTTP from AWS Client VPN (redirected to HTTPS)"
  cidr_ipv4         = var.vpc_cidr
  from_port         = 80
  ip_protocol       = "tcp"
  to_port           = 80
}

# The Client VPN CIDR rule above only covers traffic to VPC-internal
# destinations (where the VPN endpoint's own SNAT applies). Traffic from a
# full-tunnel VPN client to this ALB's *public* IP instead hairpins out
# through the VPC's NAT gateway and back in over the internet, presenting the
# NAT gateway's EIP as the source — so that EIP needs its own trust rule too.
resource "aws_vpc_security_group_ingress_rule" "grafana_lb_https_nat" {
  security_group_id = aws_security_group.grafana_lb.id
  description       = "HTTPS from VPC NAT gateway (full-tunnel VPN clients hairpin through here)"
  cidr_ipv4         = "${module.vpc.nat_public_ips[0]}/32"
  from_port         = 443
  ip_protocol       = "tcp"
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "grafana_lb_http_nat" {
  security_group_id = aws_security_group.grafana_lb.id
  description       = "HTTP from VPC NAT gateway (redirected to HTTPS)"
  cidr_ipv4         = "${module.vpc.nat_public_ips[0]}/32"
  from_port         = 80
  ip_protocol       = "tcp"
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "grafana_lb" {
  security_group_id = aws_security_group.grafana_lb.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
