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

variable "service_cidr" {
  description = <<-EOT
    The cluster's Kubernetes Service CIDR (the ClusterIP range), read from
    module.eks.cluster_service_cidr. NetworkPolicy egress is evaluated before
    kube-proxy's DNAT, so a pod dialling a Service sees the ClusterIP as the
    destination — an address in this range, not in the VPC CIDR. Any egress rule
    for an in-cluster Service must therefore admit it.
  EOT
  type        = string
}

variable "public_subnets" {
  description = "List of public subnet IDs for the RDS subnet group."
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
  description = "Whether to create the Ingress that publishes the Prefect UI on the caller's shared ALB. False in port-forward mode, where `ui_host` and `certificate_arn` are null."
  type        = bool
}

variable "ui_host" {
  description = "Hostname the UI is published under (e.g. 'prefect.example.com'). Null when publish_ui is false."
  type        = string
}

variable "certificate_arn" {
  description = "ARN of the ACM certificate for the shared ALB listener. Null when publish_ui is false."
  type        = string
}

################################################################################
# Prefect — general
################################################################################

variable "namespace" {
  description = "Kubernetes namespace for Prefect."
  type        = string
  default     = "prefect"
}

variable "bucket" {
  description = "Name of the S3 bucket used for workflow artifact storage (read by Prefect worker flows)."
  type        = string
}

variable "metrics_bucket" {
  description = "Name of the dedicated, versioned S3 bucket that pipeline QC and cost metrics are written to. The kubecost-cost-scraper flow writes CostAllocation records here. Separate from `bucket` so metrics survive derivative flushes — see terraform/modules/stack/metrics_bucket.tf."
  type        = string
}

variable "work_pool" {
  description = "Name of the Prefect Kubernetes work pool."
  type        = string
  default     = "cloudpipe-k8s-pool"
}

variable "argo_namespace" {
  description = "Namespace the Argo workflow pods run in. The cloudpipe queue manager lists Pending pods there to detect a GPU spot drought and switch new submissions to CPU segmentation (#373)."
  type        = string
  default     = "argo-workflows"
}

variable "globus_instance_arn" {
  description = "ARN of the Globus Connect Server EC2 instance. The queue manager's batch gate starts it before its live listing, since the host is stopped nightly (#652). Empty grants no EC2 permissions; the gate then warns and lists without starting the host."
  type        = string
  default     = ""
}

################################################################################
# Prefect — service account names
################################################################################

variable "server_sa_name" {
  description = "Name for the Prefect server service account (created by Helm chart)."
  type        = string
  default     = "prefect-server"
}

variable "worker_sa_name" {
  description = "Name for the Prefect worker service account (created by Helm chart)."
  type        = string
  default     = "prefect-worker"
}

################################################################################
# Prefect — database
################################################################################

variable "db_name" {
  description = "Name of the PostgreSQL database for Prefect metadata."
  type        = string
  default     = "prefect"
}

variable "db_username" {
  description = "Master username for the RDS PostgreSQL instance."
  type        = string
  default     = "prefectuser"
}

variable "db_engine_version" {
  description = "PostgreSQL engine version. Update deliberately — changing the major version triggers an instance replacement."
  type        = string
  default     = "16.13"
}

variable "db_instance_class" {
  description = "RDS instance class. db.t4g.micro is sufficient for Prefect flow run metadata."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "Initial allocated storage in GiB. gp3 minimum is 20 GiB."
  type        = number
  default     = 20
}

variable "db_max_allocated_storage" {
  description = "Upper bound for RDS storage autoscaling in GiB."
  type        = number
  default     = 100
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
  description = "Skip the final snapshot on RDS destroy. Leave false so a destroy is recoverable."
  type        = bool
  default     = false
}

################################################################################
# Logging
################################################################################

variable "alb_group_annotations" {
  description = "ALB-level ingress annotations (group.name, scheme, security-groups, listen-ports, ssl-redirect, ssl-policy, load-balancer-attributes, ...) shared by every member of the ALB ingress group. Must be identical across members, so they are set by the caller, not here."
  type        = map(string)
}

################################################################################
# Gates
################################################################################

variable "crds_available" {
  description = "Set to true once ArgoCD has synced and CRDs (external-secrets, prometheus-operator) are installed. Gates ClusterSecretStore and ExternalSecret resources."
  type        = bool
  default     = false
}

################################################################################
# Tags
################################################################################

variable "tags" {
  description = "Map of tags to apply to all taggable resources."
  type        = map(string)
  default     = {}
}
