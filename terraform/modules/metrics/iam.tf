data "aws_caller_identity" "current" {}

################################################################################
# Glue crawler service role
################################################################################

data "aws_iam_policy_document" "crawler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["glue.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "metrics_crawler" {
  name               = "cloudpipe-metrics-crawler"
  assume_role_policy = data.aws_iam_policy_document.crawler_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "metrics_crawler_glue_service" {
  role       = aws_iam_role.metrics_crawler.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

data "aws_iam_policy_document" "metrics_crawler_s3" {
  statement {
    sid       = "ListMetricsBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${var.bucket}"]
  }

  statement {
    sid       = "ReadMetricsObjects"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${var.bucket}/metrics/*"]
  }
}

resource "aws_iam_policy" "metrics_crawler_s3" {
  name   = "cloudpipe-metrics-crawler-s3"
  policy = data.aws_iam_policy_document.metrics_crawler_s3.json
  tags   = var.tags
}

resource "aws_iam_role_policy_attachment" "metrics_crawler_s3" {
  role       = aws_iam_role.metrics_crawler.name
  policy_arn = aws_iam_policy.metrics_crawler_s3.arn
}

################################################################################
# Grafana Pod Identity — Athena query + Glue read + S3 for query results
################################################################################

data "aws_iam_policy_document" "grafana" {
  # Athena: execute queries and read results
  statement {
    sid = "AthenaQuery"
    actions = [
      "athena:StartQueryExecution",
      "athena:GetQueryExecution",
      "athena:GetQueryResults",
      "athena:StopQueryExecution",
      "athena:ListQueryExecutions",
      "athena:GetWorkGroup",
    ]
    resources = [
      "arn:aws:athena:${var.region}:${data.aws_caller_identity.current.account_id}:workgroup/cloudpipe_metrics_workgroup",
    ]
  }

  # Glue: read the cloudpipe_metrics catalog
  statement {
    sid = "GlueRead"
    actions = [
      "glue:GetDatabase",
      "glue:GetDatabases",
      "glue:GetTable",
      "glue:GetTables",
      "glue:GetPartition",
      "glue:GetPartitions",
    ]
    resources = [
      "arn:aws:glue:${var.region}:${data.aws_caller_identity.current.account_id}:catalog",
      "arn:aws:glue:${var.region}:${data.aws_caller_identity.current.account_id}:database/cloudpipe_metrics",
      "arn:aws:glue:${var.region}:${data.aws_caller_identity.current.account_id}:table/cloudpipe_metrics/*",
    ]
  }

  # S3: read metrics data + write Athena query results
  statement {
    sid       = "S3ReadMetrics"
    actions   = ["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${var.bucket}", "arn:aws:s3:::${var.bucket}/metrics/*"]
  }

  statement {
    sid     = "S3AthenaResults"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = [
      "arn:aws:s3:::${var.finops_bucket}",
      "arn:aws:s3:::${var.finops_bucket}/grafana-query-results/*",
    ]
  }
}

resource "aws_iam_policy" "grafana" {
  name   = "cloudpipe-grafana-metrics"
  policy = data.aws_iam_policy_document.grafana.json
  tags   = var.tags
}

module "grafana_pod_identity" {
  source = "terraform-aws-modules/eks-pod-identity/aws"

  name = "cloudpipe-grafana"
  additional_policy_arns = {
    grafana = aws_iam_policy.grafana.arn
  }

  associations = {
    grafana = {
      cluster_name    = var.cluster_name
      namespace       = var.grafana_namespace
      service_account = var.grafana_sa_name
    }
  }

  tags = var.tags
}
