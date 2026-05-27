
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

variable "hostname" {
  type        = string
  description = "AWS Route53 zone hostname"
}

variable "certificate_arn" {
  type        = string
  description = "AWS Certificate ARN"
}

variable "vpc_id" {
  type        = string
  description = "VPC ID — used to create the ALB security group."
}

variable "inbound_prefix_list_id" {
  type        = string
  description = "ID of the managed prefix list controlling inbound HTTPS access to the Kubecost ALB."
}
