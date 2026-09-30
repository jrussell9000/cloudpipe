################################################################################
# Shared internal ALB security group — the web UIs (plan 012)
#
# ArgoCD, Argo Workflows, Prefect, Kubecost and Grafana share ONE internal ALB
# through the AWS Load Balancer Controller IngressGroup `cloudpipe-ui`. In an
# IngressGroup `security-groups` is an exclusive, group-level annotation: every
# member must carry a byte-identical value or the controller refuses to reconcile
# the whole group. So every member references this SG by its Name tag, not its
# ID — Grafana's ingress lives in GitOps-synced values.yaml and cannot read a
# Terraform ID, and the others must match it. The controller resolves the Name
# tag within the cluster VPC.
#
# Trusts the VPC CIDR only. An internal ALB has only private addresses, so
# nothing outside the VPC can reach it regardless of this SG; the two paths in
# both source from inside it:
#   - operators over Cloudflare WARP arrive from cloudflared pods (the tunnel
#     routes the private subnets, and the Access application gates who);
#   - in-cluster callers — Argo Workflows, Prefect and Grafana reaching Dex at
#     argocd.<domain> — are pods on VPC-CIDR IPs (no custom pod networking).
# The Client VPN also source-NATs into the VPC CIDR, which keeps it a working
# fallback until it is decommissioned.
#
# No campus prefix list and no NAT EIP rules, unlike the per-app ALB SGs this
# replaces: nothing reaches a private address over the internet or hairpins
# through the NAT gateway.
#
# The five per-app SGs stay until the old ALBs are gone (plan 012 §3.4) — they
# are what a rollback needs.
################################################################################

# The ALB-level annotations every member of the group carries, defined ONCE.
# These are the controller's *exclusive* group settings (plus listen-ports,
# which merges but is kept identical for clarity): if any member disagrees on
# any of them by a single byte, the whole group stops reconciling.
#
# The Terraform members — ArgoCD below, and the argo-workflows, prefect and
# finops modules — merge this map into their annotations, so they cannot drift
# from each other. Grafana's ingress is GitOps-managed and hardcodes the same
# values in gitops/apps/grafana/values.yaml; the precondition on
# kubernetes_ingress_v1.argocd_ingress (argocd.tf) fails the plan if it
# disagrees, so a drift is caught before it reaches the controller.
#
# Per-member settings stay per member: host rule, backend, healthcheck-path,
# target-type, backend-protocol, certificate-arn (merged across the group; the
# wildcard certificate covers every host) and the external-dns hostname.
#
# load-balancer-attributes: one access-log prefix for the whole group — the
# ALB is one load balancer, so one prefix. Per-app separation survives in each
# log line's domain_name field. idle_timeout is the ALB maximum so the UIs'
# long-lived streams (the Argo UI's Server-Sent Events, Grafana Explore, live
# panels) are not torn down by the 60 s default during quiet periods.
#
# deletion_protection answers Security Hub control ELB.6 (NIST.800-53.r5 CM-2,
# CM-3, SC-5(2)), which FAILED against this ALB under the NIST 800-53 Rev. 5
# standard — the only one subscribed in this account, and 800-171's parent. An
# earlier version of this comment attributed the finding to the 800-171 standard,
# which was never enabled here; see waf.tf for that correction and for its sibling
# ELB.16, and security_findings.tf for why no standard can be subscribed from here.
# READ THE TEARDOWN NOTE AT THE BOTTOM OF THIS FILE BEFORE DELETING INGRESSES.
#
# ssl-policy answers control ELB.17 (SC-8, SC-13, SC-23 — encryption in transit),
# and the `-Res-` is load-bearing. ELB.17's `sslPolicies` parameter is a fixed,
# explicitly NON-CUSTOMIZABLE allowlist of eight policies; the ALB default
# `ELBSecurityPolicy-TLS13-1-2-2021-06` is not among them, so it fails, and there
# is no way to widen the control to accept it. Do not "simplify" this back to the
# default or drop the `-Res-`: either reopens ELB.17.
#
# The control's own change log records "April 6, 2026 — Security Hub updated the
# parameter value for this control", so this almost certainly passed when plan 012
# chose the policy and was tightened underneath us. It is not an old mistake.
#
# What -Res- ("restricted") actually costs, from `describe-ssl-policies` on both:
# protocols are identical (TLS 1.2 + 1.3), the three TLS 1.3 ciphers are
# identical, and BOTH policies are already fully forward-secret (every cipher is
# ECDHE). The only delta is four CBC-mode TLS 1.2 ciphers dropped —
# ECDHE-{ECDSA,RSA}-AES{128,256}-SHA{256,384} — leaving AEAD-only (GCM and
# ChaCha20-Poly1305). So "restricted" here means AEAD-only, NOT "adds forward
# secrecy". Every client on this internal ALB (cloudflared, in-cluster Go/Python
# pods reaching Dex, browsers over the Client VPN) negotiates AEAD by preference;
# CBC is the fallback for stacks a decade older than anything on this path.
locals {
  ui_alb_group_annotations = {
    "alb.ingress.kubernetes.io/group.name"                          = "cloudpipe-ui"
    "alb.ingress.kubernetes.io/scheme"                              = "internal"
    "alb.ingress.kubernetes.io/security-groups"                     = aws_security_group.ui_alb.tags["Name"]
    "alb.ingress.kubernetes.io/manage-backend-security-group-rules" = "true"
    "alb.ingress.kubernetes.io/listen-ports"                        = "[{\"HTTP\": 80}, {\"HTTPS\": 443}]"
    "alb.ingress.kubernetes.io/ssl-redirect"                        = "443"
    "alb.ingress.kubernetes.io/ssl-policy"                          = "ELBSecurityPolicy-TLS13-1-2-Res-2021-06"
    "alb.ingress.kubernetes.io/load-balancer-attributes"            = "access_logs.s3.enabled=true,access_logs.s3.bucket=${aws_s3_bucket.access_logs.id},access_logs.s3.prefix=alb-ui,idle_timeout.timeout_seconds=4000,deletion_protection.enabled=true"
  }
}

resource "aws_security_group" "ui_alb" {
  name_prefix = "cloudpipe-ui-alb-"
  description = "Controls inbound access to the shared internal web-UI ALB."
  vpc_id      = module.vpc.vpc_id
  tags        = { Name = "cloudpipe-ui-alb-sg" }
}

resource "aws_vpc_security_group_ingress_rule" "ui_alb_https" {
  security_group_id = aws_security_group.ui_alb.id
  description       = "HTTPS from the VPC (WARP via cloudflared, in-cluster Dex callers, Client VPN)"
  cidr_ipv4         = var.vpc_cidr
  from_port         = 443
  ip_protocol       = "tcp"
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "ui_alb_http" {
  security_group_id = aws_security_group.ui_alb.id
  description       = "HTTP from the VPC (redirected to HTTPS)"
  cidr_ipv4         = var.vpc_cidr
  from_port         = 80
  ip_protocol       = "tcp"
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "ui_alb" {
  security_group_id = aws_security_group.ui_alb.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

################################################################################
# TEARDOWN NOTE — deletion protection changes how this ALB goes away
#
# `deletion_protection.enabled=true` is now in the group attributes above, so the
# load balancer controller can no longer delete the ALB during reconciliation.
# That is the point of the control, and it makes two ordinary-looking operations
# stall instead of working:
#
#   1. Removing the last Ingress from the `cloudpipe-ui` group, or changing
#      `group.name`, asks the controller to delete the ALB. Upstream documents
#      both "the controller will not be able to delete the ALB" (the
#      load-balancer-attributes note) and "any deletion protection of that ALB
#      will be ignored" (the group.name rename note) — the two notes disagree, so
#      do not rely on either. Assume the delete will be refused and clear
#      protection first.
#   2. `terraform destroy` of this root module does not touch the ALB at all,
#      because no resource here represents it. The ALB outlives the destroy, and
#      so does the WAF association (waf.tf).
#
# To take the ALB down deliberately, clear protection BEFORE removing the
# Ingresses — the ordering matters, because once the group is empty there is no
# Ingress left to carry a corrected annotation:
#
#   aws elbv2 modify-load-balancer-attributes \
#     --load-balancer-arn "$(aws elbv2 describe-load-balancers \
#         --query 'LoadBalancers[?starts_with(LoadBalancerName, `k8s-cloudpipeui`)].LoadBalancerArn' \
#         --output text)" \
#     --attributes Key=deletion_protection.enabled,Value=false
#
# Doing it through this file instead — flipping the attribute to `false` here and
# in gitops/apps/grafana/values.yaml, then applying and waiting for ArgoCD — is
# the cleaner route when there is time for it, because the controller re-asserts
# the annotation's value on every reconcile and will put protection back if only
# the AWS-side attribute was changed.
################################################################################
