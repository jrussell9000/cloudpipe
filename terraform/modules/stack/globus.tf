# The Globus Connect Server ingress: 52 AWS resources, off unless asked for.
#
# This is the one subsystem in the stack that a deployment can be complete
# without. It exists to pull ABCD imaging from one institution's Globus
# collection, and standing it up needs three things no fork of this repository
# inherits: a Globus subscription (the S3 connector is a paid feature), a
# registered Globus service client, and a GCS AMI built from packer/globus-gcs/
# in the deployer's own account — `globus_ami_id` below is this deployment's
# image id, shared with nobody, so a second account cannot launch it at all.
#
# Ungated, that made four prerequisites a deployer could not satisfy and nothing
# checked: the AMI, the subscription, a managed prefix list, and seven required
# variables. None of them surface until the first untargeted apply, which the
# phased installer reaches at phase 4 — four phases and a live cluster after the
# point of no return. Found standing up the second deployment (2026-10-09).
#
# The pipeline already runs without it: `ingress-mode=presynced` reads data
# someone else put in the bucket, and the session gate skips entirely
# (openspec/specs/globus-session-lifecycle/spec.md). So `false` is a working
# deployment, not a broken one.
module "globus" {
  source = "../globus"
  count  = var.globus_enabled ? 1 : 0

  name       = local.name
  region     = local.region
  partition  = local.partition
  account_id = local.account_id

  vpc_id           = module.vpc.vpc_id
  public_subnet_id = module.vpc.public_subnets[0]

  # The AMI the Globus Connect Server host boots.
  #
  # Written by .github/workflows/build-globus-gcs-ami.yaml, which builds
  # packer/globus-gcs/ and opens a pull request changing this one line. A pull
  # request rather than a push to main (which is how the GPU node AMI pointer
  # moves) because `ami` is ForceNew: merging this line REPLACES the GCS host.
  # That is a reviewable decision, not a side effect of a build finishing.
  #
  # A literal here rather than a variable in terraform.tfvars: that file carries
  # two lines, so every other input to this deployment comes from a default, and
  # an AMI id is a build artifact rather than something an operator answers. It
  # is also deliberately not one of the values `globus init` renders — see
  # tests/test_terraform_globus_config.py, which holds the rendered set exactly
  # equal to the answers document's.
  #
  # The migration has happened: this is the baked image
  # (cloudpipe-globus-gcs-5.4.98-20260925011242), and the live host boots it. The
  # three mechanisms that made the replacement survivable all held — the
  # deployment key was read back from SSM, cloudpipe-gcs-boot re-registered the
  # node, and the Elastic IP re-associated. Task 10.6a records the evidence.
  #
  # Bumping this line again replaces the instance again, on the same terms.
  globus_ami_id = "ami-0cf1672737681f4a0"

  globus_s3_bucket            = var.globus_s3_destination_bucket
  globus_admin_prefix_list_id = var.globus_admin_prefix_list_id
  globus_org_name             = var.globus_org_name
  globus_contact_email        = var.globus_contact_email
  globus_owner_email          = var.globus_owner_email
  globus_identity_domain      = var.globus_identity_domain
  globus_collection_name      = var.globus_collection_name
  globus_gateway_name         = var.globus_gateway_name
  globus_client_id            = var.globus_client_id
  globus_source_collection_id = var.globus_source_collection_id
  globus_source_base_path     = var.globus_source_base_path
  globus_staging_enabled      = var.globus_staging_enabled
  globus_session_timeout_days = var.globus_session_timeout_days
  globus_production_managed   = var.globus_production_managed


  argo_runner_iam_role_name = module.argo_workflows.runner_iam_role_name
}

# Adding the count above renamed every object in the module from
# `module.globus.*` to `module.globus[0].*`, and Terraform reads the old address
# as gone: without this, a deployment that already has the ingress plans a
# destroy and a create of all 52 resources, including the Elastic IP the Globus
# endpoint is registered against.
moved {
  from = module.globus
  to   = module.globus[0]
}

# `one(...[*])` rather than `[0]`, so each output is null when the ingress is
# off instead of failing the plan on an index that does not exist.
output "globus_public_ip" {
  description = "Elastic IP of the Globus Connect Server — use this when running endpoint setup. Null when globus_enabled is false."
  value       = one(module.globus[*].public_ip)
}

output "globus_collection_id_ssm_parameter" {
  description = "SSM parameter name where `globus bootstrap-endpoint` records the destination collection UUID. Null when globus_enabled is false."
  value       = one(module.globus[*].ssm_collection_id_parameter_name)
}

output "globus_staging" {
  description = "Staging test-bed names (IAM user, secrets, collection base path) for the operator CLI; null when staging is disabled or the ingress is not enabled"
  value       = one(module.globus[*].staging)
}
