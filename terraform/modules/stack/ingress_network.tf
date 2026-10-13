################################################################################
# Ingress network — where HTTPS ingress pods run (openspec add-https-ingress-mode)
################################################################################
# `ingress-mode=https` copies each subject from the NBDC source's HTTPS server
# into the data bucket. Run from the private subnets, every byte would pass the
# NAT gateway at ~$0.045/GB, on every run. So ingress nodes get public IPv4
# addresses, in subnets of their own — option B of plan 018 §8, accepted by the
# operator on 2026-10-12 ONLY with four conditions. This file is conditions 1
# and 2. Karpenter's node classes carry 3; tests/test_ingress_node_isolation.py
# is 4, and holds all of them.
#
#   1. Dedicated public subnets with their own network ACL — a second protection,
#      so that one bad security-group edit does not expose a node's kubelet to the
#      internet. Nothing else may share them: the load balancers in the module's
#      public subnets need inbound 443, which this ACL denies.
#   2. A dedicated node security group: no CIDR inbound at all, and no
#      node-to-node port range with the general node group (whose module-default
#      rule admits TCP 1025-65535 from itself). That is 3.13.5's separation.
#
# Observed before this was written (plan 018 T7, 2026-10-12): a node with a
# public IP behind the GENERAL node security group alone was unreachable on all
# ten ports it listens on. These rules are tighter than that group in every
# direction.
#
# Not here, on purpose:
#   - No `karpenter.sh/discovery` tag on the subnets. The general node classes
#     select subnets by it, so it would let any pool launch into public subnets.
#   - No `kubernetes.io/role/elb` tag. The load balancer controller must never
#     place a load balancer here.
#   - No prefix-delegation reservation (vpc.tf reserves /28s in the private /20s).
#     A /24 holds 16 /28 slots, and this pool runs 1-2 nodes; scattered node IPs
#     cost a slot or two, not the pool.

locals {
  ingress_subnet_role = "ingress"

  # Ports something on an ingress node listens on, on the node's own address.
  # hostNetwork DaemonSets: aws-node (61678 metrics, 8162), eks-pod-identity-agent
  # (2703), kube-proxy (10256 healthz). kubelet: 10250. Bottlerocket ships no sshd,
  # but 22 costs nothing to deny. The network ACL denies each from the internet
  # BEFORE it admits the return-traffic range, because that range has to include
  # them (see the ACL below). A new hostNetwork listener must be added here.
  ingress_node_listen_ports = [22, 2703, 6443, 8162, 10250, 10256, 61678]
}

resource "aws_subnet" "ingress" {
  for_each = { for idx, az in local.azs : az => idx }

  vpc_id            = module.vpc.vpc_id
  availability_zone = each.key
  # /24s at index 52-54: clear of the private /20s (0-47) and the module's public
  # /24s (48-50).
  cidr_block = cidrsubnet(var.vpc_cidr, 8, each.value + 52)

  # The node class decides, explicitly (condition 3). A subnet default that hands
  # out public IPs would give one to anything launched here by any other path.
  map_public_ip_on_launch = false

  tags = {
    Name                       = "${var.name}-ingress-${each.key}"
    "cloudpipe.io/subnet-role" = local.ingress_subnet_role
  }
}

resource "aws_route_table" "ingress" {
  vpc_id = module.vpc.vpc_id
  tags   = { Name = "${var.name}-ingress" }
}

resource "aws_route" "ingress_internet" {
  route_table_id         = aws_route_table.ingress.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = module.vpc.igw_id
}

resource "aws_route_table_association" "ingress" {
  for_each = aws_subnet.ingress

  subnet_id      = each.value.id
  route_table_id = aws_route_table.ingress.id
}

# The S3 gateway endpoint (vpc.tf) lists this table explicitly, so ingress writes
# to the data bucket stay off the internet. It cannot rely on its data source of
# every route table: that is read before this table exists.

################################################################################
# Condition 1: the network ACL
################################################################################
# Inbound is deny-by-default, with three exceptions:
#   - anything from inside the VPC (control plane, CoreDNS, the VPC resolver,
#     ECR interface endpoints);
#   - TCP 1024-65535 from anywhere, for the replies to the node's own outbound
#     connections, minus every port in ingress_node_listen_ports;
#   - ICMP "fragmentation needed", which path-MTU discovery needs. Dropping it
#     stalls large transfers silently.
#
# The return range is 1024-65535, not Linux's 32768-60999 `ip_local_port_range`,
# because the VPC CNI's SNAT runs with AWS_VPC_K8S_CNI_RANDOMIZESNAT=prng
# (`--random-fully`). Its README: outbound connections get a source port "not
# necessarily part of the ephemeral port range set at the OS level", and NACLs
# keyed to that range break. Denying the listening ports first keeps the ACL a
# real second barrier for exactly the ports that matter. The cost is that the
# rare connection whose SNAT port lands on one of those seven is dropped and
# retried.
#
# Outbound is allow-all. The security group and the pods' NetworkPolicies decide
# what may leave.

resource "aws_network_acl" "ingress" {
  vpc_id     = module.vpc.vpc_id
  subnet_ids = [for s in aws_subnet.ingress : s.id]
  tags       = { Name = "${var.name}-ingress" }
}

resource "aws_network_acl_rule" "ingress_in_vpc" {
  network_acl_id = aws_network_acl.ingress.id
  egress         = false
  rule_number    = 100
  rule_action    = "allow"
  protocol       = "-1"
  cidr_block     = var.vpc_cidr
}

resource "aws_network_acl_rule" "ingress_in_deny_listener" {
  for_each = { for idx, port in local.ingress_node_listen_ports : tostring(port) => idx }

  network_acl_id = aws_network_acl.ingress.id
  egress         = false
  rule_number    = 110 + each.value
  rule_action    = "deny"
  protocol       = "tcp"
  cidr_block     = "0.0.0.0/0"
  from_port      = tonumber(each.key)
  to_port        = tonumber(each.key)
}

resource "aws_network_acl_rule" "ingress_in_return" {
  network_acl_id = aws_network_acl.ingress.id
  egress         = false
  rule_number    = 200
  rule_action    = "allow"
  protocol       = "tcp"
  cidr_block     = "0.0.0.0/0"
  from_port      = 1024
  to_port        = 65535
}

resource "aws_network_acl_rule" "ingress_in_pmtu" {
  network_acl_id = aws_network_acl.ingress.id
  egress         = false
  rule_number    = 210
  rule_action    = "allow"
  protocol       = "icmp"
  cidr_block     = "0.0.0.0/0"
  icmp_type      = 3 # destination unreachable
  icmp_code      = 4 # fragmentation needed
}

resource "aws_network_acl_rule" "ingress_out_all" {
  network_acl_id = aws_network_acl.ingress.id
  egress         = true
  rule_number    = 100
  rule_action    = "allow"
  protocol       = "-1"
  cidr_block     = "0.0.0.0/0"
}

################################################################################
# Condition 2: the ingress-node security group
################################################################################
# Inbound only from the control plane: kubelet (10250) for logs, exec and probes,
# and 443. Ingress nodes run no webhook server, so none of the general group's
# webhook ports (4443/6443/8443/9443/10251) is opened. No CIDR rule, no rule from
# the general node group: Kubecost's node-to-node kubelet scrape is declined
# (operator, 2026-10-12), so ingress pods carry no per-pod cost rows.

resource "aws_security_group" "ingress_node" {
  name        = "${var.name}-ingress-node"
  description = "HTTPS ingress nodes: control plane inbound only, never a CIDR (add-https-ingress-mode)"
  vpc_id      = module.vpc.vpc_id

  tags = {
    Name                               = "${var.name}-ingress-node"
    "cloudpipe.io/security-group-role" = "ingress-node"
  }
}

resource "aws_vpc_security_group_ingress_rule" "ingress_node_from_control_plane" {
  for_each = { kubelet = 10250, https = 443 }

  security_group_id            = aws_security_group.ingress_node.id
  referenced_security_group_id = module.eks.cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
  description                  = "Cluster API to ingress node ${each.key}"
}

resource "aws_vpc_security_group_egress_rule" "ingress_node_all" {
  security_group_id = aws_security_group.ingress_node.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
  description       = "Ingress nodes reach the source, Globus Auth, S3 and the cluster"
}

# The two rules other groups need so ingress nodes work, and no more.
#
# The API server. The control plane also carries the EKS primary cluster group,
# whose 443-from-the-VPC rule would already admit this, but only incidentally.
# The module's cluster group admits the API only from the general node group, so
# this says it outright.
resource "aws_vpc_security_group_ingress_rule" "cluster_api_from_ingress_node" {
  security_group_id            = module.eks.cluster_security_group_id
  referenced_security_group_id = aws_security_group.ingress_node.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "Ingress nodes to cluster API"
}

# CoreDNS runs on general nodes. Ingress pods must resolve names, and nothing else
# on a general node is theirs to reach.
resource "aws_vpc_security_group_ingress_rule" "node_dns_from_ingress_node" {
  for_each = toset(["tcp", "udp"])

  security_group_id            = module.eks.node_security_group_id
  referenced_security_group_id = aws_security_group.ingress_node.id
  ip_protocol                  = each.key
  from_port                    = 53
  to_port                      = 53
  description                  = "Ingress nodes to CoreDNS (${each.key})"
}
