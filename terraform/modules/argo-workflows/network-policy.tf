################################################################################
# Kubernetes NetworkPolicy — argo-workflows namespace
# NIST 800-171 H4: SC-7 Boundary Protection
#
# Default-deny with explicit allow rules. These objects do NOTHING on their own:
# they are enforced only while the VPC CNI network-policy agent is on, which
# modules/stack/eks.tf gates on `vpc_cni_network_policy_enabled` (strict vs
# standard is the separate `vpc_cni_strict_mode`). Both are persisted in
# terraform/install-state.auto.tfvars by install.sh Phase 6; with the agent off
# every policy here is inert and nothing reports it. That was issue #635 —
# `kubectl get networkpolicy` looks identical either way, so check the add-on:
#   kubectl -n kube-system get ds aws-node -o jsonpath='{..args}' | rg network-policy
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
# lookups (e.g. ssm.<region>.amazonaws.com). Allowing port 53 to any
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

# Allow egress on port 5432 to both CIDRs that the database path crosses:
#   - the Service CIDR, for anything dialling the `pgbouncer` Service by name
#   - the VPC CIDR, for pgbouncer → RDS, and for a direct pod-IP connection
#
# Both are required, and the Service CIDR is the one that is easy to miss.
# NetworkPolicy egress is evaluated **before** kube-proxy's DNAT, so a pod that
# connects to `pgbouncer.argo-workflows.svc` is judged against the ClusterIP
# (172.20.x.x here), not against the pod IP it resolves to. The original comment
# on this rule claimed the opposite — "pod IP in VPC CIDR via kube-proxy DNAT" —
# and the VPC-only rule it justified blocked every connection to the Service.
# Verified live on 2026-10-08 with the agent enabled: a pod in this namespace
# reached pgbouncer's pod IP on 5432 and timed out against its ClusterIP.
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
          cidr = var.service_cidr
        }
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

# Allow ingress to PgBouncer from Argo components and workflow pods in the same
# namespace. The kubelet's tcpSocket probe needs no rule — probe traffic is
# exempt from policy, proven in the 2026-10-09 enforcement window.
#
# Both halves of this rule used to be wrong, and both failed silently because the
# agent had never been on (#635):
#   - the selector was `app = pgbouncer`, a label the pod does not carry. The
#     icoretech/pgbouncer subchart labels its pods `app.kubernetes.io/name`, so
#     the rule selected nothing and pgbouncer kept only default-deny.
#   - the port was 6432, pgbouncer's upstream default. This deployment does not
#     override the subchart, whose containerPort (`psql`) is 5432 — the same
#     number the Service publishes, so there is no port translation here.
#
# What breaks if it regresses is the **Argo workflow archive**: the controller's
# own persistence, which it reaches through the `pgbouncer` Service. Not metrics
# — those are written to S3 and read through Athena/duckdb, and never touch this
# database. Keep the rule pinned to the subchart's contract.
resource "kubernetes_network_policy_v1" "argo_workflows_ingress_pgbouncer" {
  metadata {
    name      = "allow-ingress-pgbouncer"
    namespace = var.namespace
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "pgbouncer"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "5432"
        protocol = "TCP"
      }
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = var.namespace
          }
        }
      }
    }
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}

# There is deliberately no pgbouncer probe rule. One was added with the port and
# selector fix, on the assumption that the kubelet's tcpSocket probe needed an
# explicit allow from the node IP. The 2026-10-08 enforcement window disproved
# that: a pod whose only policy was default-deny kept passing its httpGet probe
# while every other direction to it was blocked, so **the kubelet's probe traffic
# is exempt from NetworkPolicy** (see docs/operations.md → Enabling NetworkPolicy
# enforcement). The rule's only remaining effect was to admit the whole VPC CIDR
# to 5432 — every pod in the cluster, since node and pod IPs share subnets under
# the VPC CNI — which is precisely what default-deny is here to prevent on a
# database port. Health-port probe rules elsewhere in this repo are kept as cheap
# insurance against the exemption changing; on an app port it is not cheap.
#
# allow-ingress-pgbouncer above is what pgbouncer needs: 5432 from this namespace.

# Allow egress to the EKS Pod Identity Agent (169.254.170.23:80).
#
# Every pod in this namespace needs it: the controller, the server and each
# workflow pod run under a service account with a pod-identity association
# (argo-workflows-controller, argo-workflows-server, argo-workflows-runner), and
# the webhook injects AWS_CONTAINER_CREDENTIALS_FULL_URI=
# http://169.254.170.23/v1/credentials into them. The agent itself is
# hostNetwork and exempt from policy, but this outbound hop is not — without
# this rule no pod can fetch credentials and every S3 read and write fails.
# IMDS (169.254.169.254) stays blocked, which is the point of #635: the nodes
# run with an IMDS hop limit of 3.
resource "kubernetes_network_policy_v1" "argo_workflows_egress_pod_identity" {
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
#
# Not actually required: the 2026-10-08 enforcement window showed the kubelet's
# probe traffic is exempt from NetworkPolicy (docs/operations.md → Enabling
# NetworkPolicy enforcement). Kept because the exemption is undocumented AWS
# behaviour that an agent upgrade could reverse, and 6060 is a health port, so
# admitting the VPC CIDR to it costs little. The same reasoning did not hold for
# the pgbouncer and prefect-server rules, which fronted app ports and are gone.
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
