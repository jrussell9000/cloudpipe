################################################################################
# Athena workgroup for CloudPipe metrics queries
#
# Query results land in the finops bucket, alongside the CUR workgroup's own
# results, so they share one encryption setting and one expiry rule.
#
# This comment used to say they "share the same lifecycle policy". They did not:
# the finops bucket had no lifecycle configuration at all, so results from this
# workgroup accumulated indefinitely (#645). The rule now exists, in
# modules/finops/athenacloudcur.tf, scoped to the two result prefixes —
# grafana-query-results/ for this workgroup and query-results/ for the CUR one.
# Results are a materialised copy of whatever the query selected, and the tables
# below are subject-keyed, which is why they expire rather than being kept.
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
