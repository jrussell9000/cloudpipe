################################################################################
# RDS PostgreSQL — Argo Workflows persistence store
################################################################################

resource "aws_db_subnet_group" "this" {
  name_prefix = "${var.cluster_name}-argo-"
  subnet_ids  = var.private_subnets
  tags        = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "db" {
  name_prefix = "${var.cluster_name}-argo-postgres-"
  vpc_id      = var.vpc_id
  description = "Controls PostgreSQL access for the Argo Workflows RDS instance."
  tags        = merge(var.tags, { Name = "${var.cluster_name}-argo-postgres-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "db_vpc" {
  security_group_id = aws_security_group.db.id
  description       = "PostgreSQL from within the VPC (EKS pods)"
  cidr_ipv4         = var.vpc_cidr
  from_port         = 5432
  ip_protocol       = "tcp"
  to_port           = 5432
}

resource "aws_vpc_security_group_egress_rule" "db" {
  security_group_id = aws_security_group.db.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_db_instance" "this" {
  identifier     = "${var.cluster_name}-argo"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class
  db_name        = var.db_name
  username       = var.db_username
  # Native secret management and rotation for this password
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.db.id]

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  backup_retention_period = var.db_backup_retention_days
  publicly_accessible     = false

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  auto_minor_version_upgrade = true

  # AWS-side guard: DeleteDBInstance is refused while this is true, regardless of
  # whether the call comes from Terraform, the console, or the CLI. Clearing it is a
  # separate apply, which is the point — deletion takes two deliberate steps.
  deletion_protection = var.db_deletion_protection

  # A replacement (see replace_triggered_by below) still destroys the instance, so the
  # final snapshot is what makes that recoverable.
  skip_final_snapshot       = var.db_skip_final_snapshot
  final_snapshot_identifier = var.db_skip_final_snapshot ? null : "${var.cluster_name}-argo-final"

  tags = var.tags

  lifecycle {
    replace_triggered_by = [aws_db_subnet_group.this]
  }
}

################################################################################
# Secrets — ClusterSecretStore + ExternalSecret
#
# RDS manages the master password natively (manage_master_user_password = true).
# master_user_secret[0].secret_arn is the RDS-owned Secrets Manager secret.
# ESO syncs it into a Kubernetes Secret named "argo-db" in var.namespace via
# ExternalSecret target.template (see below), adding host/port/dbname statically.
################################################################################

# ClusterSecretStore — shared by all ExternalSecrets in the cluster.
#
# There is deliberately NO `auth` block, and that is what selects EKS Pod
# Identity. With `auth` unset the AWS provider falls back to the default
# credential chain, which resolves to the external-secrets controller pod's own
# credentials — the `external-secrets` service account, granted
# SecretsManager GetSecretValue by module.external_secrets_pod_identity.
#
# This used to carry `auth.pod.serviceAccountRef`, which looked like the thing
# selecting Pod Identity and was not: `spec.provider.aws.auth` accepts only
# `jwt` and `secretRef`, the CRD sets no x-kubernetes-preserve-unknown-fields,
# so the API server pruned it and the live store has always had `auth: {}`
# (#687). Do not add it back, and do not "fix" it into `auth.jwt` — that would
# switch the store to service-account-token auth and change which credentials
# every ExternalSecret in the cluster uses.
#
# count = 0 until ArgoCD installs the external-secrets CRDs; set crds_available=true after first ArgoCD sync.
resource "kubectl_manifest" "cluster_secretstore" {
  count     = var.crds_available ? 1 : 0
  yaml_body = <<-YAML
    apiVersion: external-secrets.io/v1beta1
    kind: ClusterSecretStore
    metadata:
      name: external-secrets-clusterstore
    spec:
      provider:
        aws:
          service: SecretsManager
          region: ${var.region}
      # Which namespaces may reference this store (#637). Without conditions a
      # ClusterSecretStore is usable from ANY namespace, so anything able to
      # create an ExternalSecret — an ArgoCD-synced app, a workflow manifest —
      # could pull a secret this store can read into its own namespace.
      #
      # Exactly the namespaces that reference it today, from
      # `kubectl get externalsecrets -A`:
      #   argo-workflows  argo-db, pgbouncer-auth-userlist
      #   prefect         prefect-db-credentials
      #   cloudflared     cloudflared-token
      #
      # argo-workflows/globus-credentials is covered by the argo-workflows
      # entry too: it used to read a second store, which has since been retired
      # and its consumer repointed here (#637). This is now the only
      # ClusterSecretStore in the cluster, so a namespace missing from this list
      # has no fallback.
      #
      # Adding a namespace is required before an ExternalSecret in it will
      # sync, and the failure is quiet — ESO keeps serving the last synced
      # Secret and reports the error only on the ExternalSecret's status.
      conditions:
        - namespaces:
            - argo-workflows
            - prefect
            - cloudflared
  YAML
}

# ExternalSecret — syncs the RDS-managed secret into the argo-workflows namespace.
# Creates a Kubernetes Secret named "argo-db" with username, password, host, port, dbname.
#
# The RDS-managed secret (rds!db-...) only stores username and password; host/port/dbname
# are not included by AWS. target.template injects the static connection fields alongside
# the dynamic credentials so pgbouncer can read all five keys from one secret.
resource "kubectl_manifest" "db_external_secret" {
  count     = var.crds_available ? 1 : 0
  yaml_body = <<-YAML
    apiVersion: external-secrets.io/v1beta1
    kind: ExternalSecret
    metadata:
      name: argo-db
      namespace: ${var.namespace}
    spec:
      refreshInterval: 1h
      secretStoreRef:
        name: external-secrets-clusterstore
        kind: ClusterSecretStore
      target:
        name: argo-db
        template:
          data:
            username: "{{ .username }}"
            password: "{{ .password }}"
            host: "${aws_db_instance.this.address}"
            port: "5432"
            dbname: "${var.db_name}"
      dataFrom:
      - extract:
          key: ${aws_db_instance.this.master_user_secret[0].secret_arn}
  YAML

  depends_on = [
    kubectl_manifest.cluster_secretstore,
    aws_db_instance.this,
    data.kubernetes_namespace_v1.this,
  ]
}

# Stable in-cluster name for the RDS endpoint. pgbouncer's gitops values point at
# argo-rds.<namespace>.svc.cluster.local instead of the instance hostname, so the
# account-specific endpoint lives only in Terraform state, never in git (ADR 020),
# and a replaced or restored instance is followed without a values edit.
#
# This works only because pgbouncer connects with server_tls_sslmode = require,
# which encrypts without checking the certificate's hostname. Moving it to
# verify-full would fail here: the name pgbouncer dials is not one on the RDS cert.
resource "kubernetes_service_v1" "rds" {
  metadata {
    name      = "argo-rds"
    namespace = var.namespace
  }

  spec {
    type          = "ExternalName"
    external_name = aws_db_instance.this.address
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}

# ExternalSecret for pgbouncer userlist — formats username+password from the
# RDS-managed secret into pgbouncer's userlist.txt format ("user" "pass").
# Mounted at /etc/pgbouncer-auth/ via pgbouncer.extraVolumes in gitops values.
resource "kubectl_manifest" "pgbouncer_userlist_external_secret" {
  count     = var.crds_available ? 1 : 0
  yaml_body = <<-YAML
    apiVersion: external-secrets.io/v1beta1
    kind: ExternalSecret
    metadata:
      name: pgbouncer-auth-userlist
      namespace: ${var.namespace}
    spec:
      refreshInterval: 1h
      secretStoreRef:
        name: external-secrets-clusterstore
        kind: ClusterSecretStore
      target:
        name: pgbouncer-auth-userlist
        template:
          data:
            userlist.txt: '"{{ .username }}" "{{ .password }}"'
      dataFrom:
      - extract:
          key: ${aws_db_instance.this.master_user_secret[0].secret_arn}
  YAML

  depends_on = [
    kubectl_manifest.cluster_secretstore,
    aws_db_instance.this,
    data.kubernetes_namespace_v1.this,
  ]
}
