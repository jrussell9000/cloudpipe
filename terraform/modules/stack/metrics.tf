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
  bucket        = local.metrics_bucket
  finops_bucket = local.finops_bucket
  # module.eks.cluster_name, not var.name: both are "cloudpipe", but the
  # reference orders the Grafana pod identity association after the cluster
  # (and its access entries) without a module-level depends_on. That blanket
  # form made every Glue table depend on all of module.eks, so a targeted
  # metrics plan dragged in whatever EKS add-on versions AWS had published
  # since (#385).
  cluster_name      = module.eks.cluster_name
  region            = var.region
  grafana_namespace = var.grafana_namespace
}
