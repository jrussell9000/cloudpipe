################################################################################
# Prefect
################################################################################

module "prefect" {
  source = "../prefect"

  # Cluster / network
  cluster_name   = var.name
  vpc_id         = module.vpc.vpc_id
  vpc_cidr       = var.vpc_cidr
  service_cidr   = module.eks.cluster_service_cidr
  public_subnets = module.vpc.public_subnets
  region         = var.region

  # UI access
  publish_ui      = local.publish_uis
  ui_host         = local.prefect_url
  certificate_arn = one(aws_acm_certificate.primary_regional[*].arn)

  # Prefect config
  namespace = var.prefect_namespace
  bucket    = var.globus_s3_destination_bucket
  # Same bucket the argo-workflows module receives (argowf.tf) — the
  # kubecost-cost-scraper flow runs on the Prefect worker and writes here.
  metrics_bucket = local.metrics_bucket
  work_pool      = var.prefect_work_pool

  # The batch gate starts the Globus host before listing through it (#652).
  # Empty when the ingress is not enabled, which the prefect module reads as "no
  # host to start" and attaches no ec2:StartInstances policy (iam.tf).
  #
  # `join` over the splat rather than a conditional on `one(...)`: an empty list
  # joins to "" with nothing to guard, where interpolating a null instance id
  # into the ARN template would fail the plan.
  globus_instance_arn = join("", [
    for instance_id in module.globus[*].instance_id :
    "arn:${local.partition}:ec2:${local.region}:${local.account_id}:instance/${instance_id}"
  ])

  # Shared internal UI ALB (ui_alb.tf)
  alb_group_annotations = local.ui_alb_group_annotations

  # Database
  db_name     = var.prefect_db_name
  db_username = var.prefect_db_username

  crds_available = var.crds_available
}

# ------------------------------------------------------------------------------
# Prefect oauth2-proxy — credentials for the proxy to authenticate against its
# issuer: the Cognito pool, or Dex in external mode (local.ui_oidc_*, cognito.tf)
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
    client-id     = local.ui_oidc_client_ids["prefect"]
    client-secret = local.ui_oidc_client_secrets["prefect"]
    cookie-secret = random_password.prefect_oauth2_proxy_cookie.result
  }
  depends_on = [module.prefect]
}
