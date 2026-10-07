################################################################################
# Cluster / Network
################################################################################

variable "cluster_name" {
  description = "Name of the EKS cluster (used for resource naming)."
  type        = string
}

variable "vpc_id" {
  description = "ID of the VPC where the cluster and database reside."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block of the VPC, used to allow PostgreSQL ingress from within the cluster."
  type        = string
}

variable "private_subnets" {
  description = "List of private subnet IDs for the RDS subnet group."
  type        = list(string)
}

variable "region" {
  description = "AWS region."
  type        = string
}

################################################################################
# UI access — published behind the caller's shared ALB, or port-forwarded
################################################################################

variable "publish_ui" {
  description = "Whether to create the Ingress that publishes the Argo Workflows UI on the caller's shared ALB. False in port-forward mode, where `ui_host` and `certificate_arn` are null."
  type        = bool
}

variable "ui_host" {
  description = "Hostname the UI is published under (e.g. 'argo.example.com'). Null when publish_ui is false."
  type        = string
}

variable "ui_base_url" {
  description = "Where a browser reaches this UI, scheme and all — the ALB hostname when published, a fixed localhost port when port-forwarded. The SSO redirect URL is built from it, so it has to agree with what the identity provider has registered."
  type        = string
}

variable "oidc_issuer_url" {
  description = "OIDC issuer the Argo Workflows server trusts for SSO. The server discovers every other endpoint from it, from inside its pod, so it must be reachable from there as well as from the browser."
  type        = string
}

variable "certificate_arn" {
  description = "ARN of the ACM certificate for the shared ALB listener. Null when publish_ui is false."
  type        = string
}

################################################################################
# Argo Workflows — general
################################################################################

variable "namespace" {
  description = "Kubernetes namespace for Argo Workflows."
  type        = string
  default     = "argo-workflows"
}

variable "bucket" {
  description = "Name of the S3 bucket used for workflow artifact storage."
  type        = string
}

variable "metrics_bucket" {
  description = "Name of the dedicated, versioned S3 bucket that pipeline QC metrics are written to. Separate from `bucket` so metrics survive derivative flushes — see terraform/modules/stack/metrics_bucket.tf."
  type        = string
}

variable "ecr_registry" {
  description = "ECR Public registry prefix (e.g. public.ecr.aws/<alias>) written into the cloudpipe-config ConfigMap."
  type        = string
}

################################################################################
# Argo Workflows — service account names
################################################################################

variable "server_sa_name" {
  description = "Name for the Argo Workflows server service account."
  type        = string
  default     = "argo-workflows-server"
}

variable "controller_sa_name" {
  description = "Name for the Argo Workflows controller service account."
  type        = string
  default     = "argo-workflows-controller"
}

variable "runner_sa_name" {
  description = "Name for the Argo Workflows runner service account."
  type        = string
  default     = "argo-workflows-runner"
}

################################################################################
# Argo Workflows — database
################################################################################

variable "db_name" {
  description = "Name of the PostgreSQL database for Argo Workflows persistence."
  type        = string
  default     = "argoworkflows"
}

variable "db_username" {
  description = "Master username for the RDS PostgreSQL instance."
  type        = string
  default     = "argouser"
}

variable "db_engine_version" {
  description = "PostgreSQL engine version. Update deliberately — changing the major version triggers an instance replacement."
  type        = string
  default     = "16.13"
}

variable "db_instance_class" {
  description = "RDS instance class. Upsized from db.t4g.micro: burst-credit exhaustion plus a ~112 max_connections ceiling caused SQLSTATE 53300 under bulk workflow operations."
  type        = string
  default     = "db.m7g.large"
}

variable "db_allocated_storage" {
  description = "Initial allocated storage in GiB. gp3 minimum is 20 GiB."
  type        = number
  default     = 100
}

variable "db_max_allocated_storage" {
  description = "Upper bound for RDS storage autoscaling in GiB. Set to 0 to disable autoscaling."
  type        = number
  default     = 500
}

variable "db_backup_retention_days" {
  description = "Number of days to retain automated RDS backups."
  type        = number
  default     = 7
}

variable "db_deletion_protection" {
  description = "Enable RDS deletion protection. AWS refuses DeleteDBInstance while true; set to false in a separate apply before an intentional teardown."
  type        = bool
  default     = true
}

variable "db_skip_final_snapshot" {
  description = "Skip the final snapshot on RDS destroy/replace. Leave false so a destroy or a replace_triggered_by replacement is recoverable."
  type        = bool
  default     = false
}

variable "db_table_name" {
  description = "Table name within the database used by Argo Workflows."
  type        = string
  default     = "argo_workflows"
}

variable "crds_available" {
  description = "Set to true once ArgoCD has synced and CRDs (external-secrets, prometheus-operator) are installed. Gates ClusterSecretStore, ExternalSecret, and ServiceMonitor resources."
  type        = bool
  default     = false
}

################################################################################
# Logging
################################################################################

variable "log_bucket" {
  description = "Name of the S3 bucket used for logging."
  type        = string
}

variable "alb_group_annotations" {
  description = "ALB-level ingress annotations (group.name, scheme, security-groups, listen-ports, ssl-redirect, ssl-policy, load-balancer-attributes, ...) shared by every member of the ALB ingress group. Must be identical across members, so they are set by the caller, not here."
  type        = map(string)
}

################################################################################
# Tags
################################################################################

variable "tags" {
  description = "Map of tags to apply to all taggable resources."
  type        = map(string)
  default     = {}
}
