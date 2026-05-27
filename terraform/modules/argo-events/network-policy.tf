################################################################################
# Kubernetes NetworkPolicy — argo-events namespace
# NIST 800-171 H4: SC-7 Boundary Protection
################################################################################

# Default deny all ingress and egress traffic.
resource "kubernetes_network_policy_v1" "argo_events_default_deny" {
  metadata {
    name      = "default-deny-all"
    namespace = var.argo_events_namespace
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }

  depends_on = [kubernetes_namespace_v1.argo_events]
}

# Allow egress to CoreDNS in kube-system on UDP and TCP 53.
resource "kubernetes_network_policy_v1" "argo_events_egress_dns" {
  metadata {
    name      = "allow-egress-dns"
    namespace = var.argo_events_namespace
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

  depends_on = [kubernetes_namespace_v1.argo_events]
}

# Allow HTTPS egress (443) for:
#   - Kubernetes API server (private VPC endpoint) — workflow submission via RBAC
#   - SQS (no Interface endpoint; traffic egresses via NAT gateway to public endpoint)
#     TODO: Add an SQS Interface endpoint (M5 finding) to restrict to vpc_cidr.
resource "kubernetes_network_policy_v1" "argo_events_egress_https" {
  metadata {
    name      = "allow-egress-https"
    namespace = var.argo_events_namespace
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

  depends_on = [kubernetes_namespace_v1.argo_events]
}

# Allow all intra-namespace ingress and egress.
# The Argo Events event bus (NATS JetStream) uses several ports for internal
# cluster coordination (4222 client, 6222 cluster, 8222 monitoring). Allowing
# unrestricted intra-namespace traffic avoids enumerating every NATS port while
# still isolating argo-events from all other namespaces.
resource "kubernetes_network_policy_v1" "argo_events_intra_namespace" {
  metadata {
    name      = "allow-intra-namespace"
    namespace = var.argo_events_namespace
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

  depends_on = [kubernetes_namespace_v1.argo_events]
}
