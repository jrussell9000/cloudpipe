######################
# Globus Connect Server
######################

data "aws_subnet" "public" {
  id = var.public_subnet_id
}

# Ubuntu 22.04 LTS x86_64
data "aws_ami" "ubuntu_2204" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# Security Group
resource "aws_security_group" "globus" {
  name_prefix = "${var.name}-globus-"
  description = "Globus Connect Server"
  vpc_id      = var.vpc_id
}

# Port 443 and GridFTP (50000-51000) must be open to 0.0.0.0/0 per Globus Connect Server v5
# architecture. HTTPS collections are accessed directly by clients (not relayed through Globus
# infrastructure), and GridFTP data channels are peer-to-peer between endpoints worldwide.
# Restricting either to specific CIDRs breaks interoperability with other institutions.
# Access control is enforced by Globus OAuth2/OIDC authentication, not network-layer filtering.
# See: https://docs.globus.org/globus-connect-server/v5/#open-tcp-ports_section
resource "aws_vpc_security_group_ingress_rule" "globus_https_v4" {
  security_group_id = aws_security_group.globus.id
  description       = "HTTPS inbound - required open to all (Globus HTTPS collections + GCS Manager API)"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "globus_https_v6" {
  security_group_id = aws_security_group.globus.id
  description       = "HTTPS inbound IPv6 - required open to all (Globus HTTPS collections + GCS Manager API)"
  cidr_ipv6         = "::/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "globus_gridftp" {
  security_group_id = aws_security_group.globus.id
  description       = "GridFTP data channel - required open to all (peer-to-peer between Globus endpoints)"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 50000
  to_port           = 51000
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "globus_ssh" {
  security_group_id = aws_security_group.globus.id
  description       = "SSH from managed prefix list"
  prefix_list_id    = var.globus_admin_prefix_list_id
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "globus_http" {
  security_group_id = aws_security_group.globus.id
  description       = "HTTP - Ubuntu package repository"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "globus_https" {
  security_group_id = aws_security_group.globus.id
  description       = "HTTPS - Globus REST API and S3"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "globus_gridftp" {
  security_group_id = aws_security_group.globus.id
  description       = "GridFTP data channel outbound"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 50000
  to_port           = 51000
  ip_protocol       = "tcp"
}

# IAM
data "aws_iam_policy_document" "globus_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "globus" {
  name_prefix        = "${var.name}-globus-"
  assume_role_policy = data.aws_iam_policy_document.globus_assume_role.json
}

data "aws_iam_policy_document" "globus_s3" {
  statement {
    sid    = "GlobusS3Access"
    effect = "Allow"
    actions = [
      "s3:GetBucketLocation",
      "s3:ListBucket",
      "s3:ListBucketMultipartUploads",
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = [
      "arn:${var.partition}:s3:::${var.globus_s3_bucket}",
      "arn:${var.partition}:s3:::${var.globus_s3_bucket}/*",
    ]
  }
}

resource "aws_iam_policy" "globus_s3" {
  name_prefix = "${var.name}-globus-s3-"
  policy      = data.aws_iam_policy_document.globus_s3.json
}

resource "aws_iam_role_policy_attachment" "globus_s3" {
  role       = aws_iam_role.globus.name
  policy_arn = aws_iam_policy.globus_s3.arn
}

resource "aws_iam_role_policy_attachment" "globus_ssm" {
  role       = aws_iam_role.globus.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "globus" {
  name_prefix = "${var.name}-globus-"
  role        = aws_iam_role.globus.name
}

# EC2 Instance
resource "aws_instance" "globus" {
  ami                    = data.aws_ami.ubuntu_2204.id
  instance_type          = "c5n.xlarge"
  subnet_id              = var.public_subnet_id
  vpc_security_group_ids = [aws_security_group.globus.id]
  iam_instance_profile   = aws_iam_instance_profile.globus.name

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    name                    = var.name
    region                  = var.region
    globus_org_name         = var.globus_org_name
    globus_contact_email    = var.globus_contact_email
    globus_s3_bucket        = var.globus_s3_bucket
    globus_collection_name  = var.globus_collection_name
    globus_client_id        = var.globus_client_id
    globus_use_s3_gateway   = var.globus_use_s3_gateway
  })

  user_data_replace_on_change = false

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 50
    encrypted             = true
    delete_on_termination = true
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  tags = {
    Name = "${var.name}-globus"
  }
}

################################################################################
# Staging EBS volume — POSIX gateway approach only.
# GridFTP writes here; globus-s3-sync-template syncs to S3 after.
# Not provisioned when globus_use_s3_gateway = true (S3 connector writes directly).
################################################################################

resource "aws_ebs_volume" "globus_staging" {
  count             = var.globus_use_s3_gateway ? 0 : 1
  availability_zone = data.aws_subnet.public.availability_zone
  size              = 500
  type              = "gp3"
  throughput        = 250
  iops              = 3000
  encrypted         = true

  tags = {
    Name = "${var.name}-globus-staging"
  }
}

resource "aws_volume_attachment" "globus_staging" {
  count       = var.globus_use_s3_gateway ? 0 : 1
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.globus_staging[0].id
  instance_id = aws_instance.globus.id
}

# Elastic IP
resource "aws_eip" "globus" {
  domain = "vpc"

  tags = {
    Name = "${var.name}-globus"
  }
}

resource "aws_eip_association" "globus" {
  instance_id   = aws_instance.globus.id
  allocation_id = aws_eip.globus.id
}

################################################################################
# SSM Parameters — instance ID and collection ID for workflow use
################################################################################

# Stored in SSM so the Argo start-instance step can discover the instance ID
# at runtime without baking it into the workflow template.
resource "aws_ssm_parameter" "globus_instance_id" {
  name  = "/${var.name}/globus/instance-id"
  type  = "String"
  value = aws_instance.globus.id

  tags = {
    Name = "${var.name}-globus-instance-id"
  }
}

resource "aws_ssm_parameter" "globus_source_collection_id" {
  name  = "/${var.name}/globus/source-collection-id"
  type  = "String"
  value = var.globus_source_collection_id

  tags = {
    Name = "${var.name}-globus-source-collection-id"
  }
}

resource "aws_ssm_parameter" "globus_source_base_path" {
  name  = "/${var.name}/globus/source-base-path"
  type  = "String"
  value = var.globus_source_base_path

  tags = {
    Name = "${var.name}-globus-source-base-path"
  }
}

# Placeholder — overwritten by gcs-finalize-setup after the one-time interactive
# Globus OAuth2 setup. Workflow submissions read this value at runtime.
resource "aws_ssm_parameter" "globus_collection_id" {
  name  = "/${var.name}/globus/collection-id"
  type  = "String"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    # Prevent Terraform from reverting the value written by gcs-finalize-setup.
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-collection-id"
  }
}

# Placeholder — overwritten by gcs-finalize-setup on first setup.
# On future instance replacements, user_data reads this key to run node setup
# non-interactively (no browser required — endpoint/collection/token stay the same).
resource "aws_ssm_parameter" "globus_deployment_key" {
  name  = "/${var.name}/globus/deployment-key"
  type  = "SecureString"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    # Prevent Terraform from reverting the value written by gcs-finalize-setup.
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-deployment-key"
  }
}

# Placeholders for Globus Auth service credentials used to run GCS CLI management
# commands (storage-gateway, collection, user-credentials) without an interactive
# login session.  Populate after one-time setup:
#   1. Register a service account at https://app.globus.org/settings/developers
#   2. Add it as an endpoint administrator:
#        globus-connect-server endpoint role create administrator \
#          <client-uuid>@clients.auth.globus.org
#   3. aws ssm put-parameter --name /${var.name}/globus/gcs-client-id   --value <uuid>   --type SecureString --overwrite
#      aws ssm put-parameter --name /${var.name}/globus/gcs-client-secret --value <secret> --type SecureString --overwrite
resource "aws_ssm_parameter" "globus_gcs_client_id" {
  name  = "/${var.name}/globus/gcs-client-id"
  type  = "SecureString"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-gcs-client-id"
  }
}

resource "aws_ssm_parameter" "globus_gcs_client_secret" {
  name  = "/${var.name}/globus/gcs-client-secret"
  type  = "SecureString"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-gcs-client-secret"
  }
}

# Placeholder — overwritten by gcs-finalize-setup after endpoint creation.
# Needed alongside gcs-client-id/secret so the instance can authenticate
# GCS CLI management commands without an interactive login session.
resource "aws_ssm_parameter" "globus_endpoint_id" {
  name  = "/${var.name}/globus/endpoint-id"
  type  = "String"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-endpoint-id"
  }
}

################################################################################
# Globus instance SSM write permission — lets gcs-finalize-setup store the
# collection UUID after the one-time interactive OAuth2 setup
################################################################################

data "aws_iam_policy_document" "globus_ssm_write" {
  statement {
    sid    = "WriteGlobusParams"
    effect = "Allow"
    actions = [
      "ssm:PutParameter",
    ]
    resources = [
      aws_ssm_parameter.globus_collection_id.arn,
      aws_ssm_parameter.globus_deployment_key.arn,
      aws_ssm_parameter.globus_endpoint_id.arn,
    ]
  }

  statement {
    sid    = "ReadGlobusInstanceParams"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
    ]
    resources = [
      aws_ssm_parameter.globus_deployment_key.arn,
      aws_ssm_parameter.globus_gcs_client_id.arn,
      aws_ssm_parameter.globus_gcs_client_secret.arn,
      aws_ssm_parameter.globus_endpoint_id.arn,
    ]
  }
}

resource "aws_iam_role_policy" "globus_ssm_write" {
  name   = "${var.name}-globus-ssm-write"
  role   = aws_iam_role.globus.name
  policy = data.aws_iam_policy_document.globus_ssm_write.json
}

################################################################################
# Argo runner EC2 permissions — start Globus instance + read SSM parameters.
# SSM send-command permissions are only needed for the POSIX staging approach
# (globus-s3-sync-template runs aws s3 sync on the instance via SSM).
################################################################################

resource "aws_iam_role_policy" "runner_globus_ec2" {
  name = "${var.name}-runner-globus-ec2"
  role = var.argo_runner_iam_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "StartGlobusInstance"
        Effect   = "Allow"
        Action   = ["ec2:StartInstances"]
        Resource = "arn:${var.partition}:ec2:${var.region}:${var.account_id}:instance/${aws_instance.globus.id}"
      },
      {
        # Describe actions are not resource-scoped by AWS
        Sid      = "DescribeGlobusInstance"
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstanceStatus", "ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        Sid    = "ReadGlobusSSMParameters"
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        Resource = [
          aws_ssm_parameter.globus_instance_id.arn,
          aws_ssm_parameter.globus_collection_id.arn,
        ]
      },
    ]
  })
}

# SSM send-command permissions — only needed for the POSIX staging approach.
# globus-s3-sync-template runs aws s3 sync on the GCS instance via SSM.
resource "aws_iam_role_policy" "runner_globus_ssm" {
  count = var.globus_use_s3_gateway ? 0 : 1
  name  = "${var.name}-runner-globus-ssm"
  role  = var.argo_runner_iam_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "SSMSendCommandGlobusInstance"
        Effect   = "Allow"
        Action   = ["ssm:SendCommand"]
        Resource = [
          "arn:${var.partition}:ec2:${var.region}:${var.account_id}:instance/${aws_instance.globus.id}",
          "arn:${var.partition}:ssm:${var.region}::document/AWS-RunShellScript",
        ]
      },
      {
        Sid      = "SSMGetCommandInvocationGlobusInstance"
        Effect   = "Allow"
        Action   = ["ssm:GetCommandInvocation"]
        Resource = "*"
      },
    ]
  })
}

################################################################################
# EventBridge Scheduler — nightly stop safety net
################################################################################

data "aws_iam_policy_document" "globus_scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "globus_scheduler" {
  name_prefix        = "${var.name}-globus-scheduler-"
  assume_role_policy = data.aws_iam_policy_document.globus_scheduler_assume.json
}

resource "aws_iam_role_policy" "globus_scheduler_ec2" {
  name = "${var.name}-globus-scheduler-ec2"
  role = aws_iam_role.globus_scheduler.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "StopGlobusInstance"
      Effect   = "Allow"
      Action   = ["ec2:StopInstances"]
      Resource = "arn:${var.partition}:ec2:${var.region}:${var.account_id}:instance/${aws_instance.globus.id}"
    }]
  })
}

# Stops the instance at midnight UTC every day.
# Acts as a safety net if a workflow completes without explicitly stopping the instance.
resource "aws_scheduler_schedule" "globus_stop" {
  name       = "${var.name}-globus-stop"
  group_name = "default"

  flexible_time_window { mode = "OFF" }
  schedule_expression          = "cron(0 0 * * ? *)"
  schedule_expression_timezone = "UTC"

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ec2:stopInstances"
    role_arn = aws_iam_role.globus_scheduler.arn
    input    = jsonencode({ InstanceIds = [aws_instance.globus.id] })
  }
}
