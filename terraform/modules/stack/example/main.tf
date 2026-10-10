# A minimal root module that deploys the CloudPipe stack.
#
# This is the starting point for a new deployment: copy this directory somewhere
# outside the repository, point `source` at the module (or at a tagged release of
# it), and fill in your own `terraform.tfvars`. It is a real root, not a sketch —
# the same file layout the reference deployment uses, with the parts that are
# specific to that deployment removed.
#
# The root holds exactly what a module may not: the backend, the provider
# configurations, the variable declarations `terraform.tfvars` binds to, and the
# re-exported outputs. Everything that is actually created lives in the module.
# See design D4 and D5 of
# openspec/changes/archive/2026-10-01-public-upstream-readiness, and
# ../../../../docs/infrastructure.md.
#
# CI runs `terraform init -backend=false && terraform validate` here, so this
# file cannot fall behind the module's interface without a red check.

terraform {
  required_version = ">= 1.10"

  # No backend here, deliberately: CI validates this root with
  # `-backend=false`, and a backend block would send it looking for state.
  # A real deployment uses S3 with a lock file — see
  # ../../../../docs-internal/decisions/015-s3-backend-after-state-loss.md, which exists
  # because local state was lost once. Uncomment and fill in before the first
  # apply:
  #
  # backend "s3" {
  #   bucket       = "<your-state-bucket>"
  #   key          = "cloudpipe/terraform.tfstate"
  #   region       = "<your-region>"
  #   encrypt      = true
  #   use_lockfile = true
  # }

  # Mirrors the module's own `required_providers`. `kubernetes`, `local`, `tls`,
  # `time` and `null` are deliberately absent from both: they are inferred from
  # the resources that use them, so their versions come from one place — the
  # committed `.terraform.lock.hcl` — rather than from two constraints that drift.
  #
  # That lock file is a byte-for-byte copy of the reference deployment's, and CI
  # initialises this root with `-lockfile=readonly` so it cannot quietly resolve
  # something else. Without a lock, `init` re-fetches each provider's SHA256SUMS
  # to establish trust, which is a live dependency on that provider's release
  # hosting — it broke CI once, on a gavinbunney/kubectl checksum timeout.
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
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

################################################################################
# Providers
#
# The module declares `configuration_aliases = [aws.us_east_1, aws.billing]`, so
# both must exist here and be named in the `providers` map below. Terraform hands
# a child module the default configuration of each provider automatically, but
# never an aliased one.
################################################################################

provider "aws" {
  region = var.region
}

# Services that exist only in us-east-1 regardless of var.region: ECR Public, and
# ACM certificates attached to CloudFront. Pinned, not derived — moving this
# alias to another region breaks those two.
provider "aws" {
  region = "us-east-1"
  alias  = "us_east_1"
}

# Cost and Usage Report resources, which AWS also confines to us-east-1.
provider "aws" {
  region = "us-east-1"
  alias  = "billing"
}

# Cloudflare authenticates from the CLOUDFLARE_API_TOKEN environment variable,
# never a Terraform variable — a variable would persist the token in state. That
# is why this block is empty. The token needs account-level Zero Trust: Edit,
# plus Tunnel and Access permissions.
provider "cloudflare" {}

################################################################################
# Cluster endpoint for the Kubernetes-family providers
#
# These two data sources are the reason a provider configuration cannot live in
# the module: a provider may not be configured from a module's outputs, so the
# cluster has to be looked up here, in the root, by name.
#
# Consequence worth knowing before the first apply: this lookup fails while the
# cluster does not exist yet. A fresh deployment is phased (`-target` the VPC and
# the cluster first) rather than one apply — see "Bootstrap and install sequence"
# in ../../../../docs/infrastructure.md. Do not run a bare `terraform apply`
# against an empty account and conclude the example is broken.
################################################################################

data "aws_eks_cluster" "upstream" {
  name = var.name
}

data "aws_ecrpublic_authorization_token" "token" {
  provider = aws.us_east_1
}

# exec-based auth calls `aws eks get-token` on demand, so a long apply cannot
# fail halfway through on an expired token. The `registries` block feeds a fresh
# ECR Public token to Helm on every apply, which removes the manual
# `helm registry login` step.
provider "helm" {
  registries = [
    {
      url      = "oci://public.ecr.aws"
      username = data.aws_ecrpublic_authorization_token.token.user_name
      password = data.aws_ecrpublic_authorization_token.token.password
    }
  ]
  kubernetes = {
    host                   = data.aws_eks_cluster.upstream.endpoint
    cluster_ca_certificate = base64decode(data.aws_eks_cluster.upstream.certificate_authority[0].data)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", var.name, "--region", var.region]
    }
  }
}

provider "kubernetes" {
  host                   = data.aws_eks_cluster.upstream.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.upstream.certificate_authority[0].data)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", var.name, "--region", var.region]
  }
}

# gavinbunney/kubectl, for raw manifests the kubernetes provider cannot express.
provider "kubectl" {
  host                   = data.aws_eks_cluster.upstream.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.upstream.certificate_authority[0].data)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", var.name, "--region", var.region]
  }
}

################################################################################
# Inputs
#
# Only the module inputs that have no default are declared here. Every one of
# them identifies a deployment — its domain, its accounts, its repository, its
# data — so the module refuses to guess: with no default, Terraform prompts for a
# missing value, or fails under `-input=false` naming the variable.
#
# The descriptions and the validation rules live on the module's own declarations
# in ../variables.tf, which is where a caller reads them; they are not repeated
# here. To change any input that does have a default (`vpc_cidr`,
# `kubernetes_version`, `prefect_namespace`, and ~30 others), add a `variable`
# block here and one line in the module call below. `name` is included as the
# worked example of that, because the provider configurations above need it.
################################################################################

variable "name" {
  description = "Resource name prefix, and the EKS cluster name the providers above look up."
  type        = string
  default     = "cloudpipe"
}

variable "region" {
  type = string
}

variable "domain" {
  type = string
}

# The module's `external_identity` input, for a deployment that already
# integrates an identity provider directly. There is no sensible example value:
# the three IDs come from resources the deployment creates itself, outside this
# module, and a direct integration is that provider's shape rather than a
# generic one. See design D6 of
# openspec/changes/optional-domain-and-cognito-auth.
#
# A new deployment does not set this. Leaving it null gets the Cognito user
# pool the module creates, and an institutional provider reaches that pool
# through Cognito federation.
variable "external_identity" {
  type = object({
    access_identity_provider_id = string
    access_policy_id            = string
    dex_connector = object({
      id            = string
      name          = string
      issuer        = string
      client_id     = string
      client_secret = string
    })
  })
  sensitive = true
  default   = null
}

variable "operator_emails" {
  type = list(string)
}

variable "github_user_url" {
  type = string
}

variable "github_repo" {
  type = string
}

variable "gitops_repo_url" {
  type = string
}

variable "github_oidc_allowed_subs" {
  type = list(string)
}

variable "cloudflare_account_id" {
  type = string
}

variable "cloudflare_team_domain" {
  type = string
}

variable "cloudflare_team_name" {
  type = string
}

variable "globus_s3_destination_bucket" {
  type = string
}

# The Globus ingress and the seven inputs it needs.
#
# These do have defaults in the module — `globus_enabled` is false and the seven
# are empty — so by the rule above they would not be declared here at all. They
# are, because they are the one group of optional inputs a deployer is likely to
# want and cannot discover from an empty root: turning the ingress on means
# setting eight values at once, and the module refuses an empty one once the
# flag is true. Leaving them out would mean editing this file before editing
# tfvars.
variable "globus_enabled" {
  type    = bool
  default = false
}

variable "globus_source_collection_id" {
  type    = string
  default = ""
}

variable "globus_client_id" {
  type    = string
  default = ""
}

variable "globus_admin_prefix_list_id" {
  type    = string
  default = ""
}

variable "globus_org_name" {
  type    = string
  default = ""
}

variable "globus_contact_email" {
  type    = string
  default = ""
}

variable "globus_owner_email" {
  type    = string
  default = ""
}

variable "globus_identity_domain" {
  type    = string
  default = ""
}

################################################################################
# The stack
################################################################################

module "stack" {
  source = "../"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
    aws.billing   = aws.billing
  }

  name                         = var.name
  region                       = var.region
  domain                       = var.domain
  external_identity            = var.external_identity
  operator_emails              = var.operator_emails
  github_user_url              = var.github_user_url
  github_repo                  = var.github_repo
  gitops_repo_url              = var.gitops_repo_url
  github_oidc_allowed_subs     = var.github_oidc_allowed_subs
  cloudflare_account_id        = var.cloudflare_account_id
  cloudflare_team_domain       = var.cloudflare_team_domain
  cloudflare_team_name         = var.cloudflare_team_name
  globus_enabled               = var.globus_enabled
  globus_s3_destination_bucket = var.globus_s3_destination_bucket
  globus_source_collection_id  = var.globus_source_collection_id
  globus_client_id             = var.globus_client_id
  globus_admin_prefix_list_id  = var.globus_admin_prefix_list_id
  globus_org_name              = var.globus_org_name
  globus_contact_email         = var.globus_contact_email
  globus_owner_email           = var.globus_owner_email
  globus_identity_domain       = var.globus_identity_domain
}

################################################################################
# Outputs
#
# An output of a child module is not an output of the root. Every value the
# module exposes is forwarded here under the same name, so `terraform output`
# prints it — the bootstrap steps and the image-build workflows read several of
# these. Descriptions stay on the module's declarations.
################################################################################

output "cloudflare_account_id" {
  value = module.stack.cloudflare_account_id
}

# The install and teardown scripts read these two; see ../installer.tf for why
# they are outputs rather than something a script works out for itself.
output "cluster_name" {
  value = module.stack.cluster_name
}

# Re-exported for the teardown, which names the buckets it did not delete.
output "data_bucket" {
  value = module.stack.data_bucket
}

output "metrics_bucket" {
  value = module.stack.metrics_bucket
}

output "terraform_state_bucket" {
  value = module.stack.terraform_state_bucket
}

output "region" {
  value = module.stack.region
}

output "cloudflare_tunnel_id" {
  value = module.stack.cloudflare_tunnel_id
}

output "cognito_user_pool_id" {
  value = module.stack.cognito_user_pool_id
}

output "cognito_federation_redirect_uri" {
  value = module.stack.cognito_federation_redirect_uri
}

output "ecr_cache_registry" {
  value = module.stack.ecr_cache_registry
}

output "ecr_private_registry" {
  value = module.stack.ecr_private_registry
}

output "ecr_registry" {
  value = module.stack.ecr_registry
}

output "github_actions_ecr_role_arn" {
  value = module.stack.github_actions_ecr_role_arn
}

output "github_actions_packer_role_arn" {
  value = module.stack.github_actions_packer_role_arn
}

output "globus_collection_id_ssm_parameter" {
  value = module.stack.globus_collection_id_ssm_parameter
}

output "globus_public_ip" {
  value = module.stack.globus_public_ip
}

output "globus_staging" {
  value = module.stack.globus_staging
}

output "security_findings_topic_arn" {
  value = module.stack.security_findings_topic_arn
}

output "ui_alb_waf_web_acl_arn" {
  value = module.stack.ui_alb_waf_web_acl_arn
}
