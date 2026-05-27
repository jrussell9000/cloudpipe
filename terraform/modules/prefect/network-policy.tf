################################################################################
# Kubernetes NetworkPolicy — prefect namespace
# Default-deny with explicit allow rules.
################################################################################

resource "kubernetes_network_policy_v1" "default_deny" {
  metadata {
    name      = "default-deny-all"
    namespace = var.namespace
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}

# Allow intra-namespace traffic so server and worker can communicate directly.
resource "kubernetes_network_policy_v1" "intra_namespace" {
  metadata {
    name      = "allow-intra-namespace"
    namespace = var.namespace
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
    ingress {
      from {
        pod_selector {}
      }
    }
    egress {
      to {
        pod_selector {}
      }
    }
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}

# DNS
resource "kubernetes_network_policy_v1" "egress_dns" {
  metadata {
    name      = "allow-egress-dns"
    namespace = var.namespace
  }
  spec {
    pod_selector {}
    policy_types = ["Egress"]
    egress {
      ports {
        port     = "53"
        protocol = "UDP"
      }
      ports {
        port     = "53"
        protocol = "TCP"
      }
      to {
        ip_block {
          cidr = "0.0.0.0/0"
        }
      }
    }
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}

# HTTPS egress — Kubernetes API server, AWS APIs (S3, SQS, Secrets Manager), GitHub
resource "kubernetes_network_policy_v1" "egress_https" {
  metadata {
    name      = "allow-egress-https"
    namespace = var.namespace
  }
  spec {
    pod_selector {}
    policy_types = ["Egress"]
    egress {
      ports {
        port     = "443"
        protocol = "TCP"
      }
      to {
        ip_block {
          cidr = "0.0.0.0/0"
        }
      }
    }
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}

# PostgreSQL egress to Prefect DB
resource "kubernetes_network_policy_v1" "egress_rds" {
  metadata {
    name      = "allow-egress-rds"
    namespace = var.namespace
  }
  spec {
    pod_selector {}
    policy_types = ["Egress"]
    egress {
      ports {
        port     = "5432"
        protocol = "TCP"
      }
      to {
        ip_block {
          cidr = var.vpc_cidr
        }
      }
    }
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}

# Allow ingress to Prefect server (port 4200) from oauth2-proxy only.
# Direct VPC access is removed — all external traffic must flow through oauth2-proxy
# to enforce SSO authentication.
resource "kubernetes_network_policy_v1" "ingress_server" {
  metadata {
    name      = "allow-ingress-prefect-server"
    namespace = var.namespace
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "prefect-server"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "4200"
        protocol = "TCP"
      }
      from {
        pod_selector {
          match_labels = {
            "app.kubernetes.io/name" = "oauth2-proxy"
          }
        }
      }
    }
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}

# Allow ingress to oauth2-proxy (port 4180) from ALB ENIs.
resource "kubernetes_network_policy_v1" "ingress_oauth2_proxy" {
  metadata {
    name      = "allow-ingress-oauth2-proxy"
    namespace = var.namespace
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "oauth2-proxy"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "4180"
        protocol = "TCP"
      }
      from {
        ip_block {
          cidr = var.vpc_cidr
        }
      }
    }
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}
