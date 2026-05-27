################################################################################
# Athena workgroup for CloudPipe metrics queries
#
# Query results land in the finops bucket so they share the same lifecycle
# policy and encryption as existing Athena results.
################################################################################

resource "aws_athena_workgroup" "cloudpipe_metrics" {
  name = "cloudpipe_metrics_workgroup"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = false

    result_configuration {
      output_location = "s3://${var.finops_bucket}/grafana-query-results/"

      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }

  tags = var.tags
}
