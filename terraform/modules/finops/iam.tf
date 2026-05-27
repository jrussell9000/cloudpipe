# Create assume role policy for crawler service role
data "aws_iam_policy_document" "crawler-assume-policy" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["glue.amazonaws.com"]
    }
  }
}

# Create crawler service role and attach assume role policy
resource "aws_iam_role" "crawler-service-role" {
  name               = "crawler-service-role"
  assume_role_policy = data.aws_iam_policy_document.crawler-assume-policy.json
}

# Attach AWSGlueServiceRole policy to crawler service role
resource "aws_iam_role_policy_attachment" "AWSGlueServiceRole-attachment" {
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
  role       = aws_iam_role.crawler-service-role.name
}

# Create full access policy for S3 bucket holding CUR reports
data "aws_iam_policy_document" "cur-report-s3-access" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${local.finops_bucket_name}"]
  }
  statement {
    actions   = ["s3:*"]
    resources = ["arn:aws:s3:::${local.finops_bucket_name}/*"]
  }
}

resource "aws_iam_policy" "cur-report-s3-access" {
  name   = "cur-report-s3-access"
  policy = data.aws_iam_policy_document.cur-report-s3-access.json
}

# Attach bucket access policy to crawler service role
resource "aws_iam_role_policy_attachment" "cur-report-s3-access" {
  role       = aws_iam_role.crawler-service-role.name
  policy_arn = aws_iam_policy.cur-report-s3-access.arn
}

# Create put/query policy for S3 bucket holding CUR reports
data "aws_iam_policy_document" "s3-bucket-cur-report-policy" {
  statement {
    actions = [
      "s3:GetBucketAcl",
      "s3:GetBucketPolicy"
    ]
    principals {
      type        = "Service"
      identifiers = ["billingreports.amazonaws.com"]
    }
    resources = ["arn:aws:s3:::${local.finops_bucket_name}"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
  statement {
    actions = ["s3:PutObject"]
    principals {
      type        = "Service"
      identifiers = ["billingreports.amazonaws.com"]
    }
    resources = ["arn:aws:s3:::${local.finops_bucket_name}/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}


resource "aws_s3_bucket_policy" "s3-bucket-cur-report-policy" {
  bucket = aws_s3_bucket.finops.id
  policy = data.aws_iam_policy_document.s3-bucket-cur-report-policy.json
}

# Kubecost policies

# The unified policy document for single-account access
data "aws_iam_policy_document" "kubecost_cur_integration_policy" {

  # Athena Results Bucket - Read/Write
  statement {
    sid    = "KubecostAthenaResultsReadWrite"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
      "s3:GetBucketLocation"
    ]
    resources = [
      "arn:aws:s3:::${local.finops_bucket_name}",
      "arn:aws:s3:::${local.finops_bucket_name}/*"
    ]
  }

  # Athena and Glue Access within the same account
  statement {
    sid    = "KubecostAthenaGlueAccess"
    effect = "Allow"
    actions = [
      "glue:GetDatabase",
      "glue:GetDatabases",
      "glue:GetTable",
      "glue:GetPartitions",
      "glue:GetPartition",
      "athena:StartQueryExecution",
      "athena:GetQueryExecution",
      "athena:GetQueryResults"
    ]
    resources = [
      "arn:aws:glue:*:${var.account_id}:database/${var.athena_database_name}",
      "arn:aws:glue:*:${var.account_id}:catalog",
      "arn:aws:glue:*:${var.account_id}:table/${var.athena_database_name}/*",
      "arn:aws:athena:*:${var.account_id}:workgroup/${var.athena_workgroup}"
    ]
  }
}

# Creates the standalone IAM Policy
resource "aws_iam_policy" "kubecost_cur_integration" {
  name        = "kubecost-cur-athena-integration"
  description = "Grants Kubecost direct access to CUR data via Athena in the same account"
  policy      = data.aws_iam_policy_document.kubecost_cur_integration_policy.json
}
