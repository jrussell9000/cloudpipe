# EXTERNAL SECRETS
# Helm release is managed by ArgoCD (gitops/apps/external-secrets/)
# Pod Identity Association keeps the IAM binding in Terraform; SA is created by Helm
# The account's own identity, for scoping the grants below to this account and
# region instead of `*:*`.
data "aws_caller_identity" "current" {}

# Scoped to the four secrets that are actually read, with no write permission
# (#637). What this replaced granted read AND write on every secret in the
# account, plus kms:Decrypt on every key:
#
#   secretsmanager:*          arn:aws:secretsmanager:*:*:secret:*
#   kms:Decrypt               arn:aws:kms:*:*:key/*
#   ssm:GetParameter*         arn:aws:ssm:*:*:parameter/*
#
# so any principal able to create an ExternalSecret in any namespace could copy
# any secret in the account into that namespace.
#
# Verified with `aws iam simulate-custom-policy` against the real ARNs rather
# than by reading: the four consumed secrets evaluate `allowed`, and
# cloudpipe/github-pat, cloudpipe/argocd-dex-oidc and globus/s3-gateway/* all
# evaluate `implicitDeny`.
locals {
  # The live consumers, from `kubectl get externalsecrets -A`:
  #   argo-workflows/argo-db + pgbouncer-auth-userlist -> the argo RDS secret
  #   prefect/prefect-db-credentials                   -> the prefect RDS secret
  #   argo-workflows/globus-credentials                -> globus/refresh-token
  #   cloudflared/cloudflared-token                    -> cloudpipe/cloudflare-tunnel-token
  #
  # Secrets Manager appends a random six-character suffix, so each entry needs a
  # trailing wildcard. It must be `*` and not `??????`: IAM documents `?` as a
  # single-character wildcard, but simulate-custom-policy returns implicitDeny
  # for `globus/refresh-token-??????` against the real `-ForUhs` suffix while
  # `-*` returns allowed. Do not "tighten" these to `?`.
  #
  # Consequence of `-*`, stated rather than hidden: this also matches
  # `globus/refresh-token-staging-*`, a sibling secret ESO does not read. Both
  # are Globus refresh tokens for this deployment, so the over-match is the same
  # class of credential. Pinning the exact `-ForUhs` suffix would exclude it but
  # would break the sync silently if the secret were ever recreated — and ESO
  # keeps serving its last copy on an access failure, so that breakage is quiet.
  #
  # rds!db-* is deliberately a prefix: the names are RDS-generated UUIDs and RDS
  # recreates the secret with a new one when an instance is replaced.
  external_secrets_arns = [
    "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:rds!db-*",
    "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:globus/refresh-token-*",
    "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:cloudpipe/cloudflare-tunnel-token-*",
  ]
}

module "external_secrets_pod_identity" {
  source = "terraform-aws-modules/eks-pod-identity/aws"

  name                           = "external-secrets"
  attach_external_secrets_policy = true

  # No PushSecret exists anywhere in gitops/, terraform/ or argo/, and none is
  # planned, so the create/update/delete grant this enabled had no consumer at
  # all. It covered CreateSecret, PutSecretValue, TagResource and DeleteSecret.
  external_secrets_create_permission = false

  external_secrets_secrets_manager_arns = local.external_secrets_arns

  # No ExternalSecret reads Parameter Store; every one uses the Secrets Manager
  # provider. An empty list omits both SSM statements entirely (the module gates
  # them on `length(...) > 0`), which is what we want — unlike the KMS list
  # below, where empty means the opposite.
  external_secrets_ssm_parameter_arns = []

  # MUST be set explicitly. The module's KMS statement is unconditional and
  # falls back to every key in every account when the list is empty:
  #
  #   resources = coalescelist(var.external_secrets_kms_key_arns,
  #                            ["arn:${local.partition}:kms:*:*:key/*"])
  #
  # so leaving this out would be WIDER than passing it, not narrower.
  #
  # Every secret above uses the AWS-managed aws/secretsmanager key (all eleven
  # secrets in the account report KmsKeyId unset), for which Secrets Manager
  # decrypts on the caller's behalf and no kms:Decrypt grant is strictly needed.
  # Scoped to this account and region rather than to that key's ARN because the
  # AWS-managed key is created lazily on first use, so a data-source lookup of
  # it would fail at plan time in a fresh account.
  external_secrets_kms_key_arns = [
    "arn:aws:kms:${var.region}:${data.aws_caller_identity.current.account_id}:key/*",
  ]

  associations = {
    cloudpipe = {
      cluster_name    = var.eks_cluster.cluster_name
      namespace       = "external-secrets"
      service_account = "external-secrets"
    }
  }
}

# ------------------------------------------------------------------------------
# SUPPLEMENTARY RBAC FIX FOR CERT-CONTROLLER
# ------------------------------------------------------------------------------
# This bypasses the restrictive resourceNames generated by the Helm chart,
# preventing the 403 Forbidden -> 500 Healthz error loop.
#
# Split into a ClusterRole and a namespaced Role (#637). Only the webhook
# configurations are cluster-scoped; the secrets, events and leases this
# controller touches all live in its own namespace, and granting them
# cluster-wide let the cert-controller read, patch and create Secrets in EVERY
# namespace.
#
# Verified before narrowing, because getting it wrong breaks ExternalSecret
# admission cluster-wide:
#   * the binding's only subject is the external-secrets-cert-controller
#     service account — the MAIN controller's cluster-wide Secret writes come
#     from the chart's separate `external-secrets-controller` ClusterRole, which
#     this does not touch;
#   * the external-secrets namespace holds exactly one secret,
#     `external-secrets-webhook`, which is the cert this controller manages.
#
# Keep in lockstep with gitops/apps/cluster-config/templates/external-secrets-patch.yaml.

resource "kubernetes_cluster_role_v1" "external_secrets_cert_controller_patch" {
  metadata {
    name = "external-secrets-cert-controller-patch"
  }

  # Cluster-scoped objects, so this rule cannot be namespaced.
  rule {
    api_groups = ["admissionregistration.k8s.io"]
    resources  = ["validatingwebhookconfigurations", "mutatingwebhookconfigurations"]
    verbs      = ["get", "list", "watch", "update", "patch"]
  }

  # Also defined in gitops/apps/cluster-config (ArgoCD manages the live state
  # post-bootstrap; this copy exists so the patch is in place before ArgoCD is
  # up). Without this, every plan wants to strip ArgoCD's tracking-id annotation.
  lifecycle {
    ignore_changes = [
      metadata[0].annotations["argocd.argoproj.io/tracking-id"],
    ]
  }
}

resource "kubernetes_cluster_role_binding_v1" "external_secrets_cert_controller_patch_binding" {
  metadata {
    name = "external-secrets-cert-controller-patch-binding"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.external_secrets_cert_controller_patch.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "external-secrets-cert-controller"
    namespace = "external-secrets"
  }

  # See lifecycle note on the ClusterRole above — same dual-ownership with
  # gitops/apps/cluster-config.
  lifecycle {
    ignore_changes = [
      metadata[0].annotations["argocd.argoproj.io/tracking-id"],
    ]
  }
}

# The namespaced half of the patch (#637): the webhook cert secret, this
# controller's own events, and its leader-election lease. All three live in
# external-secrets, so none of them needs a cluster-wide grant.
resource "kubernetes_role_v1" "external_secrets_cert_controller_patch" {
  metadata {
    name      = "external-secrets-cert-controller-patch"
    namespace = "external-secrets"
  }

  rule {
    api_groups = [""]
    resources  = ["secrets", "events"]
    verbs      = ["get", "list", "watch", "update", "patch", "create"]
  }

  rule {
    api_groups = ["coordination.k8s.io"]
    resources  = ["leases"]
    verbs      = ["get", "create", "update", "patch"]
  }

  lifecycle {
    ignore_changes = [
      metadata[0].annotations["argocd.argoproj.io/tracking-id"],
    ]
  }
}

resource "kubernetes_role_binding_v1" "external_secrets_cert_controller_patch" {
  metadata {
    name      = "external-secrets-cert-controller-patch-binding"
    namespace = "external-secrets"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.external_secrets_cert_controller_patch.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "external-secrets-cert-controller"
    namespace = "external-secrets"
  }

  lifecycle {
    ignore_changes = [
      metadata[0].annotations["argocd.argoproj.io/tracking-id"],
    ]
  }
}
