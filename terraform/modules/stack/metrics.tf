################################################################################
# CloudPipe Metrics observability layer
#
# Glue crawlers, Athena workgroup, and IAM for querying pipeline QC metrics
# stored under s3://{bucket}/metrics/.  Grafana Pod Identity is created here
# so it is ready when the Grafana Helm chart is deployed via ArgoCD.
################################################################################

module "metrics" {
  source = "../metrics"

  # The dedicated metrics bucket, not the data bucket — see metrics_bucket.tf.
  # Crawler targets, Glue table locations, and the crawler/Grafana IAM policies
  # in modules/metrics/ are all scoped to "${var.bucket}/metrics/*", so they
  # follow from this one input.
  bucket            = local.metrics_bucket
  finops_bucket     = "${var.name}-finops"
  cluster_name      = var.name
  region            = var.region
  grafana_namespace = var.grafana_namespace
  depends_on        = [module.eks]
}
