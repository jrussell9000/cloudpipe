# Address moves for the optional-domain change (design D1 and D6 of
# openspec/changes/optional-domain-and-cognito-auth).
#
# Every resource that only exists to publish a web UI now carries
# `count = local.publish_uis ? 1 : 0`, so its address gained an index:
# `aws_foo.bar` became `aws_foo.bar[0]`. Terraform does not infer that. Without
# the block below it reads the old address as gone and the new one as absent,
# and plans a destroy and a create for the same live object — which for the ACM
# certificate and the WAF web ACL means a real outage of the published UIs.
#
# These live in this module rather than the caller's root because the resources
# do. The root's moved_to_stack.tf carries the different case: resources that
# moved between modules.
#
# The resources published-mode-only resources depend on are indexed too, so
# there is no partial state to reason about: in published mode every count is 1
# and every address below exists; without a domain every count is 0 and nothing
# here is created. A test asserts this file stays in step with the `count` lines
# (tests/test_terraform_optional_domain.py), because a `count` added without a
# move is the destructive mistake.

# ── DNS and TLS (dns.tf) ───────────────────────────────────────────────────────
# data.aws_route53_zone.brc needs no block: a data source is re-read on every
# plan rather than tracked as a managed object.

moved {
  from = aws_acm_certificate.primary_regional
  to   = aws_acm_certificate.primary_regional[0]
}

moved {
  from = aws_acm_certificate_validation.primary_regional
  to   = aws_acm_certificate_validation.primary_regional[0]
}

# ── The shared UI ALB's security group (ui_alb.tf) ─────────────────────────────

moved {
  from = aws_security_group.ui_alb
  to   = aws_security_group.ui_alb[0]
}

moved {
  from = aws_vpc_security_group_ingress_rule.ui_alb_https
  to   = aws_vpc_security_group_ingress_rule.ui_alb_https[0]
}

moved {
  from = aws_vpc_security_group_ingress_rule.ui_alb_http
  to   = aws_vpc_security_group_ingress_rule.ui_alb_http[0]
}

moved {
  from = aws_vpc_security_group_egress_rule.ui_alb
  to   = aws_vpc_security_group_egress_rule.ui_alb[0]
}

# ── The UI WAF and its log destination (waf.tf) ────────────────────────────────

moved {
  from = aws_wafv2_web_acl.ui_alb
  to   = aws_wafv2_web_acl.ui_alb[0]
}

moved {
  from = aws_wafv2_web_acl_logging_configuration.ui_alb
  to   = aws_wafv2_web_acl_logging_configuration.ui_alb[0]
}

moved {
  from = aws_kms_key.ui_waf_logs
  to   = aws_kms_key.ui_waf_logs[0]
}

moved {
  from = aws_kms_alias.ui_waf_logs
  to   = aws_kms_alias.ui_waf_logs[0]
}

moved {
  from = aws_cloudwatch_log_group.ui_waf
  to   = aws_cloudwatch_log_group.ui_waf[0]
}

# ── The ArgoCD ingress (argocd.tf) ─────────────────────────────────────────────

moved {
  from = kubernetes_ingress_v1.argocd_ingress
  to   = kubernetes_ingress_v1.argocd_ingress[0]
}

# ── external-dns's IAM binding (addons.tf) ─────────────────────────────────────
# A whole module, so this move carries every resource inside it.

moved {
  from = module.external_dns_pod_identity
  to   = module.external_dns_pod_identity[0]
}
