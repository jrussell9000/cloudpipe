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
    # The Service CIDR peer is what actually carries oauth2-proxy → prefect-server
    # and worker → prefect-server: both dial `prefect-server.prefect.svc:4200`,
    # and egress is evaluated before kube-proxy's DNAT, so the destination is the
    # ClusterIP and no podSelector can match it. allow-ingress-prefect-server
    # still restricts who may arrive at 4200, because ingress is evaluated after
    # DNAT at the server pod.
    egress {
      to {
        pod_selector {}
      }
      to {
        ip_block {
          cidr = var.service_cidr
        }
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

# Allow egress to the EKS Pod Identity Agent (169.254.170.23:80).
#
# The worker and every flow-run pod it creates run as the prefect-worker service
# account (prefect/prefect.yaml sets service_account_name on all four
# deployments), which has a pod-identity association, so the webhook injects
# AWS_CONTAINER_CREDENTIALS_FULL_URI=http://169.254.170.23/v1/credentials. The
# agent is hostNetwork and exempt from policy, but this outbound hop is not.
# Without it the worker gets no AWS credentials and the cost scraper and queue
# manager both fail. IMDS (169.254.169.254) stays blocked.
resource "kubernetes_network_policy_v1" "egress_pod_identity" {
  metadata {
    name      = "allow-egress-pod-identity"
    namespace = var.namespace
  }
  spec {
    pod_selector {}
    policy_types = ["Egress"]
    egress {
      ports {
        port     = "80"
        protocol = "TCP"
      }
      to {
        ip_block {
          cidr = "169.254.170.23/32"
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

# There is deliberately no prefect-server probe rule. One was added on the
# assumption that the kubelet's httpGet on 4200 needed an explicit allow from the
# node IP, and it was the one rule here that had to widen a restriction to do so:
# 4200 is both the probe port and the API port, so admitting the node IP meant
# admitting the VPC CIDR, which under the VPC CNI is every pod in the cluster.
#
# The 2026-10-08 enforcement window disproved the assumption — the kubelet's probe
# traffic is exempt from NetworkPolicy (see docs/operations.md → Enabling
# NetworkPolicy enforcement) — so the rule's whole remaining effect was to admit
# the VPC CIDR, and removing it closed the API to everything outside this
# namespace. Verified under enforcement on 2026-10-09: a pod in `default` times
# out against prefect-server:4200, a pod in `prefect` connects.
#
# Inside the namespace, note that allow-ingress-prefect-server is not the only
# rule in play: allow-intra-namespace admits every pod here on every port, which
# is what the worker and the flow-run pods it creates actually use to reach
# PREFECT_API_URL (http://prefect-server.prefect.svc.cluster.local:4200/api).
# So the oauth2-proxy pod_selector above is not the last word on who may reach
# 4200 — it governs only what crosses into this namespace, where the answer is
# now nothing. The API's basic auth (#636) is the control for in-namespace
# clients, and the ALB target group points at 4180, so SSO still fronts every
# path from outside the VPC.

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
