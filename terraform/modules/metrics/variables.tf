variable "bucket" {
  description = "Name of the main cloudpipe S3 bucket (metrics/ prefix lives here)."
  type        = string
}

variable "finops_bucket" {
  description = "Name of the finops S3 bucket used for Athena query results."
  type        = string
}

variable "cluster_name" {
  description = "EKS cluster name — used to scope Pod Identity associations."
  type        = string
}

variable "region" {
  description = "AWS region."
  type        = string
}

variable "grafana_namespace" {
  description = "Kubernetes namespace where Grafana is deployed."
  type        = string
  default     = "grafana"
}

variable "grafana_sa_name" {
  description = "Kubernetes service account name for Grafana."
  type        = string
  default     = "grafana"
}

variable "tags" {
  description = "Map of tags to apply to all taggable resources."
  type        = map(string)
  default     = {}
}
