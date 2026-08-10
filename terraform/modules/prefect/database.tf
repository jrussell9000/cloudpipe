################################################################################
# RDS PostgreSQL — Prefect metadata store
################################################################################

resource "aws_db_subnet_group" "this" {
  name_prefix = "${var.cluster_name}-prefect-"
  subnet_ids  = var.public_subnets
  tags        = var.tags
}

resource "aws_security_group" "db" {
  name_prefix = "${var.cluster_name}-prefect-postgres-"
  vpc_id      = var.vpc_id
  description = "Controls PostgreSQL access for the Prefect RDS instance."
  tags        = merge(var.tags, { Name = "${var.cluster_name}-prefect-postgres-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "db_operator" {
  security_group_id = aws_security_group.db.id
  description       = "PostgreSQL from operator workstation"
  prefix_list_id    = var.inbound_prefix_list_id
  from_port         = 5432
  ip_protocol       = "tcp"
  to_port           = 5432
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
  identifier     = "${var.cluster_name}-prefect"
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

  deletion_protection = false
  skip_final_snapshot = true

  tags = var.tags
}

################################################################################
# Secrets — ClusterSecretStore (shared) + ExternalSecret
#
# RDS manages the master password natively (manage_master_user_password = true).
# ESO syncs it into a Kubernetes Secret named "prefect-db-credentials" with a
# pre-built "connection-url" key for direct use by the Prefect server pod.
################################################################################

# ExternalSecret — constructs the full asyncpg connection URL from the
# individual fields in the RDS-managed Secrets Manager secret.
resource "kubectl_manifest" "db_external_secret" {
  count     = var.crds_available ? 1 : 0
  yaml_body = <<-YAML
    apiVersion: external-secrets.io/v1beta1
    kind: ExternalSecret
    metadata:
      name: prefect-db-credentials
      namespace: ${var.namespace}
    spec:
      refreshInterval: 1h
      secretStoreRef:
        name: external-secrets-clusterstore
        kind: ClusterSecretStore
      target:
        name: prefect-db-credentials
        template:
          data:
            connection-string: 'postgresql+asyncpg://{{ .username }}:{{ .password | replace "%" "%25" | replace "[" "%5B" | replace "]" "%5D" | replace "#" "%23" | replace "@" "%40" | replace "?" "%3F" | replace "/" "%2F" }}@${aws_db_instance.this.address}:${aws_db_instance.this.port}/${var.db_name}?ssl=require'
      dataFrom:
      - extract:
          key: ${aws_db_instance.this.master_user_secret[0].secret_arn}
  YAML

  depends_on = [
    aws_db_instance.this,
    data.kubernetes_namespace_v1.this,
  ]
}
