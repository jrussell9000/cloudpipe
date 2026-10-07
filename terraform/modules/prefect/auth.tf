################################################################################
# API basic auth (#636)
#
# oauth2-proxy puts SSO in front of the Prefect UI but lets `^/api/` through
# (skip-auth-regex in gitops/apps/prefect/values.yaml): the CLI and the worker
# cannot do a browser sign-in. Without this, that bypass was an unauthenticated
# API reachable from every pod in the VPC — the UI ALB and oauth2-proxy's
# NetworkPolicy both admit the whole VPC CIDR — and the API sets the queue's
# Variables and creates flow runs that submit Argo workflows.
#
# Prefect's own basic auth closes it: the server requires
# PREFECT_SERVER_API_AUTH_STRING on /api/ (GET /health and /ready stay open for
# the probes), and every client sends PREFECT_API_AUTH_STRING. The prefect-server
# and prefect-worker charts read this Secret through basicAuth.existingSecret,
# which requires the key `auth-string`; the worker passes the value on to the
# flow-run pods it creates. The value never appears in gitops/.
#
# Retrieve it locally (pixi run -e ops does this for you):
#   kubectl -n prefect get secret prefect-api-auth -o jsonpath='{.data.auth-string}' | base64 -d
# Rotate: terraform apply -replace=module.stack.module.prefect.random_password.api_auth,
# then restart prefect-server and prefect-worker (they read it at pod start).
################################################################################

resource "random_password" "api_auth" {
  length  = 40
  special = false
}

resource "kubernetes_secret_v1" "api_auth" {
  metadata {
    name      = "prefect-api-auth"
    namespace = var.namespace
  }

  data = {
    "auth-string" = "cloudpipe:${random_password.api_auth.result}"
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}
