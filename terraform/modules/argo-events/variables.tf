variable "name" {
  description = "Root name prefix used for resource naming"
  type        = string
}

variable "region" {
  description = "AWS region"
  type        = string
}

variable "cluster_name" {
  description = "EKS cluster name — used for Pod Identity association"
  type        = string
}

variable "argo_events_namespace" {
  description = "Kubernetes namespace for Argo Events"
  type        = string
  default     = "argo-events"
}

variable "argo_events_handler_sa" {
  description = "Service account name for the Argo Events handler"
  type        = string
  default     = "argo-events-handler-sa"
}

variable "argo_workflows_namespace" {
  description = "Kubernetes namespace for Argo Workflows — used for cross-namespace RoleBinding"
  type        = string
  default     = "argo-workflows"
}

variable "argo_workflows_workflow2trigger" {
  description = "WorkflowTemplate name that the Sensor will trigger"
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block of the VPC. Used in NetworkPolicy egress rules."
  type        = string
}

variable "globus_source_collection_id" {
  description = "Globus source collection UUID (DAIRC MMPS endpoint)."
  type        = string
  default     = "<YOUR_GLOBUS_SOURCE_COLLECTION_ID>"
}

variable "globus_source_base_path" {
  description = "Root path on the Globus source collection. Subject ID is appended automatically."
  type        = string
  default     = "/dairc/derivatives/mmps_mproc"
}

variable "globus_dest_collection_id" {
  description = "Globus destination collection UUID (cloudpipe-s3). Sourced from SSM at apply time — no manual updates needed."
  type        = string
}

variable "globus_dest_base_path" {
  description = "Root path on the Globus destination collection."
  type        = string
  default     = "/mmps_mproc"
}

variable "globus_scan_types" {
  description = "JSON array of BIDS scan types to transfer, e.g. [\"T1w\",\"T2w\",\"rest\",\"nback\"]."
  type        = string
  default     = "[\"T1w\",\"T2w\",\"rest\",\"nback\"]"
}

variable "crds_available" {
  description = "Set to true once ArgoCD has synced and installed Argo Events CRDs. Gates EventBus, EventSource, and Sensor manifests."
  type        = bool
  default     = false
}
