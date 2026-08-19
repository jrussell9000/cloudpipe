################################################################################
# IAM — Pod Identity role for Prefect worker
# The worker executes dispatcher flows that read S3, read SSM, and submit
# workflows directly to the Argo API. The server needs no AWS permissions.
################################################################################

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "worker" {
  # Read subject lists and scan for S3 completion markers
  statement {
    sid       = "S3ReadWrite"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${var.bucket}/*"]
  }

  statement {
    sid       = "S3ListBucket"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.bucket}"]
  }

  statement {
    sid       = "S3GetBucketLocation"
    actions   = ["s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::*"]
  }

  # The kubecost-cost-scraper flow writes CostAllocation records to the dedicated
  # metrics bucket. That scraper was repointed from the data bucket to this one,
  # but this role was not updated, so every nightly run with cost data to write
  # failed with AccessDenied on s3:PutObject (first observed 2026-07-24 02:00 UTC;
  # earlier nightlies passed only because they had nothing to write).
  #
  # PutObject/GetObject but NOT DeleteObject, matching the argo-workflows module:
  # the metrics bucket is the run of record, and nothing that flushes derivatives
  # should be able to delete from it. See terraform/metrics_bucket.tf.
  statement {
    sid       = "ListMetricsBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${var.metrics_bucket}"]
  }

  statement {
    sid       = "S3MetricsReadWrite"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["arn:aws:s3:::${var.metrics_bucket}/*"]
  }

  # The pod-cost pass looks up on-demand LIST prices to build the no-spot
  # counterfactual (src/metrics/ec2_pricing.py). Granted here in the same change
  # that starts calling it: the lookup swallows its own failures by design, so
  # without this the scraper would keep succeeding while writing NULL rate
  # columns every night, and nothing downstream would say why.
  #
  # The Pricing API is global and has no resource-level ARNs — "*" is the only
  # valid resource for it. Read-only, and it exposes public list prices, not
  # this account's bill.
  statement {
    sid       = "PricingReadOnDemandRates"
    actions   = ["pricing:GetProducts"]
    resources = ["*"]
  }

  # cloudpipe_queue_manager reads the Globus dest collection UUID at runtime
  # so instance replacements take effect without redeploying the flow.
  statement {
    sid     = "SSMReadGlobusParams"
    actions = ["ssm:GetParameter"]
    resources = [
      "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${var.cluster_name}/globus/collection-id",
      "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${var.cluster_name}/globus/source-collection-id",
      "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${var.cluster_name}/globus/source-base-path",
    ]
  }
}

resource "aws_iam_policy" "worker" {
  name   = "${var.cluster_name}-prefect-worker"
  policy = data.aws_iam_policy_document.worker.json
  tags   = var.tags
}

module "worker_pod_identity" {
  source = "terraform-aws-modules/eks-pod-identity/aws"

  name = "${var.cluster_name}-prefect-worker"
  additional_policy_arns = {
    worker = aws_iam_policy.worker.arn
  }

  associations = {
    cloudpipe = {
      cluster_name    = var.cluster_name
      namespace       = var.namespace
      service_account = var.worker_sa_name
    }
  }

  tags = var.tags
}

################################################################################
# RBAC — Kubernetes work pool job management
# The Prefect worker (Kubernetes work pool) creates and monitors Jobs
# in the prefect namespace to execute flow runs.
################################################################################

resource "kubernetes_role_v1" "worker_jobs" {
  metadata {
    name      = "prefect-worker-jobs"
    namespace = var.namespace
  }

  rule {
    api_groups = ["batch"]
    resources  = ["jobs"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }

  rule {
    api_groups = [""]
    resources  = ["pods", "pods/log"]
    verbs      = ["get", "list", "watch"]
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}

resource "kubernetes_role_binding_v1" "worker_jobs" {
  metadata {
    name      = "prefect-worker-jobs"
    namespace = var.namespace
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.worker_jobs.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = var.worker_sa_name
    namespace = var.namespace
  }
}
