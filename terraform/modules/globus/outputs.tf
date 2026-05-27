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
  description = "SSM parameter name storing the Globus collection UUID (written by gcs-finalize-setup)"
  value       = aws_ssm_parameter.globus_collection_id.name
}
