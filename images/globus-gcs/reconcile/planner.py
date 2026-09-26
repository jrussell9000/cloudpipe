"""Compare the declared configuration with live state and decide what to do.

The planner is pure: it takes two dicts of data and returns a plan. Nothing here
runs a command, opens a socket, or reads a file, which is what makes the rules
below testable against recorded fixtures rather than against a live endpoint.

Three rules constrain every decision, and all three exist because this code runs
against an endpoint that is already serving production transfers:

1. **It never deletes.** Not gateways, not collections, not roles, not
   credentials. A reconcile that can delete can lose data that is not backed up
   anywhere, because the bucket is the backup.
2. **It never touches what it was not told about.** Objects are matched by
   display name; anything live whose name is absent from the declaration is
   reported as unmanaged and left exactly alone.
3. **It refuses rather than recreates.** When a field Globus cannot update
   differs, the answer is an error naming the field — never a delete-and-create,
   which is rule 1 wearing a disguise.

`managed: false` is the fourth control, and it is different in kind: the object
IS declared, drift IS reported, and apply simply does not act. It is how
production is held read-only while the same code is proved on staging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CREATE = "create"
UPDATE = "update"

GATEWAY = "storage_gateway"
COLLECTION = "collection"
ROLE = "role"

#: Fields Globus cannot change on an existing storage gateway. A difference here
#: is an error, because the only way to "fix" it is to delete and recreate, and
#: this code does not delete. `high_assurance` is the one the spec names; the
#: bucket is included because repointing a live gateway at a different bucket
#: silently changes where every future transfer lands.
GATEWAY_IMMUTABLE = ("high_assurance", "bucket")

#: Updatable in place with `storage-gateway update s3`.
GATEWAY_MUTABLE = ("authentication_timeout_mins", "domains")

#: A collection's gateway and base path define what it *is*. Changing either
#: would move where data is read and written, so they are create-time only.
COLLECTION_IMMUTABLE = ("gateway", "base_path")

#: Visibility and sharing, updatable in place with `collection update`.
#:
#: Compared since 11.10. They were declared from 8.8 and used when creating a
#: collection, but read back by nothing — so a collection flipped public after
#: creation was not reported as drift, and the document asserted a property it
#: did not enforce. Declared-but-unenforced is worse than undeclared: it reads
#: like a control and is not one.
COLLECTION_MUTABLE = ("public", "allow_guest_collections", "force_encryption")


@dataclass(frozen=True)
class Action:
    """One thing to do, and the reason it is needed.

    `fields` carries only what differs, so a plan reads as "this, because that"
    rather than as a full object dump an operator has to diff by eye.
    """

    kind: str
    object_type: str
    name: str
    fields: dict[str, tuple[Any, Any]] = field(default_factory=dict)

    def describe(self) -> str:
        if self.kind == CREATE:
            return f"create {self.object_type} {self.name!r}"
        changes = ", ".join(
            f"{key}: {live!r} -> {declared!r}"
            for key, (live, declared) in sorted(self.fields.items())
        )
        return f"update {self.object_type} {self.name!r} ({changes})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "object_type": self.object_type,
            "name": self.name,
            "fields": {k: {"live": v[0], "declared": v[1]} for k, v in self.fields.items()},
        }


@dataclass(frozen=True)
class Problem:
    """A difference that cannot be reconciled without deleting something."""

    object_type: str
    name: str
    field_name: str
    live: Any
    declared: Any

    def describe(self) -> str:
        return (
            f"{self.object_type} {self.name!r}: {self.field_name} is {self.live!r} live but "
            f"{self.declared!r} in the configuration, and Globus cannot change it. "
            f"Reconciling would mean deleting and recreating the object, which this tool "
            f"never does."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "object_type": self.object_type,
            "name": self.name,
            "field": self.field_name,
            "live": self.live,
            "declared": self.declared,
        }


@dataclass(frozen=True)
class Note:
    """Something observed and deliberately not acted on."""

    object_type: str
    name: str
    reason: str

    def describe(self) -> str:
        return f"{self.object_type} {self.name!r}: {self.reason}"

    def as_dict(self) -> dict[str, Any]:
        return {"object_type": self.object_type, "name": self.name, "reason": self.reason}


@dataclass(frozen=True)
class Plan:
    actions: tuple[Action, ...] = ()
    problems: tuple[Problem, ...] = ()
    notes: tuple[Note, ...] = ()
    live_ids: dict[str, dict[str, str]] = field(default_factory=dict)
    """Globus's ids for the DECLARED objects that already exist, by type then name.

    Not a decision, and so not in `render`: it is how the operator's side learns
    an id it must record — the collection id every other tool reads from SSM —
    without a second remote run. Undeclared live objects are left out, for the
    same reason the planner never acts on them."""

    @property
    def empty(self) -> bool:
        """No work to do. The second run of an unchanged configuration is empty."""
        return not self.actions

    @property
    def blocked(self) -> bool:
        """Something needs a human. `apply` must refuse while this is true."""
        return bool(self.problems)

    def as_dict(self) -> dict[str, Any]:
        return {
            "actions": [a.as_dict() for a in self.actions],
            "problems": [p.as_dict() for p in self.problems],
            "notes": [n.as_dict() for n in self.notes],
            "live_ids": {kind: dict(ids) for kind, ids in self.live_ids.items()},
        }

    def render(self) -> str:
        lines: list[str] = []
        for problem in self.problems:
            lines.append(f"ERROR  {problem.describe()}")
        for action in self.actions:
            lines.append(f"PLAN   {action.describe()}")
        for note in self.notes:
            lines.append(f"NOTE   {note.describe()}")
        if not lines:
            lines.append("No changes. The endpoint matches the configuration.")
        return "\n".join(lines)


def plan(document: dict[str, Any], live: dict[str, Any]) -> Plan:
    """What would have to happen for `live` to match `document`.

    `live` is the normalised view from `gcs.read_live_state`: display-name-keyed
    dicts of gateways, collections and roles.
    """
    actions: list[Action] = []
    problems: list[Problem] = []
    notes: list[Note] = []

    live_gateways = live.get("storage_gateways", {})
    live_collections = live.get("collections", {})
    live_roles = live.get("roles", ())

    declared_gateways = _by_name(document.get("storage_gateways") or [])
    declared_collections = _by_name(document.get("collections") or [])

    for name, declared in declared_gateways.items():
        _plan_object(
            object_type=GATEWAY,
            name=name,
            declared=declared,
            live=live_gateways.get(name),
            immutable=GATEWAY_IMMUTABLE,
            mutable=GATEWAY_MUTABLE,
            actions=actions,
            problems=problems,
            notes=notes,
        )

    for name, declared in declared_collections.items():
        _plan_object(
            object_type=COLLECTION,
            name=name,
            declared=declared,
            live=live_collections.get(name),
            immutable=COLLECTION_IMMUTABLE,
            mutable=COLLECTION_MUTABLE,
            actions=actions,
            problems=problems,
            notes=notes,
        )

    notes.extend(_unmanaged(GATEWAY, live_gateways, declared_gateways))
    notes.extend(_unmanaged(COLLECTION, live_collections, declared_collections))

    actions.extend(_plan_roles(document.get("roles") or [], live_roles))

    live_ids = {
        GATEWAY: _declared_ids(declared_gateways, live_gateways),
        COLLECTION: _declared_ids(declared_collections, live_collections),
    }
    return Plan(tuple(actions), tuple(problems), tuple(notes), live_ids)


def _declared_ids(declared: dict[str, Any], live: dict[str, Any]) -> dict[str, str]:
    return {
        name: str(live[name]["id"])
        for name in declared
        if isinstance(live.get(name), dict) and live[name].get("id")
    }


def _plan_object(
    *,
    object_type: str,
    name: str,
    declared: dict[str, Any],
    live: dict[str, Any] | None,
    immutable: tuple[str, ...],
    mutable: tuple[str, ...],
    actions: list[Action],
    problems: list[Problem],
    notes: list[Note],
) -> None:
    managed = declared.get("managed", True)

    if live is None:
        # Nothing to conflict with, so `managed: false` still blocks creation:
        # an object declared read-only is one we were told not to act on.
        if managed:
            actions.append(Action(CREATE, object_type, name))
        else:
            notes.append(
                Note(object_type, name, "declared managed:false and does not exist; not created")
            )
        return

    frozen = _differences(declared, live, immutable)
    changed = _differences(declared, live, mutable)
    unverifiable = _unreported(declared, live, immutable + mutable)

    # A field the endpoint never reports is UNKNOWN, not different: comparing a
    # declared value against nothing reads as a difference, and `base_path` is
    # immutable, so it would block every apply on a collection that is in fact
    # correct.
    #
    # The case that prompted this was base_path on GCS 5.4.98 — but that one
    # turned out to be OUR bug, not the endpoint's: `collection list` omits
    # private policies unless asked, and `gcs.py` was not asking. Fixed there,
    # so base_path is now compared for real. The mechanism stays, because the
    # next genuinely unreported field will not announce itself, and a note is
    # the response that cannot do harm in either direction.
    for key in sorted(unverifiable):
        notes.append(
            Note(
                object_type,
                name,
                f"{key} cannot be checked: the endpoint does not report it, so it is "
                f"left alone (declared {declared[key]!r})",
            )
        )

    if not managed:
        # Drift is REPORTED even when it will not be applied. Silence here would
        # make a read-only declaration look like an unchanged endpoint.
        for key, (live_value, declared_value) in sorted({**frozen, **changed}.items()):
            notes.append(
                Note(
                    object_type,
                    name,
                    f"declared managed:false; {key} differs ({live_value!r} live, "
                    f"{declared_value!r} declared) and will not be changed",
                )
            )
        return

    # Immutable differences are reported for every offending field, not just the
    # first: an operator deciding whether to rebuild wants the whole picture.
    for key, (live_value, declared_value) in sorted(frozen.items()):
        problems.append(Problem(object_type, name, key, live_value, declared_value))

    if changed and not frozen:
        actions.append(Action(UPDATE, object_type, name, changed))


def _differences(
    declared: dict[str, Any], live: dict[str, Any], keys: tuple[str, ...]
) -> dict[str, tuple[Any, Any]]:
    """{field: (live, declared)} for keys the declaration actually states.

    A key absent from the declaration is not a difference — it is a field the
    configuration does not have an opinion about, and having no opinion must
    never translate into an update.
    """
    differences: dict[str, tuple[Any, Any]] = {}
    for key in keys:
        if key not in declared or key not in live:
            # Absent from live is handled by `_unreported`: unknown is not a
            # difference, and acting on one would be acting on a guess.
            continue
        live_value = live.get(key)
        declared_value = declared[key]
        if _normalise(live_value) != _normalise(declared_value):
            differences[key] = (live_value, declared_value)
    return differences


def _unreported(declared: dict[str, Any], live: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    """Declared fields the endpoint does not report at all.

    Distinct from a difference on purpose: "the endpoint says something else" and
    "the endpoint says nothing" call for opposite responses. The first is drift to
    resolve; the second cannot be resolved by this tool at all, and treating it as
    drift would either propose a change nothing can verify or — for an immutable
    field — refuse every apply forever.
    """
    return [key for key in keys if key in declared and key not in live]


def _normalise(value: Any) -> Any:
    """Compare lists order-insensitively; Globus does not promise an order."""
    if isinstance(value, list):
        return sorted(str(item) for item in value)
    return value


def _unmanaged(object_type: str, live: dict[str, Any], declared: dict[str, Any]) -> list[Note]:
    return [
        Note(object_type, name, "unmanaged, left unchanged")
        for name in sorted(live)
        if name not in declared
    ]


def _plan_roles(declared: list[Any], live: Any) -> list[Action]:
    """Roles are create-only: a role is present or it is not.

    There is no update — a changed role is a different grant — and removal is
    deletion, which this tool does not do. An operator revoking a role does it
    themselves, deliberately.
    """
    existing = {_role_key(role) for role in live or []}
    actions = []
    for role in declared:
        if not isinstance(role, dict):
            continue
        if _role_key(role) not in existing:
            name = f"{role.get('role')} for {role.get('principal')}"
            if role.get("collection"):
                name += f" on {role['collection']}"
            actions.append(Action(CREATE, ROLE, name))
    return actions


def _role_key(role: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(role.get("principal", "")),
        str(role.get("role", "")),
        str(role.get("collection") or ""),
    )


def _by_name(objects: list[Any]) -> dict[str, dict[str, Any]]:
    return {
        obj["display_name"]: obj
        for obj in objects
        if isinstance(obj, dict) and isinstance(obj.get("display_name"), str)
    }
