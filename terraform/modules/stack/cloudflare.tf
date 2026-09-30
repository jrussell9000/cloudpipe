# Cloudflare Zero Trust — phase 1 of ADR 014: private-network access to the EKS
# API server, replacing the AWS Client VPN.
#
# Phase 1 is deliberately ADDITIVE. The Client VPN (vpn.tf) stays up and remains
# the working path until `kubectl get nodes` has been proven through the tunnel
# with the VPN disconnected. Do not touch vpn.tf from here.
#
# Provider auth is the CLOUDFLARE_API_TOKEN environment variable, never a
# Terraform variable — a variable would persist the token in state. See
# handoffs/cloudflare-access-oidc-registration.md §2.6. The provider block that
# reads it is in the root's providers.tf: a module configures no provider, and
# an empty one here would be a deprecated proxy declaration rather than a
# configuration.

# The tunnel itself. `config_src = "cloudflare"` makes this a remotely-managed
# tunnel: ingress/route config lives in Cloudflare rather than in a local
# cloudflared config file.
#
# `tunnel_secret` is deliberately left unset. It is optional and — verified
# against provider v5.23.0 — NOT computed, so leaving it null keeps it out of
# state rather than having Cloudflare populate it on read. A remotely-managed
# tunnel does not need it. For the same reason there is deliberately no
# `data "cloudflare_zero_trust_tunnel_cloudflared_token"` anywhere in this
# config: that data source is the one thing that would write the connector
# token into state. The token is stored by hand in Secrets Manager and reaches
# the cluster via External Secrets (gitops/apps/cloudflared/). See ADR 014
# Amendment 1.
resource "cloudflare_zero_trust_tunnel_cloudflared" "cluster" {
  account_id = var.cloudflare_account_id
  name       = "cloudpipe-eks"
  config_src = "cloudflare"
}

# Non-sensitive — an ID, not the token. install.sh reads it to fetch the
# connector token over the API and pipe it straight into Secrets Manager, which
# is how the token stays out of state while still self-healing when the tunnel
# is recreated (a new tunnel has a new token).
output "cloudflare_tunnel_id" {
  value = cloudflare_zero_trust_tunnel_cloudflared.cluster.id
}

output "cloudflare_account_id" {
  value = var.cloudflare_account_id
}

# Private-network routes to the EKS API server.
#
# ADR 014 says to route the "control-plane (intra) subnet CIDRs", but there is
# no intra tier: eks.tf sets control_plane_subnet_ids = module.vpc.intra_subnets
# and nothing ever assigns intra_subnets, so it is an empty list and the EKS
# module falls back to subnet_ids (the private subnets). Confirmed against the
# live cluster — its three subnets are the cloudpipe-private-* /20s.
#
# Derived from the module output rather than hardcoded: the CIDRs are computed
# from var.vpc_cidr via cidrsubnet(), so hardcoding would silently drift if that
# variable ever changed.
#
# Note this routes ALL private-subnet traffic for a connected WARP client, not
# only API-server traffic. Narrowing to the API server's ENIs is not viable —
# they are dynamic and unmanaged. The Access application's port restriction and
# the NetID allowlist policy are what gate access; the route is reachability
# only.
resource "cloudflare_zero_trust_tunnel_cloudflared_route" "private_subnets" {
  for_each = toset(module.vpc.private_subnets_cidr_blocks)

  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.cluster.id
  network    = each.value
  comment    = "cloudpipe EKS private subnet"
}

# --- Access identity provider (UW-Madison NetID) ------------------------------
#
# Unblocked 2026-08-17: DoIT registered the client and the credentials are in
# Secrets Manager at cloudpipe/cloudflare-access-oidc, created by hand (§2.2 —
# Terraform must never own an aws_secretsmanager_secret here).
#
# VERIFIED WORKING 2026-08-17. An IdP Test returns a resolved identity —
# email "<netid>@<institution_domain>", with acr https://refeds.org/profile/mfa proving
# NetID MFA was enforced. The long-open token_endpoint_auth_method question
# (handoffs/cloudflare-access-oidc-doit-reply.md §1.3) is settled by the same
# round-trip: whatever Cloudflare sends, the institution IdP accepts it. Do not
# reopen it.
#
# The IdP alone is inert — it grants nothing until a policy references it, which
# is what makes the dashboard's "Test" button a zero-blast-radius diagnostic.
# The policy and applications that DO create an access path are further down.
#
# One hard-won constraint, worth not rediscovering: Cloudflare's generic OIDC
# connector reads claims **only from the ID token**. There is no userinfo_url
# argument anywhere in this resource's schema and it never calls /userinfo, so a
# claim released only at the userinfo endpoint is invisible to Access. Getting
# here required DoIT to release an identity claim in the ID token itself.
#
# Corollary: Dex works against this same IdP only because Dex calls /userinfo.
# Never infer Cloudflare's behaviour from Dex's.
data "aws_secretsmanager_secret_version" "cloudflare_access_oidc" {
  secret_id = "cloudpipe/cloudflare-access-oidc"
}

locals {
  cloudflare_access_oidc = jsondecode(
    data.aws_secretsmanager_secret_version.cloudflare_access_oidc.secret_string
  )
}

# Cloudflare requires auth_url/token_url/certs_url EXPLICITLY — there is no
# discovery-only mode, unlike Dex, which takes just the issuer (plan §2.1). The
# values come from <institution_oidc_issuer>/.well-known/openid-configuration, read
# live on 2026-08-15.
#
# The client secret DOES land in Terraform state. That is a recorded, deliberate
# tradeoff (plan §2.7), not an oversight — unlike the tunnel token, which is
# kept out of state by refusing the data source that would read it.
resource "cloudflare_zero_trust_access_identity_provider" "netid" {
  account_id = var.cloudflare_account_id
  name       = "UW-Madison NetID"
  type       = "oidc"

  config = {
    client_id     = local.cloudflare_access_oidc["clientID"]
    client_secret = local.cloudflare_access_oidc["clientSecret"]
    auth_url      = "${var.institution_oidc_issuer}/idp/profile/oidc/authorize"
    token_url     = "${var.institution_oidc_issuer}/idp/profile/oidc/token"
    certs_url     = "${var.institution_oidc_issuer}/idp/profile/oidc/keyset"

    scopes = ["openid", "profile", "email"]

    # `claims` are surfaced in the Access JWT passed to origins and usable as
    # policy selectors. eppn is the identity; `acr` is listed only so the
    # admin policy below can REQUIRE the MFA profile — an OIDC-claim selector
    # can only match a claim named here.
    #
    # This list was briefly much longer, as a diagnostic. If you ever need to see
    # what the institution IdP actually puts in the ID token, that technique is the
    # only way to observe it from our side: list claim names here and read them
    # back from `oidc_fields` in the IdP Test dialog. Include a control group of
    # protocol claims (iss/aud/auth_time/acr) when you do — an empty result
    # otherwise cannot distinguish "claim absent" from "probe never ran", which
    # made the first round of testing worthless. See
    # handoffs/cloudflare-access-oidc-doit-reply.md.
    claims = ["eduperson_principal_name", "acr"]

    # eduperson_principal_name, NOT email — a deliberate deviation from the plan
    # (§3.4 says "email" to match Dex's userNameKey: email).
    #
    # DoIT released eduperson_principal_name in the ID token on 2026-08-17 rather
    # than email, on the grounds that it is the more stable identifier. That is
    # correct: a NetID user's email can change, their eppn does not. Since eppn is
    # netid@<institution_domain> it is email-shaped, so Access policies keyed on email still
    # work against it — email_claim_name is just "which claim holds the thing
    # Access treats as the user's email".
    #
    # Consequence to keep in mind: Access identities are now eppn while Dex's are
    # email. Those happen to coincide for UW accounts, but they are not the same
    # field, so never assume a value from one path is comparable to the other.
    email_claim_name = "eduperson_principal_name"
    pkce_enabled     = true
  }
}

# --- Authorization: who may enroll a device and reach the API server ----------
#
# ONE reusable policy, attached to both applications below, so "who is an admin"
# cannot drift between "may enroll WARP" and "may reach the cluster".
#
# `include` is the allowlist — var.admin_netid's eppn, never a blanket
# whole-domain rule (that would grant cluster reachability to every
# university). An `email` selector matches because eppn is email-shaped and the
# IdP maps it into Access's email slot (email_claim_name above).
#
# `require` fails closed on MFA. The institution IdP returns
# acr = https://refeds.org/profile/mfa for an MFA login (IdP Test, 2026-08-17);
# requiring it here means a login that ever arrives without MFA is denied,
# rather than trusting NetID to keep enforcing it. Because the selector is bound
# to this IdP's ID, it also implicitly rejects every other login method (e.g.
# One-time PIN), which carries no such claim.
resource "cloudflare_zero_trust_access_policy" "cluster_admins" {
  account_id = var.cloudflare_account_id
  name       = "cloudpipe cluster admins (NetID + MFA)"
  decision   = "allow"

  include = [
    { email = { email = "${var.admin_netid}@${var.institution_domain}" } },
  ]

  require = [
    {
      oidc = {
        identity_provider_id = cloudflare_zero_trust_access_identity_provider.netid.id
        claim_name           = "acr"
        claim_value          = "https://refeds.org/profile/mfa"
      }
    },
  ]
}

# --- Access organization (account singleton) ----------------------------------
#
# Managed for ONE setting: warp_auth_session_duration, which Cloudflare requires
# before any app may set allow_authenticate_via_warp (see private_services). The org
# already existed (created 2026-07-09 with the Zero Trust account).
#
# Adopting it is safe, verified in the provider source at v5.23.0
# (internal/services/zero_trust_organization/resource.go): Create calls
# Organizations.Update — a PUT on the existing org — so it cannot make a second
# one, and Delete is a no-op, so cleanup.sh's full destroy leaves the org and
# its team domain alone. The resource does not support `terraform import`, and
# with Create being an update it does not need to.
#
# auth_domain and name are pinned to the live values read over the API on
# 2026-09-17, because both are optional-but-not-computed and must never be sent
# as null. auth_domain is load-bearing: it is the team domain in the NetID
# redirect URI DoIT registered. `name` is the stale auto-generated one; it is
# cosmetic (handoffs/cloudflare-access-oidc-registration.md §2.5), and renaming
# it is a separate, deliberate change.
#
# 24h matches the EKS app's session_duration. When the WARP session identity
# lapses, the client shows "Authentication required"; clicking it, or
# `warp-cli debug access-reauth`, renews it with a NetID login.
locals {
  cloudflare_team_domain = var.cloudflare_team_domain
}

resource "cloudflare_zero_trust_organization" "this" {
  account_id                 = var.cloudflare_account_id
  auth_domain                = local.cloudflare_team_domain
  name                       = var.cloudflare_team_name
  warp_auth_session_duration = "24h"
}

# Device enrollment permissions. Without a `warp` application, WARP enrollment
# against the org is refused outright — plan 010 originally missed this, and
# the org had zero Access applications as of 2026-09-17. It is a singleton per
# account: if one is ever created in the dashboard, import it rather than
# letting this create a second.
#
# Not listed under Access → Applications in the dashboard: a warp-type app
# surfaces only as the "Device enrollment permissions" settings page.
resource "cloudflare_zero_trust_access_application" "warp_enrollment" {
  account_id = var.cloudflare_account_id
  type       = "warp"
  # Cloudflare forces this exact name on every warp-type app, whatever is sent,
  # so any other value (as in Cloudflare's own Terraform example) is a
  # perpetual in-place rename in every plan. Verified after the first apply,
  # 2026-09-17.
  name = "Warp Login App"

  # No app_launcher_visible: Cloudflare's own Terraform example sets it, but
  # provider v5.23.0 rejects it for type = "warp" at validate time.
  allowed_idps              = [cloudflare_zero_trust_access_identity_provider.netid.id]
  auto_redirect_to_identity = true

  policies = [
    { id = cloudflare_zero_trust_access_policy.cluster_admins.id, precedence = 1 },
  ]
}

# Everything private that operators reach over WARP, as ONE private-network
# application: TCP 443 and 80 on each private subnet the tunnel routes. The
# routes above are reachability only; this is what gates it. Destinations are
# derived from the same module output as the routes so the two cannot disagree.
#
# Two services live on those subnets:
#   - the EKS API server (443) — plan 010;
#   - the shared internal web-UI ALB (443, and 80 for its HTTPS redirect
#     listener) — ArgoCD, Argo Workflows, Grafana, Prefect, Kubecost; plan 012.
#     Without port 80 here, http://<ui>.<domain> times out at Gateway instead of
#     redirecting.
# The destinations are CIDR+port, not per service, because the API server's and
# the ALB's ENIs are dynamic. Because the app accepts the WARP session identity,
# a browser reaches the UIs with no per-app Access login — expected; each UI
# still logs in through Dex.
#
# This does not by itself put packets on the tunnel — that is the device
# profile's split tunnel and the Gateway TCP proxy, both at the end of this file.
#
# depends_on the organization: allow_authenticate_via_warp below is refused
# (API error 12130) until the org has a warp_auth_session_duration, so on a
# fresh apply the org must be written first.
# The rename from `eks_api` to this name is recorded in the root's
# moved_to_stack.tf, not here. Inside the module both of its addresses would
# carry the module.stack prefix, and this resource would then have two declared
# sources — the rename and the extraction — which Terraform rejects as an
# ambiguous move. In the root the two are a chain instead: eks_api →
# private_services → module.stack.private_services.
resource "cloudflare_zero_trust_access_application" "private_services" {
  depends_on = [cloudflare_zero_trust_organization.this]

  account_id = var.cloudflare_account_id
  type       = "self_hosted"
  name       = "cloudpipe private services (EKS API + web UIs)"

  # 443 entries first, so adding 80 appends rather than shifting the existing
  # list positions.
  destinations = concat(
    [
      for cidr in module.vpc.private_subnets_cidr_blocks : {
        type        = "private"
        cidr        = cidr
        port_range  = "443"
        l4_protocol = "tcp"
      }
    ],
    [
      for cidr in module.vpc.private_subnets_cidr_blocks : {
        type        = "private"
        cidr        = cidr
        port_range  = "80"
        l4_protocol = "tcp"
      }
    ],
  )

  allowed_idps              = [cloudflare_zero_trust_access_identity_provider.netid.id]
  auto_redirect_to_identity = true
  app_launcher_visible      = false
  session_duration          = "24h"

  # Accept the WARP client's session identity (the NetID login done at device
  # enrollment) instead of demanding a per-app browser login. Without this the
  # app inherits the org default, false, and a raw TCP client like kubectl
  # cannot perform that login: Gateway accepts the TCP connection at the edge
  # and then holds it, so kubectl reports "TLS handshake timeout" while the
  # tunnel itself is fine (a port with no Access app, e.g. kubelet 10250,
  # completes TLS over the same path). Diagnosed 2026-09-17. Set per app, which
  # always overrides the org setting.
  allow_authenticate_via_warp = true

  policies = [
    { id = cloudflare_zero_trust_access_policy.cluster_admins.id, precedence = 1 },
  ]
}

# --- WARP client reachability -------------------------------------------------
#
# The Access resources above decide WHO may connect; these two decide whether a
# WARP client's packets reach the tunnel at all. Both are account singletons, so
# "create" configures the existing object rather than making a new one.
#
# Both need the API token's account-level "Zero Trust: Edit" permission, beyond
# the Tunnel and Access permissions the rest of this file uses (plan 010 §3.5).
# Without it every plan fails on these two with API error 10000 "Authentication
# error" — which is the token's scope, not a wrong token.

# Split Tunnels in INCLUDE mode: only the VPC goes through WARP. Everything else
# — general internet, Zoom, the UW-Madison VPN, a home LAN — stays on the
# device's own connection. Nothing in cloudpipe uses Gateway filtering, so
# routing all traffic through Cloudflare would buy nothing.
#
# This replaces WARP's default EXCLUDE mode, whose list contains 10.0.0.0/8 and
# so silently kept cluster traffic OFF the tunnel (a TCP timeout with the client
# connected, indistinguishable from a broken tunnel). Include mode also avoids
# hand-maintaining carve-outs of 10/8 around the VPC.
#
# var.vpc_cidr rather than the three private /20s: phase 2's internal ALB lives
# in the same VPC (plan 012), and the tunnel routes are what bound reachability.
# A home network that itself uses 10.0.0.0/16 still collides, as with the VPN.
#
# The team domain is included so WARP can reach Access for re-authentication and
# block pages; harmless if unneeded.
#
# tunnel_protocol is pinned to the account's existing value: left unset, the
# provider plans it as "" rather than reading it back, which would reset the
# client's transport. Every other attribute planned equal to the account's
# values on 2026-09-17.
#
# dns_search_suffixes = [] is the account's value, stated explicitly: left
# unset (optional+computed), the provider marks it unknown on every plan, which
# turns an unchanged profile into a perpetual in-place update (found by the
# post-apply plan, 2026-09-17).
#
# Even so, every plan still shows an in-place update with only
# `policy_id -> (known after apply)`. That is a provider bug (policy_id lacks
# UseStateForUnknown in v5.23.0), cloudflare/terraform-provider-cloudflare#7343.
# Not suppressible here — ignore_changes covers configured attributes only —
# and applying it re-sends identical values. Accept it; re-check on a provider
# bump.
#
# The provider cannot destroy this resource — `terraform destroy` (cleanup.sh)
# only drops it from state and the account keeps these settings, which is the
# right outcome for a cluster teardown.
resource "cloudflare_zero_trust_device_default_profile" "this" {
  account_id          = var.cloudflare_account_id
  tunnel_protocol     = "masque"
  dns_search_suffixes = []

  include = [
    {
      address     = var.vpc_cidr
      description = "cloudpipe VPC (EKS API server, internal ALB)"
    },
    {
      host        = local.cloudflare_team_domain
      description = "Zero Trust team domain"
    },
  ]
}

# The Gateway TCP proxy carries WARP traffic onto a tunnel's private-network
# routes; with it off, private destinations are unreachable regardless of the
# Access policy. That is the only value cloudpipe needs.
#
# The other four are pinned to what the account already had on 2026-09-17
# (read over the API before the first plan), NOT chosen: in provider v5.23.0
# they are optional but not computed, so leaving one unset plans it to null and
# silently switches off whatever Cloudflare defaulted. Change them deliberately
# or not at all.
resource "cloudflare_zero_trust_device_settings" "this" {
  account_id            = var.cloudflare_account_id
  gateway_proxy_enabled = true

  gateway_udp_proxy_enabled             = true
  use_zt_virtual_ip                     = true
  root_certificate_installation_enabled = false
  disable_for_time                      = 0
}
