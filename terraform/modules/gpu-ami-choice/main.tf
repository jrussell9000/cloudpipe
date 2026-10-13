# Which AMI the GPU node class launches, decided here because Karpenter cannot.
#
# Karpenter refuses an `alias` selector term alongside any other term ("'alias'
# is mutually exclusive, cannot be set with a combination of other
# amiSelectorTerms"), so "use the pre-baked AMI if there is one, else the stock
# one" cannot be written as a node class. Terraform chooses, and the node class
# is rendered with exactly one kind of term (openspec/changes/
# publish-images-for-deployers, decision 3). In order:
#
#   tags   This account owns a pre-baked AMI tagged with the current image
#          digests and EKS version. The reference deployment's own encrypted
#          bake. It must win, so the reference never starts launching a copy.
#   id     The publisher's public AMI for this region, pinned by ID in
#          published-gpu-amis.json, when that file's digests and EKS version are
#          the current ones. Pinned by ID, not found by name: an AMI ID can only
#          be created by its owner, while anyone can register a public AMI with
#          any name. The ID in a reviewed commit is the trust anchor, as a digest
#          is for an image.
#   alias  The stock EKS-optimized AL2023 AMI (Karpenter resolves the NVIDIA
#          variant for GPU instance types). GPU nodes then pull the images at
#          start instead of finding them baked in.
#
# No provider, no data source: everything it needs is passed in, which is what
# lets tests/test_terraform_gpu_ami_choice.py run every outcome offline.

variable "baked_ami_found" {
  description = "Whether this account owns a pre-baked GPU AMI tagged with the current digests and EKS version."
  type        = bool
}

variable "published" {
  description = "published-gpu-amis.json, decoded: { fastsurfer_digest, fireants_digest, eks_version, amis = { <region> = <ami id> } }."
  type        = any
}

variable "fastsurfer_digest" {
  type = string
}

variable "fireants_digest" {
  type = string
}

variable "eks_version" {
  type = string
}

variable "region" {
  type = string
}

variable "selection" {
  description = "`auto` chooses in the order above. `alias` always uses the stock AMI, for when a pinned public AMI cannot be launched."
  type        = string

  validation {
    condition     = contains(["auto", "alias"], var.selection)
    error_message = "selection must be \"auto\" or \"alias\"."
  }
}

variable "alias" {
  type = string

  validation {
    condition     = can(regex("^al2023@(latest|v[0-9]{8})$", var.alias))
    error_message = "alias must be an AL2023 Karpenter alias, al2023@latest or al2023@vYYYYMMDD; the node class's amiFamily is AL2023."
  }
}

locals {
  published_matches = (
    try(var.published.fastsurfer_digest, "") == var.fastsurfer_digest
    && try(var.published.fireants_digest, "") == var.fireants_digest
    && try(var.published.eks_version, "") == var.eks_version
  )
  published_id = local.published_matches ? try(var.published.amis[var.region], null) : null

  kind = (
    var.selection == "alias" ? "alias"
    : var.baked_ami_found ? "tags"
    : local.published_id != null ? "id"
    : "alias"
  )
}

output "kind" {
  description = "`tags`, `id` or `alias`: which selector term the node class is rendered with."
  value       = local.kind

  precondition {
    condition     = local.published_id == null || can(regex("^ami-[0-9a-f]{8,17}$", local.published_id))
    error_message = "published-gpu-amis.json pins something that is not an AMI ID for this region."
  }
}

output "ami_id" {
  description = "The published AMI ID when kind is `id`, else null."
  value       = local.kind == "id" ? local.published_id : null
}
