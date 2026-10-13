"""What the stack needs from a Cloudflare API token, in one place.

Three consumers read this: `cloudpipe setup`'s closing message, which prints the
link a deployer creates the token from; `cloudpipe preflight`, which probes each
permission; and `tests/test_setup_wizard_cloudflare.py`, which holds the list
equal to the resources `terraform/modules/stack/cloudflare.tf` declares. Before
this module the knowledge was one sentence of prose in two places, and both named
a permission level the dashboard does not offer.

**Why a link and not Terraform.** The pinned provider can create a token
(`cloudflare_account_token`), but only when authenticated with a token that holds
"API Tokens: Edit" — a credential that can mint any other. That swaps a narrow
hand-made token for a broad one and removes no manual step. A dashboard template
URL removes the step that actually goes wrong: choosing permissions. The deployer
opens it, reviews four pre-selected rows, and creates the token.

**Where the keys come from.** `access`, `access_acct` and `teams` are in
Cloudflare's template-URL reference
(developers.cloudflare.com/fundamentals/api/how-to/account-owned-token-template/).
`argotunnel`, for Cloudflare Tunnel, is not in that table; it is the key two
independent public projects use for the same group. A wrong key is silently
dropped from the form rather than rejected, which is why preflight probes all
four rather than trusting the link: a token created from a stale link fails
preflight naming the missing group.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass


@dataclass(frozen=True)
class Permission:
    """One permission group the stack's Cloudflare resources need."""

    #: The dashboard template key.
    key: str
    #: The group's name exactly as the dashboard's dropdown shows it.
    name: str
    #: The level to grant. The `Access: *` and Tunnel groups say Write in the
    #: dashboard and `Zero Trust` says Edit; the template encoding calls both
    #: "edit", which is what is stored here.
    level: str
    #: A read-only account endpoint that only this group grants, for preflight.
    probe: str
    #: Which resources in `terraform/modules/stack/cloudflare.tf` need it.
    needed_by: str

    @property
    def dashboard_level(self) -> str:
        """The level as the dashboard labels it, which differs between groups."""
        return "Edit" if self.key == "teams" else "Write"


PERMISSIONS = (
    Permission(
        key="access_acct",
        name="Access: Organizations, Identity Providers, and Groups",
        level="edit",
        probe="/access/identity_providers",
        needed_by="cloudflare_zero_trust_organization",
    ),
    Permission(
        key="access",
        name="Access: Apps and Policies",
        level="edit",
        probe="/access/apps",
        needed_by="cloudflare_zero_trust_access_application (both)",
    ),
    Permission(
        key="argotunnel",
        name="Cloudflare Tunnel",
        level="edit",
        probe="/cfd_tunnel",
        needed_by="cloudflare_zero_trust_tunnel_cloudflared and its route",
    ),
    Permission(
        key="teams",
        name="Zero Trust",
        level="edit",
        probe="/devices/settings",
        needed_by="cloudflare_zero_trust_device_default_profile and _device_settings",
    ),
)

TOKEN_NAME = "CloudPipe deployment"

_TEMPLATE_BASE = "https://dash.cloudflare.com/profile/api-tokens"


def token_template_url(account_id: str | None) -> str:
    """The dashboard link that opens token creation with every permission selected.

    A user token, scoped to `account_id`. A user token rather than an
    account-owned one because the account template URL takes no account id —
    its scope is chosen in the form — and the wrong account is the mistake this
    link exists to rule out. `zoneId=all` is required by the template format and
    grants nothing here: the token carries no zone-scoped permission.

    With no account id yet, the scope is left for the deployer to choose, which
    is the honest fallback rather than a guess.
    """
    keys = [{"key": permission.key, "type": permission.level} for permission in PERMISSIONS]
    query = urllib.parse.urlencode(
        {
            "permissionGroupKeys": json.dumps(keys, separators=(",", ":")),
            "accountId": account_id or "*",
            "zoneId": "all",
            "name": TOKEN_NAME,
        },
        quote_via=urllib.parse.quote,
    )
    return f"{_TEMPLATE_BASE}?{query}"


def permission_rows() -> str:
    """The four rows as a deployer would enter them by hand, for a message."""
    return "; ".join(
        f"Account > {permission.name} > {permission.dashboard_level}" for permission in PERMISSIONS
    )
