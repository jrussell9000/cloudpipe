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
  description = "Fastsurfer git-sha tag baked into the GPU AMI. Informational only — AMI selection is keyed on fastsurfer_ami_digest. Kept so the pinned image is traceable back to a source commit."
  type        = string
}

variable "fireants_ami_tag" {
  description = "FireANTs git-sha tag baked into the GPU AMI. Informational only — AMI selection is keyed on fireants_ami_digest."
  type        = string
}

# Selection is keyed on the digest, not the tag. A rebuild at an unchanged
# commit yields the same tag but a different image, so a tag-keyed selector
# would keep matching the OLD AMI while the workflow templates pinned the new
# digest — the pre-bake would silently become a cache miss and every GPU node
# would re-pull ~16 GB at pod start (the #123 failure mode).
variable "fastsurfer_ami_digest" {
  description = "Fastsurfer image digest (sha256:...) pre-baked into the GPU AMI, used to select it via Karpenter amiSelectorTerms. MUST equal the digest pinned in the workflow templates."
  type        = string

  validation {
    condition     = can(regex("^sha256:[a-f0-9]{64}$", var.fastsurfer_ami_digest))
    error_message = "The fastsurfer_ami_digest variable must be a full image digest of the form sha256:<64 hex chars>."
  }
}

variable "fireants_ami_digest" {
  description = "FireANTs image digest (sha256:...) pre-baked into the GPU AMI, used to select it via Karpenter amiSelectorTerms. MUST equal the digest pinned in registration-workflow-template.yaml."
  type        = string

  validation {
    condition     = can(regex("^sha256:[a-f0-9]{64}$", var.fireants_ami_digest))
    error_message = "The fireants_ami_digest variable must be a full image digest of the form sha256:<64 hex chars>."
  }
}

variable "eks_version" {
  description = "EKS cluster version (e.g. 1.35). Must match the version used when the GPU AMI was built."
  type        = string
}
