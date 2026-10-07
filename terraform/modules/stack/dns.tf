# Published mode only — there is no zone to look up without a domain, and a
# lookup with `name = null` fails at plan. See local.publish_uis (locals.tf).
data "aws_route53_zone" "brc" {
  count = local.publish_uis ? 1 : 0

  name = var.domain
}

# A second, us-east-1 copy of this certificate existed for a Cognito custom
# domain that was never built; nothing attached it (ACM reported InUse: false)
# and it was removed on 2026-10-05. The Cognito pool planned in
# optional-domain-and-cognito-auth uses an AWS-hosted prefix domain, which
# needs no certificate.

# ACM certificate for ALB TLS termination, in the deployment region (same region as the load balancer)
resource "aws_acm_certificate" "primary_regional" {
  count = local.publish_uis ? 1 : 0

  domain_name = data.aws_route53_zone.brc[0].name
  subject_alternative_names = [
    "*.${var.domain}",
    "*.kubecost.${var.domain}",
    "*.argo.${var.domain}"
  ]

  validation_method = "DNS"
  key_algorithm     = "RSA_2048"

  options {
    certificate_transparency_logging_preference = "ENABLED"
  }

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name   = var.domain
    Region = var.region
  }
}

# DNS validation for the regional certificate
resource "aws_acm_certificate_validation" "primary_regional" {
  count = local.publish_uis ? 1 : 0

  certificate_arn         = aws_acm_certificate.primary_regional[0].arn
  validation_record_fqdns = [for record in aws_acm_certificate.primary_regional[0].domain_validation_options : record.resource_record_name]

  timeouts {
    create = "8m"
  }
}
