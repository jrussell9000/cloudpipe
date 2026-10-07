################################################################################
# Grafana SSO (OIDC)
#
# Grafana authenticates with its native generic_oauth: against the Cognito pool
# directly in Cognito mode, against the ArgoCD-bundled Dex in external mode
# (local.ui_oidc_*, cognito.tf; endpoints, root_url and the admin binding in
# argocd.tf's override). The client ID and secret reach the grafana namespace
# as the grafana-sso Secret below.
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
    clientID     = local.ui_oidc_client_ids["grafana"]
    clientSecret = local.ui_oidc_client_secrets["grafana"]
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
# The local login form is an escape hatch only; normal access is SSO. Read the
# password with:
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

# The grafana-oidc-config ConfigMap that carried the domain and the admin
# address into values.yaml's $__env{...} expressions is gone (tasks 2.7 and 3.4
# of openspec/changes/optional-domain-and-cognito-auth). Every value it fed —
# root_url, the SSO endpoints, the admin binding — now comes from the
# ApplicationSet override in argocd.tf, which can express both access modes and
# both identity modes; a bare domain expanded at startup could not.
