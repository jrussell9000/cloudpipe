################################################################################
# IAM — Pod Identity roles for controller, runner, and server
################################################################################

data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# Controller — S3 read/write for artifact storage
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "controller" {
  statement {
    sid       = "ListBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${var.bucket}"]
  }

  statement {
    sid       = "S3ReadWrite"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${var.bucket}/*"]
  }
}

resource "aws_iam_policy" "controller" {
  name   = "${var.cluster_name}-argo-controller"
  policy = data.aws_iam_policy_document.controller.json
  tags   = var.tags
}

module "controller_pod_identity" {
  source = "terraform-aws-modules/eks-pod-identity/aws"

  name = "${var.cluster_name}-argo-controller"
  additional_policy_arns = {
    controller = aws_iam_policy.controller.arn
  }

  associations = {
    cloudpipe = {
      cluster_name    = var.cluster_name
      namespace       = var.namespace
      service_account = var.controller_sa_name
    }
  }

  tags = var.tags
}

# ---------------------------------------------------------------------------
# Runner — S3 read/write for artifact access during workflow execution
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "runner" {
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
    resources = ["arn:aws:s3:::${var.bucket}"]
  }

  statement {
    sid       = "SSMGlobusCollectionId"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/cloudpipe/globus/*"]
  }

  statement {
    sid       = "AbcdV6S3ReadWrite"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::<YOUR_INPUT_S3_BUCKET>/*"]
  }

  statement {
    sid       = "AbcdV6S3List"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::<YOUR_INPUT_S3_BUCKET>"]
  }
}

resource "aws_iam_policy" "runner" {
  name   = "${var.cluster_name}-argo-runner"
  policy = data.aws_iam_policy_document.runner.json
  tags   = var.tags
}

module "runner_pod_identity" {
  source = "terraform-aws-modules/eks-pod-identity/aws"

  name = "${var.cluster_name}-argo-runner"
  additional_policy_arns = {
    runner = aws_iam_policy.runner.arn
  }

  associations = {
    cloudpipe = {
      cluster_name    = var.cluster_name
      namespace       = var.namespace
      service_account = var.runner_sa_name
    }
  }

  tags = var.tags
}

# ---------------------------------------------------------------------------
# Server — S3 read access for serving archived logs after pod deletion
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "server" {
  statement {
    sid       = "S3GetLogs"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${var.bucket}/*"]
  }
  statement {
    sid       = "S3ListBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${var.bucket}"]
  }
}

resource "aws_iam_policy" "server" {
  name   = "${var.cluster_name}-argo-server"
  policy = data.aws_iam_policy_document.server.json
  tags   = var.tags
}

module "server_pod_identity" {
  source = "terraform-aws-modules/eks-pod-identity/aws"

  name = "${var.cluster_name}-argo-server"
  additional_policy_arns = {
    server = aws_iam_policy.server.arn
  }

  associations = {
    cloudpipe = {
      cluster_name    = var.cluster_name
      namespace       = var.namespace
      service_account = var.server_sa_name
    }
  }

  tags = var.tags
}

################################################################################
# RBAC — Kubernetes Role + RoleBinding for the runner service account
################################################################################

resource "kubernetes_role_v1" "runner" {
  metadata {
    name      = var.runner_sa_name
    namespace = var.namespace
  }

  rule {
    api_groups = ["argoproj.io"]
    resources  = ["workflowtaskresults"]
    verbs      = ["create", "patch"]
  }
  rule {
    api_groups = ["argoproj.io"]
    resources  = ["workflows"]
    verbs      = ["get", "watch"]
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}

resource "kubernetes_role_binding_v1" "runner" {
  metadata {
    name      = "${var.runner_sa_name}-binding"
    namespace = var.namespace
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.runner.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = var.runner_sa_name
    namespace = var.namespace
  }
}
