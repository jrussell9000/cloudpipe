variable "eks_cluster" {
  description = "The entire EKS cluster module object passed from the root."
  type        = any
}

variable "vpc_id" {
  description = "The VPC ID where the cluster is deployed."
  type        = string
}

variable "region" {
  description = "The AWS region."
  type        = string
}

variable "route53_zone_arn" {
  description = "ARN of the Route53 zone for external-dns and cert-manager."
  type        = string
}

variable "route53_zone_name" {
  description = "Name of the Route53 zone for external-dns and cert-manager."
  type        = string
}

variable "cluster_name" {
  description = "Name of the EKS cluster."
  type        = string
}

variable "tags" {
  description = "A map of tags to add to all resources."
  type        = map(string)
  default     = {}
}
