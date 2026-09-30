################################################################################
# Argo Workflows
################################################################################

# ------------------------------------------------------------------------------
# Argo Workflows SSO — credentials for the server to authenticate against Dex
# ------------------------------------------------------------------------------
resource "kubernetes_secret_v1" "argo_workflows_sso" {
  metadata {
    name      = "argo-workflows-sso"
    namespace = var.argo_workflows_namespace
  }
  data = {
    clientID     = "argo-workflows"
    clientSecret = random_password.argo_workflows_dex_client.result
  }
  depends_on = [module.argo_workflows]
}

# ------------------------------------------------------------------------------
# Argo Workflows SSO RBAC — maps the admin NetID to an admin-level service account
# ------------------------------------------------------------------------------
resource "kubernetes_service_account_v1" "argo_admin" {
  metadata {
    name      = "argo-admin"
    namespace = var.argo_workflows_namespace
    annotations = {
      "workflows.argoproj.io/rbac-rule"            = "\"${var.admin_netid}@${var.institution_domain}\" == email"
      "workflows.argoproj.io/rbac-rule-precedence" = "1"
    }
  }
  depends_on = [module.argo_workflows]
}

resource "kubernetes_role_v1" "argo_admin_pod_logs" {
  metadata {
    name      = "argo-admin-pod-logs"
    namespace = var.argo_workflows_namespace
  }
  rule {
    api_groups = [""]
    resources  = ["pods", "pods/log"]
    verbs      = ["get", "list", "watch"]
  }
}

resource "kubernetes_role_binding_v1" "argo_admin_pod_logs" {
  metadata {
    name      = "argo-admin-pod-logs"
    namespace = var.argo_workflows_namespace
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.argo_admin_pod_logs.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.argo_admin.metadata[0].name
    namespace = var.argo_workflows_namespace
  }
}

resource "kubernetes_secret_v1" "argo_admin_token" {
  metadata {
    name      = "argo-admin.service-account-token"
    namespace = var.argo_workflows_namespace
    annotations = {
      "kubernetes.io/service-account.name" = kubernetes_service_account_v1.argo_admin.metadata[0].name
    }
  }
  type       = "kubernetes.io/service-account-token"
  depends_on = [kubernetes_service_account_v1.argo_admin]
}

resource "kubernetes_cluster_role_binding_v1" "argo_admin_sso" {
  metadata {
    name = "argo-admin-sso"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = "argo-workflows-admin"
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.argo_admin.metadata[0].name
    namespace = var.argo_workflows_namespace
  }
}

# Allow the Prefect worker SA to list/get Argo workflows (needed for the queue
# managers' ConcurrencyGate, which lists workflows to count active ones)
resource "kubernetes_role_binding_v1" "prefect_worker_argo_view" {
  metadata {
    name      = "prefect-worker-argo-view"
    namespace = var.argo_workflows_namespace
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = "argo-workflows-view"
  }
  subject {
    kind      = "ServiceAccount"
    name      = "prefect-worker"
    namespace = var.prefect_namespace
  }
}

# Allow the Prefect worker SA to submit workflows directly (needed for submit())
resource "kubernetes_role_v1" "prefect_worker_argo_submit" {
  metadata {
    name      = "prefect-worker-argo-submit"
    namespace = var.argo_workflows_namespace
  }
  rule {
    api_groups = ["argoproj.io"]
    resources  = ["workflows"]
    verbs      = ["create"]
  }
}

resource "kubernetes_role_binding_v1" "prefect_worker_argo_submit" {
  metadata {
    name      = "prefect-worker-argo-submit"
    namespace = var.argo_workflows_namespace
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.prefect_worker_argo_submit.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = "prefect-worker"
    namespace = var.prefect_namespace
  }
}

module "argo_workflows" {
  source = "../argo-workflows"

  # Cluster / network
  cluster_name           = var.name
  vpc_id                 = module.vpc.vpc_id
  vpc_cidr               = var.vpc_cidr
  private_subnets        = module.vpc.private_subnets
  nat_gateway_ip         = module.vpc.nat_public_ips[0]
  region                 = var.region
  inbound_prefix_list_id = var.uwmadison_prefix_list_id

  # DNS / TLS
  route53_zone_name = data.aws_route53_zone.brc.name
  certificate_arn   = aws_acm_certificate.primary_regional.arn

  # Argo Workflows config
  namespace      = var.argo_workflows_namespace
  bucket         = var.globus_s3_destination_bucket
  metrics_bucket = aws_s3_bucket.metrics.id

  # Private ECR: layer blobs are served from S3 via the gateway endpoint the
  # cluster already has, so image pulls no longer traverse the NAT gateway.
  # Rollback is this one line back to local.ecr_public_registry -- images are
  # dual-pushed to both registries during the migration, so both resolve.
  ecr_registry = local.ecr_private_registry

  # Database
  db_name       = var.argo_workflows_db_name
  db_username   = var.argo_workflows_db_username
  db_table_name = var.argo_workflows_db_table_name

  crds_available = var.crds_available

  log_bucket            = var.log_bucket
  alb_group_annotations = local.ui_alb_group_annotations
}
