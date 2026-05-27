data "aws_caller_identity" "current" {}

data "aws_iam_role" "sfn_first_level" {
  name = var.sfn_first_level_role_name
}

data "aws_iam_role" "sfn_segment_subfields" {
  name = var.sfn_segment_subfields_role_name
}

locals {
  sfn_first_level_definition = templatefile("${path.module}/definitions/first_level_processing.asl.json", {
    account_id         = data.aws_caller_identity.current.account_id
    region             = var.region
    job_definition_arn = "arn:aws:batch:${var.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.main.name}"
    job_queue_arn      = aws_batch_job_queue.main.arn
  })

  sfn_segment_subfields_definition = templatefile("${path.module}/definitions/segment_subfields.asl.json", {
    account_id                 = data.aws_caller_identity.current.account_id
    region                     = var.region
    segment_job_definition_arn = var.segment_subfields_job_definition_arn
    segment_job_queue_arn      = var.segment_subfields_job_queue_arn
  })
}

resource "aws_sfn_state_machine" "first_level_processing" {
  name     = "ABCDv6-First-Level-Processing"
  role_arn = data.aws_iam_role.sfn_first_level.arn
  type     = "STANDARD"

  definition = local.sfn_first_level_definition

  logging_configuration {
    level                  = "OFF"
    include_execution_data = false
  }

  tracing_configuration {
    enabled = false
  }
}

resource "aws_sfn_state_machine" "segment_subfields" {
  name     = "ABCDv6-Segment-Subfields"
  role_arn = data.aws_iam_role.sfn_segment_subfields.arn
  type     = "STANDARD"

  definition = local.sfn_segment_subfields_definition

  logging_configuration {
    level                  = "OFF"
    include_execution_data = false
  }

  tracing_configuration {
    enabled = false
  }
}
