variable "cluster_name" {
  description = "Name of the EKS cluster"
  type        = string
}

variable "cluster_endpoint" {
  description = "Endpoint for your Kubernetes API server"
  type        = string
}

variable "node_iam_role_additional_policies" {
  description = "Additional policies to be added to the IAM role created for Karpenter nodes"
  type        = map(string)
  default = {
    AmazonSSMManagedInstanceCore   = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    CloudWatchAgentServerPolicy    = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
    AmazonEKSVPCResourceController = "arn:aws:iam::aws:policy/AmazonEKSVPCResourceController"
  }
}

variable "karpenter_version" {
  description = "Version of the Karpenter Helm chart to install"
  type        = string
  default     = "1.11.0"
}

variable "fastsurfer_ami_tag" {
  description = "Fastsurfer image tag (e.g. sha-abc123) used to select the pre-baked GPU AMI via Karpenter amiSelectorTerms. Update when a new AMI is built."
  type        = string
}

variable "fireants_ami_tag" {
  description = "FireANTs image tag (e.g. sha-abc123) used to select the pre-baked GPU AMI via Karpenter amiSelectorTerms. Update when a new AMI is built."
  type        = string
}

variable "eks_version" {
  description = "EKS cluster version (e.g. 1.35). Must match the version used when the GPU AMI was built."
  type        = string
}
