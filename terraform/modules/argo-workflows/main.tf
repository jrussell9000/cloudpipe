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
    "persistence" = <<-YAML
      nodeStatusOffLoad: false
      archive: true
      postgresql:
        host: ${aws_db_instance.this.address}
        port: ${aws_db_instance.this.port}
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
      issuer: https://argocd.<YOUR_DOMAIN>/api/dex
      clientId:
        name: argo-workflows-sso
        key: clientID
      clientSecret:
        name: argo-workflows-sso
        key: clientSecret
      redirectUrl: https://argo.<YOUR_DOMAIN>/oauth2/callback
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
    bucket       = var.bucket
    ecr_registry = var.ecr_registry
    region       = var.region
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
      "alb.ingress.kubernetes.io/security-groups"                      = aws_security_group.lb.id
      "alb.ingress.kubernetes.io/manage-backend-security-group-rules"  = "true"

      # Set protocols - backend protocol is HTTP because we terminate TLS at the load balancer
      "alb.ingress.kubernetes.io/backend-protocol"     = "HTTP"
      "alb.ingress.kubernetes.io/healthcheck-protocol" = "HTTP"
      "alb.ingress.kubernetes.io/healthcheck-path"     = "/"

      # ALB access logging disabled — the log bucket uses SSE-KMS which ALB does not support.
      "alb.ingress.kubernetes.io/load-balancer-attributes" = "access_logs.s3.enabled=false"
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
