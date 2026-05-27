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

variable "inbound_prefix_list_id" {
  description = "Managed prefix list ID controlling inbound access to the Argo Workflows ALB"
  type        = string
}

################################################################################
# DNS / TLS
################################################################################

variable "route53_zone_name" {
  description = "Name of the Route53 hosted zone (e.g. 'example.com'). Used to build the Argo Workflows subdomain."
  type        = string
}

variable "certificate_arn" {
  description = "ARN of the ACM certificate for the Argo Workflows ALB listener."
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
  default     = "16.6"
}

variable "db_instance_class" {
  description = "RDS instance class. db.t4g.micro is sufficient for Argo Workflows archive metadata."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "Initial allocated storage in GiB. gp3 minimum is 20 GiB."
  type        = number
  default     = 20
}

variable "db_max_allocated_storage" {
  description = "Upper bound for RDS storage autoscaling in GiB. Set to 0 to disable autoscaling."
  type        = number
  default     = 100
}

variable "db_backup_retention_days" {
  description = "Number of days to retain automated RDS backups."
  type        = number
  default     = 7
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

################################################################################
# Tags
################################################################################

variable "tags" {
  description = "Map of tags to apply to all taggable resources."
  type        = map(string)
  default     = {}
}
