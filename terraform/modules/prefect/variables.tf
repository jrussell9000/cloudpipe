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

variable "public_subnets" {
  description = "List of public subnet IDs for the RDS subnet group."
  type        = list(string)
}

variable "region" {
  description = "AWS region."
  type        = string
}

variable "inbound_prefix_list_id" {
  description = "Managed prefix list ID controlling inbound access to the Prefect ALB."
  type        = string
}

variable "nat_gateway_ip" {
  description = "Public Elastic IP of the VPC's NAT gateway. Full-tunnel Client VPN traffic to this ALB's public IP hairpins out through the NAT gateway and back in over the internet, so the ALB security group must trust it as a source."
  type        = string
}

################################################################################
# DNS / TLS
################################################################################

variable "route53_zone_name" {
  description = "Name of the Route53 hosted zone (e.g. 'example.com'). Used to build the Prefect subdomain."
  type        = string
}

variable "certificate_arn" {
  description = "ARN of the ACM certificate for the Prefect ALB listener."
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
  description = "Name of the dedicated, versioned S3 bucket that pipeline QC and cost metrics are written to. The kubecost-cost-scraper flow writes CostAllocation records here. Separate from `bucket` so metrics survive derivative flushes — see terraform/metrics_bucket.tf."
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

variable "access_log_bucket" {
  description = "SSE-S3 bucket receiving this module's ALB access logs. The master log bucket is SSE-KMS and ALB cannot deliver to it."
  type        = string
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
