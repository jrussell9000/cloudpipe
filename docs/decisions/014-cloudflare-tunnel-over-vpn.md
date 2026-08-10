# 014 — Cloudflare Tunnel + Access to replace AWS Client VPN

**Status**: Proposed

## Context

The EKS API server has no public endpoint in steady state (`endpoint_public_access = false` after the initial bootstrap apply — see [infrastructure.md → Bootstrap phases](../infrastructure.md)). Today, private access for `kubectl`/`argo`/operator use is provided by an AWS Client VPN endpoint (`terraform/vpn.tf`): a split-tunnel OpenVPN-style connection with Terraform-managed client certificates (1-year validity).

The VPN works but has ongoing costs and operational friction: certificate lifecycle management, a per-user VPN client, and an AWS-specific connection profile that doesn't reuse the identity provider already federated for other cloudPipe services.

ArgoCD already federates the UW-Madison NetID OIDC IdP into Dex for its own login (`terraform/argocd.tf`) and for Argo Workflows RBAC (`var.admin_netid`, see `terraform/argowf.tf`, `terraform/grafana.tf`). That same IdP is a natural identity source for gating API-server access, without inventing a second identity system.

## Decision (planned, not yet implemented)

Replace the AWS Client VPN with a Cloudflare Tunnel + Cloudflare Access application in front of the EKS API server.

**Phase 1 scope**: EKS API server / `kubectl` access only. ArgoCD stays on its existing ALB (gated by the UW-Madison prefix list) until a phase 2 migration.

Planned shape, based on the design work already done:

- **Tunnel**: a `cloudflare_zero_trust_tunnel_cloudflared` resource with `config_src = "cloudflare"` (remotely managed), routed to the EKS control-plane (intra) subnet CIDRs via `cloudflare_zero_trust_tunnel_cloudflared_route`, so `kubectl` traffic reaches the API server as a private-network route rather than a hostname-based ingress rule.
- **Cluster side**: a `cloudflared` Deployment (via a new `gitops/apps/cloudflared/` ArgoCD app) authenticated with a tunnel token stored as a Kubernetes Secret. The `cloudflared` namespace and tunnel-token Secret are created directly in Terraform rather than via the ArgoCD ApplicationSet's `CreateNamespace=true`, to avoid a cross-tool ordering race between namespace creation and secret population.
- **Identity**: register the existing UW-Madison OIDC IdP as a *second*, independent OIDC client in Cloudflare Access (reusing the same login.<YOUR_INSTITUTION_DOMAIN> issuer already used for Dex), rather than standing up a separate identity provider.
- **Authorization**: an explicit NetID allowlist reusing `var.admin_netid` (the same variable already gating ArgoCD's Dex RBAC admin role and Argo Workflows RBAC), rather than a blanket `@<YOUR_INSTITUTION_DOMAIN>` policy.
- **Access application**: `self_hosted`, `private` destination type (CIDR + port 443, TCP) — no public hostname exposed.
- **Credentials**: Cloudflare Access OIDC client ID/secret pre-created manually in Secrets Manager (`cloudpipe/cloudflare-access-oidc`), consistent with this repo's existing convention of never letting Terraform create `aws_secretsmanager_secret` resources (see the `argocd_dex_oidc` data source in `terraform/argocd.tf` for the same pattern). Cloudflare API auth for the provider itself is via the `CLOUDFLARE_API_TOKEN` environment variable, not a Terraform variable — matching how AWS credentials are supplied to this stack.
- **New Terraform surface**: a `cloudflare` provider (`cloudflare/cloudflare ~> 5.0`), a `var.cloudflare_account_id` variable, and a new `terraform/cloudflare.tf` file holding the tunnel/route/IdP/policy/Access-application/namespace/secret resources described above.
- `terraform/eks.tf`'s comment on `endpoint_public_access` should be updated to reference the tunnel once it lands, replacing the current "once the VPN is in place" language.

## Why not implemented yet

This ADR exists to preserve the design so it isn't lost, without committing the infrastructure change yet. The VPN remains the access path until this is explicitly picked back up.

## Consequences (anticipated, to revisit when implemented)

- Removes per-user VPN client setup and Terraform-managed certificate rotation in exchange for a Cloudflare Zero Trust dependency (tunnel + Access must both be healthy for any `kubectl` access).
- Reuses `var.admin_netid` and the UW-Madison OIDC issuer rather than introducing a new identity/authorization model — keeps a single source of truth for "who is an admin" across ArgoCD, Argo Workflows RBAC, Grafana, and (eventually) cluster access.
- Phase 1 is additive to the VPN, not a replacement in the same change — the VPN should stay in place until the tunnel path is validated, then be removed in a follow-up.
- ArgoCD's ALB access path is explicitly out of scope for phase 1; migrating it is a separate, later decision.
