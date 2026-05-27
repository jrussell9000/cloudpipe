output "external_secrets_role_arn" {
  value       = module.external_secrets_pod_identity.iam_role_arn
  description = "ARN of the External Secrets Pod Identity IAM role"
}

