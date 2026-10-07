module "finops" {
  source = "../finops"

  eks_cluster           = module.eks
  account_id            = data.aws_caller_identity.current.account_id
  root_name             = var.name
  region                = var.region
  publish_ui            = local.publish_uis
  ui_host               = local.kubecost_url
  certificate_arn       = one(aws_acm_certificate.primary_regional[*].arn)
  alb_group_annotations = local.ui_alb_group_annotations
  # Provider aliases (such as aws.billing to us-east-1) are not automatically inherited (vs. default providers)
  providers = {
    aws         = aws         # Passes your default regional provider
    aws.billing = aws.billing # Passes the us-east-1 billing provider
  }
  depends_on = [module.addons]
}
