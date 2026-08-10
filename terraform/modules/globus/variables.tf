variable "name" {
  description = "Root name prefix used for resource naming"
  type        = string
}

variable "region" {
  description = "AWS region"
  type        = string
}

variable "partition" {
  description = "AWS partition (e.g. aws, aws-us-gov)"
  type        = string
}

variable "account_id" {
  description = "AWS account ID"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID to place the Globus instance in"
  type        = string
}

variable "public_subnet_id" {
  description = "Public subnet ID for the Globus EC2 instance"
  type        = string
}

variable "globus_s3_bucket" {
  description = "S3 bucket name that the Globus Connect Server will access"
  type        = string
}

variable "globus_admin_prefix_list_id" {
  description = "ID of the AWS managed prefix list allowed SSH access to the Globus Connect Server"
  type        = string
}

variable "argo_runner_iam_role_name" {
  description = "IAM role name of the Argo Workflows runner — receives EC2/SSM permissions to start the Globus instance"
  type        = string
}

################################################################################
# Globus Connect Server setup
################################################################################

variable "globus_org_name" {
  description = "Organization name displayed on the Globus endpoint."
  type        = string
}

variable "globus_contact_email" {
  description = "Contact email displayed on the Globus endpoint."
  type        = string
}

variable "globus_collection_name" {
  description = "Display name for the S3-backed Globus mapped collection."
  type        = string
  default     = "cloudpipe-s3"
}

variable "globus_use_s3_gateway" {
  description = <<-EOT
    When true, configure a Globus S3 storage gateway so GridFTP writes directly
    to S3 via multipart upload — no local staging volume required.
    Requires a Globus subscription with the S3 add-on (standard tier).
    When false (default), use a POSIX gateway over a local EBS staging volume;
    works with any GCS endpoint and does not require a Globus subscription.
  EOT
  type        = bool
  default     = false
}

variable "globus_client_id" {
  description = "Client ID of the Globus service account app registered in the Globus Developers Portal. Used by gcs-finalize-setup to create the endpoint as a service account rather than a personal identity, enabling association with the institution's Globus subscription."
  type        = string
}

variable "globus_source_collection_id" {
  description = "Globus source collection UUID (remote endpoint data is transferred from). Stored in SSM so flows read it at runtime."
  type        = string
}

variable "globus_source_base_path" {
  description = "Root path on the source collection containing subject data. Subject ID is appended automatically. Stored in SSM so flows read it at runtime."
  type        = string
}

variable "globus_owner_email" {
  description = "Globus identity (email) that owns the endpoint — typically an institutional Globus admin. Used as the endpoint --owner during setup."
  type        = string
}

variable "globus_identity_domain" {
  description = "Identity domain permitted to authenticate to the storage gateway (e.g. your institution's domain, 'example.edu'). Passed as --domain during gateway creation."
  type        = string
}

