################################################################################
# What this root is told about the deployment.
#
# Nothing here has a default except `create_data_bucket`: a bucket name is the
# one value a deployer cannot be guessed at, and a wrong guess makes a bucket
# nobody asked for in an account where it then has to be deleted by hand.
################################################################################

variable "region" {
  description = "The AWS region these buckets are created in. The same region as the deployment: every object the pipeline writes crosses a network boundary otherwise."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]$", var.region))
    error_message = "region must be an AWS region name, in the form xx-place-N."
  }
}

variable "state_bucket" {
  description = "Name of the bucket holding the stack's Terraform state. This is the bucket the stack's backend block names, and the reason this root keeps its own state local."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9.-]{3,63}$", var.state_bucket))
    error_message = "state_bucket must be a valid S3 bucket name."
  }
}

variable "data_bucket" {
  description = "Name of the imaging data bucket — the stack's globus_s3_destination_bucket. Set create_data_bucket to false to adopt a bucket that already exists."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9.-]{3,63}$", var.data_bucket))
    error_message = "data_bucket must be a valid S3 bucket name."
  }
}

variable "metrics_bucket" {
  description = "Name of the metrics bucket — the stack's metrics_bucket input. Holds the run of record for pipeline QC, which is why it outlives any one cluster."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9.-]{3,63}$", var.metrics_bucket))
    error_message = "metrics_bucket must be a valid S3 bucket name."
  }
}

# False for a deployment whose data bucket already exists — including this one,
# whose bucket predates Terraform and stays outside this root's state. The stack
# configures the bucket by name either way, so nothing downstream can tell which
# happened (design D4, D5).
variable "create_data_bucket" {
  description = "Whether this root creates the data bucket. False adopts an existing one: the stack still configures it by name."
  type        = bool
  default     = true
}
