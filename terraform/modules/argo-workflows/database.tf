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

# resource "aws_vpc_security_group_ingress_rule" "db_uwmadison" {
#   security_group_id = aws_security_group.db.id
#   description       = "PostgreSQL from operator workstation"
#   prefix_list_id    = var.inbound_prefix_list_id
#   from_port         = 5432
#   ip_protocol       = "tcp"
#   to_port           = 5432
# }

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

  # Set deletion_protection = true before promoting to production.
  # skip_final_snapshot must be false and a final_snapshot_identifier provided at that point.
  deletion_protection = false
  skip_final_snapshot = true

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
# ESO syncs it into a Kubernetes Secret named "argo-db" in var.namespace.
# The secret contains username, password, dbname, host, port keys.
################################################################################

# ClusterSecretStore — shared by all ExternalSecrets in the cluster.
# Uses EKS Pod Identity via the external-secrets service account, which has
# SecretsManager GetSecretValue permission via module.external_secrets_pod_identity.
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
          auth:
            pod:
              serviceAccountRef:
                name: external-secrets
                namespace: external-secrets
  YAML
}

# ExternalSecret — syncs the RDS-managed secret into the argo-workflows namespace.
# Creates a Kubernetes Secret named "argo-db" with username and password keys.
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
