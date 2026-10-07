# Amazon Cognito — the default identity provider (design D3 and D4 of
# openspec/changes/optional-domain-and-cognito-auth).
#
# One user pool gates both doors into the deployment: Cloudflare Access, which
# decides who may enroll a WARP device and reach the private network, and the
# web UIs — ArgoCD through Dex, and Argo Workflows, Prefect and Grafana as the
# pool's own clients (cognito_clients below). Everything here exists only when the
# caller supplies no `external_identity` — a deployment that already integrates
# an identity provider directly keeps doing that, and gets none of this.
#
# NIST SP 800-171 3.5.3 (multifactor authentication for network access) is
# satisfied by the pool's own configuration, which is the point of declaring it
# here: MFA is a reviewed line of code, so switching it off shows in a plan
# instead of happening in a console.
#
# Users are not created here. Creating one is a mutation of the deployer's
# account that belongs to the setup wizard's v2; until then the deployer runs
# `aws cognito-idp admin-create-user` (the change's task 5.5 documents it).

locals {
  # Which identity mode this deployment runs, derived from whether an external
  # provider was supplied rather than chosen by a second variable that could
  # disagree with it (design D6 as built, following D1's argument).
  #
  # nonsensitive() because the variable is marked sensitive for the Dex client
  # secret inside it, and sensitivity propagates through `== null` — which would
  # make every `count` below a sensitive value, and Terraform refuses those.
  # Whether the object is null reveals nothing in it.
  use_cognito = nonsensitive(var.external_identity == null)
  cognito     = local.use_cognito ? 1 : 0
}

# Cognito domain prefixes are global across every AWS account, and the provider
# does not check availability before it tries, so a fixed name collides with
# the first other deployment that picked it. Hex, not random_string: a prefix
# may not contain `aws`, `amazon` or `cognito`, and hex digits cannot spell any
# of them.
resource "random_id" "cognito_domain" {
  count       = local.cognito
  byte_length = 4
}

resource "aws_cognito_user_pool" "operators" {
  count = local.cognito

  name = "${var.name}-operators"

  # The plan is pinned, not left to Cognito's default (which is Essentials for
  # a new pool today), so that a downgrade to Lite is a visible change: Lite has
  # no password history, and 3.5.8 needs it. Essentials is free below 10,000
  # monthly active users (design open question 2).
  user_pool_tier = "ESSENTIALS"

  # The pool is the only way in to a private cluster; losing it is a lockout.
  # deletion_protection makes Cognito itself refuse DeleteUserPool, which also
  # covers a replacement forced by an attribute change — the delete half fails
  # and nothing is removed. prevent_destroy stops the same plan earlier.
  # cleanup.sh knows about both and tears the pool down outside Terraform.
  deletion_protection = "ACTIVE"

  # Sign in with an email address. It is also the identity the Access policy and
  # every UI's role binding match on, so there is one identifier throughout.
  username_attributes = ["email"]

  username_configuration {
    case_sensitive = false
  }

  # Administrator-created users only. An unknown visitor to the login page has
  # no way to make an account.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  # 3.5.3. ON means every local user must complete MFA, and the authenticator
  # app is the only factor offered: there is deliberately no
  # sms_configuration and no email_mfa_configuration block. SP 800-63B restricts
  # SMS and does not allow email as an authenticator, and email MFA would need
  # SES besides.
  mfa_configuration = "ON"

  software_token_mfa_configuration {
    enabled = true
  }

  # Password only as the first factor. Essentials pools also offer passwordless
  # first factors (email or SMS one-time codes, passkeys), and a passwordless
  # sign-in does not take the MFA step at all — so an emailed code would become
  # the whole login. Pinned rather than left to the default for that reason.
  sign_in_policy {
    allowed_first_auth_factors = ["PASSWORD"]
  }

  # 3.5.7 and 3.5.8. Cognito cannot require a number of changed characters, and
  # its passwords never expire; the SSP covers both through the required second
  # factor and reuse prevention. 24 is the most history Cognito keeps.
  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    password_history_size            = 24
    temporary_password_validity_days = 7
  }

  # Recovery by an administrator only. Self-service recovery would send a code
  # to an email address, which makes the mailbox a way to reset a password.
  account_recovery_setting {
    recovery_mechanism {
      name     = "admin_only"
      priority = 1
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# The login pages, on an AWS-hosted prefix domain, so no owned domain is
# needed. Version 2 is managed login, which needs a branding style per app
# client (below) before that client has a page at all.
resource "aws_cognito_user_pool_domain" "operators" {
  count = local.cognito

  domain                = "${var.name}-${random_id.cognito_domain[0].hex}"
  user_pool_id          = aws_cognito_user_pool.operators[0].id
  managed_login_version = 2
}

locals {
  # Every Cognito endpoint the configuration uses, built from the pool and its
  # domain in this one place. The paths are Cognito's own, the same for every
  # pool; nothing institution-specific is written anywhere in this file.
  cognito_issuer     = local.use_cognito ? "https://${aws_cognito_user_pool.operators[0].endpoint}" : null
  cognito_login_host = local.use_cognito ? "${aws_cognito_user_pool_domain.operators[0].domain}.auth.${var.region}.amazoncognito.com" : null

  cognito_authorize_url = local.use_cognito ? "https://${local.cognito_login_host}/oauth2/authorize" : null
  cognito_token_url     = local.use_cognito ? "https://${local.cognito_login_host}/oauth2/token" : null
  cognito_userinfo_url  = local.use_cognito ? "https://${local.cognito_login_host}/oauth2/userInfo" : null
  cognito_jwks_url      = local.use_cognito ? "${local.cognito_issuer}/.well-known/jwks.json" : null

  # Cloudflare Access's callback is fixed by the team domain. Building it here
  # rather than reading the identity provider's computed `redirect_url` avoids a
  # cycle: that provider needs this client's secret first.
  cloudflare_access_callback_url = "https://${local.cloudflare_team_domain}/cdn-cgi/access/callback"

  # Every relying party on the pool, keyed by client name, with the one callback
  # each registers (design D4, revised).
  #
  #   - cloudflare-access: WARP enrollment and the private network.
  #   - dex: the ArgoCD UI. The callback is the redirect URI the connector in
  #     argocd.tf sends.
  #   - argo-workflows, prefect, grafana: the three UIs whose servers call the
  #     issuer from inside a pod as well as from the browser. Dex's issuer is
  #     ArgoCD's URL, which in port-forward mode means localhost — the pod
  #     itself. Cognito's endpoints are public and the same from both sides, so
  #     these UIs use the pool directly. Their callbacks are the ones their Dex
  #     static clients register, so each UI's own redirect setting holds in
  #     either mode.
  #
  # local.ui_base_urls names localhost in port-forward mode, and Cognito accepts
  # a plain-HTTP callback to localhost on any port.
  cognito_clients = {
    "cloudflare-access" = local.cloudflare_access_callback_url
    "dex"               = "${local.ui_base_urls["argocd"]}/api/dex/callback"
    "argo-workflows"    = "${local.ui_base_urls["argo"]}/oauth2/callback"
    "prefect"           = "${local.ui_base_urls["prefect"]}/oauth2/callback"
    "grafana"           = "${local.ui_base_urls["grafana"]}/login/generic_oauth"
  }
}

# One client per relying party (design D3, D4). Their shared settings:
#
#   - authorization-code flow only, with a secret, through the managed login
#     pages (`allowed_oauth_flows_user_pool_client`);
#   - the COGNITO provider only, until federation adds one (group 4);
#   - SRP for the password step, so the password never crosses the wire, and no
#     plain USER_PASSWORD_AUTH or CUSTOM_AUTH;
#   - existence errors suppressed, so the login page does not confirm which
#     email addresses have accounts.
#
# The client secrets are in Terraform state, as the reference deployment's
# Access client secret already is — a recorded tradeoff against POA&M P3-2, in
# exchange for no secret being created by hand.
resource "aws_cognito_user_pool_client" "this" {
  for_each = local.use_cognito ? local.cognito_clients : {}

  name         = each.key
  user_pool_id = aws_cognito_user_pool.operators[0].id

  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = [each.value]

  explicit_auth_flows           = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true
}

# A client created through the API has no managed login page until a style is
# applied to it. Cognito's own look is enough.
resource "aws_cognito_managed_login_branding" "this" {
  for_each = local.use_cognito ? local.cognito_clients : {}

  user_pool_id                = aws_cognito_user_pool.operators[0].id
  client_id                   = aws_cognito_user_pool_client.this[each.key].id
  use_cognito_provided_values = true
}

# Dex reads its client secret from this Secret rather than from the Helm values,
# through the `$cognito-dex-client:clientSecret` reference in argocd.tf. ArgoCD
# resolves `$<secret>:<key>` only for Secrets labelled part-of argocd.
resource "kubernetes_secret_v1" "cognito_dex_client" {
  count = local.cognito

  metadata {
    name      = "cognito-dex-client"
    namespace = "argocd"
    labels = {
      "app.kubernetes.io/part-of" = "argocd"
    }
  }
  data = {
    clientSecret = aws_cognito_user_pool_client.this["dex"].client_secret
  }
  depends_on = [helm_release.argocd]
}

# --- Cloudflare Access against the pool (design D4) --------------------------
#
# A generic OIDC provider whose three endpoints come from the locals above.
# Cloudflare has no discovery mode, so they must be stated; deriving them is
# what keeps a hand-written path — the class of bug a provider-specific layout
# is — out of the configuration.
#
# Cloudflare reads claims only from the ID token, never from userinfo. Cognito
# puts `email` in the ID token for the `email` scope, which is the one claim
# Access needs.
resource "cloudflare_zero_trust_access_identity_provider" "cognito" {
  count = local.cognito

  account_id = var.cloudflare_account_id
  name       = "cloudpipe operators (Cognito)"
  type       = "oidc"

  config = {
    client_id     = aws_cognito_user_pool_client.this["cloudflare-access"].id
    client_secret = aws_cognito_user_pool_client.this["cloudflare-access"].client_secret
    auth_url      = local.cognito_authorize_url
    token_url     = local.cognito_token_url
    certs_url     = local.cognito_jwks_url

    scopes           = ["openid", "email", "profile"]
    email_claim_name = "email"
    pkce_enabled     = true
  }
}

# Who may enroll a device and reach the cluster: the operator emails, signed in
# through the pool. One policy for both applications in cloudflare.tf, so the
# two cannot drift.
#
# `require` binds the policy to the Cognito provider. An `email` include on its
# own would be satisfied by any login method that yields that address — the
# one-time PIN Cloudflare offers by default among them, which authenticates by
# an emailed code alone and fails 3.5.3.
resource "cloudflare_zero_trust_access_policy" "operators" {
  count = local.cognito

  account_id = var.cloudflare_account_id
  name       = "cloudpipe operators (Cognito + MFA)"
  decision   = "allow"

  include = [for email in var.operator_emails : { email = { email = email } }]

  # Never empty: operator_emails' own validation requires an address.
  require = [
    { login_method = { id = cloudflare_zero_trust_access_identity_provider.cognito[0].id } },
  ]
}

locals {
  # What the rest of the module uses, in either mode. In external mode these
  # are exactly the values the caller passed, so a deployment that supplies
  # `external_identity` renders what it rendered before this file existed.
  access_identity_provider_id = local.use_cognito ? cloudflare_zero_trust_access_identity_provider.cognito[0].id : var.external_identity.access_identity_provider_id
  access_policy_id            = local.use_cognito ? cloudflare_zero_trust_access_policy.operators[0].id : var.external_identity.access_policy_id

  # Dex's connector. In Cognito mode the secret is a reference ArgoCD resolves,
  # not the value, and the userinfo call is skipped: the ID token already
  # carries `email` and a boolean `email_verified`, so the extra round trip adds
  # nothing. Email verification is enforced, because a Cognito user's address is
  # set by the administrator who creates them.
  dex_connector = local.use_cognito ? {
    id                           = "cognito"
    name                         = "Single sign-on"
    issuer                       = local.cognito_issuer
    client_id                    = aws_cognito_user_pool_client.this["dex"].id
    client_secret                = "$cognito-dex-client:clientSecret"
    get_user_info                = false
    insecure_skip_email_verified = false
    } : {
    id                           = var.external_identity.dex_connector.id
    name                         = var.external_identity.dex_connector.name
    issuer                       = var.external_identity.dex_connector.issuer
    client_id                    = var.external_identity.dex_connector.client_id
    client_secret                = var.external_identity.dex_connector.client_secret
    get_user_info                = true
    insecure_skip_email_verified = true
  }

  # Who gets each UI's administrator role, in either identity mode (task 2.4;
  # one admin model for both since contract 2.0).
  admin_emails = var.operator_emails

  # The email domains the SSO proxies admit, derived from the operator list so a
  # deployer does not maintain a second one. Defence in depth in Cognito mode,
  # where only administrator-created users exist at all; with an external
  # provider it is what keeps the rest of an institution out.
  admin_email_domains = distinct([for email in var.operator_emails : split("@", email)[1]])

  # Where Argo Workflows, Prefect and Grafana sign in, in either mode: the pool
  # directly in Cognito mode (cognito_clients above), Dex's static clients
  # (argocd.tf) in external mode. External mode's values are exactly the ones
  # these UIs were already configured with.
  ui_oidc_issuer = local.use_cognito ? local.cognito_issuer : local.dex_issuer_url

  ui_oidc_client_ids = {
    for ui in ["argo-workflows", "prefect", "grafana"] :
    ui => local.use_cognito ? aws_cognito_user_pool_client.this[ui].id : ui
  }

  ui_oidc_client_secrets = local.use_cognito ? {
    for ui in ["argo-workflows", "prefect", "grafana"] :
    ui => aws_cognito_user_pool_client.this[ui].client_secret
    } : {
    "argo-workflows" = random_password.argo_workflows_dex_client.result
    "prefect"        = random_password.prefect_dex_client.result
    "grafana"        = random_password.grafana_dex_client.result
  }

  # Grafana's generic_oauth takes its three endpoints separately rather than
  # discovering them from the issuer.
  ui_oidc_authorize_url = local.use_cognito ? local.cognito_authorize_url : "${local.dex_issuer_url}/auth"
  ui_oidc_token_url     = local.use_cognito ? local.cognito_token_url : "${local.dex_issuer_url}/token"
  ui_oidc_userinfo_url  = local.use_cognito ? local.cognito_userinfo_url : "${local.dex_issuer_url}/userinfo"
}

output "cognito_user_pool_id" {
  description = "The operators' Cognito user pool, or null when an external identity provider is in use. Create users in it with `aws cognito-idp admin-create-user`."
  value       = one(aws_cognito_user_pool.operators[*].id)
}
