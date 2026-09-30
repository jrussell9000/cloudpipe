# Provider requirements for the stack module.
#
# No `backend` and no `provider` blocks: a module never configures either. The
# calling root owns both, which is what lets a second deployment run this same
# module against its own state and credentials (ADR 020).
#
# `configuration_aliases` declares the two aliased AWS providers this module
# expects to be handed. Terraform passes default provider configurations to a
# child module implicitly, but never aliased ones — without these two lines the
# resources that use them fail to plan, and the root's `providers` block would
# have nothing to bind to.
#
# The list otherwise mirrors the root's, deliberately: `kubernetes`, `local`,
# `tls`, `time` and `null` stay undeclared here exactly as they are there, so
# their versions keep coming from one place — the root's own `required_providers`
# and the committed `.terraform.lock.hcl` — rather than from two constraints that
# can drift apart.
terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source                = "hashicorp/aws"
      version               = "~> 6.0"
      configuration_aliases = [aws.us_east_1, aws.billing]
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
    kubectl = {
      source  = "gavinbunney/kubectl"
      version = "~> 1.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}
