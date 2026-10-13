output "node_iam_role_name" {
  description = "IAM role the Karpenter-launched nodes run as. The stack attaches the pull-through cache policy to it (image_cache.tf)."
  value       = module.karpenter_infra.node_iam_role_name
}
