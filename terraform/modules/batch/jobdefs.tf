# ── Job Definition ─────────────────────────────────────────────────────────────
resource "aws_batch_job_definition" "main" {
  name                       = "abcdv6-first-level-processing"
  type                       = "container"
  deregister_on_new_revision = true


  retry_strategy {
    attempts = 3

    evaluate_on_exit {
      on_reason = "Host EC2*"
      action    = "RETRY"
    }

    evaluate_on_exit {
      on_reason = ".*"
      action    = "EXIT"
    }
  }

  timeout {
    attempt_duration_seconds = 7200
  }

  container_properties = jsonencode({
    image      = "public.ecr.aws/l9e7l1h1/cloudpipe/fmri-first-level-proc:sha-61de8f3d65ad4547b3021e0e6d8f04a5bd5aa1b9"
    jobRoleArn = var.job_role_arn

    resourceRequirements = [
      { type = "VCPU", value = "4" },
      { type = "MEMORY", value = "16384" }
    ]

    mountPoints = [
      {
        containerPath = "/scratch"
        readOnly      = false
        sourceVolume  = "scratch"
      }
    ]

    volumes = [
      {
        name = "scratch"
        host = {
          sourcePath = "/scratch"
        }
      }
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/aws/batch/fmri-first-level-proc"
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "fmri-first-level-proc"
      }
    }
  })

  propagate_tags = true
  tags = {
    Name = "fmri-first-level-processing"
  }

}
