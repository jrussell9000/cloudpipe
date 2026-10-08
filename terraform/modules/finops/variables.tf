
variable "time_unit" {
  type    = string
  default = "HOURLY"
}

variable "athena_database_name" {
  type    = string
  default = "athena_cur_database"
}

variable "athena_workgroup" {
  type    = string
  default = "cur_athena_workgroup"
}

variable "athena_result_retention_days" {
  description = "Days before Athena query results under query-results/ and grafana-query-results/ expire. Does not affect the CUR data under athena/ or the Kubecost store."
  type        = number
  default     = 7

  validation {
    # Athena result reuse caps at 7 days, so anything shorter can expire a
    # result the engine still considers reusable.
    condition     = var.athena_result_retention_days >= 7
    error_message = "athena_result_retention_days must be at least 7, the maximum Athena result-reuse window."
  }
}

# Passthrough variables
variable "root_name" {
  description = "Root name (e.g., cloudpipe) used in infrastructure creation"
  type        = string
}

variable "region" {
  type        = string
  description = "Infrastructure creation region"
}

variable "eks_cluster" {
  description = "The entire EKS cluster module object passed from the root."
  type        = any
}

variable "account_id" {
  type        = string
  description = "The AWS Account ID passed from the parent module."
}

variable "publish_ui" {
  type        = bool
  description = "Whether to create the Ingress that publishes the Kubecost UI on the caller's shared ALB. False in port-forward mode, where `ui_host` and `certificate_arn` are null."
}

variable "ui_host" {
  type        = string
  description = "Hostname the UI is published under (e.g. 'kubecost.example.com'). Null when publish_ui is false."
}

variable "alb_group_annotations" {
  description = "ALB-level ingress annotations (group.name, scheme, security-groups, listen-ports, ssl-redirect, ssl-policy, load-balancer-attributes, ...) shared by every member of the ALB ingress group. Must be identical across members, so they are set by the caller, not here."
  type        = map(string)
}

variable "certificate_arn" {
  type        = string
  description = "ARN of the ACM certificate for the shared ALB listener. Null when publish_ui is false."
}
