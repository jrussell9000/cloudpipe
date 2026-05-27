output "namespace" {
  description = "Kubernetes namespace where Prefect is deployed."
  value       = data.kubernetes_namespace_v1.this.metadata[0].name
}

output "db_address" {
  description = "Endpoint address of the Prefect RDS PostgreSQL instance."
  value       = aws_db_instance.this.address
}

output "db_secret_arn" {
  description = "ARN of the RDS-managed Secrets Manager secret holding the Prefect database credentials."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "worker_iam_role_name" {
  description = "Name of the IAM role used by the Prefect worker service account."
  value       = module.worker_pod_identity.iam_role_name
}

output "lb_security_group_id" {
  description = "ID of the ALB security group for the Prefect server."
  value       = aws_security_group.lb.id
}
