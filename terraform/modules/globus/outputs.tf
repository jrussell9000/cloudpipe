output "public_ip" {
  description = "Elastic IP of the Globus Connect Server — use this when running endpoint setup"
  value       = aws_eip.globus.public_ip
}

output "instance_id" {
  description = "EC2 instance ID of the Globus Connect Server"
  value       = aws_instance.globus.id
}

output "ssm_parameter_name" {
  description = "SSM parameter name storing the Globus instance ID"
  value       = aws_ssm_parameter.globus_instance_id.name
}

output "ssm_collection_id_parameter_name" {
  description = "SSM parameter name storing the Globus collection UUID (written by `globus bootstrap-endpoint`)"
  value       = aws_ssm_parameter.globus_collection_id.name
}

output "staging" {
  description = "Names the operator CLI needs for the staging environment; null when staging is disabled. `iam_user_name` is null until an existing user is named in `globus_staging_iam_user_name` — the operator CLI treats that as a missing prerequisite, not as an error."
  value = var.globus_staging_enabled ? {
    gateway_name              = local.staging_gateway_name
    collection_name           = local.staging_gateway_name
    prefix                    = local.staging_prefix
    collection_base_path      = "/${var.globus_s3_bucket}/${local.staging_prefix}"
    iam_user_name             = one(data.aws_iam_user.globus_staging[*].user_name)
    credential_secret_name    = aws_secretsmanager_secret.globus_staging_s3_credential[0].name
    refresh_token_secret_name = aws_secretsmanager_secret.globus_staging_refresh_token[0].name
  } : null
}
