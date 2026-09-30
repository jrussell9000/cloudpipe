
# Borrowing heavily from https://github.com/aws-samples/karpenter-blueprints/blob/main/cluster/terraform/main.tf
module "eks" {

  source  = "terraform-aws-modules/eks/aws"
  version = "21.15.1"

  # Name and version of the EKS cluster
  name               = var.name
  kubernetes_version = var.kubernetes_version

  # Use an access entry for the terraform caller instad of turning this on
  enable_cluster_creator_admin_permissions = false

  access_entries = {
    terraform_admin = {
      principal_arn = data.aws_iam_session_context.current.issuer_arn
      policy_associations = {
        admin = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = {
            type = "cluster"
          }
        }
      }
    }
  }

  # Public access is enabled during initial install for bootstrapping (install.sh
  # passes -var="endpoint_public_access=true"), then disabled in the final apply
  # once the VPN is in place. Private endpoint stays on permanently so nodes and
  # pods can reach the API server from within the VPC.
  endpoint_public_access  = var.endpoint_public_access
  endpoint_private_access = true

  # Enable etcd encryption for secrets
  encryption_config = {
    resources        = ["secrets"]
    provider_key_arn = aws_kms_key.eks_secrets.arn
  }

  # VPC and subnets where the EKS cluster will be created
  vpc_id                   = module.vpc.vpc_id
  subnet_ids               = module.vpc.private_subnets
  control_plane_subnet_ids = module.vpc.intra_subnets

  # Enable IAM Roles for Service Accounts (IRSA)
  enable_irsa = true

  # Allow nodes to scrape kubelet metrics (port 10250) from each other.
  # Required for the Kubecost finops agent (runs on backend nodegroup) to reach
  # kubelet on Karpenter-provisioned nodes. Both use the same node security group
  # (tagged karpenter.sh/discovery), so a self-referencing rule is sufficient.
  node_security_group_additional_rules = {
    kubelet_intra_node = {
      description = "Kubelet scraping between nodes (Kubecost finops agent to Karpenter nodes)"
      protocol    = "tcp"
      from_port   = 10250
      to_port     = 10250
      type        = "ingress"
      self        = true
    }
  }

  # As of 21.10.1, module does NOT create an open egress rule for the
  # cluster security group
  security_group_additional_rules = {
    clustergroup_open_egress = {
      # Note: Setting protocol = "-1" with from_port and to_port = 0 will result in all ports being open.
      protocol    = "-1"
      from_port   = 0
      to_port     = 0
      type        = "egress"
      description = "Allow all traffic from EKS nodes to GuardDuty endpoint"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  # Configuring logging
  create_cloudwatch_log_group            = true
  cloudwatch_log_group_retention_in_days = 365
  cloudwatch_log_group_kms_key_id        = aws_kms_key.eks_logs.arn
  enabled_log_types                      = ["api", "audit", "authenticator"]


  # Define minimally required cluster add-ons - we'll add more in addons.tf
  addons = {
    # amazon-cloudwatch-observability is deliberately absent.
    #
    # The addon's only remaining function was ContainerInsights METRICS.
    # Container logs were already off (no fluent-bit) and Application Signals
    # was already off, so the cloudwatch-agent DaemonSet was the entire addon —
    # and nothing consumed what it produced.
    #
    # Verified 2026-09-07; all three consumption paths came back empty:
    #   - `aws cloudwatch describe-alarms` → 0 alarms in the whole account
    #   - no Grafana CloudWatch datasource (the plugin is not installed)
    #   - no reference to the ContainerInsights namespace anywhere under
    #     gitops/, src/, scripts/ or prefect/
    #
    # Measured 2026-09-01..09-07 (one 5-day production batch), in the deployment region:
    #   CW:MetricMonitorUsage   $675.78   2,260 metric-months
    #   CW:DataProcessing-Bytes  $56.55   114.5 GB — the performance log
    #                            ------            group backing those metrics
    #                           $732.33   per batch
    #
    # The billing MODE was the trap, and the comment this replaces had it
    # backwards: it claimed basic (per-metric) was "far cheaper" than enhanced
    # (per-observation) "for a small cluster with bursty batch workloads".
    # Per-metric billing is unbounded in POD COUNT — every ephemeral Argo pod
    # name mints new billable custom metrics at $0.30 each. Metric-months/day
    # tracked pod churn 32x across the batch: 20.9 idle → 679 peak → 9.9 once
    # it drained. Per-observation billing is bounded by scrape rate instead.
    #
    # Removing the addon also returns the DaemonSet's per-node memory request
    # to allocatable on every node — a packing win on the CPU pools
    # (scripts/analyze_pod_copacking.py, issue #132).
    #
    # NOTE this is NOT the EKS control-plane log types (`enabled_log_types`
    # above). Those are api/audit/authenticator at 365-day KMS-encrypted
    # retention and are a stated NIST 800-171 control
    # (tools/nist-800-171-assessment.md, item H1) — they stay.
    #
    # Cluster metrics still come from Prometheus → Grafana, which is what the
    # dashboards in gitops/apps/grafana/ actually read. If ContainerInsights is
    # ever wanted back, re-add it with enhanced_container_insights rather than
    # basic — and give it a consumer first.
    coredns = {
      most_recent = true
      configuration_values = jsonencode({
        nodeSelector = {
          "eks.amazonaws.com/nodegroup" = "backend"
        }
        tolerations = [
          {
            key      = "CriticalAddonsOnly"
            operator = "Equal"
            value    = "true"
            effect   = "NoSchedule"
          }
        ]
        corefile = <<-COREFILE
          .:53 {
              errors
              health {
                  lameduck 5s
                }
              ready
              kubernetes cluster.local in-addr.arpa ip6.arpa {
                pods insecure
                fallthrough in-addr.arpa ip6.arpa
              }
              prometheus :9153
              forward . ${local.vpc_dns_resolver}
              cache 30
              loop
              reload
              loadbalance
          }
        COREFILE
      })
    }
    eks-pod-identity-agent = {
      before_compute = true
      most_recent    = true
    }
    kube-proxy = {
      most_recent = true
    }
    metrics-server = {
      most_recent = true
      configuration_values = jsonencode({
        nodeSelector = {
          "eks.amazonaws.com/nodegroup" = "backend"
        }
        tolerations = [
          {
            key      = "CriticalAddonsOnly"
            operator = "Equal"
            value    = "true"
            effect   = "NoSchedule"
          }
        ]
      })
    }
    # vpc-cni must be before_compute so nodes have a CNI plugin when they join
    # and can reach Ready state (required for the node group to become ACTIVE).
    # Strict mode is gated by vpc_cni_strict_mode (default false); install.sh
    # enables it in the final apply after kube-system NetworkPolicies are in place.
    vpc-cni = {
      before_compute              = true
      most_recent                 = true
      resolve_conflicts_on_create = "OVERWRITE"
      resolve_conflicts_on_update = "OVERWRITE"
      configuration_values = jsonencode({
        enableNetworkPolicy = tostring(var.vpc_cni_strict_mode)
        nodeAgent = {
          healthProbeBindAddr = "8163"
          metricsBindAddr     = "8162"
        }
        env = merge(
          {
            ENABLE_PREFIX_DELEGATION = "true"
            WARM_PREFIX_TARGET       = "1"

            # MINIMUM_IP_TARGET/WARM_IP_TARGET OVERRIDE WARM_PREFIX_TARGET when set
            # (AWS docs, cni-increase-ip-addresses-procedure). WARM_PREFIX_TARGET is
            # left in place as the documented fallback if these two are ever removed.
            #
            # Why: WARM_PREFIX_TARGET=1 makes every node hold one whole SPARE /28
            # beyond current need. Because prefix delegation allocates 16 CONTIGUOUS,
            # 16-ALIGNED addresses at a time, that spare is not just 16 wasted IPs —
            # it consumes a /28 slot no other node can use. Measured on the
            # 2026-08-10 200-subject batch (GitHub #218): the third AZ held 113 nodes
            # running 268 pods, but had 203 prefixes = 3248 addresses reserved. A 12x
            # overprovision; 97 of 113 nodes held exactly 2 prefixes while running
            # 2-4 pods. Only ONE fully-free aligned /28 remained in the /20, so pods
            # stalled in Init:0/1 for up to 93 min on "failed to assign an IP
            # address" (42,843 FailedCreatePodSandBox events) with ~700 addresses
            # still nominally free.
            #
            # Sizing: allocation is ceil(max(MINIMUM_IP_TARGET, pods + WARM_IP_TARGET)
            # / 16) prefixes. At 10/2 a node stays on a single /28 up to 14 pods,
            # which covers every pipeline node (cpu-heavy runs 2-4 pods incl.
            # DaemonSets; the busiest node in the cluster ran 31 and simply takes
            # 3 prefixes). Projected for that AZ: 3248 -> ~1808 reserved.
            #
            # This does NOT fix the other fragmentation source: node-primary ENI IPs
            # are scattered singly through the same /20 and sterilized 52 more /28
            # slots (700 addresses). That one is handled outside the CNI, by the
            # `prefix`-type subnet CIDR reservations in vpc.tf
            # (aws_ec2_subnet_cidr_reservation.pod_prefixes, GitHub #220), which keep
            # single-IP assignment and prefix delegation in separate parts of each /20.
            MINIMUM_IP_TARGET = "10"
            WARM_IP_TARGET    = "2"
          },
          var.vpc_cni_strict_mode ? { NETWORK_POLICY_ENFORCING_MODE = "strict" } : {}
        )
      })
    }
  }

  # Define managed node groups for the EKS cluster
  eks_managed_node_groups = {
    # The backend group will hold all non-Karpenter, non-NVIDIA system processes
    backend = {
      name = "backend"
      # Don't use this name as a prefix prepended to a random alphanumeric string
      use_name_prefix = false
      ami_type        = "BOTTLEROCKET_ARM_64"
      # m7g.2xlarge (8 vCPU / 32 GiB), up from m7g.xlarge (4 / 16) on 2026-09-08.
      #
      # The xlarge was not survivable at cohort scale. On 2026-09-05 the Kubecost
      # aggregator OOMKilled (exitCode 137, 10:38:58Z), the finops agent
      # OOMKilled (23:43:38Z), and a second finops replica was EVICTED with the
      # node down to 34 MB free:
      #
      #   The node was low on resource: memory. Threshold quantity: 100Mi,
      #   available: 34908Ki. Container finops-agent was using 4120944Ki,
      #   request is 2Gi
      #
      # The aggregator's in-flight ingestion died with it, so Kubecost served
      # `data: [null]` for that day and the daily rollup did not rebuild until
      # this node change gave it headroom on 2026-09-08. Every stalled rollup
      # then returned, 09-05 included (3,152 workflows).
      #
      # An earlier version of this comment called that loss permanent and sized
      # a 2,848-subject rerun on it. That was wrong; no reprocessing is needed.
      # Verified against AWS on 2026-09-08: the CUR is complete for the whole
      # window, Kubecost holds 407 node assets for 09-05 against 408 instances
      # billed, and 09-05's reconciled spot cost lands within 1.7% of what AWS
      # actually charged — the closest of any day in the window.
      #
      # The error was treating adjustment MAGNITUDE as a proxy for whether
      # reconciliation had run. It is not: the adjustment measures how wrong the
      # initial estimate was, not how right the final number is. 09-05's base
      # pricing was already spot-aware, so it needed almost no correction, which
      # looks identical to never having been corrected. The days that actually
      # diverge are the ones with the dramatic adjustments — 09-03 (+24.5%) and
      # 09-04 (+12.7%) against billed cost, despite adjustments of -20.6% and
      # -34.9%. Check reconciliation against the invoice
      # (`aws ce get-cost-and-usage`, grouped by USAGE_TYPE so spot is separable
      # from on-demand), never against the size of the adjustment.
      #
      # The trigger was load: 09-05 saw 2,899 workflow completions against
      # ~1,880 on the days either side, and allocation cardinality tracks
      # completions. Limits on the old node summed to 140% of a 13.78Gi
      # allocatable, so a spike had nowhere to go and the kubelet evicted.
      #
      # 32 GiB (~28Gi allocatable) takes the same limits to ~70% and leaves room
      # to raise the aggregator's own cap, which is done alongside this in
      # modules/finops/yamls/values-eks-cost-monitoring.yaml.
      #
      # Cost: $0.1632 -> $0.3264 per node-hour, x2 nodes = +$238/mo. Bought
      # deliberately: a reprocessing run to recover the lost days costs ~$1,150
      # of compute, and running it on a backend that cannot hold the cost data
      # would spend that to produce a second hole.
      instance_types = ["m7g.2xlarge"]

      min_size = 0
      max_size = 3
      # Two nodes, not one. A single m7g.xlarge (13.78Gi allocatable) could not
      # hold this namespace's real footprint: the Kubecost aggregator alone
      # requests 5Gi and the finops agent needs 8Gi at cohort scale (see
      # modules/finops/yamls/values-eks-cost-monitoring.yaml), on a node that
      # also carries Prometheus, Grafana, ArgoCD and all of Prefect. Limits
      # summed to 109% of allocatable before this change.
      #
      # It was also a single point of failure with teeth: on 2026-08-18 one
      # eviction tainted this node `node.kubernetes.io/memory-pressure:NoSchedule`
      # and nothing could reschedule, because Karpenter does not manage
      # `eks.amazonaws.com/nodegroup` and so cannot substitute a node here.
      # Every pod pinned to this group stayed Pending until the taint cleared.
      #
      # Both nodes land in the same AZ by the subnet pin below, so EBS-backed
      # pods can still move between them.
      desired_size = 2

      # Pin to the first AZ so nodes are always co-located with EBS volumes
      # provisioned in that AZ (kubecost, prefect PVCs). private_subnets[0]
      # is always the first AZ because local.azs is sorted alphabetically.
      subnet_ids = [module.vpc.private_subnets[0]]

      # Node group IAM Role/Instance Profile
      use_name_prefix          = false
      iam_role_name            = "${var.name}-backend-nodegroup-iam-role"
      iam_role_use_name_prefix = false
      iam_role_additional_policies = {
        # Required by SSM
        AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
        # Required by Container Insights
        CloudWatchAgentServerPolicy = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
        # Required to pull images from private ECR registries (if so desired)
        AmazonEC2ContainerRegistryReadOnly = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
        # Required by EKS CNI
        AmazonEKS_CNI_Policy = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
        # Required for writing to AMP
        AmazonPrometheusRemoteWriteAccess = "arn:aws:iam::aws:policy/AmazonPrometheusRemoteWriteAccess"
      }

      metadata_options = {
        http_endpoint               = "enabled"
        http_tokens                 = "required"
        http_put_response_hop_limit = 3
        instance_metadata_tags      = "enabled"
      }

      # Prefix delegation assigns /28 blocks to ENIs; pod IPs are not the ENI's
      # primary IP, so AWS drops outbound pod traffic unless SrcDstCheck is off.
      network_interfaces = [{ source_dest_check = false }]

      taints = {
        criticaladdons = {
          key    = "CriticalAddonsOnly"
          value  = "true"
          effect = "NO_SCHEDULE"
        }
      }
    }

    # Will host the Karpenter controller
    karpenter = {
      name            = "karpenter"
      use_name_prefix = false
      ami_type        = "BOTTLEROCKET_ARM_64"
      instance_types  = ["m7g.xlarge"]

      min_size     = 0
      max_size     = 3
      desired_size = 1

      # SSM requires this
      metadata_options = {
        http_endpoint               = "enabled"
        http_tokens                 = "required"
        http_put_response_hop_limit = 3
        instance_metadata_tags      = "enabled"
      }


      # Node group IAM Role/Instance Profile
      use_name_prefix          = false
      iam_role_name            = "${var.name}-karpenter-nodegroup-iam-role"
      iam_role_use_name_prefix = false
      iam_role_additional_policies = {
        # Required by SSM
        AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
        # Required by Container Insights
        CloudWatchAgentServerPolicy = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
        # Required to pull images from private ECR registries (if so desired)
        AmazonEC2ContainerRegistryReadOnly = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
        # Required by EKS CNI
        AmazonEKS_CNI_Policy = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
        # Required for writing to AMP
        AmazonPrometheusRemoteWriteAccess = "arn:aws:iam::aws:policy/AmazonPrometheusRemoteWriteAccess"
      }

      # Prefix delegation assigns /28 blocks to ENIs; pod IPs are not the ENI's
      # primary IP, so AWS drops outbound pod traffic unless SrcDstCheck is off.
      network_interfaces = [{ source_dest_check = false }]

      labels = {
        # Used to ensure Karpenter runs on nodes that it does not manage
        "karpenter.sh/controller" = "true"
      }
    }
    argo = {
      name            = "argo"
      use_name_prefix = false
      ami_type        = "BOTTLEROCKET_ARM_64"
      instance_types  = ["m7g.xlarge"]

      min_size     = 0
      max_size     = 3
      desired_size = 1

      # SSM requires this
      metadata_options = {
        http_endpoint               = "enabled"
        http_tokens                 = "required"
        http_put_response_hop_limit = 3
        instance_metadata_tags      = "enabled"
      }

      # Node group IAM Role/Instance Profile
      use_name_prefix          = false
      iam_role_name            = "${var.name}-argo-nodegroup-iam-role"
      iam_role_use_name_prefix = false
      iam_role_additional_policies = {
        # Required by SSM
        AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
        # Required by Container Insights
        CloudWatchAgentServerPolicy = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
        # Required to pull images from private ECR registries (if so desired)
        AmazonEC2ContainerRegistryReadOnly = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
        # Required by EKS CNI
        AmazonEKS_CNI_Policy = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
        # Required for writing to AMP
        AmazonPrometheusRemoteWriteAccess = "arn:aws:iam::aws:policy/AmazonPrometheusRemoteWriteAccess"
      }

      # Prefix delegation assigns /28 blocks to ENIs; pod IPs are not the ENI's
      # primary IP, so AWS drops outbound pod traffic unless SrcDstCheck is off.
      network_interfaces = [{ source_dest_check = false }]

      taints = {
        argo = {
          key    = "argoproj.io/backend"
          value  = "true"
          effect = "NO_SCHEDULE"
        }
      }
    }
  }
  node_security_group_tags = {
    "karpenter.sh/discovery" = var.name
  }
}

resource "aws_kms_key" "eks_secrets" {
  description             = "KMS key for EKS secrets encryption"
  deletion_window_in_days = 7
}

data "aws_iam_policy_document" "eks_logs_kms" {
  statement {
    sid     = "EnableRootAccess"
    effect  = "Allow"
    actions = ["kms:*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
    resources = ["*"]
  }

  statement {
    sid    = "AllowCloudWatchLogs"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    principals {
      type        = "Service"
      identifiers = ["logs.${var.region}.amazonaws.com"]
    }
    resources = ["*"]
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

resource "aws_kms_key" "eks_logs" {
  description             = "KMS key for EKS logs"
  deletion_window_in_days = 7
  policy                  = data.aws_iam_policy_document.eks_logs_kms.json
}

