################################################################################
# Kubernetes NetworkPolicy — argo-workflows namespace
# NIST 800-171 H4: SC-7 Boundary Protection
#
# Default-deny with explicit allow rules. Requires the VPC CNI Network Policy
# Controller addon (enableNetworkPolicy = "true"), which is already set in eks.tf.
################################################################################

# Default deny all ingress and egress traffic.
resource "kubernetes_network_policy_v1" "argo_workflows_default_deny" {
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

# Allow egress on UDP and TCP 53 for DNS.
# Pods resolve names through the kube-dns ClusterIP (172.20.0.10), which lives
# in the EKS service CIDR — not the VPC CIDR and not selectable by namespace.
# A namespace_selector only matches pod IPs and silently breaks external DNS
# lookups (e.g. ssm.<YOUR_AWS_REGION>.amazonaws.com). Allowing port 53 to any
# destination is safe; the dedicated port is restriction enough.
resource "kubernetes_network_policy_v1" "argo_workflows_egress_dns" {
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

# Allow HTTPS egress (443) to both VPC-internal targets (kube-apiserver, RDS via
# Secrets Manager) and external targets (S3 via Gateway endpoint).
#
# The S3 VPC endpoint is a Gateway type (route-table based), so from a NetworkPolicy
# perspective the destination IPs are still S3's public ranges. Restricting to
# vpc_cidr alone would block S3 access. 0.0.0.0/0:443 is required until the S3
# Gateway endpoint is replaced with an Interface endpoint (M5 finding).
resource "kubernetes_network_policy_v1" "argo_workflows_egress_https" {
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

# Allow egress to RDS PostgreSQL (5432) within the VPC.
# The workflow-controller uses this for workflow persistence.
resource "kubernetes_network_policy_v1" "argo_workflows_egress_rds" {
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

# Allow ingress to the Argo Workflows server on port 2746.
# Source is the VPC CIDR, which covers:
#   - ALB ENI IPs in the public subnets (health checks, forwarded user traffic)
#   - Any in-VPC operator kubectl port-forward
resource "kubernetes_network_policy_v1" "argo_workflows_ingress_server" {
  metadata {
    name      = "allow-ingress-argo-server"
    namespace = var.namespace
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "argo-workflows-server"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "2746"
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

# Allow ingress to the workflow-controller Prometheus metrics endpoint (9090).
# TODO: Tighten to a namespace_selector once the Prometheus scraper namespace is known.
resource "kubernetes_network_policy_v1" "argo_workflows_ingress_metrics" {
  metadata {
    name      = "allow-ingress-controller-metrics"
    namespace = var.namespace
  }
  spec {
    pod_selector {
      match_labels = {
        "app" = "workflow-controller"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "9090"
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

# Allow kubelet liveness probe to workflow-controller on port 6060.
# Probe traffic originates from the node IP (host network) and is blocked by
# default-deny-all without this rule.
resource "kubernetes_network_policy_v1" "argo_workflows_ingress_controller_probe" {
  metadata {
    name      = "allow-ingress-controller-probe"
    namespace = var.namespace
  }
  spec {
    pod_selector {
      match_labels = {
        "app" = "workflow-controller"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "6060"
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
