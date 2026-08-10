################################################################################
# Namespace — created by ArgoCD (CreateNamespace=true in ApplicationSet);
# referenced here so dependent resources have an explicit handle.
################################################################################

data "kubernetes_namespace_v1" "this" {
  metadata {
    name = var.namespace
  }
}

################################################################################
# ALB security group — restricts inbound to operator IP only
################################################################################

resource "aws_security_group" "lb" {
  name_prefix = "${var.cluster_name}-prefect-lb-"
  vpc_id      = var.vpc_id
  description = "Controls inbound access to the Prefect ALB."
  tags        = merge(var.tags, { Name = "${var.cluster_name}-prefect-lb-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "lb_https" {
  security_group_id = aws_security_group.lb.id
  description       = "HTTPS from operator workstation"
  prefix_list_id    = var.inbound_prefix_list_id
  from_port         = 443
  ip_protocol       = "tcp"
  to_port           = 443
}

# The rule above only covers traffic to VPC-internal destinations (where the
# VPN endpoint's own SNAT applies). Traffic from a full-tunnel VPN client to
# this ALB's *public* IP instead hairpins out through the VPC's NAT gateway
# and back in over the internet, presenting the NAT gateway's EIP as the
# source — so that EIP needs its own trust rule too.
resource "aws_vpc_security_group_ingress_rule" "lb_https_nat" {
  security_group_id = aws_security_group.lb.id
  description       = "HTTPS from VPC NAT gateway (full-tunnel VPN clients hairpin through here)"
  cidr_ipv4         = "${var.nat_gateway_ip}/32"
  from_port         = 443
  ip_protocol       = "tcp"
  to_port           = 443
}

# The ALB has an HTTP:80 listener whose only job is a 301 redirect to HTTPS (the ingress's
# ssl-redirect annotation). That redirect can only fire if port 80 is actually reachable, so
# the port-80 listener needs the same source trust as 443 — otherwise http:// is dropped at
# the security group and times out instead of redirecting. Mirror both 443 rules onto port 80.
resource "aws_vpc_security_group_ingress_rule" "lb_http" {
  security_group_id = aws_security_group.lb.id
  description       = "HTTP (redirects to HTTPS) from operator workstation"
  prefix_list_id    = var.inbound_prefix_list_id
  from_port         = 80
  ip_protocol       = "tcp"
  to_port           = 80
}

resource "aws_vpc_security_group_ingress_rule" "lb_http_nat" {
  security_group_id = aws_security_group.lb.id
  description       = "HTTP (redirects to HTTPS) from VPC NAT gateway (full-tunnel VPN clients hairpin through here)"
  cidr_ipv4         = "${var.nat_gateway_ip}/32"
  from_port         = 80
  ip_protocol       = "tcp"
  to_port           = 80
}

# Split-tunnel VPN clients (and genuinely VPC-internal callers) reach the ALB without
# hairpinning through the NAT gateway — their traffic keeps the VPN endpoint's VPC-CIDR
# SNAT source instead of the NAT EIP. Trust the VPC CIDR on both ports so those paths work
# too, bringing Prefect to full parity with the Grafana/ArgoCD/Kubecost ALBs.
resource "aws_vpc_security_group_ingress_rule" "lb_https_client_vpn" {
  security_group_id = aws_security_group.lb.id
  description       = "HTTPS from AWS Client VPN (source-NATs to VPC CIDR)"
  cidr_ipv4         = var.vpc_cidr
  from_port         = 443
  ip_protocol       = "tcp"
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "lb_http_client_vpn" {
  security_group_id = aws_security_group.lb.id
  description       = "HTTP (redirects to HTTPS) from AWS Client VPN (source-NATs to VPC CIDR)"
  cidr_ipv4         = var.vpc_cidr
  from_port         = 80
  ip_protocol       = "tcp"
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "lb" {
  security_group_id = aws_security_group.lb.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

################################################################################
# Ingress — ALB with TLS termination and HTTP→HTTPS redirect
################################################################################

resource "kubernetes_ingress_v1" "this" {
  metadata {
    name      = "prefect-ingress"
    namespace = var.namespace
    annotations = {
      "external-dns.alpha.kubernetes.io/hostname" = "prefect.${var.route53_zone_name}"

      "alb.ingress.kubernetes.io/scheme"      = "internet-facing"
      "alb.ingress.kubernetes.io/target-type" = "ip"

      "alb.ingress.kubernetes.io/certificate-arn" = var.certificate_arn
      "alb.ingress.kubernetes.io/listen-ports"    = "[{\"HTTP\": 80}, {\"HTTPS\": 443}]"
      "alb.ingress.kubernetes.io/ssl-redirect"    = "443"
      "alb.ingress.kubernetes.io/ssl-policy"      = "ELBSecurityPolicy-TLS13-1-2-2021-06"

      "alb.ingress.kubernetes.io/security-groups"                     = aws_security_group.lb.id
      "alb.ingress.kubernetes.io/manage-backend-security-group-rules" = "true"

      "alb.ingress.kubernetes.io/backend-protocol"     = "HTTP"
      "alb.ingress.kubernetes.io/healthcheck-protocol" = "HTTP"
      "alb.ingress.kubernetes.io/healthcheck-path"     = "/ping"

      # idle_timeout raised to the ALB maximum (4000s) so the UI's live-update streams survive
      # quiet periods; the 60s default tears them down and the page silently shows stale state.
      "alb.ingress.kubernetes.io/load-balancer-attributes" = "access_logs.s3.enabled=false,idle_timeout.timeout_seconds=4000"
    }
  }

  spec {
    ingress_class_name = "alb"

    rule {
      host = "prefect.${var.route53_zone_name}"
      http {
        path {
          path      = "/*"
          path_type = "ImplementationSpecific"
          backend {
            service {
              # fullnameOverride: "prefect-oauth2-proxy" is set in the Helm values so
              # the service name is predictable regardless of the Helm release name.
              name = "prefect-oauth2-proxy"
              port {
                number = 4180
              }
            }
          }
        }
      }
    }
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}
