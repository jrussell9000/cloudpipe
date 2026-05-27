# ── Launch Template ────────────────────────────────────────────────────────────
resource "aws_launch_template" "batch" {
  name = "Batch-50GBdisk"

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 50
      volume_type           = "gp3"
      iops                  = 3000
      throughput            = 125
      encrypted             = true
      delete_on_termination = true
    }
  }

  metadata_options {
    http_tokens                 = "required" # IMDSv2
    http_put_response_hop_limit = 1
  }

  user_data = base64encode(<<-MIME
    Content-Type: multipart/mixed; boundary="==BOUNDARY=="
    MIME-Version: 1.0

    --==BOUNDARY==
    Content-Type: text/x-shellscript; charset="us-ascii"

    #!/bin/bash
    set -euo pipefail

    # ── 1. Mount NVMe instance store ──────────────────────────────────────────
    DEVICE=$(lsblk -dpno NAME,MODEL | grep -i "Instance Storage" | awk '{print $1}')
    if [ -z "$DEVICE" ]; then
      DEVICE=/dev/nvme1n1
    fi
    mkfs.xfs "$DEVICE"
    mkdir -p /scratch
    mount "$DEVICE" /scratch
    chmod 1777 /scratch

    # ── 2. Enable EC2 detailed monitoring ─────────────────────────────────────
    TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
      -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
    INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
      http://169.254.169.254/latest/meta-data/instance-id)
    REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
      http://169.254.169.254/latest/meta-data/placement/region)
    aws ec2 monitor-instances --instance-ids "$INSTANCE_ID" --region "$REGION" \
      || echo "WARNING: Could not enable detailed monitoring, continuing anyway"

    # ── 3. Install CloudWatch agent ────────────────────────────────────────────
    yum install -y amazon-cloudwatch-agent \
      || { echo "WARNING: CloudWatch agent install failed"; exit 0; }

    # ── 4. Write agent config ──────────────────────────────────────────────────
    cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json << 'EOF'
    {
      "metrics": {
        "namespace": "BatchNode/Performance",
        "metrics_collected": {
          "cpu": {
            "measurement": ["cpu_usage_idle", "cpu_usage_iowait", "cpu_usage_user"],
            "metrics_collection_interval": 30,
            "totalcpu": true
          },
          "mem": {
            "measurement": ["mem_used_percent", "mem_available"],
            "metrics_collection_interval": 30
          },
          "disk": {
            "measurement": ["disk_used_percent", "disk_inodes_free", "disk_io_time"],
            "metrics_collection_interval": 30,
            "resources": ["/", "/scratch"]
          },
          "diskio": {
            "measurement": ["io_time", "read_bytes", "write_bytes", "reads", "writes"],
            "metrics_collection_interval": 30,
            "resources": ["*"]
          }
        },
        "append_dimensions": {
          "InstanceId": "$${aws:InstanceId}",
          "InstanceType": "$${aws:InstanceType}"
        }
      },
      "logs": {
        "logs_collected": {
          "files": {
            "collect_list": [
              {
                "file_path": "/var/log/messages",
                "log_group_name": "/aws/batch/instance",
                "log_stream_name": "{instance_id}/messages",
                "retention_in_days": 14
              },
              {
                "file_path": "/var/log/cloud-init-output.log",
                "log_group_name": "/aws/batch/instance",
                "log_stream_name": "{instance_id}/cloud-init",
                "retention_in_days": 14
              }
            ]
          }
        }
      }
    }
    EOF

    # ── 5. Start CloudWatch agent ──────────────────────────────────────────────
    /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
      -a fetch-config \
      -m ec2 \
      -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json \
      -s \
      || echo "WARNING: CloudWatch agent failed to start, continuing anyway"

    --==BOUNDARY==--
  MIME
  )
}

# ── Compute Environment ────────────────────────────────────────────────────────
resource "aws_batch_compute_environment" "main" {
  name_prefix = "abcdv6-first-level-processing-"
  type        = "MANAGED"
  state       = "ENABLED"

  lifecycle {
    create_before_destroy = true
  }

  compute_resources {
    type                = "SPOT"
    allocation_strategy = "SPOT_CAPACITY_OPTIMIZED"

    min_vcpus = 0
    max_vcpus = 256

    instance_type = [
      "m6gd.xlarge",
      "m6gd.2xlarge",
      "m6gd.4xlarge",
      "r6gd.xlarge",
      "r6gd.2xlarge",
      "r6gd.4xlarge",
      "c6gd.2xlarge",
      "c6gd.4xlarge",
      "m7gd.xlarge",
      "m7gd.2xlarge",
      "m7gd.4xlarge",
      "r7gd.xlarge",
      "r7gd.2xlarge"
    ]

    subnets            = var.subnets
    security_group_ids = var.security_group_ids
    instance_role      = var.instance_role

    launch_template {
      launch_template_name = aws_launch_template.batch.name
      version              = "$Latest"
    }

    tags = {
      Environment = "production"
      Project     = "abcdv6"
    }
  }
}

# ── Job Queue ──────────────────────────────────────────────────────────────────
resource "aws_batch_job_queue" "main" {
  name     = "abcdv6-first-level-processing-queue"
  state    = "ENABLED"
  priority = 1

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.main.arn
  }
}

