# The three names the stack root has to repeat. `globus_s3_destination_bucket`
# and `metrics_bucket` there must equal `data_bucket` and `metrics_bucket` here,
# and backend.tf must name `state_bucket` — so when preflight finds one unset, it
# points at `terraform output` in this root rather than asking the deployer to
# remember what they typed.

output "state_bucket" {
  description = "The bucket backend.tf in the stack root names."
  value       = aws_s3_bucket.terraform_state.bucket
}

output "data_bucket" {
  description = "The imaging data bucket: the stack root's globus_s3_destination_bucket."
  value       = var.data_bucket
}

output "metrics_bucket" {
  description = "The metrics bucket: the stack root's metrics_bucket, unless it uses the <name>-metrics default."
  value       = var.metrics_bucket
}
