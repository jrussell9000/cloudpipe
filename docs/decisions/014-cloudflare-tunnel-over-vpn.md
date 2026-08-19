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
- **Cluster side**: a `cloudflared` Deployment (via a new `gitops/apps/cloudflared/` ArgoCD app) authenticated with a tunnel token stored as a Kubernetes Secret. The `cloudflared` namespace and tunnel-token Secret are created directly in Terraform rather than via the ArgoCD ApplicationSet's `CreateNamespace=true`, to avoid a cross-tool ordering race between namespace creation and secret population. **Superseded — see Amendment 1.**
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

## Amendment 1 (2026-08-15) — keep the tunnel token out of Terraform state

The original "Cluster side" bullet has Terraform create the tunnel-token Kubernetes Secret. Doing so requires Terraform to read the token, which persists it in the state file. State is remote and encrypted, but a value written to state can survive in prior versions of the state object after it is removed from the current one — so it is materially cheaper to never write it than to retract it later.

Revised shape:

- Terraform creates the tunnel (`config_src = "cloudflare"`); state holds only its ID and name. A remotely-managed tunnel requires no `tunnel_secret` input.
- **Do not** declare `data "cloudflare_zero_trust_tunnel_cloudflared_token"`. In `cloudflare/cloudflare ~> 5.0` the token is exposed through that data source rather than as an attribute of the tunnel resource, so omitting it is sufficient to keep the value out of state. Verified against v5.23.0: the resource has no `token` attribute, and its `tunnel_secret` field is optional and *not* computed, so leaving it unset leaves it null in state rather than being populated on read.
- Store the token manually in Secrets Manager (`cloudpipe/cloudflare-tunnel-token`), consistent with this repo's existing convention of never letting Terraform create `aws_secretsmanager_secret` resources.
- Terraform declares an `ExternalSecret` referencing that secret's ARN; External Secrets Operator (already deployed, with a `ClusterSecretStore`) materializes the Kubernetes Secret in-cluster. Terraform holds the pointer, never the payload — the same pattern already used for RDS credentials in `terraform/modules/argo-workflows/database.tf`.

This also dissolves the namespace/secret ordering race the original bullet was working around: ESO owns Secret creation, so Terraform only needs to create the namespace.

Trade-off: the tunnel token becomes a manual step. If the tunnel is recreated its token changes, and a stale stored value surfaces as a `cloudflared` connectivity failure rather than an obvious credential error.

**Scope of this amendment.** The Access identity provider's OIDC client secret cannot be handled this way — `cloudflare_zero_trust_access_identity_provider` must transmit `client_secret` to Cloudflare, so Terraform is necessarily the reader. That value is **deliberately accepted in state** (decided 2026-08-15), because the alternative — creating the identity provider by hand and referencing it by ID — moves the configuration that governs who can reach the cluster outside version control, where drift is invisible to `terraform plan`. The IaC coverage is worth more than removing one rotatable secret.

The resulting posture is therefore "one secret in state, deliberately" — the Access IdP client secret — with the tunnel token specifically excluded by the design above. It is not "no secrets in state", and should not be described that way.
