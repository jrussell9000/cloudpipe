################################################################################
# Namespace — created by install.sh before this module runs; referenced here
# so dependent resources have an explicit handle without Terraform owning it.
################################################################################

data "kubernetes_namespace_v1" "this" {
  metadata {
    name = var.namespace
  }
}

################################################################################
# ALB security group — restricts inbound to operator IP only
################################################################################

resource "aws_security_group" "lb" {
  name_prefix = "${var.cluster_name}-argo-lb-"
  vpc_id      = var.vpc_id
  description = "Controls inbound access to the Argo Workflows ALB."
  tags        = merge(var.tags, { Name = "${var.cluster_name}-argo-lb-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "lb_https" {
  security_group_id = aws_security_group.lb.id
  description       = "HTTPS from operator workstation"
  prefix_list_id    = var.inbound_prefix_list_id
  from_port         = 443
  ip_protocol       = "tcp"
  to_port           = 443
}

# Allow access via the AWS Client VPN too (source-NATs to the VPC CIDR) — lets
# a single VPN connection reach both the cluster API and this ALB, without
# also requiring the UW-Madison VPN.
resource "aws_vpc_security_group_ingress_rule" "lb_https_client_vpn" {
  security_group_id = aws_security_group.lb.id
  description       = "HTTPS from AWS Client VPN (source-NATs to VPC CIDR)"
  cidr_ipv4         = var.vpc_cidr
  from_port         = 443
  ip_protocol       = "tcp"
  to_port           = 443
}

# The rule above only covers traffic to VPC-internal destinations (where the
# VPN endpoint's own SNAT applies). Traffic from a full-tunnel VPN client to
# this ALB's *public* IP instead hairpins out through the VPC's NAT gateway
# and back in over the internet, presenting the NAT gateway's EIP as the
# source — so that EIP needs its own trust rule too.
resource "aws_vpc_security_group_ingress_rule" "lb_https_nat" {
  security_group_id = aws_security_group.lb.id
  description       = "HTTPS from VPC NAT gateway (full-tunnel VPN clients hairpin through here)"
  cidr_ipv4         = "${var.nat_gateway_ip}/32"
  from_port         = 443
  ip_protocol       = "tcp"
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "lb" {
  security_group_id = aws_security_group.lb.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

################################################################################
# workflow-controller-configmap — owns dynamic DB persistence config
# ArgoCD's Helm chart uses controller.configMap.create: false
################################################################################

resource "kubernetes_config_map_v1" "workflow_controller" {
  metadata {
    name      = "argo-workflows-controller-configmap"
    namespace = var.namespace
  }

  data = {
    # Controller-side concurrency and pod-creation limits.
    #
    # These live here, not in the chart's values.yaml, because
    # controller.configMap.create is false: the chart's
    # workflow-controller-config-map.yaml is the only template that consumes
    # controller.parallelism / namespaceParallelism / resourceRateLimit, so with
    # creation disabled those values render nowhere and are silently inert. They
    # sat unset in values.yaml for months before #206 (see also the note above
    # the "sso" key, which is here for the same reason).
    #
    # namespaceParallelism is the server-side backstop for the Prefect queue
    # managers' client-side gate. Unlike the client gate it cannot be raced: the
    # controller owns the state it is counting, so a submission burst that
    # outruns label propagation still cannot exceed it (#206) — excess workflows
    # are held in Pending until a slot frees.
    #
    # Above the Prefect Variables, never equal to one: this is a runaway
    # backstop, not the working cap. It applies namespace-wide across BOTH
    # pipelines (ADR 008), so setting it to either pipeline's cap would silently
    # hold the other pipeline's workflows Pending whenever the first is at
    # capacity — which presents as a stalled batch, not a rejected submission.
    #
    # 400 = cloudpipe's 300 target + first-level's 25 + headroom. Raised from 100
    # for the 300-concurrent full-ABCD run; at 100 it was itself the binding cap
    # (a 300-wide submission ran 100 and left 200 Pending).
    #
    # Keep this consistent with the two Variables. The invariant is
    # namespaceParallelism > cloudpipe-max-concurrent + first-level-max-concurrent;
    # this value does NOT set the working concurrency, so raising it alone
    # changes nothing until `prefect variable set cloudpipe-max-concurrent 300`.
    "namespaceParallelism" = "400"

    # Global cap across all namespaces. Only argo-workflows runs workflows, so
    # this is effectively a second, looser ceiling above namespaceParallelism.
    "parallelism" = "1000"

    # Sized against post-#66 pod counts, not the pre-consolidation estimate in
    # #75/#63: functional-preprocessing and bold-to-t1w are now one pod per
    # session (not per run), so a typical 2-session subject creates ~23 pods
    # and a 4-session subject ~29, vs. the ~75/subject the old default was
    # implicitly sized against. At 300 concurrent that's ~7-9k pod creates
    # per batch; 50/s clears it in ~3-4 min instead of ~7 min at 20/s.
    # Argo's own default is unlimited (math.MaxFloat32), which is what was
    # actually in effect while this sat inert in values.yaml.
    # Validate against API server latency (Infrastructure Health dashboard)
    # during a scaled test batch before relying on this at 300 concurrent.
    "resourceRateLimit" = <<-YAML
      limit: 50
      burst: 90
    YAML

    "persistence" = <<-YAML
      connectionPool:
        maxOpenConns: 40     # 32 workers + headroom
        maxIdleConns: 10     # Go's default of 2 would cause constant churn
        connMaxLifetime: 1h
      nodeStatusOffLoad: true
      archive: true
      archiveTTL: 720h
      postgresql:
        host: pgbouncer
        port: 5432
        database: ${aws_db_instance.this.db_name}
        tableName: ${var.db_table_name}
        userNameSecret:
          name: argo-db
          key: username
        passwordSecret:
          name: argo-db
          key: password
    YAML

    # Default artifact repository for workflow logs and unscoped artifacts.
    # Referenced by templates that omit an explicit artifactRepositoryRef.
    "artifactRepository" = <<-YAML
      archiveLogs: true
      s3:
        bucket: ${var.bucket}
        keyFormat: "logs/{{workflow.name}}/{{pod.name}}"
        endpoint: s3-accelerate.amazonaws.com
        region: ${var.region}
        useSDKCreds: true
        encryptionOptions:
          enableEncryption: true
    YAML

    # SSO config — must live here because the Helm chart's configMap.create is
    # false (Terraform owns this ConfigMap), so Helm never writes this key.
    "sso" = <<-YAML
      issuer: https://argocd.${var.route53_zone_name}/api/dex
      clientId:
        name: argo-workflows-sso
        key: clientID
      clientSecret:
        name: argo-workflows-sso
        key: clientSecret
      redirectUrl: https://argo.${var.route53_zone_name}/oauth2/callback
      scopes:
      - openid
      - profile
      - email
      rbac:
        enabled: true
    YAML
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}

################################################################################
# Artifact repository ConfigMap
# Named "artifact-repositories" so Argo resolves artifactRepositoryRef by key.
# The "cloudpipe-artifacts" key is used by fast-tmpl and fast-long templates.
################################################################################

resource "kubernetes_config_map_v1" "artifact_repositories" {
  metadata {
    name      = "artifact-repositories"
    namespace = var.namespace
    annotations = {
      # Makes cloudpipe-artifacts the cluster-wide default for any workflow
      # that sets artifactRepositoryRef without an explicit configMap name.
      "workflows.argoproj.io/default-artifact-repository" = "cloudpipe-artifacts"
    }
  }

  data = {
    "cloudpipe-artifacts" = <<-YAML
      archiveLogs: true
      s3:
        bucket: ${var.bucket}
        keyFormat: "logs/{{workflow.name}}/{{pod.name}}"
        endpoint: s3-accelerate.amazonaws.com
        region: ${var.region}
        useSDKCreds: true
        encryptionOptions:
          enableEncryption: true
    YAML
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}

################################################################################
# Runtime config ConfigMap — exposes infra-level values to workflows as
# workflow parameter defaults, avoiding hardcoded bucket names in YAML.
################################################################################

resource "kubernetes_config_map_v1" "cloudpipe_config" {
  metadata {
    name      = "cloudpipe-config"
    namespace = var.namespace
    labels = {
      "workflows.argoproj.io/configmap-type" = "Parameter"
    }
  }

  data = {
    bucket = var.bucket
    # Read by the `metrics-bucket` workflow parameter. Kept distinct from
    # `bucket` so a template cannot write a metric to the data bucket (or a
    # derivative to the metrics bucket) by defaulting the wrong one.
    metrics_bucket = var.metrics_bucket
    ecr_registry   = var.ecr_registry
    region         = var.region
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}

################################################################################
# Ingress — ALB with TLS termination and HTTP→HTTPS redirect
# (Terraform-managed because cert ARN is a dynamic Terraform output)
################################################################################

resource "kubernetes_ingress_v1" "this" {
  metadata {
    name      = "argoworkflows-ingress"
    namespace = var.namespace
    annotations = {
      # Create a Route53 alias record automatically via external-dns
      "external-dns.alpha.kubernetes.io/hostname" = "argo.${var.route53_zone_name}"

      "alb.ingress.kubernetes.io/scheme"      = "internet-facing"
      "alb.ingress.kubernetes.io/target-type" = "ip"

      # TLS Configuration
      "alb.ingress.kubernetes.io/certificate-arn" = var.certificate_arn
      "alb.ingress.kubernetes.io/listen-ports"    = "[{\"HTTP\": 80}, {\"HTTPS\": 443}]"
      "alb.ingress.kubernetes.io/ssl-redirect"    = "443"
      "alb.ingress.kubernetes.io/ssl-policy"      = "ELBSecurityPolicy-TLS13-1-2-2021-06"

      # Restrict inbound access to the prefix list via a dedicated security group.
      # inbound-cidrs does not accept prefix list IDs — security-groups is required.
      "alb.ingress.kubernetes.io/security-groups"                     = aws_security_group.lb.id
      "alb.ingress.kubernetes.io/manage-backend-security-group-rules" = "true"

      # Set protocols - backend protocol is HTTP because we terminate TLS at the load balancer
      "alb.ingress.kubernetes.io/backend-protocol"     = "HTTP"
      "alb.ingress.kubernetes.io/healthcheck-protocol" = "HTTP"
      "alb.ingress.kubernetes.io/healthcheck-path"     = "/"

      # ALB access logging disabled — the log bucket uses SSE-KMS which ALB does not support.
      # idle_timeout raised to the ALB maximum (4000s): the Argo UI live-updates the DAG over a
      # Server-Sent Events stream that sends nothing between workflow events. At the 60s default,
      # any step running longer than a minute idles the stream out and the UI silently freezes on
      # stale state until refreshed.
      "alb.ingress.kubernetes.io/load-balancer-attributes" = "access_logs.s3.enabled=false,idle_timeout.timeout_seconds=4000"
    }
  }

  spec {
    ingress_class_name = "alb"

    rule {
      host = "argo.${var.route53_zone_name}"
      http {
        path {
          path      = "/*"
          path_type = "ImplementationSpecific"
          backend {
            service {
              name = "argo-workflows-server"
              port {
                number = 2746
              }
            }
          }
        }
      }
    }
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}

################################################################################
# SSO Session Secret Access — server needs to create/manage secrets to store
# SSO session tokens when auth-mode=sso is enabled
################################################################################
resource "kubernetes_role_v1" "server_sso_secrets" {
  metadata {
    name      = "argo-workflows-server-sso-secrets"
    namespace = var.namespace
  }

  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["create", "get", "update", "delete", "list", "watch"]
  }
}

resource "kubernetes_role_binding_v1" "server_sso_secrets" {
  metadata {
    name      = "argo-workflows-server-sso-secrets"
    namespace = var.namespace
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.server_sso_secrets.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "argo-workflows-server"
    namespace = var.namespace
  }
}

################################################################################
# DB Secret Access - Add a Role and RoleBinding that grants the
# Argo workflow controller service account access only to the DB secret
################################################################################
resource "kubernetes_role_v1" "controller_db_secret" {
  metadata {
    name      = "argo-workflows-controller-db-secret"
    namespace = var.namespace
  }

  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = ["argo-db"]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "controller_db_secret" {
  metadata {
    name      = "argo-workflows-controller-db-secret"
    namespace = var.namespace
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.controller_db_secret.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "argo-workflows-controller"
    namespace = var.namespace
  }
}

################################################################################
# DB Secret Access - Add a Role and RoleBinding that grants the
# Argo server service account access only to the DB secret. Needed to read
# back offloaded node status (server/apiserver/argoserver.go wires a real
# offload repo whenever persistence is configured, regardless of
# nodeStatusOffLoad) - without this, `argo get`/UI fail on offloaded
# workflows with "offload node status is not supported" (issue #81).
################################################################################
resource "kubernetes_role_v1" "server_db_secret" {
  metadata {
    name      = "argo-workflows-server-db-secret"
    namespace = var.namespace
  }

  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = ["argo-db"]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "server_db_secret" {
  metadata {
    name      = "argo-workflows-server-db-secret"
    namespace = var.namespace
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.server_db_secret.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "argo-workflows-server"
    namespace = var.namespace
  }
}
