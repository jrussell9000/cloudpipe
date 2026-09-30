module "finops" {
  source = "../finops"

  eks_cluster            = module.eks
  account_id             = data.aws_caller_identity.current.account_id
  root_name              = var.name
  region                 = var.region
  hostname               = data.aws_route53_zone.brc.name
  certificate_arn        = aws_acm_certificate.primary_regional.arn
  vpc_id                 = module.vpc.vpc_id
  vpc_cidr               = var.vpc_cidr
  nat_gateway_ip         = module.vpc.nat_public_ips[0]
  inbound_prefix_list_id = var.uwmadison_prefix_list_id
  alb_group_annotations  = local.ui_alb_group_annotations
  # Provider aliases (such as aws.billing to us-east-1) are not automatically inherited (vs. default providers)
  providers = {
    aws         = aws         # Passes your default regional provider
    aws.billing = aws.billing # Passes the us-east-1 billing provider
  }
  depends_on = [module.addons]
}
