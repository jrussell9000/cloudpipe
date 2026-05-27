output "glue_database_name" {
  description = "Name of the Glue catalog database for CloudPipe metrics."
  value       = aws_glue_catalog_database.metrics.name
}

output "athena_workgroup_name" {
  description = "Name of the Athena workgroup for CloudPipe metrics queries."
  value       = aws_athena_workgroup.cloudpipe_metrics.name
}

output "grafana_role_arn" {
  description = "ARN of the IAM role assigned to the Grafana pod via Pod Identity."
  value       = module.grafana_pod_identity.iam_role_arn
}
