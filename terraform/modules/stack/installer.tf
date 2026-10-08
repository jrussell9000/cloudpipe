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
