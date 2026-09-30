locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 3)
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name = var.name
  cidr = var.vpc_cidr

  azs             = local.azs
  private_subnets = [for k, v in local.azs : cidrsubnet(var.vpc_cidr, 4, k)]
  public_subnets  = [for k, v in local.azs : cidrsubnet(var.vpc_cidr, 8, k + 48)]

  enable_nat_gateway   = true
  single_nat_gateway   = true
  enable_dns_hostnames = true
  enable_dns_support   = true

  enable_flow_log                   = true
  flow_log_destination_type         = "s3"
  flow_log_destination_arn          = "${aws_s3_bucket.logs.arn}/vpc-flow-logs/"
  flow_log_max_aggregation_interval = 60

  # Custom format — the default (v2) format cannot attribute NAT gateway traffic.
  # For a packet traversing the NAT gateway, srcaddr/dstaddr hold the NAT ENI's
  # address, so every byte looks like it came from the NAT gateway itself. The
  # pkt-srcaddr/pkt-dstaddr fields (v3) preserve the ORIGINAL endpoints, which is
  # what lets us trace ingress back to the pod/node responsible.
  #
  # Also included beyond the default fields:
  #   flow-direction  — ingress vs egress relative to the ENI (v5)
  #   traffic-path    — how egress left the VPC (v5). Populated for egress only;
  #                     null/"-" on ingress rows, confirmed in the delivered data.
  #                     Observed values here so far: 1, 7, 8. The integer->path
  #                     mapping is NOT yet confirmed against AWS docs — do not
  #                     assume a particular value means "NAT gateway" without
  #                     checking. Attribution does not depend on this field:
  #                     pkt-srcaddr/pkt-dstaddr are the reliable mechanism.
  #   instance-id     — direct attribution when the ENI is attached to an EC2
  #                     instance. Blank for pod ENIs managed by the VPC CNI, so
  #                     pkt-srcaddr remains the primary attribution key.
  #   subnet-id / az-id — locate traffic without joining against ENI metadata,
  #                     which is important because ENIs are ephemeral under
  #                     Karpenter and may not exist by the time we query.
  # Field order here is the physical column order in the delivered records.
  flow_log_log_format = "$${version} $${account-id} $${vpc-id} $${subnet-id} $${az-id} $${instance-id} $${interface-id} $${srcaddr} $${dstaddr} $${pkt-srcaddr} $${pkt-dstaddr} $${srcport} $${dstport} $${protocol} $${packets} $${bytes} $${start} $${end} $${action} $${log-status} $${flow-direction} $${traffic-path}"

  # Parquet + partitioning are chosen for query cost, not just tidiness: this is a
  # high-volume log (the traffic under investigation is ~1 TB) and Athena bills on
  # bytes scanned. Columnar storage plus hour-level partition pruning keeps an
  # attribution query from scanning the whole history.
  #
  # NOTE: this leaves TWO layouts under vpc-flow-logs/. Delivery was repaired on
  # 2026-07-19 ~10:09 UTC (see the KMS/ACL fixes in logging.tf), so roughly ten
  # hours of plain-text v2 records landed under the flat prefix before this change:
  #
  #   AWSLogs/<account-id>/vpcflowlogs/<region>/YYYY/MM/DD/...log.gz   (old, v2)
  #   AWSLogs/aws-account-id=<account-id>/aws-service=vpcflowlogs/...parquet  (new)
  #
  # Any Athena table must point at the hive-partitioned path, NOT the vpc-flow-logs/
  # root — a table spanning both hits mixed formats and mixed schemas. The old
  # objects are not worth migrating: they use the default v2 format and therefore
  # lack pkt-srcaddr/pkt-dstaddr, so they cannot attribute NAT traffic anyway.
  flow_log_file_format                = "parquet"
  flow_log_hive_compatible_partitions = true
  flow_log_per_hour_partition         = true

  # NOTE: manage_default_network_acl is intentionally omitted.
  # Setting it without explicit ingress/egress rules removes AWS's default
  # allow-all entries (rule 32767), producing a deny-all NACL that blocks DNS
  # and prevents Bottlerocket nodes from bootstrapping.
  manage_default_route_table    = true
  default_route_table_tags      = { Name = "${var.name}-default" }
  manage_default_security_group = true
  default_security_group_tags   = { Name = "${var.name}-default" }

  public_subnet_tags = {
    "kubernetes.io/cluster/${var.name}" = "shared"
    "kubernetes.io/role/elb"            = 1
  }

  private_subnet_tags = {
    "kubernetes.io/cluster/${var.name}" = "shared"
    "kubernetes.io/role/internal-elb"   = 1
    "karpenter.sh/discovery"            = local.name
  }
}


################################################################################
# Prefix-delegation CIDR reservations
################################################################################
# Prefix delegation hands each node ENI a /28 — 16 CONTIGUOUS, 16-ALIGNED
# addresses. Node primary ENI IPs, by contrast, are assigned singly by AWS from
# anywhere in the subnet, and each one that lands in an otherwise-empty /28 slot
# sterilises the whole slot: 16 addresses that cannot be delegated while that node
# lives. From the slot census of 10.0.32.0/20 taken during the 2026-08-10
# 200-subject batch (GitHub #220): of the 256 /28 slots, 203 held a pod prefix, 52
# were blocked ONLY by a loose single IP (~700 unusable addresses), and exactly 1
# was fully free.
# The waste grows with node count, because every node adds another single IP.
#
# A `prefix`-type subnet CIDR reservation stops AWS assigning single addresses out
# of the reserved range, which un-interleaves the two allocation patterns: node
# primary IPs pack into the unreserved region, and the reserved region stays
# cleanly divisible into /28s.
#
# Note the AWS docs are explicit that a reservation MAY cover a range that already
# holds assigned addresses — "the IP address range can include addresses that are
# already in use. Creating a subnet reservation does not unassign any IP addresses
# that are already in use" (vpc/latest/userguide/subnet-cidr-reservation.html).
# #220 assumed the opposite and therefore proposed new pod subnets off a secondary
# VPC CIDR plus CNI custom networking. That is not needed: reservations retrofit
# onto these subnets, which avoids moving pod IPs out of 10.0.0.0/16 — every
# `cidr_ipv4 = var.vpc_cidr` security-group rule and NetworkPolicy `ipBlock` in
# this repo (RDS ingress for Argo/Prefect, the kube-system policies, the ALB
# rules) depends on pod addresses staying inside it.
#
# The split per private /20 (4,096 addresses = 256 /28 slots), e.g. the third AZ:
#
#   10.0.32.0/22  unreserved   1,024 singles — node primary ENIs, secondary-ENI
#                              primaries, VPC endpoint / RDS / Client VPN ENIs,
#                              and the CNI's single-IP fallback
#   10.0.36.0/22  reserved  \  3,072 addresses = 192 /28 slots for delegation
#   10.0.40.0/21  reserved  /
#
# Sizing: post-#219 (MINIMUM_IP_TARGET=10) a node stays on ONE /28 up to 14 pods,
# so prefix slots needed ≈ node count. The 300-concurrent batch peaked at 113
# nodes in a single AZ, so 192 slots is ~1.7x headroom, and 1,024 singles covers
# those nodes even at 4 ENIs each. To shift the balance later, move the boundary:
# drop the /22 reservation for 2,048 singles, or add cidrsubnet(cidr, 2, 0) as a
# third reservation to hand the whole /20 to prefixes.
#
# Two caveats, both accepted deliberately:
#   - Reservations are NOT retroactive. Long-lived non-node ENIs (the EKS cluster
#     ENI, Client VPN, the two ECR interface endpoints, RDS in 2c) predate this and
#     sit scattered across each /20, so ~3 slots per AZ stay blocked until those
#     ENIs are recreated. That is ~1.6% versus the 20% measured above.
#   - Apply on a drained cluster. Pipeline node IPs inside a reserved range are
#     left alone by the reservation; they only clear when the node terminates.
locals {
  # Two reservations per subnet because three quarters of a /20 is not expressible
  # as a single CIDR: the second /22 plus the upper /21. The "-lower"/"-upper" keys
  # name that reserved PAIR — the unreserved singles region sits below both. Keys
  # are part of the resource address, so renaming them recreates the reservations.
  prefix_reservations = merge([
    for idx, cidr in module.vpc.private_subnets_cidr_blocks : {
      "${local.azs[idx]}-lower" = {
        subnet_id  = module.vpc.private_subnets[idx]
        cidr_block = cidrsubnet(cidr, 2, 1)
      }
      "${local.azs[idx]}-upper" = {
        subnet_id  = module.vpc.private_subnets[idx]
        cidr_block = cidrsubnet(cidr, 1, 1)
      }
    }
  ]...)
}

resource "aws_ec2_subnet_cidr_reservation" "pod_prefixes" {
  for_each = local.prefix_reservations

  subnet_id        = each.value.subnet_id
  cidr_block       = each.value.cidr_block
  reservation_type = "prefix"
  description      = "VPC CNI prefix delegation (${each.key}) - no single-IP assignment from this range"
}


data "aws_route_tables" "rts" {
  vpc_id = module.vpc.vpc_id
}

data "aws_vpc_endpoint_service" "s3" {
  service         = "s3"
  service_type    = "Gateway"
  service_regions = [var.region]
}

# Be very careful adding policies to this - doing so can interfere with ECR image pulls
resource "aws_vpc_endpoint" "s3" {
  vpc_id              = module.vpc.vpc_id
  service_name        = data.aws_vpc_endpoint_service.s3.service_name
  route_table_ids     = data.aws_route_tables.rts.ids
  private_dns_enabled = false
}

################################################################################
# ECR interface endpoints
################################################################################
# Division of labour worth understanding before touching these: an image pull is
# two conversations. The ECR API (auth token, manifest lookup) goes to
# ecr.api/ecr.dkr; the actual layer BLOBS are redirected to S3 and already ride
# the free gateway endpoint above. So these two endpoints carry very little
# traffic — they exist to remove the residual NAT hops on the auth path, not to
# move bulk data. Pulls still succeed without them (via NAT); with them, an image
# pull never leaves the VPC.
#
# private_dns_enabled rewrites <acct>.dkr.ecr.<region>.amazonaws.com to the
# endpoint ENIs, so no client config changes anywhere.

resource "aws_security_group" "vpc_endpoints" {
  name        = "${var.name}-vpc-endpoints"
  description = "HTTPS from within the VPC to interface endpoints"
  vpc_id      = module.vpc.vpc_id

  # Interface endpoints only ever receive connections; SG state tracking handles
  # the responses, so no egress rule is needed. If ECR calls start failing with
  # what look like auth errors, check this rule first — a blocked token request
  # surfaces as an authentication failure, not a network one.
  ingress {
    description = "HTTPS from within the VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = {
    Name = "${var.name}-vpc-endpoints"
  }
}

resource "aws_vpc_endpoint" "ecr_api" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.ecr.api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnets
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = {
    Name = "${var.name}-ecr-api"
  }
}

resource "aws_vpc_endpoint" "ecr_dkr" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.ecr.dkr"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnets
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = {
    Name = "${var.name}-ecr-dkr"
  }
}
