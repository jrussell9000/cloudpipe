"""Talk to `globus-connect-server`, and turn what it says into plain dicts.

Stdlib only, deliberately. This runs on the GCS instance, baked into the AMI,
where adding a dependency means rebuilding the image — so it shells out to the
`globus-connect-server` CLI rather than importing an SDK.

The CLI's `-F json` output comes back in more than one shape. A list command
returns `{"DATA_TYPE": "result#1.0.0", "data": [...]}`, a single-object command
returns the object itself, and some versions wrap that object in `data` as well.
`unwrap` collapses all three rather than letting each call site guess, because
guessing wrong yields an empty live state — which the planner would read as "none
of this exists yet" and cheerfully propose recreating a live endpoint.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Sequence
from typing import Any

GCS = "globus-connect-server"

#: Live field -> the name the configuration document uses. Only fields the
#: planner compares are mapped; everything else is ignored rather than guessed at.
GATEWAY_FIELDS = {
    "display_name": "display_name",
    "high_assurance": "high_assurance",
    "authentication_timeout_mins": "authentication_timeout_mins",
    "allowed_domains": "domains",
}

#: Create-time flags every S3 gateway here is built with, in the order the
#: appendix in `docs/globus-setup.md` records them. Constants rather than
#: declared fields because nothing in the configuration document has an opinion
#: about them and inventing one per flag would be a schema nobody asked for.
#:
#: `--no-allow-multiple-keys` is the load-bearing one: without it Globus reads
#: the first component of every transfer path as a bucket name and every
#: transfer fails with `Bucket not allowed`.
S3_GATEWAY_CREATE_FLAGS = (
    "--s3-user-credential",
    "--admin-managed-credentials",
    "--no-allow-multiple-keys",
)

#: On for every collection here, and not a per-collection choice.
COLLECTION_CREATE_FLAGS = ("--enable-https",)

#: Declared field -> (flag when true, flag when false). Visibility and sharing
#: are NOT constants: production is public and allows guest collections,
#: staging is `--private --force-encryption --no-allow-guest-collections`
#: (`retire-globus-s3-access-keys` 5.2a).
#:
#: This was a constant tuple carrying production's flags, and
#: `simplify-globus-ingress` 8.8 caught it by rebuilding the staging collection
#: from config: it came back **public and guest-enabled**, widening a collection
#: rooted at the ABCD bucket because of where the flags had been copied from.
#:
#: **An absent field takes the closed value**, which is the opposite of GCS's
#: own default — GCS creates a collection public. A declaration that forgets to
#: say should produce the collection that is easy to open later, not the one
#: that has already been advertised.
COLLECTION_POLICY_FLAGS = {
    "public": ("--public", "--private"),
    "allow_guest_collections": ("--allow-guest-collections", "--no-allow-guest-collections"),
    "force_encryption": ("--force-encryption", "--no-force-encryption"),
}

#: The value assumed for a policy field the declaration omits. `force_encryption`
#: is the one where "closed" means true.
COLLECTION_POLICY_DEFAULTS = {
    "public": False,
    "allow_guest_collections": False,
    "force_encryption": True,
}

COLLECTION_FIELDS = {
    "display_name": "display_name",
    "collection_base_path": "base_path",
    # Visibility and sharing. Declared since 8.8 and used at create time, but
    # not read back until 11.10 — so a collection flipped public after creation
    # was not drift, and the document asserted something nothing enforced.
    "public": "public",
    "allow_guest_collections": "allow_guest_collections",
    "force_encryption": "force_encryption",
}


class GcsError(RuntimeError):
    """A `globus-connect-server` call failed, with its stderr attached."""


def run(args: Sequence[str], *, runner=subprocess.run) -> dict[str, Any] | list[Any]:
    """One `globus-connect-server … -F json` call, parsed."""
    argv = [GCS, *args, "-F", "json"]
    result = runner(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise GcsError(
            f"{' '.join(argv)} exited {result.returncode}: {(result.stderr or '').strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GcsError(f"{' '.join(argv)} did not return JSON: {result.stdout[:200]!r}") from exc


def unwrap(payload: Any) -> list[dict[str, Any]]:
    """Every object in a response, whatever shape it arrived in.

    Handles `{"data": [...]}`, `{"data": {...}}`, a bare object, a bare list, and
    — the shape that actually arrives from `storage-gateway list` on GCS
    5.4.98 — a LIST whose single item is a result envelope:

        [{"DATA_TYPE": "result#1.1.0", "code": "success", "data": [ … ]}]

    Read as a bare list, that yields the envelope itself, which has no
    `display_name`, so every gateway vanishes and the planner reports the live
    endpoint's gateway as not existing. That is exactly the "guessing wrong
    yields an empty live state" failure this function exists to prevent, and it
    reached production: the first real `configure --plan-only` said
    `storage_gateway 'cloudpipe-s3-gateway': … does not exist` about a gateway
    that had been serving transfers for months (2026-09-23).

    A payload with no recognisable objects yields `[]` — but callers must not
    treat `[]` as proof of an empty endpoint unless the call succeeded, which is
    why `run` raises rather than returning empty on failure.
    """
    found: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        # An envelope is a wrapper around its `data`, never an object itself.
        # Matched on CARRYING `data` rather than on the DATA_TYPE string, so a
        # version that renames `result#1.1.0` still unwraps — and an envelope
        # whose `data` is null yields nothing rather than the envelope.
        if "data" in node:
            visit(node["data"])
            return
        found.append(node)

    visit(payload)
    return found


#: Substrings that mean "GCS is not answering yet", as opposed to "GCS answered
#: and said no". Matched on the CLI's stderr, which is the only signal available:
#: the CLI exits 1 for both cases, so the exit code cannot separate them.
#:
#: Recorded from the boot race rather than imagined — the association ran at
#: 14:11:36Z and GridFTP came up at 14:11:42Z, six seconds later, and what the
#: association recorded was `Error contacting 032fde.854f.gaccess.io … GCS
#: services are not running`.
#:
#: Kept deliberately short. Every string added here is a failure that becomes a
#: five-minute wait instead of an immediate answer, so an authorization or
#: session error must never match.
NOT_SERVING = (
    "gcs services are not running",
    "error contacting",
    "connection refused",
    "failed to establish a new connection",
)


def is_not_serving(error: BaseException) -> bool:
    """Whether this failure looks like GCS still starting, not GCS refusing."""
    text = str(error).lower()
    return any(marker in text for marker in NOT_SERVING)


def wait_until_serving(
    *,
    timeout_s: float,
    runner=subprocess.run,
    sleep=time.sleep,
    now=time.monotonic,
    log=None,
) -> None:
    """Block until the endpoint answers, or give up after `timeout_s`.

    The reconcile association is triggered at boot and wins the race against
    `cloudpipe-gcs-boot` by seconds, so its first plan is structurally a "cannot
    compare" and `doctor` check 7 reports that until someone re-runs by hand.
    Recording "cannot say" is the correct response to an unreadable endpoint (it
    is the trap task 10.5 recorded) — but at boot the endpoint is not unreadable,
    it is four seconds away.

    Opt-in via `--wait-for-gcs`, and zero by default, because this is a fix for
    ONE caller. An operator running `configure` against an endpoint that is
    genuinely down wants to be told in two seconds, not in five minutes.

    Only a not-serving failure is waited on. Anything else — an expired session,
    a rejected client — is returned immediately by raising, since no amount of
    waiting fixes it and a five-minute pause before the real message is worse
    than the message.
    """
    if timeout_s <= 0:
        return
    deadline = now() + timeout_s
    attempt = 0
    while True:
        attempt += 1
        try:
            run(["endpoint", "show"], runner=runner)
            if attempt > 1 and log:
                log(f"GCS answered on attempt {attempt}")
            return
        except GcsError as exc:
            if not is_not_serving(exc):
                raise
            remaining = deadline - now()
            if remaining <= 0:
                raise GcsError(f"GCS was still not serving after {timeout_s:g}s: {exc}") from exc
            if attempt == 1 and log:
                log(f"GCS is not serving yet; waiting up to {timeout_s:g}s")
            sleep(min(WAIT_INTERVAL_S, remaining))


#: Short enough that the six-second boot race costs one or two polls.
WAIT_INTERVAL_S = 3.0


def ensure_complete(payload: Any, what: str) -> None:
    """Refuse a listing the endpoint says is only its first page.

    The result envelope carries `has_next_page`, and this package never asked.
    Harmless while `apply` could not create — an object on page 2 read as absent
    was planned as a CREATE and then skipped — but the moment creates work, an
    unseen object is created a SECOND time. That is the one way this tool can
    duplicate live state, so it arrives with the task that implements creates.

    Refusing rather than paginating, deliberately. The REST API takes `marker`
    and `page_size` (`GET /api/storage_gateways`), but the CLI's reference for
    `storage-gateway list` and `collection list` documents no flag that exposes
    either, and this runs on the CLI. Inventing `--marker` and baking it into an
    AMI is the guess the create rule in `__main__.apply` exists to forbid; a
    wrong guess here would fail loudly at least, but a *right-looking* one that
    silently returns page 1 again would loop or duplicate.

    So: read what the endpoint reports and stop when it says there is more. This
    deployment declares two gateways and two collections, far short of any page
    size, so the guard is not expected to fire — if it ever does, the fix is a
    CLI that paginates, not a flag improvised here.
    """
    for node in _envelopes(payload):
        if node.get("has_next_page") is True:
            raise GcsError(
                f"{what} returned only the first page (has_next_page is true), and this "
                f"tool cannot request the next one. Refusing rather than planning against "
                f"a partial listing, which would create objects that already exist."
            )


def _envelopes(payload: Any) -> list[dict[str, Any]]:
    """Every dict carrying `data` — the envelopes `unwrap` looks through."""
    found: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict) and "data" in node:
            found.append(node)
            visit(node["data"])

    visit(payload)
    return found


def _project(obj: dict[str, Any], fields: dict[str, str]) -> dict[str, Any]:
    projected = {declared: obj[live] for live, declared in fields.items() if live in obj}
    # The S3 connector nests its settings under `policies`; the bucket lives
    # there as a list even though a gateway created with --bucket has one.
    policies = obj.get("policies")
    if isinstance(policies, dict):
        buckets = policies.get("s3_buckets")
        if isinstance(buckets, list) and len(buckets) == 1:
            projected["bucket"] = buckets[0]
        elif isinstance(buckets, list):
            projected["bucket"] = buckets
        # `s3_endpoint` is nested too, and is NOT top level — measured on the
        # staging gateway, which stored `https://127.0.0.1:8443` under
        # `policies` (`retire-globus-s3-access-keys` 5.2). Projected so a
        # created gateway's endpoint is readable afterwards.
        #
        # Deliberately absent from GATEWAY_MUTABLE and GATEWAY_IMMUTABLE: moving
        # a live gateway's endpoint IS the production cutover, which
        # `retire-globus-s3-access-keys` section 8 does by hand, under a canary,
        # with a rehearsed rollback. Planning it as ordinary drift would let a
        # routine reconcile re-point production's ingress.
        endpoint = policies.get("s3_endpoint")
        if isinstance(endpoint, str):
            projected["s3_endpoint"] = endpoint
    if "id" in obj:
        projected["id"] = obj["id"]
    return projected


def read_live_state(*, runner=subprocess.run) -> dict[str, Any]:
    """The endpoint as it is now, keyed by display name.

    Keyed by display name because that is how the spec says objects are matched.
    It is also the only identifier a human writes down: ids are assigned by
    Globus and would make the configuration document unwritable by hand.
    """
    gateways = {}
    gateway_payload = run(["storage-gateway", "list"], runner=runner)
    ensure_complete(gateway_payload, "storage-gateway list")
    for raw in unwrap(gateway_payload):
        projected = _project(raw, GATEWAY_FIELDS)
        name = projected.get("display_name")
        if isinstance(name, str):
            gateways[name] = projected

    gateway_names = {g["id"]: name for name, g in gateways.items() if "id" in g}

    collections = {}
    # `--include-private-policies` is REQUIRED, not a nicety: without it
    # `collection_base_path` is absent from the response entirely — measured on
    # GCS 5.4.98, where the default listing omits the key rather than nulling
    # it. It is a private policy, and private policies are opt-in.
    #
    # Without the flag the planner does not MISREAD base_path, it cannot read it
    # at all: `_unreported` degrades to "cannot be checked" rather than to
    # drift, which is safe. It is also useless, because base_path is the prefix
    # confinement — staging is rooted at /<YOUR_S3_BUCKET>/scratch/globus-staging and the
    # whole point is that it stays there. Unverifiable base_path means the one
    # field bounding a collection's reach is the one field never checked.
    collection_payload = run(["collection", "list", "--include-private-policies"], runner=runner)
    ensure_complete(collection_payload, "collection list")
    for raw in unwrap(collection_payload):
        projected = _project(raw, COLLECTION_FIELDS)
        # Resolve the gateway id back to its display name so the planner can
        # compare like with like; the document names gateways, not UUIDs.
        gateway_id = raw.get("storage_gateway_id")
        if gateway_id in gateway_names:
            projected["gateway"] = gateway_names[gateway_id]
        name = projected.get("display_name")
        if isinstance(name, str):
            collections[name] = projected

    return {
        "storage_gateways": gateways,
        "collections": collections,
        "roles": _read_roles(runner=runner),
    }


def _read_roles(*, runner=subprocess.run) -> list[dict[str, Any]]:
    """Endpoint roles, or none if this endpoint will not report them.

    Roles are the least load-bearing part of the document and the most likely to
    differ between GCS versions, so a failure here degrades to "no roles known"
    rather than failing the whole reconcile. The planner only ever *adds* roles,
    so the worst case is proposing one that already exists.
    """
    try:
        return [
            {
                "principal": raw.get("principal") or raw.get("identity_id") or "",
                "role": raw.get("role") or "",
                "collection": raw.get("collection_id") or "",
            }
            for raw in unwrap(run(["endpoint", "role", "list"], runner=runner))
        ]
    except GcsError:
        return []


def gateway_update_command(gateway_id: str, fields: dict[str, Any]) -> list[str]:
    """The `storage-gateway update s3` invocation for one planned update.

    Takes Globus's **UUID**, not the display name the rest of this package matches
    on. `storage-gateway update` rejects a name outright — "Invalid value for
    'STORAGE_GATEWAY_ID': … is not a valid UUID" — so the parameter is named for
    what it must hold; it was once named `name`, which is exactly how the display
    name came to be passed here (found against production, 2026-09-23).
    """
    argv = ["storage-gateway", "update", "s3", gateway_id]
    if "authentication_timeout_mins" in fields:
        argv += ["--authentication-timeout-mins", str(fields["authentication_timeout_mins"])]
    for domain in fields.get("domains", ()) or ():
        argv += ["--domain", str(domain)]
    return argv


def collection_update_command(collection_id: str, fields: dict[str, Any]) -> list[str]:
    """The `collection update` invocation for one planned visibility change.

    Takes Globus's UUID, like every other update. Only the fields that differ
    are emitted, so a plan reads as "this, because that" rather than re-asserting
    settings nobody changed.

    These exact flags repaired the staging collection by hand after 8.8 rebuilt
    it public (`--private --force-encryption --no-allow-guest-collections`), so
    they are a form that has run rather than one read off a manual page.
    """
    if not collection_id:
        raise GcsError("cannot update a collection without its id")
    argv = ["collection", "update", str(collection_id)]
    for field, (when_true, when_false) in sorted(COLLECTION_POLICY_FLAGS.items()):
        if field in fields:
            argv.append(when_true if fields[field] else when_false)
    if len(argv) == 3:
        raise GcsError(
            f"collection {collection_id} update was planned with no field this tool "
            f"can change: {sorted(fields)}"
        )
    return argv


def gateway_create_command(declared: dict[str, Any]) -> list[str]:
    """The `storage-gateway create s3` invocation for one declared gateway.

    Every flag here was run by hand first and recorded — the production gateway's
    in the appendix of `docs/globus-setup.md`, the staging gateway's in
    `retire-globus-s3-access-keys` 5.2, which also confirmed that GCS accepts and
    stores `--s3-endpoint` pointed at a loopback listener. That ordering is the
    rule `__main__.apply` states: no create-time flag is baked into an AMI before
    it has run once against a real endpoint.

    `--s3-endpoint` is emitted only for a gateway whose declaration carries a
    listener. A gateway that is not cut over declares `s3_listener: null`, and
    omitting the flag is what leaves it talking to AWS directly — the state
    production is in until `retire-globus-s3-access-keys` section 8.

    What this does NOT do is register a credential. `user-credentials s3-create`
    prompts for a key pair, and this package never handles credentials; a created
    gateway has none until an operator registers one, which `doctor` reports.
    """
    missing = [key for key in ("display_name", "bucket") if not declared.get(key)]
    if missing:
        raise GcsError(
            f"cannot create a storage gateway without {', '.join(missing)}: "
            f"got {declared.get('display_name') or '<unnamed>'!r}"
        )

    argv = [
        "storage-gateway",
        "create",
        "s3",
        str(declared["display_name"]),
        "--bucket",
        str(declared["bucket"]),
    ]
    for domain in declared.get("domains") or ():
        argv += ["--domain", str(domain)]

    listener = declared.get("s3_listener")
    if isinstance(listener, dict) and listener.get("endpoint"):
        argv += ["--s3-endpoint", str(listener["endpoint"])]

    argv += list(S3_GATEWAY_CREATE_FLAGS)

    if declared.get("high_assurance"):
        argv.append("--high-assurance")
    timeout = declared.get("authentication_timeout_mins")
    if timeout is not None:
        argv += ["--authentication-timeout-mins", str(timeout)]
    return argv


def collection_create_command(gateway_id: str, declared: dict[str, Any]) -> list[str]:
    """The `collection create` invocation for one declared collection.

    Takes the gateway's UUID, like every other `create`/`update` subcommand, so
    the caller has to resolve the name first — including the id of a gateway
    created moments earlier in the same apply.

    `base_path` is positional and is the confinement: the S3 connector reads the
    first component of a path as the bucket, so the collection is rooted at
    `/<bucket>` (production) or `/<bucket>/<prefix>` (staging). Rooting it at `/`
    makes every transfer path's first component a bucket name and fails every
    transfer with `Bucket not allowed`.

    Visibility and sharing come from the DECLARATION, not from constants here.
    They were constants once, carrying production's flags, and rebuilding the
    staging collection from config then returned it public and guest-enabled
    (`simplify-globus-ingress` 8.8). An omitted field takes the closed value, so
    a declaration that forgets to say produces the collection that can still be
    opened rather than one already advertised.
    """
    for key in ("display_name", "base_path"):
        if not declared.get(key):
            raise GcsError(
                f"cannot create a collection without {key}: "
                f"got {declared.get('display_name') or '<unnamed>'!r}"
            )
    if not gateway_id:
        raise GcsError(
            f"cannot create collection {declared['display_name']!r} without its gateway's id"
        )
    argv = [
        "collection",
        "create",
        str(gateway_id),
        str(declared["base_path"]),
        str(declared["display_name"]),
        *COLLECTION_CREATE_FLAGS,
    ]
    for field, (when_true, when_false) in sorted(COLLECTION_POLICY_FLAGS.items()):
        value = declared.get(field, COLLECTION_POLICY_DEFAULTS[field])
        argv.append(when_true if value else when_false)
    return argv


def created_id(payload: Any) -> str | None:
    """Globus's id for the object a `create` just returned, or None.

    The two creates return different shapes — `storage-gateway create` wraps its
    object in `{"data": [...]}` while `collection create` returns the object
    itself — which is why this goes through `unwrap` rather than reading
    `["data"][0]["id"]`, the form that raises `KeyError: 'data'` on a collection
    (recorded in `docs/globus-setup.md`).

    None rather than an exception when no id comes back: the object may well have
    been created, and the caller decides whether it needs the id. The collection
    create does need it, and says so.
    """
    for obj in unwrap(payload):
        value = obj.get("id")
        if isinstance(value, str) and value:
            return value
    return None
