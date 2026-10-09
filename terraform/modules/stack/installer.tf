# Outputs that exist for the install and teardown scripts, not for Terraform.
#
# Both scripts have to make `aws` calls — update the kubeconfig, read and write
# the tunnel token in Secrets Manager, list the user pool's users, find the
# shared ALB by tag — and every one of those needs the deployment's region and
# the cluster's name. Neither is available to a shell script any other honest
# way:
#
#   - `name` has a default, so a correct deployment may set it in no tfvars file
#     at all. A parser reading the deployer's tfvars would have to re-encode
#     Terraform's own defaults, which is a second source of truth for a value
#     Terraform already resolves.
#   - `terraform console` would answer both, and holds a state lock for as long
#     as it is open.
#   - The caller's AWS profile answers the region, and answers it wrongly: it
#     describes the workstation, not the deployment. `cloudpipe preflight`
#     shipped with exactly that bug.
#
# So the stack states them, the roots re-export them (a child module's output is
# not the root's), and the scripts read them with `terraform output -raw`.

output "region" {
  description = "The deployment's AWS region. Read by the install and teardown scripts, which steer every aws call at it."
  value       = var.region
}

output "cluster_name" {
  description = "The EKS cluster's name, which is var.name. Read by the install and teardown scripts for `aws eks update-kubeconfig`."
  value       = var.name
}

# The three buckets the bootstrap root owns and this stack only uses. The
# teardown names them at the end, because they are what it deliberately did NOT
# delete, and a deployer deciding whether they are finished needs to be told
# which buckets are still costing them money.
#
# Stated here for the same reason as the two above: two of the three have
# nullable variables that fall back to a `var.name` prefix, so a correct
# deployment may name them in no tfvars file at all. The teardown reads these
# BEFORE its destroy, since a destroyed root has no outputs left to read.

output "data_bucket" {
  description = "The imaging data bucket. An echo of globus_s3_destination_bucket, so the teardown can name a bucket it must not delete."
  value       = var.globus_s3_destination_bucket
}

output "metrics_bucket" {
  description = "The metrics bucket, resolved through its var.name fallback. Created by the bootstrap root; configured, never created, here."
  value       = local.metrics_bucket
}

output "terraform_state_bucket" {
  description = "The bucket holding this deployment's Terraform state, resolved through its var.name fallback."
  value       = local.cloudtrail_state_bucket
}
