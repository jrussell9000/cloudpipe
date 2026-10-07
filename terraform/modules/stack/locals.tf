locals {
  name   = var.name
  region = var.region

  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  # Derive the VPC DNS resolver from the VPC CIDR (always base address + 2 in AWS).
  # Use this instead of hardcoding 10.0.0.2 so changes to var.vpc_cidr propagate automatically.
  vpc_dns_resolver = cidrhost(var.vpc_cidr, 2)

  # Which access mode this deployment runs, derived from whether it has a domain
  # (design D1 of openspec/changes/optional-domain-and-cognito-auth). Every
  # resource that only exists to publish a UI — the ALB, its certificate, its
  # WAF, the ingresses, external-dns — keys off this one flag.
  publish_uis = var.domain != null

  # Service hostnames derived from var.domain. Change var.domain once and all of
  # them update together.
  #
  # Deliberately null in port-forward mode, rather than a placeholder: a
  # hostname has no meaning without a domain, and Terraform refuses to
  # interpolate null into a string ("Invalid template interpolation value"). So
  # a use that escapes its `local.publish_uis` guard fails at plan time instead
  # of rendering "argocd." into a live ingress.
  argocd_url   = local.publish_uis ? "argocd.${var.domain}" : null
  argo_url     = local.publish_uis ? "argo.${var.domain}" : null
  prefect_url  = local.publish_uis ? "prefect.${var.domain}" : null
  grafana_url  = local.publish_uis ? "grafana.${var.domain}" : null
  kubecost_url = local.publish_uis ? "kubecost.${var.domain}" : null

  # The Client VPN's certificates are self-signed and its endpoint is reached at
  # the AWS-assigned DNS name, so this is a common name and a log-group label,
  # never a resolvable host. It still has to be a string in both modes, because
  # it reaches tls_cert_request.subject — where a change would reissue the
  # certificates.
  vpn_url = local.publish_uis ? "vpn.${var.domain}" : "vpn.${var.name}.internal"

  # Where a browser reaches each UI, in whichever mode this deployment runs.
  # Everything that builds a redirect URI, an issuer or an API URL reads these,
  # so the two modes differ in one place.
  #
  # The port-forward ports are FIXED, not chosen per session (design D2): Dex and
  # Cognito both register redirect URIs exactly and cannot wildcard a port, so a
  # port that moved would need every client re-registered. `cloudpipe ui <name>`
  # forwards to the matching port.
  ui_base_urls = local.publish_uis ? {
    argocd   = "https://${local.argocd_url}"
    argo     = "https://${local.argo_url}"
    prefect  = "https://${local.prefect_url}"
    grafana  = "https://${local.grafana_url}"
    kubecost = "https://${local.kubecost_url}"
    } : {
    argocd   = "http://localhost:8080"
    argo     = "http://localhost:2746"
    prefect  = "http://localhost:4200"
    grafana  = "http://localhost:3000"
    kubecost = "http://localhost:9090"
  }

  # Dex runs inside ArgoCD and is served under its URL, so every Dex client
  # derives its issuer from one place.
  #
  # Known limitation in port-forward mode, open question 8 of the change above:
  # this URL is correct for a browser and wrong for a pod, where `localhost` is
  # the pod itself. Argo Workflows' SSO takes only an issuer and discovers the
  # rest from it, so it cannot split the two. The resolution is design D4 as
  # revised: in Cognito mode those UIs become Cognito app clients directly,
  # whose endpoints are public and identical from both sides (task 2.6). Dex
  # keeps ArgoCD, which always reaches it from the browser that is using it.
  dex_issuer_url = "${local.ui_base_urls["argocd"]}/api/dex"

  # Constant, and deliberately not read back from the security group's own tag:
  # every member of the `cloudpipe-ui` IngressGroup must send a byte-identical
  # `security-groups` annotation, and in port-forward mode the group does not
  # exist to be read.
  ui_alb_sg_name = "cloudpipe-ui-alb-sg"
}
