################################################################################
# Namespace — created by ArgoCD (CreateNamespace=true in ApplicationSet);
# referenced here so dependent resources have an explicit handle.
################################################################################

data "kubernetes_namespace_v1" "this" {
  metadata {
    name = var.namespace
  }
}

################################################################################
# Ingress — ALB with TLS termination and HTTP→HTTPS redirect
################################################################################

resource "kubernetes_ingress_v1" "this" {
  count = var.publish_ui ? 1 : 0

  metadata {
    name      = "prefect-ingress"
    namespace = var.namespace
    # ALB-level settings (scheme, group, security group, listeners, TLS policy,
    # attributes) come from the caller, shared by every member of the ingress
    # group. They must be identical across members; do not override them here.
    annotations = merge(var.alb_group_annotations, {
      "external-dns.alpha.kubernetes.io/hostname" = var.ui_host

      "alb.ingress.kubernetes.io/target-type"     = "ip"
      "alb.ingress.kubernetes.io/certificate-arn" = var.certificate_arn

      "alb.ingress.kubernetes.io/backend-protocol"     = "HTTP"
      "alb.ingress.kubernetes.io/healthcheck-protocol" = "HTTP"
      "alb.ingress.kubernetes.io/healthcheck-path"     = "/ping"
    })
  }

  spec {
    ingress_class_name = "alb"

    rule {
      host = var.ui_host
      http {
        path {
          path      = "/*"
          path_type = "ImplementationSpecific"
          backend {
            service {
              # fullnameOverride: "prefect-oauth2-proxy" is set in the Helm values so
              # the service name is predictable regardless of the Helm release name.
              name = "prefect-oauth2-proxy"
              port {
                number = 4180
              }
            }
          }
        }
      }
    }
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}
