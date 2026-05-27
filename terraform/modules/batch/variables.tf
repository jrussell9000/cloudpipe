variable "image" {
  description = "ECR image URI for the Batch job container"
  type        = string
}

variable "segment_subfields_job_definition_arn" {
  description = "ARN of the Batch job definition for the ABCDv6-Segment-Subfields state machine"
  type        = string
}

variable "segment_subfields_job_queue_arn" {
  description = "ARN of the Batch job queue for the ABCDv6-Segment-Subfields state machine"
  type        = string
}

variable "sfn_first_level_role_name" {
  description = "IAM role name for the ABCDv6-First-Level-Processing state machine"
  type        = string
}

variable "sfn_segment_subfields_role_name" {
  description = "IAM role name for the ABCDv6-Segment-Subfields state machine"
  type        = string
}

variable "instance_role" {
  description = "ARN of the IAM instance profile for ECS container instances"
  type        = string
}

variable "job_role_arn" {
  description = "ARN of the IAM role assumed by the Batch job container"
  type        = string
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "<YOUR_AWS_REGION>"
}

variable "security_group_ids" {
  description = "Security group IDs for the Batch compute environment"
  type        = list(string)
}

variable "subnets" {
  description = "Subnet IDs for the Batch compute environment"
  type        = list(string)
}
