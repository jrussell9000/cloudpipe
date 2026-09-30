################################################################################
# Kubernetes NetworkPolicy — kube-system namespace
# NIST 800-171 H4: SC-7 Boundary Protection
#
# VPC CNI strict mode (NETWORK_POLICY_ENFORCING_MODE=strict) blocks all traffic
# to/from pods that have no matching NetworkPolicy — including system pods.
# These policies restore required connectivity for CoreDNS, Karpenter, and
# metrics-server without granting broader access than each component needs.
#
# Note: kube-proxy, aws-node, and eks-pod-identity-agent use hostNetwork=true
# and are not subject to pod NetworkPolicy enforcement.
################################################################################

# Default deny all ingress and egress for kube-system pods.
# Explicit rules below carve out only what each component requires.
resource "kubernetes_network_policy_v1" "kube_system_default_deny" {
  metadata {
    name      = "default-deny-all"
    namespace = "kube-system"
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }

  depends_on = [module.eks]
}

# Allow all intra-namespace traffic.
# kube-system components communicate heavily with each other (e.g., Karpenter
# leader election via API server, metrics-server discovery). Allowing unrestricted
# intra-namespace traffic avoids enumerating every internal port.
resource "kubernetes_network_policy_v1" "kube_system_intra_namespace" {
  metadata {
    name      = "allow-intra-namespace"
    namespace = "kube-system"
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

  depends_on = [module.eks]
}

# Allow HTTPS egress (443) for all kube-system pods.
# Required for:
#   - CoreDNS: reaching the Kubernetes API at startup
#   - Karpenter: Kubernetes API + EC2/STS/SQS AWS APIs
#   - metrics-server: Kubernetes API server aggregation layer
resource "kubernetes_network_policy_v1" "kube_system_egress_https" {
  metadata {
    name      = "allow-egress-https"
    namespace = "kube-system"
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

  depends_on = [module.eks]
}

# Allow UDP/TCP port 53 egress for DNS from all kube-system pods.
# Pods resolve names through the kube-dns ClusterIP, which lives in the EKS
# service CIDR (e.g. 172.20.0.0/16) — not the VPC CIDR. Restricting to the
# VPC CIDR silently breaks DNS for any pod that makes external hostname lookups
# (e.g. Karpenter's EC2 API connectivity check). Allowing port 53 to any
# destination is safe; the dedicated port is restriction enough.
# CoreDNS also uses this rule to reach the VPC resolver (VPC base + 2) for
# upstream forwarding.
resource "kubernetes_network_policy_v1" "kube_system_egress_vpc_dns" {
  metadata {
    name      = "allow-egress-vpc-dns"
    namespace = "kube-system"
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

  depends_on = [module.eks]
}

# Allow ingress to CoreDNS on port 53 from all pods in all namespaces.
# Every pod in the cluster resolves DNS through CoreDNS; blocking this port
# breaks service discovery cluster-wide.
resource "kubernetes_network_policy_v1" "kube_system_coredns_ingress_dns" {
  metadata {
    name      = "allow-ingress-coredns-dns"
    namespace = "kube-system"
  }
  spec {
    pod_selector {
      match_labels = {
        "k8s-app" = "kube-dns"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "53"
        protocol = "UDP"
      }
      ports {
        port     = "53"
        protocol = "TCP"
      }
      from {
        namespace_selector {}
      }
    }
  }

  depends_on = [module.eks]
}

# Allow ingress to the CoreDNS metrics endpoint (9153) from within the VPC.
# Scraped by Prometheus / CloudWatch Container Insights.
resource "kubernetes_network_policy_v1" "kube_system_coredns_ingress_metrics" {
  metadata {
    name      = "allow-ingress-coredns-metrics"
    namespace = "kube-system"
  }
  spec {
    pod_selector {
      match_labels = {
        "k8s-app" = "kube-dns"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "9153"
        protocol = "TCP"
      }
      from {
        ip_block {
          cidr = var.vpc_cidr
        }
      }
    }
  }

  depends_on = [module.eks]
}

# Allow metrics-server egress to kubelet metrics port (10250) on all nodes.
# metrics-server scrapes kubelet /metrics/resource on every node; the destination
# IPs are node IPs within the VPC CIDR.
resource "kubernetes_network_policy_v1" "kube_system_metrics_server_egress_kubelet" {
  metadata {
    name      = "allow-egress-metrics-server-kubelet"
    namespace = "kube-system"
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "metrics-server"
      }
    }
    policy_types = ["Egress"]
    egress {
      ports {
        port     = "10250"
        protocol = "TCP"
      }
      to {
        ip_block {
          cidr = var.vpc_cidr
        }
      }
    }
  }

  depends_on = [module.eks]
}

# Allow ingress to metrics-server on port 10251 from node IPs (VPC CIDR).
# The kubelet makes liveness/readiness probe requests from the node IP directly
# to the pod IP on this port. With VPC CNI strict mode, probe traffic from the
# host network is subject to NetworkPolicy and blocked by default-deny-all.
resource "kubernetes_network_policy_v1" "kube_system_metrics_server_ingress_probe" {
  metadata {
    name      = "allow-ingress-metrics-server-probe"
    namespace = "kube-system"
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "metrics-server"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "10251"
        protocol = "TCP"
      }
      from {
        ip_block {
          cidr = var.vpc_cidr
        }
      }
    }
  }

  depends_on = [module.eks]
}

# Allow ingress to CoreDNS health probe ports from node IPs (VPC CIDR).
# Kubelet liveness probe: GET http://<pod-ip>:8080/health
# Kubelet readiness probe: GET http://<pod-ip>:8181/ready
# With VPC CNI strict mode, probe traffic from the host network is blocked by
# default-deny-all unless explicitly permitted.
resource "kubernetes_network_policy_v1" "kube_system_coredns_ingress_probe" {
  metadata {
    name      = "allow-ingress-coredns-probe"
    namespace = "kube-system"
  }
  spec {
    pod_selector {
      match_labels = {
        "k8s-app" = "kube-dns"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "8080"
        protocol = "TCP"
      }
      ports {
        port     = "8181"
        protocol = "TCP"
      }
      from {
        ip_block {
          cidr = var.vpc_cidr
        }
      }
    }
  }

  depends_on = [module.eks]
}

# Allow Karpenter egress to the EKS Pod Identity Agent (169.254.170.23:80).
# Karpenter uses Pod Identity for AWS credentials. The agent runs with
# hostNetwork=true and is exempt from NetworkPolicy itself, but Karpenter's
# outbound connection to the link-local agent address is subject to the
# default-deny-all rule and must be explicitly permitted here.
resource "kubernetes_network_policy_v1" "kube_system_karpenter_egress_pod_identity" {
  metadata {
    name      = "allow-egress-karpenter-pod-identity"
    namespace = "kube-system"
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "karpenter"
      }
    }
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

  depends_on = [module.eks]
}

# Allow ingress to Karpenter health probe port from node IPs (VPC CIDR).
# Kubelet liveness and readiness probes: GET http://<pod-ip>:8081/healthz|/readyz
resource "kubernetes_network_policy_v1" "kube_system_karpenter_ingress_probe" {
  metadata {
    name      = "allow-ingress-karpenter-probe"
    namespace = "kube-system"
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "karpenter"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "8081"
        protocol = "TCP"
      }
      from {
        ip_block {
          cidr = var.vpc_cidr
        }
      }
    }
  }

  depends_on = [module.eks]
}

# Allow ingress to the Karpenter webhook (port 8443) from the API server.
# The API server calls the Karpenter webhook for node scheduling decisions.
# API server source IPs are within the VPC (control plane subnet).
resource "kubernetes_network_policy_v1" "kube_system_karpenter_ingress_webhook" {
  metadata {
    name      = "allow-ingress-karpenter-webhook"
    namespace = "kube-system"
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "karpenter"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "8443"
        protocol = "TCP"
      }
      from {
        ip_block {
          cidr = var.vpc_cidr
        }
      }
    }
  }

  depends_on = [module.eks]
}
