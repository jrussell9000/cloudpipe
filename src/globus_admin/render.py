"""Answers in, Terraform inputs and a configuration document out.

Two artifacts, and they are not the same kind of thing.

`globus.auto.tfvars` is what Terraform reads. It is written as its own file
rather than into `terraform.tfvars` because that file belongs to the whole
cluster — the domain, the GitHub repository, the log bucket — and rewriting it
would mean parsing and preserving values this tool knows nothing about.
Terraform loads every `*.auto.tfvars`, so a separate file needs no merge and no
`include` mechanism. Cluster-wide variables (`name`, `region`) are deliberately
NOT written here: they are needed long before Globus, and silently overriding
them from an answers document is not a favour to anyone.

The GCS configuration document is a **preview**. Terraform renders the real one
into SSM from `local.globus_config`, and that stays authoritative — rendering it
here as well exists so an operator can read what their answers imply before
applying, and diff it against what is live. The duplication is the obvious risk,
so `tests/test_terraform_globus_config.py` renders the real module with
`terraform console` and asserts the two agree.

Everything here is a pure function of the answers: same input, byte-identical
output, no clock and no environment. That is what makes "run it twice and
compare" a usable check rather than a promise.
"""

from __future__ import annotations

import json
from typing import Any

from .config import DEFAULT_SESSION_TIMEOUT_DAYS, DEFAULT_STAGING_PREFIX

#: The loopback listener ports and production's cutover state, restated from
#: `local.s3_gateways` in `terraform/modules/globus/s3_gateway_roles.tf`. They are
#: not answers — a deployment has no reason to choose them — so they are held
#: equal to the module by the same render comparison as everything else here.
PRODUCTION_LISTENER_PORT = 8444
STAGING_LISTENER_PORT = 8443
#: Flipped with the Terraform `cut_over` in `retire-globus-s3-access-keys` 8.3.
PRODUCTION_CUT_OVER = False

#: Terraform variables this tool owns, in the order they are written. Order is
#: fixed rather than sorted so a diff between two renders shows what changed
#: rather than everything moving.
TFVARS_ORDER = (
    "globus_s3_destination_bucket",
    "globus_collection_name",
    "globus_gateway_name",
    "globus_org_name",
    "globus_contact_email",
    "globus_owner_email",
    "globus_identity_domain",
    "globus_client_id",
    "globus_source_collection_id",
    "globus_source_base_path",
    "globus_admin_prefix_list_id",
    "globus_session_timeout_days",
    "globus_production_managed",
    "globus_staging_enabled",
    "globus_iam_user_name",
    "globus_staging_iam_user_name",
)

HEADER = """\
# Rendered by `pixi run globus init` from the answers document.
#
# Do not edit by hand: re-running init overwrites this file, and an edit here
# would be silently lost. Change the answers document instead.
#
# Terraform loads every *.auto.tfvars file, so these sit alongside
# terraform.tfvars rather than inside it — that file holds cluster-wide values
# this tool does not own. `name` and `region` are cluster-wide too and are NOT
# set here; set them in terraform.tfvars.
"""


def tfvars(answers: dict[str, Any]) -> str:
    """`globus.auto.tfvars`, as text."""
    values = _terraform_values(answers)
    lines = [HEADER]
    for name in TFVARS_ORDER:
        lines.append(f"{name} = {_hcl(values[name])}")
    return "\n".join(lines) + "\n"


def gcs_config(answers: dict[str, Any]) -> dict[str, Any]:
    """The configuration document these answers imply.

    Mirrors `local.globus_config` in `terraform/modules/globus/main.tf`, field for
    field. When that changes, this changes, and the cross-check test fails until
    it does.
    """
    collection = _collection_name(answers)
    gateway = _gateway_name(answers)
    bucket = str(answers["bucket"])
    timeout = _timeout_days(answers) * 24 * 60
    managed = _production_managed(answers)
    staging = _staging_enabled(answers)

    gateways = [
        {
            "display_name": gateway,
            "type": "s3",
            "bucket": bucket,
            "domains": list(_domains(answers)),
            "high_assurance": True,
            "authentication_timeout_mins": timeout,
            "credential_secret": f"globus/s3-gateway/{collection}",
            # Not cut over yet — see `PRODUCTION_CUT_OVER`.
            "s3_listener": _s3_listener(answers, "production", PRODUCTION_LISTENER_PORT)
            if PRODUCTION_CUT_OVER
            else None,
            "managed": managed,
        }
    ]
    collections = [
        {
            "display_name": collection,
            "gateway": gateway,
            "base_path": f"/{bucket}",
            "managed": managed,
            # Visibility is declared, not left to create-time flags — see the
            # comment on the same block in `terraform/modules/globus/main.tf`,
            # which this renderer is held byte-equal to by
            # `tests/test_terraform_globus_config.py`.
            "public": True,
            "allow_guest_collections": True,
            # ON in live production, read off the 1.1 snapshot. Declared `False`
            # at first from the setup appendix, which does not pass the flag —
            # see the note in `terraform/modules/globus/main.tf`.
            "force_encryption": True,
        }
    ]

    if staging:
        gateways.append(
            {
                "display_name": f"{collection}-staging",
                "type": "s3",
                "bucket": bucket,
                "domains": list(_domains(answers)),
                "high_assurance": True,
                "authentication_timeout_mins": timeout,
                "credential_secret": f"globus/s3-gateway/{collection}-staging",
                "s3_listener": _s3_listener(answers, "staging", STAGING_LISTENER_PORT),
                "managed": True,
            }
        )
        prefix = str(answers.get("staging_prefix") or DEFAULT_STAGING_PREFIX).strip("/")
        collections.append(
            {
                "display_name": f"{collection}-staging",
                "gateway": f"{collection}-staging",
                "base_path": f"/{bucket}/{prefix}",
                "managed": True,
                # Closed on every axis: nothing browses to staging and nothing
                # shares from it. 8.8 rebuilt it from config and got a PUBLIC
                # collection back, which is why these are declared at all.
                "public": False,
                "allow_guest_collections": False,
                "force_encryption": True,
            }
        )

    return {"storage_gateways": gateways, "collections": collections, "roles": []}


def gcs_config_json(answers: dict[str, Any]) -> str:
    """The document as text, keys sorted — the same shape Terraform's `jsonencode` emits."""
    return json.dumps(gcs_config(answers), indent=2, sort_keys=True) + "\n"


def _terraform_values(answers: dict[str, Any]) -> dict[str, Any]:
    return {
        "globus_s3_destination_bucket": str(answers["bucket"]),
        "globus_collection_name": _collection_name(answers),
        # Rendered as given, empty included: empty means "derive it", and the
        # module derives the same way this renderer does.
        "globus_gateway_name": str(answers.get("gateway_name") or "").strip(),
        "globus_org_name": _org_name(answers),
        "globus_contact_email": str(answers["contact_email"]),
        "globus_owner_email": str(answers["owner_email"]),
        # One domain: the Terraform variable is singular, while the answers allow
        # several. The first is used and `init` says so rather than dropping the
        # rest silently.
        "globus_identity_domain": _domains(answers)[0],
        "globus_client_id": str(answers["service_client_id"]),
        "globus_source_collection_id": str(answers["source_collection_id"]),
        "globus_source_base_path": str(answers["source_base_path"]),
        "globus_admin_prefix_list_id": str(answers.get("admin_prefix_list_id") or ""),
        "globus_session_timeout_days": _timeout_days(answers),
        # Rendered from the answers, and always rendered: this file loads after
        # terraform.tfvars, so a value set there would be silently overridden.
        "globus_production_managed": _production_managed(answers),
        "globus_staging_enabled": _staging_enabled(answers),
        # Empty unless an answers document names one. There is no default: an
        # organization SCP can deny `iam:CreateUser`, and the keyless staging
        # design does not need a user at all.
        "globus_iam_user_name": str(answers.get("iam_user_name") or "").strip(),
        "globus_staging_iam_user_name": str(answers.get("staging_iam_user_name") or ""),
    }


def _s3_listener(answers: dict[str, Any], slug: str, port: int) -> dict[str, str]:
    """A gateway's listener, as `local.s3_listener_declarations` renders it.

    The role name uses the deployment name where Terraform uses `var.name`. The two
    are the same value by convention rather than by construction — `name` is set in
    terraform.tfvars, not rendered from the answers — so a deployment that broke
    the convention would see a preview naming the wrong role, and `doctor` check
    12 reporting it absent.
    """
    return {
        "writer_role": f"{answers['deployment_name']}-globus-{slug}-writer",
        "endpoint": f"https://127.0.0.1:{port}",
    }


def _collection_name(answers: dict[str, Any]) -> str:
    """The production gateway and collection's display name.

    Derived from the deployment name, which is why it is not a question. An
    override exists for a deployment that has already named one.
    """
    override = str(answers.get("collection_name") or "").strip()
    return override or f"{answers['deployment_name']}-s3"


def _org_name(answers: dict[str, Any]) -> str:
    """The organization name shown on the endpoint's public listing.

    An optional override over a derived default, the same shape as
    `collection_name`, rather than a question. That distinction is what keeps the
    minimal input set intact: the spec caps what may be *required*, and an
    override a wizard never prompts for does not count against it.

    The override exists because deriving it does not reproduce a real deployment
    — re-deriving this one's answers from live state (task 8.13) matched 13 of
    14 Terraform inputs, and this was the miss. A derived-only value would have
    meant quietly renaming the organization on a public listing the first time
    anyone ran `init`.
    """
    override = str(answers.get("org_name") or "").strip()
    return override or str(answers["deployment_name"])


def _domains(answers: dict[str, Any]) -> tuple[str, ...]:
    plural = answers.get("identity_domains")
    if isinstance(plural, list) and plural:
        return tuple(str(d) for d in plural)
    single = answers.get("identity_domain")
    return (str(single),) if single else ()


def _timeout_days(answers: dict[str, Any]) -> int:
    return int(answers.get("session_timeout_days") or DEFAULT_SESSION_TIMEOUT_DAYS)


def _staging_enabled(answers: dict[str, Any]) -> bool:
    """Opt-in. A fresh deployment gets a staging gateway only if it asks.

    Staging and read-only production exist for one situation: a production
    endpoint configured by hand before this tool, which the tool must be proved
    against before it may change anything. A fresh deployment is built by the
    tool from nothing, so it has neither need — hence staging off and production
    managed (`_production_managed`) unless the answers say otherwise.

    Both defaults deliberately disagree with the Terraform variables'
    (`globus_staging_enabled = true`, `globus_production_managed = false`). Those
    describe the deployment this repository runs, which is exactly that
    hand-built case and applies Terraform on its variable defaults; flipping them
    would change it on the next apply. A fresh deployment never sees them,
    because `init` always renders both values.
    """
    return bool(answers.get("staging_enabled", False))


def _gateway_name(answers: dict[str, Any]) -> str:
    """The production gateway's display name — the collection's unless overridden.

    Kept separate from the collection's because a gateway created before this
    tool need not follow the derivation, and the reconcile matches by display
    name: a derived-but-wrong name declares a gateway that does not exist and
    leaves the real one unmanaged. See `environments.resolve`.
    """
    return str(answers.get("gateway_name") or "").strip() or _collection_name(answers)


def _production_managed(answers: dict[str, Any]) -> bool:
    """On unless the answers hold production read-only. See `_staging_enabled`."""
    return bool(answers.get("production_managed", True))


def _hcl(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))
