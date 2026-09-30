# See https://www.themomentum.ai/blog/building-client-vpn-on-aws-with-terraform
# for a starter example

################################################################################
# CA Key and Certificate (Root CA)
################################################################################

resource "tls_private_key" "ca_key" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "ca_cert" {
  private_key_pem = tls_private_key.ca_key.private_key_pem

  subject {
    common_name  = "VPN Root CA"
    organization = var.name
    country      = "US" # Added for compliance
  }

  validity_period_hours = 87600 # 10 years
  is_ca_certificate     = true

  allowed_uses = [
    "cert_signing",
    "crl_signing",
    "digital_signature",
    "key_encipherment"
  ]
}

################################################################################
# VPN Server Key and Certificate
################################################################################

resource "tls_private_key" "vpn_key" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "vpn_csr" {
  private_key_pem = tls_private_key.vpn_key.private_key_pem

  subject {
    common_name  = local.vpn_url
    organization = var.name
    country      = "US"
  }
}

resource "tls_locally_signed_cert" "vpn_cert" {
  cert_request_pem   = tls_cert_request.vpn_csr.cert_request_pem
  ca_private_key_pem = tls_private_key.ca_key.private_key_pem
  ca_cert_pem        = tls_self_signed_cert.ca_cert.cert_pem

  validity_period_hours = 8760 # 1 year

  allowed_uses = [
    "digital_signature",
    "key_encipherment",
    "server_auth",
    "client_auth",
  ]

  set_subject_key_id = true
}

################################################################################
# Client Key and Certificate
################################################################################

resource "tls_private_key" "client_key" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "client_csr" {
  private_key_pem = tls_private_key.client_key.private_key_pem

  subject {
    common_name  = "client.${local.vpn_url}"
    organization = var.name
    country      = "US"
  }
}


resource "tls_locally_signed_cert" "client_cert" {
  cert_request_pem   = tls_cert_request.client_csr.cert_request_pem
  ca_private_key_pem = tls_private_key.ca_key.private_key_pem
  ca_cert_pem        = tls_self_signed_cert.ca_cert.cert_pem

  validity_period_hours = var.certificate_validity_period_hours

  allowed_uses = [
    "digital_signature",
    "key_encipherment",
    "client_auth",
  ]

  set_subject_key_id = true
}

################################################################################
# Import Certificates to ACM
################################################################################

resource "aws_acm_certificate" "vpn_cert" {
  private_key       = tls_private_key.vpn_key.private_key_pem
  certificate_body  = tls_locally_signed_cert.vpn_cert.cert_pem
  certificate_chain = tls_self_signed_cert.ca_cert.cert_pem
}

resource "aws_acm_certificate" "ca_cert" {
  private_key      = tls_private_key.ca_key.private_key_pem
  certificate_body = tls_self_signed_cert.ca_cert.cert_pem
}

################################################################################
# Create Client VPN Endpoint #
################################################################################

resource "aws_security_group" "vpn" {
  name_prefix = "client-vpn-endpoint-sg"
  description = "Security group for Client VPN endpoint"
  vpc_id      = module.vpc.vpc_id

  # Open to the internet — the operator works remotely full-time and is not
  # reliably reachable from the UW-Madison prefix list (its own VPN can't run
  # concurrently with this one, see docs/decisions and this repo's handoffs).
  # Mutual-TLS client certificate authentication (authentication_options
  # below) is the actual access control here, not source IP.
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

}

resource "aws_ec2_client_vpn_endpoint" "vpn" {
  description            = "Client VPN endpoint"
  server_certificate_arn = aws_acm_certificate.vpn_cert.arn
  client_cidr_block      = var.client_cidr_block
  vpc_id                 = module.vpc.vpc_id
  split_tunnel           = var.split_tunnel

  authentication_options {
    type                       = "certificate-authentication"
    root_certificate_chain_arn = aws_acm_certificate.ca_cert.arn
  }

  transport_protocol = "udp"
  security_group_ids = [aws_security_group.vpn.id]

  connection_log_options {
    enabled               = true
    cloudwatch_log_group  = aws_cloudwatch_log_group.vpn_logs.name
    cloudwatch_log_stream = aws_cloudwatch_log_stream.vpn_logs.name
  }

  dns_servers = [local.vpc_dns_resolver]

  session_timeout_hours = 8

  client_login_banner_options {
    enabled     = true
    banner_text = "This VPN is for authorized users only. All activities may be monitored and recorded."
  }

}

# Associating subnets and adding authorization rules
resource "aws_ec2_client_vpn_network_association" "vpn_subnet" {
  for_each               = toset(module.vpc.private_subnets)
  client_vpn_endpoint_id = aws_ec2_client_vpn_endpoint.vpn.id
  subnet_id              = each.value
}

resource "aws_ec2_client_vpn_authorization_rule" "vpn" {
  for_each               = toset(concat([var.vpc_cidr], var.secondary_cidr_blocks))
  client_vpn_endpoint_id = aws_ec2_client_vpn_endpoint.vpn.id
  target_network_cidr    = each.value
  authorize_all_groups   = true
}

# One route per (secondary CIDR, subnet) pair — see the comment on
# aws_ec2_client_vpn_route.internet below for why targeting a single subnet
# makes reachability depend on which subnet a client happens to land on.
resource "aws_ec2_client_vpn_route" "secondary_cidrs" {
  for_each = {
    for pair in setproduct(var.secondary_cidr_blocks, module.vpc.private_subnets) :
    "${pair[0]}-${pair[1]}" => { cidr = pair[0], subnet = pair[1] }
  }
  client_vpn_endpoint_id = aws_ec2_client_vpn_endpoint.vpn.id
  destination_cidr_block = each.value.cidr
  target_vpc_subnet_id   = each.value.subnet

  depends_on = [aws_ec2_client_vpn_network_association.vpn_subnet]
}

# Full-tunnel (split_tunnel = false) requires an explicit authorization rule
# and route for internet-bound traffic — AWS Client VPN does not add these
# automatically just because split-tunnel is disabled. Without them, clients
# get no path to the internet at all once connected. Egress goes through the
# VPC's existing NAT gateway (module.vpc, enable_nat_gateway = true).
resource "aws_ec2_client_vpn_authorization_rule" "internet" {
  client_vpn_endpoint_id = aws_ec2_client_vpn_endpoint.vpn.id
  target_network_cidr    = "0.0.0.0/0"
  authorize_all_groups   = true
}

#
# A Client VPN route is scoped to a single target subnet. A connecting client is
# assigned to one of the associated subnets (and gets an IP from that subnet's
# /27 slice of client_cidr_block) — which one is not predictable. A route that
# targets only one subnet therefore gives internet access only to the clients
# that happen to land on that subnet; clients on the others send traffic into
# the tunnel where it matches no route and is silently dropped. With three
# associations that was a 2-in-3 chance of "VPN connects but no internet",
# previously misdiagnosed as a client-side route race. One route per subnet.
resource "aws_ec2_client_vpn_route" "internet" {
  for_each               = toset(module.vpc.private_subnets)
  client_vpn_endpoint_id = aws_ec2_client_vpn_endpoint.vpn.id
  destination_cidr_block = "0.0.0.0/0"
  target_vpc_subnet_id   = each.value

  depends_on = [aws_ec2_client_vpn_network_association.vpn_subnet]
}


################################################################################
# Logging
################################################################################

resource "aws_cloudwatch_log_group" "vpn_logs" {
  # encrypted by default
  name              = "/aws/vpn/${local.vpn_url}"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_stream" "vpn_logs" {
  name           = "vpn-connection-logs"
  log_group_name = aws_cloudwatch_log_group.vpn_logs.name
}

################################################################################
# Generate Client VPN Config File
################################################################################

resource "local_file" "vpn_config" {
  filename = "${path.root}/client.ovpn"
  content  = <<-EOT
client
dev tun
proto udp
remote ${trimprefix(aws_ec2_client_vpn_endpoint.vpn.dns_name, "*.")} 443
remote-random-hostname
resolv-retry infinite
nobind
remote-cert-tls server
cipher AES-256-GCM
verify-x509-name ${local.vpn_url} name
reneg-sec 0
verb 3

<ca>
${tls_self_signed_cert.ca_cert.cert_pem}
</ca>

<cert>
${tls_locally_signed_cert.client_cert.cert_pem}
</cert>

<key>
${tls_private_key.client_key.private_key_pem}
</key>
EOT

  file_permission = "0600"

  depends_on = [
    aws_ec2_client_vpn_endpoint.vpn,
    tls_locally_signed_cert.client_cert,
    tls_private_key.client_key,
    tls_self_signed_cert.ca_cert
  ]
}

################################################################################
# Create Security Group(s)
################################################################################

resource "aws_security_group_rule" "eks_api_from_vpn" {
  type              = "ingress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = [var.vpc_cidr]
  security_group_id = module.eks.cluster_primary_security_group_id
  description       = "Allow VPN clients to reach the Kubernetes API server (Client VPN source-NATs to VPC CIDR)"
}
