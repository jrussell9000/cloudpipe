output "sqs_queue_arn" {
  description = "ARN of the SQS FIFO job queue"
  value       = aws_sqs_queue.job_queue.arn
}

output "sqs_queue_url" {
  description = "URL of the SQS FIFO job queue"
  value       = aws_sqs_queue.job_queue.url
}

output "sqs_queue_name" {
  description = "Name of the SQS FIFO job queue"
  value       = aws_sqs_queue.job_queue.name
}
