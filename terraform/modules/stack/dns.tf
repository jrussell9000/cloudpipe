data "aws_route53_zone" "brc" {
  name = var.domain
}

# ACM certificate for Cognito custom domain — must live in us-east-1
resource "aws_acm_certificate" "primary_us_east_1" {
  provider = aws.us_east_1

  domain_name = var.domain
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
    Region = "us-east-1"
  }
}

# DNS validation for the us-east-1 certificate
resource "aws_acm_certificate_validation" "primary_us_east_1" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.primary_us_east_1.arn
  validation_record_fqdns = [for record in aws_acm_certificate.primary_us_east_1.domain_validation_options : record.resource_record_name]

  timeouts {
    create = "8m"
  }
}

# ACM certificate for ALB TLS termination, in the deployment region (same region as the load balancer)
resource "aws_acm_certificate" "primary_regional" {
  domain_name = data.aws_route53_zone.brc.name
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
  certificate_arn         = aws_acm_certificate.primary_regional.arn
  validation_record_fqdns = [for record in aws_acm_certificate.primary_regional.domain_validation_options : record.resource_record_name]

  timeouts {
    create = "8m"
  }
}
