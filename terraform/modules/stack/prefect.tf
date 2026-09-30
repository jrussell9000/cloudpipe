################################################################################
# Prefect
################################################################################

module "prefect" {
  source = "../prefect"

  # Cluster / network
  cluster_name           = var.name
  vpc_id                 = module.vpc.vpc_id
  vpc_cidr               = var.vpc_cidr
  public_subnets         = module.vpc.public_subnets
  region                 = var.region
  inbound_prefix_list_id = var.uwmadison_prefix_list_id
  nat_gateway_ip         = module.vpc.nat_public_ips[0]

  # DNS / TLS
  route53_zone_name = data.aws_route53_zone.brc.name
  certificate_arn   = aws_acm_certificate.primary_regional.arn

  # Prefect config
  namespace = var.prefect_namespace
  bucket    = var.globus_s3_destination_bucket
  # Same bucket the argo-workflows module receives (argowf.tf) — the
  # kubecost-cost-scraper flow runs on the Prefect worker and writes here.
  metrics_bucket = aws_s3_bucket.metrics.id
  work_pool      = var.prefect_work_pool

  # Shared internal UI ALB (ui_alb.tf)
  alb_group_annotations = local.ui_alb_group_annotations

  # Database
  db_name     = var.prefect_db_name
  db_username = var.prefect_db_username

  crds_available = var.crds_available
}

# ------------------------------------------------------------------------------
# Prefect oauth2-proxy — credentials for the proxy to authenticate against Dex
# ------------------------------------------------------------------------------
resource "random_password" "prefect_oauth2_proxy_cookie" {
  length  = 32
  special = false
}

resource "kubernetes_secret_v1" "prefect_oauth2_proxy" {
  metadata {
    name      = "prefect-oauth2-proxy"
    namespace = var.prefect_namespace
  }
  data = {
    client-id     = "prefect"
    client-secret = random_password.prefect_dex_client.result
    cookie-secret = random_password.prefect_oauth2_proxy_cookie.result
  }
  depends_on = [module.prefect]
}
