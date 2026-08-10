
module "kubecost_agent_pod_identity" {
  source = "terraform-aws-modules/eks-pod-identity/aws"

  name = "kubecost-serviceaccount"

  attach_custom_policy = true

  additional_policy_arns = {
    KubecostCURIntegration = aws_iam_policy.kubecost_cur_integration.arn
  }

  policy_statements = [
    {
      sid = "BucketLevelAccess"
      actions = [
        "s3:ListBucket",
        "s3:GetBucketLocation" # Added for Thanos initial bucket validation
      ]
      effect = "Allow"
      resources = [
        aws_s3_bucket.finops.arn
      ]
    },
    {
      sid = "ObjectLevelAccess"
      actions = [
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:PutObject",
        "s3:PutObjectAcl"
      ]
      effect = "Allow"
      resources = [
        "${aws_s3_bucket.finops.arn}/*"
      ]
    },
    {
      sid       = "SpotPricingFeed"
      actions   = ["ec2:DescribeSpotPriceHistory"]
      effect    = "Allow"
      resources = ["*"]
    }
  ]

  associations = {
    # 1. Primary SA used by the Core App and FinOps Agent
    kubecost-serviceaccount = {
      cluster_name    = var.eks_cluster.cluster_name
      namespace       = "kubecost"
      service_account = "kubecost-serviceaccount"
    }
    # 2. Catch-all for the Aggregator in case Helm ignores the SA override
    kubecost-aggregator = {
      cluster_name    = var.eks_cluster.cluster_name
      namespace       = "kubecost"
      service_account = "kubecost"
    }
    # 3. Cluster controller uses its own dedicated SA
    kubecost-cluster-controller = {
      cluster_name    = var.eks_cluster.cluster_name
      namespace       = "kubecost"
      service_account = "kubecost-cluster-controller"
    }
  }
}

resource "kubernetes_namespace_v1" "kubecost" {
  metadata {
    name = "kubecost"
  }
}

# The chart's RBAC references these SAs but does not create them when create=false is set.
# They must exist before the Helm release so pods can schedule.
resource "kubernetes_service_account_v1" "kubecost_serviceaccount" {
  metadata {
    name      = "kubecost-serviceaccount"
    namespace = "kubecost"
  }
  depends_on = [kubernetes_namespace_v1.kubecost]
}

resource "kubernetes_service_account_v1" "kubecost_cluster_controller" {
  metadata {
    name      = "kubecost-cluster-controller"
    namespace = "kubecost"
  }
  depends_on = [kubernetes_namespace_v1.kubecost]
}

resource "helm_release" "kubecost" {
  name             = "kubecost"
  chart            = "kubecost"
  repository       = "oci://public.ecr.aws/kubecost"
  version          = "3.2.1"
  namespace        = "kubecost"
  create_namespace = false
  wait             = false
  values = [
    templatefile("${path.module}/yamls/values-eks-cost-monitoring.yaml", {
      clusterId = var.eks_cluster.cluster_name
    })
  ]
  depends_on = [
    kubernetes_secret_v1.kubecost_federated_store,
    kubernetes_secret_v1.kubecost_cloud_integration,
    kubernetes_service_account_v1.kubecost_serviceaccount,
    kubernetes_service_account_v1.kubecost_cluster_controller,
  ]
}

# Cloud integration secret — v3 requires this as a secret rather than inline Helm values JSON.
# The secret key must be "cloud-integration.json".
resource "kubernetes_secret_v1" "kubecost_cloud_integration" {
  metadata {
    name      = "cloud-integration"
    namespace = "kubecost"
  }

  data = {
    "cloud-integration.json" = jsonencode({
      aws = [
        {
          athenaBucketName = "s3://${aws_s3_bucket.finops.id}/query-results/"
          athenaRegion     = var.region
          athenaDatabase   = var.athena_database_name
          athenaTable      = local.athena_table_name
          athenaWorkgroup  = var.athena_workgroup
          projectID        = var.account_id
        }
      ]
    })
  }

  depends_on = [kubernetes_namespace_v1.kubecost]
}

# Create the Kubernetes secret
resource "kubernetes_secret_v1" "kubecost_federated_store" {
  metadata {
    name      = "kubecost-s3-configs"
    namespace = "kubecost"
  }

  data = {
    "federated-store.yaml" = templatefile("${path.module}/yamls/federated-store.yaml", {
      bucket_name = aws_s3_bucket.finops.id
      region      = var.region
    })
    # Added actions-store.yaml to prevent Cluster Controller crash-loop
    "actions-store.yaml" = templatefile("${path.module}/yamls/actions-store.yaml", {
      bucket_name = aws_s3_bucket.finops.id
      region      = var.region
    })
  }

  depends_on = [kubernetes_namespace_v1.kubecost]
}

################################################################################
# ALB security group — restricts inbound to managed prefix list
################################################################################

resource "aws_security_group" "kubecost_lb" {
  name_prefix = "${var.root_name}-kubecost-lb-"
  vpc_id      = var.vpc_id
  description = "Controls inbound access to the Kubecost ALB."
  tags        = { Name = "${var.root_name}-kubecost-lb-sg" }
}

resource "aws_vpc_security_group_ingress_rule" "kubecost_lb_https" {
  security_group_id = aws_security_group.kubecost_lb.id
  description       = "HTTPS from UW Madison prefix list"
  prefix_list_id    = var.inbound_prefix_list_id
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "kubecost_lb_http" {
  security_group_id = aws_security_group.kubecost_lb.id
  description       = "HTTP from UW Madison prefix list (redirected to HTTPS)"
  prefix_list_id    = var.inbound_prefix_list_id
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

# Allow access via the AWS Client VPN too (source-NATs to the VPC CIDR, same
# as the EKS API server rule) — lets a single VPN connection reach both the
# cluster API and this ALB, without also requiring the UW-Madison VPN.
resource "aws_vpc_security_group_ingress_rule" "kubecost_lb_https_client_vpn" {
  security_group_id = aws_security_group.kubecost_lb.id
  description       = "HTTPS from AWS Client VPN (source-NATs to VPC CIDR)"
  cidr_ipv4         = var.vpc_cidr
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "kubecost_lb_http_client_vpn" {
  security_group_id = aws_security_group.kubecost_lb.id
  description       = "HTTP from AWS Client VPN (redirected to HTTPS)"
  cidr_ipv4         = var.vpc_cidr
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

# The Client VPN CIDR rule above only covers traffic to VPC-internal
# destinations. Traffic from a full-tunnel VPN client to this ALB's *public*
# IP instead hairpins out through the VPC's NAT gateway and back in over the
# internet, presenting the NAT gateway's EIP as the source — so that EIP
# needs its own trust rule too.
resource "aws_vpc_security_group_ingress_rule" "kubecost_lb_https_nat" {
  security_group_id = aws_security_group.kubecost_lb.id
  description       = "HTTPS from VPC NAT gateway (full-tunnel VPN clients hairpin through here)"
  cidr_ipv4         = "${var.nat_gateway_ip}/32"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "kubecost_lb_http_nat" {
  security_group_id = aws_security_group.kubecost_lb.id
  description       = "HTTP from VPC NAT gateway (redirected to HTTPS)"
  cidr_ipv4         = "${var.nat_gateway_ip}/32"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "kubecost_lb" {
  security_group_id = aws_security_group.kubecost_lb.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# Creating an ingress for Kubecost
resource "kubectl_manifest" "kubecost_ingress" {
  yaml_body = templatefile("${path.module}/yamls/kubecost-alb-ingress.yaml", {
    hostname        = var.hostname
    certificate_arn = var.certificate_arn
    security_group  = aws_security_group.kubecost_lb.id
  })
  depends_on = [helm_release.kubecost]
}

# Yes, we do need both of these...
# Modified from https://aws.amazon.com/blogs/mt/integrating-kubecost-with-amazon-managed-service-for-prometheus/
module "kubecost_pod_identity_amp" {
  source = "terraform-aws-modules/eks-pod-identity/aws"
  name   = "kubecost-cost-analyzer-amp"
  additional_policy_arns = {
    "AmazonPrometheusQueryAccess"       = "arn:aws:iam::aws:policy/AmazonPrometheusQueryAccess"
    "AmazonPrometheusRemoteWriteAccess" = "arn:aws:iam::aws:policy/AmazonPrometheusRemoteWriteAccess"
  }

  # Add EBS permissions policy here
  associations = {
    kubecost-cost-analyzer-amp = {
      cluster_name    = var.eks_cluster.cluster_name
      namespace       = "kubecost"
      service_account = "kubecost-cost-analyzer-amp"
    }
  }
}

module "kubecost_pod_identity_prometheus" {
  source = "terraform-aws-modules/eks-pod-identity/aws"

  name = "kubecost-prometheus-server-amp"

  additional_policy_arns = {
    "AmazonPrometheusQueryAccess"       = "arn:aws:iam::aws:policy/AmazonPrometheusQueryAccess"
    "AmazonPrometheusRemoteWriteAccess" = "arn:aws:iam::aws:policy/AmazonPrometheusRemoteWriteAccess"
  }

  # Add EBS permissions policy here
  associations = {
    kubecost-prometheus-server-amp = {
      cluster_name    = var.eks_cluster.cluster_name
      namespace       = "kubecost"
      service_account = "kubecost-prometheus-server-amp"
    }
  }
}
