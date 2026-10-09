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
#
# The egress side needs the Service CIDR as well as the pod selector. Egress is
# evaluated before kube-proxy's DNAT, so a pod that dials a Service by name is
# judged against the ClusterIP, which no podSelector can ever match. The peer is
# still safe: ingress is evaluated at the destination pod, after DNAT, so the
# target's own policies remain the gate.
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
      to {
        ip_block {
          cidr = module.eks.cluster_service_cidr
        }
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
#
# This one is load-bearing, unlike the other probe rules. 10251 is
# `--secure-port`, so the same port serves the kubelet's probes *and* the API
# server's aggregation requests for `metrics.k8s.io` (Service 443 → targetPort
# 10251). The 2026-10-08 window proved only that the **kubelet** is exempt from
# NetworkPolicy; the API server reaches pods from a control-plane ENI, which was
# not tested. Removing this could take out `kubectl top` and every HPA.
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
#
# Not actually required — probe traffic is exempt, proven 2026-10-08
# (docs/operations.md → Enabling NetworkPolicy enforcement). Kept as insurance:
# these are health ports, and CoreDNS going NotReady would take DNS down
# cluster-wide, so this is the cheapest rule in the file to keep.
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
#
# Not actually required — probe traffic is exempt, proven 2026-10-08. Kept as
# insurance; 8081 is a health port, separate from the 8080 metrics port above.
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

# Allow Prometheus to scrape Karpenter's metrics port (8080, named http-metrics
# on both the Deployment and the Service). The scraper runs in the `prometheus`
# namespace via the `karpenter` ServiceMonitor; its pod IPs sit in the VPC CIDR.
#
# Without this rule the Karpenter metrics — node launches, disruption decisions,
# the signal every capacity and cost dashboard reads — stop arriving, and nothing
# else fails, so the loss is easy to miss.
resource "kubernetes_network_policy_v1" "kube_system_karpenter_ingress_metrics" {
  metadata {
    name      = "allow-ingress-karpenter-metrics"
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
        port     = "8080"
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

# There is deliberately no karpenter webhook rule here. One used to allow 8443
# from the VPC CIDR, but Karpenter has served no webhook since v1: its container
# and Service expose only 8080 (http-metrics) and 8081 (http, the probes), and no
# webhook configuration in the cluster points at a karpenter Service. If a future
# upgrade reintroduces one, this is the rule to add back.
