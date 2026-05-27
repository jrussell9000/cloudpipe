output "namespace" {
  description = "Kubernetes namespace where Argo Workflows is deployed."
  value       = data.kubernetes_namespace_v1.this.metadata[0].name
}

output "db_address" {
  description = "Endpoint address of the RDS PostgreSQL instance."
  value       = aws_db_instance.this.address
}

output "runner_iam_role_name" {
  description = "Name of the IAM role used by the Argo Workflows runner service account."
  value       = module.runner_pod_identity.iam_role_name
}

output "db_secret_arn" {
  description = "ARN of the RDS-managed Secrets Manager secret holding the database credentials."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "lb_security_group_id" {
  description = "ID of the ALB security group for the Argo Workflows server."
  value       = aws_security_group.lb.id
}
