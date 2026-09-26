"""The GCS configuration document: what a valid declaration looks like.

Terraform renders this document into `/<prefix>/globus/config` (`jsonencode` of
the gateway and collection declarations), the on-instance reconcile reads it, and
`doctor` checks it. All three need the same idea of "valid", so the rules live
here rather than in any one of them.

Written as a function rather than a JSON Schema file for one reason: the rule
that actually catches mistakes is *referential* — a collection naming a gateway
that is not declared — and a schema cannot express that. The same pass also
enforces the field-level rules, so there is one place to look.

Errors are returned, not raised: a checklist wants every problem at once, not the
first one.

`schemas/gcs-config.schema.json` publishes the same structure for consumers that
cannot import this module. The two are held together by
`tests/globus/test_configdoc_schema.py`, with one deliberate asymmetry: the
schema sets `additionalProperties: false`, and this module ignores unknown keys.
That is the safe direction. An editor or CI job gets told about a typo, while an
instance running an older copy of this code keeps working against a document a
newer Terraform rendered with a field it has never heard of — a reconcile that
refused to parse would take ingress down to report a field it did not need.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GATEWAY_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "display_name": str,
    "type": str,
    "bucket": str,
    "domains": list,
    "high_assurance": bool,
    "authentication_timeout_mins": int,
    "credential_secret": str,
}

GATEWAY_OPTIONAL: dict[str, type | tuple[type, ...]] = {
    # The loopback signing listener this gateway is registered against: the role
    # it signs as and the endpoint Globus sends to. Null (or absent) for a gateway
    # not yet cut over, which still signs with a static key — a state `doctor`
    # reports, not an invalid document, since one that refused to validate would
    # take the rest of the checklist down with it. Replaced `iam_user`
    # (`retire-globus-s3-access-keys` 4.4); an older document still carrying that
    # key validates, because unknown keys are ignored here.
    "s3_listener": (dict, type(None)),
    # The Globus identity the S3 credential is registered for. Optional for the
    # same reason: the gateway can exist before anyone has registered a key.
    "credential_identity": str,
    # `false` means the reconcile reports drift but never applies it — how
    # production is declared read-only during a rollout.
    "managed": bool,
}

COLLECTION_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "display_name": str,
    "gateway": str,
    "base_path": str,
}

COLLECTION_OPTIONAL: dict[str, type | tuple[type, ...]] = {
    "managed": bool,
    # Create-time visibility and sharing, declared because the two collections
    # genuinely differ and the reconcile cannot guess which it is building.
    #
    # Added after `simplify-globus-ingress` 8.8 rebuilt the staging collection
    # and it came back **public and guest-enabled**: the create flags were
    # constants copied from production's setup appendix, while staging was
    # created `--private --force-encryption --no-allow-guest-collections`
    # (`retire-globus-s3-access-keys` 5.2a). A collection rooted at the ABCD
    # bucket must not be publicly listed because of where its flags were copied
    # from.
    #
    # Absent means the restrictive value, not GCS's default. GCS defaults a
    # collection to public; a declaration that forgets to say should produce the
    # closed collection, because that is the direction a mistake can be undone
    # from.
    "public": bool,
    "allow_guest_collections": bool,
    "force_encryption": bool,
}

LISTENER_REQUIRED: dict[str, type | tuple[type, ...]] = {
    # An IAM role NAME, not an ARN: the answers document cannot know the account
    # id, and `render.gcs_config` must produce the same document Terraform does.
    "writer_role": str,
    "endpoint": str,
}

ROLE_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "principal": str,
    "role": str,
}

ROLE_OPTIONAL: dict[str, type | tuple[type, ...]] = {
    # Absent means the role is granted on the endpoint itself rather than on a
    # collection. Present, it must name a declared collection — referential, so
    # only this module can check it.
    "collection": str,
}

SUPPORTED_GATEWAY_TYPES = ("s3",)

#: The published structural contract. It states shapes, not relationships: the
#: referential rules below (a collection naming an undeclared gateway, a role
#: scoped to an undeclared collection) cannot be written in JSON Schema, which is
#: why this module rather than that file is authoritative.
SCHEMA_PATH = Path(__file__).with_name("schemas") / "gcs-config.schema.json"


def schema() -> dict[str, Any]:
    """The published JSON Schema, as a dict.

    Used by consumers that want the structural contract without importing this
    module's logic, and by the test that holds the two descriptions together.
    """
    return json.loads(SCHEMA_PATH.read_text())


def validate(document: Any) -> list[str]:
    """Every problem with `document`, as sentences an operator can act on."""
    if not isinstance(document, dict):
        return [f"the configuration must be a JSON object, not {type(document).__name__}"]

    problems: list[str] = []
    gateways = document.get("storage_gateways")
    if not isinstance(gateways, list) or not gateways:
        problems.append("`storage_gateways` must be a non-empty list")
        gateways = []

    declared, gateway_problems = _validate_gateways(gateways)
    problems.extend(gateway_problems)

    collections = document.get("collections")
    if not isinstance(collections, list):
        problems.append("`collections` must be a list")
        collections = []

    collection_problems, declared_collections = _validate_collections(collections, declared)
    problems.extend(collection_problems)

    roles = document.get("roles", [])
    if not isinstance(roles, list):
        problems.append("`roles` must be a list")
        roles = []
    problems.extend(_validate_roles(roles, declared_collections))
    return problems


def _validate_gateways(gateways: list[Any]) -> tuple[set[str], list[str]]:
    problems: list[str] = []
    declared: set[str] = set()

    for index, gateway in enumerate(gateways):
        where = f"storage_gateways[{index}]"
        if not isinstance(gateway, dict):
            problems.append(f"{where} must be an object")
            continue
        name = gateway.get("display_name")
        if isinstance(name, str) and name:
            if name in declared:
                problems.append(f"{where}: two gateways are both named {name!r}")
            declared.add(name)
        problems.extend(_field_problems(where, gateway, GATEWAY_REQUIRED))
        problems.extend(_optional_field_problems(where, gateway, GATEWAY_OPTIONAL))

        kind = gateway.get("type")
        if isinstance(kind, str) and kind and kind not in SUPPORTED_GATEWAY_TYPES:
            problems.append(
                f"{where}: type {kind!r} is not supported; expected one of "
                f"{', '.join(SUPPORTED_GATEWAY_TYPES)}"
            )
        timeout = gateway.get("authentication_timeout_mins")
        # `bool` is an `int` in Python, so a stray `true` would otherwise pass.
        if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout <= 0:
            problems.append(f"{where}: authentication_timeout_mins must be positive")
        domains = gateway.get("domains")
        if isinstance(domains, list) and not domains:
            problems.append(
                f"{where}: domains is empty, so no identity could ever use this gateway"
            )
        listener = gateway.get("s3_listener")
        if isinstance(listener, dict):
            problems.extend(_listener_problems(f"{where}.s3_listener", listener))

    return declared, problems


def _listener_problems(where: str, listener: dict[str, Any]) -> list[str]:
    """A declared listener must be complete, and must be HTTPS.

    Half a listener is worse than none: a role with no endpoint cannot be probed,
    and an endpoint with no role cannot be checked for confinement, so either
    half alone would pass as "cut over" while the check that matters has nothing
    to verify. HTTPS because the design keeps TLS on loopback (D5); plain HTTP
    here would mean a gateway registered against a path nobody has tested.
    """
    problems = _field_problems(where, listener, LISTENER_REQUIRED)
    endpoint = listener.get("endpoint")
    if isinstance(endpoint, str) and endpoint.strip() and not endpoint.startswith("https://"):
        problems.append(f"{where}: endpoint must be https:// (got {endpoint!r})")
    return problems


def _validate_collections(collections: list[Any], declared: set[str]) -> tuple[list[str], set[str]]:
    """Problems, plus the collection names roles are allowed to reference."""
    problems: list[str] = []
    names: set[str] = set()

    for index, collection in enumerate(collections):
        where = f"collections[{index}]"
        if not isinstance(collection, dict):
            problems.append(f"{where} must be an object")
            continue
        name = collection.get("display_name")
        if isinstance(name, str) and name:
            if name in names:
                problems.append(f"{where}: two collections are both named {name!r}")
            names.add(name)
        problems.extend(_field_problems(where, collection, COLLECTION_REQUIRED))
        problems.extend(_optional_field_problems(where, collection, COLLECTION_OPTIONAL))
        gateway_name = collection.get("gateway")
        if isinstance(gateway_name, str) and gateway_name and gateway_name not in declared:
            problems.append(
                f"{where}: names gateway {gateway_name!r}, which is not declared in "
                "storage_gateways"
            )
        base_path = collection.get("base_path")
        if isinstance(base_path, str) and base_path and not base_path.startswith("/"):
            problems.append(f"{where}: base_path must start with / (got {base_path!r})")

    return problems, names


def _validate_roles(roles: list[Any], collections: set[str]) -> list[str]:
    """Role assignments, and the one relationship they can get wrong.

    A role naming no collection is an endpoint role, which is valid and common.
    A role naming a collection that is not declared is the referential mistake
    this function exists for — the reconcile would have nothing to grant it on.
    """
    problems: list[str] = []

    for index, role in enumerate(roles):
        where = f"roles[{index}]"
        if not isinstance(role, dict):
            problems.append(f"{where} must be an object")
            continue
        problems.extend(_field_problems(where, role, ROLE_REQUIRED))
        problems.extend(_optional_field_problems(where, role, ROLE_OPTIONAL))
        collection = role.get("collection")
        if isinstance(collection, str) and collection and collection not in collections:
            problems.append(
                f"{where}: scoped to collection {collection!r}, which is not declared in "
                "collections"
            )

    return problems


def summarize(document: Any) -> dict[str, Any]:
    """A short, safe description for a report: names and timeouts, no credentials."""
    if not isinstance(document, dict):
        return {}
    gateways = document.get("storage_gateways")
    collections = document.get("collections")
    return {
        "gateways": [
            {
                "display_name": g.get("display_name"),
                "high_assurance": g.get("high_assurance"),
                "authentication_timeout_mins": g.get("authentication_timeout_mins"),
                "managed": g.get("managed", True),
                # A role name, not a credential; `None` until the gateway is cut over.
                "writer_role": (
                    g["s3_listener"].get("writer_role")
                    if isinstance(g.get("s3_listener"), dict)
                    else None
                ),
            }
            for g in gateways
            if isinstance(g, dict)
        ]
        if isinstance(gateways, list)
        else [],
        "collections": [
            {"display_name": c.get("display_name"), "gateway": c.get("gateway")}
            for c in collections
            if isinstance(c, dict)
        ]
        if isinstance(collections, list)
        else [],
    }


def _field_problems(
    where: str, obj: dict[str, Any], required: dict[str, type | tuple[type, ...]]
) -> list[str]:
    problems = []
    for key, expected in required.items():
        if key not in obj:
            problems.append(f"{where}: missing {key}")
            continue
        value = obj[key]
        # A declared `high_assurance: 1` is a Terraform mistake worth naming.
        wrong_type = not isinstance(value, expected) or (
            expected is int and isinstance(value, bool)
        )
        if wrong_type:
            problems.append(
                f"{where}: {key} must be {_type_name(expected)}, not {type(value).__name__}"
            )
        elif isinstance(value, str) and not value.strip():
            problems.append(f"{where}: {key} is empty")
    return problems


def _optional_field_problems(
    where: str, obj: dict[str, Any], optional: dict[str, type | tuple[type, ...]]
) -> list[str]:
    """Type-check optional fields that are present, and only that.

    Absent is fine, and so is null where the type allows it: Terraform renders
    `s3_listener: null` for a gateway not yet cut over, which is the state that
    field exists to express.
    """
    problems = []
    for key, expected in optional.items():
        if key not in obj:
            continue
        value = obj[key]
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            problems.append(
                f"{where}: {key} must be {_type_name(expected)}, not {type(value).__name__}"
            )
    return problems


def _type_name(expected: type | tuple[type, ...]) -> str:
    names = {
        bool: "true or false",
        int: "a whole number",
        str: "text",
        list: "a list",
        dict: "an object",
        type(None): "null",
    }
    if isinstance(expected, tuple):
        return " or ".join(names.get(t, t.__name__) for t in expected)
    return names.get(expected, expected.__name__)
