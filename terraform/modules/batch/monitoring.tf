# ── Batch Job Event Monitoring ─────────────────────────────────────────────────

# Once deployed, each event record will contain detail.jobName, detail.status, detail.statusReason, detail.startedAt, and detail.stoppedAt. A Logs Insights query for duration and failures:

# fields detail.jobName, detail.status, detail.statusReason,
#        (detail.stoppedAt - detail.startedAt) / 1000 as duration_sec
# | filter detail.status = "FAILED"
# | sort @timestamp desc

resource "aws_cloudwatch_log_group" "batch_events" {
  name              = "/aws/batch/job-events"
  retention_in_days = 90
}

# EventBridge requires an explicit resource policy to write to CloudWatch Logs.
resource "aws_cloudwatch_log_resource_policy" "batch_events" {
  policy_name = "abcdv6-batch-events-policy"

  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"]
        }
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.batch_events.arn}:*"
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_cloudwatch_event_rule.batch_job_state.arn
          }
        }
      }
    ]
  })
}

resource "aws_cloudwatch_event_rule" "batch_job_state" {
  name        = "abcdv6-batch-job-state-changes"
  description = "Captures SUCCEEDED/FAILED state changes for the abcdv6-first-level-processing job definition"

  event_pattern = jsonencode({
    source      = ["aws.batch"]
    detail-type = ["Batch Job State Change"]
    detail = {
      jobDefinition = [
        {
          prefix = "arn:aws:batch:${var.region}:${data.aws_caller_identity.current.account_id}:job-definition/abcdv6-first-level-processing:"
        }
      ]
      status = ["SUCCEEDED", "FAILED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "batch_to_logs" {
  rule = aws_cloudwatch_event_rule.batch_job_state.name
  arn  = aws_cloudwatch_log_group.batch_events.arn
}
