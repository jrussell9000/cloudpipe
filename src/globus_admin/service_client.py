"""Register — and delete — the confidential client that owns this deployment.

For most of this change the service client was treated as a human gate: the
Terraform module creates two placeholder parameters and a comment tells the
operator to open the Globus Developers Portal, register a confidential client,
and paste the id and secret in. That was never a limit of Globus — it was a limit
of what this package had implemented. Globus Auth publishes
`POST /v2/api/projects`, `POST /v2/api/clients` and
`POST /v2/api/clients/<id>/credentials`, all three gated on one scope
(`manage_projects`), and `globus_sdk` wraps each of them.

Three decisions shape this module, and only the first is about convenience.

**The consent is deliberately not the pipeline's.** `globus login` stores its
refresh token in Secrets Manager, where the Prefect flow reads it. Adding
`manage_projects` to *that* app's scope requirements would be the smallest
possible diff and the largest change to the deployment's blast radius: a
compromise of that one secret would then be able to create and delete Globus Auth
clients and projects, not only move data. So this builds its own `UserApp`, asks
for `manage_projects` beside `openid`/`profile`/`email` and nothing else, and sets
`request_refresh_tokens=False` — there is nothing here worth renewing, because the
token is meant to die with the process.

**Nothing is persisted, by construction.** The token lives in a
`MemoryTokenStorage` this module owns, exactly as `login.py` does. The only values
that reach durable storage are the client id and its secret, written as SSM
SecureStrings by the command, and the command is what decides that.

**A client secret is returned exactly once.** `create_client_credential` is the
only chance to read it — Globus has no "show me the secret again" call — so a
failure *after* the credential exists cannot be repaired by reading it a second
time. That is why `register` returns the secret to its caller rather than storing
it, and why the caller is handed a rollback (`delete_client`) instead of a retry.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .exits import CliError, ExitCode, Remedy
from .login import AUTH_RESOURCE_SERVER, require_allowed_domain

#: Its own app name, and its own in-memory store. Distinct from `login.APP_NAME`
#: so nothing about this consent can be confused with the pipeline's.
APP_NAME = "cloudpipe-globus-admin-projects"

#: Globus Auth's own name for the client an endpoint needs. Mutually exclusive
#: with `public_client` in `create_client`: exactly one must be given, and this
#: one says what the client is *for* rather than what it may hold.
CLIENT_TYPE = "globus_connect_server"

#: The credential's display name in the Globus console. Fixed, and deliberately
#: not dated or generated: one credential per client is all a deployment needs, and
#: a second one under a unique name would be indistinguishable in that console from
#: the one the endpoint actually authenticates with.
CREDENTIAL_NAME = "globus-connect-server"

CODE_PREFIX = "service_client"

#: Shape only, not a version check. `admin_ids` is rejected wholesale by Globus if a
#: single element is not a UUID, and the rejection message truncates the list — so an
#: element is checked here, where it can be named, rather than there.
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass(frozen=True)
class Project:
    """One Globus Auth project the logged-in identity administers.

    `admin_ids` is carried because `update_project` **replaces** that list rather
    than appending to it, so adding an administrator is a read-modify-write and the
    read has to come from somewhere. `get_projects` already returns it on every
    record, which is why there is no separate lookup: dropping it here and fetching
    it again later would be two requests for one fact.

    It holds the same field the write sends, and nothing adjacent to it — see
    `_admin_ids`, where reading one field and writing another is recorded as the
    mistake that made the first live attempt fail.

    `admin_group_ids` is deliberately *not* carried. `update_project` leaves any
    field it is not passed alone, so a group-administered project keeps its groups
    as long as nothing sends that field — and not holding the value is the clearest
    way to guarantee nothing sends it.
    """

    id: str
    display_name: str
    admin_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, str]:
        # Deliberately not `admin_ids`: these are identity UUIDs of people, they
        # are not needed to act on the report, and a JSON envelope is the one
        # output of this tooling that gets pasted into issues.
        return {"id": self.id, "display_name": self.display_name}


@dataclass(frozen=True)
class Registration:
    """A client that now exists, and the one chance to read its secret.

    `secret` is never logged, never emitted, and never returned in a JSON
    envelope. `as_dict` is what a report may carry, and it deliberately has no
    field for it — a dataclass with `repr` suppressed would still print under
    `pytest`'s assertion rewriting, so the protection is in what callers are
    given rather than in how this prints.
    """

    client_id: str
    secret: str
    project_id: str
    name: str

    def as_dict(self) -> dict[str, str]:
        return {"client_id": self.client_id, "project_id": self.project_id, "name": self.name}


@dataclass(frozen=True)
class AuthSession:
    """An authorized `AuthClient`, and the identity that authorized it."""

    auth: Any
    username: str
    identity_id: str | None


@dataclass
class ManageProjectsSession:
    """One `manage_projects` login, held in memory for the length of a command.

    The same forced-reauthentication parameters as `login.LoginSession`, for a
    different reason: there the recent authentication event is what a High
    Assurance collection demands, here it is that creating or deleting a Globus
    Auth client is worth an explicit login rather than a browser session someone
    left open. Both arrive at `prompt=login`.
    """

    client_id: str
    domains: tuple[str, ...]
    no_browser: bool = False
    app_factory: Callable[..., Any] | None = None
    auth_factory: Callable[..., Any] | None = None
    userinfo: Callable[[Any], dict[str, Any]] | None = None

    def run(self) -> AuthSession:
        from .login import auth_params, userinfo_of

        storage = _memory_storage()
        app = (self.app_factory or _build_app)(
            client_id=self.client_id, no_browser=self.no_browser, storage=storage
        )
        app.login(auth_params=auth_params(self.domains), force=True)

        auth = (self.auth_factory or _build_auth_client)(app)
        info = (self.userinfo or userinfo_of)(app)
        username = str(info.get("preferred_username") or info.get("email") or "").strip()
        identity_id = info.get("sub")
        require_allowed_domain(username, self.domains)
        return AuthSession(auth=auth, username=username, identity_id=str(identity_id or "") or None)


def projects(auth: Any) -> tuple[Project, ...]:
    """Every project the logged-in identity administers.

    `get_projects` answers for the caller's own administered projects only, which
    is the same set a client may be created in — so this listing is not merely
    informative: it is the authoritative answer to "where could this client go".
    """
    from . import errors

    try:
        response = auth.get_projects()
    except Exception as exc:  # pragma: no cover - network failures
        raise errors.globus_error(exc) from exc

    found: list[Project] = []
    for record in _iter_projects(response):
        project_id = str(record.get("id") or "").strip()
        if not project_id:
            continue
        name = str(record.get("display_name") or record.get("project_name") or "").strip()
        found.append(Project(project_id, name, _admin_ids(record)))
    return tuple(found)


def choose_project(available: tuple[Project, ...], *, requested: str = "") -> str:
    """Which project to create the client in, or a refusal that lists the options.

    Pure, and separated from the API calls because every branch here is a
    judgement rather than a request. The one that matters is the middle case: with
    several projects and no `--project-id`, picking the first would put a
    deployment's endpoint-owning client somewhere nobody chose, and a client
    cannot be moved between projects afterwards.
    """
    names = {project.id: project.display_name for project in available}
    if requested:
        if requested in names:
            return requested
        raise CliError(
            f"{CODE_PREFIX}.project_not_administered",
            f"--project-id names {requested}, which is not one of the Globus Auth projects "
            "this identity administers. A client can only be created in a project you "
            f"administer. Visible: {_describe(available) or 'none'}.",
            exit_code=ExitCode.INVALID,
            detail={"requested": requested, "available": [p.as_dict() for p in available]},
        )

    if not available:
        raise CliError(
            f"{CODE_PREFIX}.no_projects",
            "This identity administers no Globus Auth project, and a client has to belong "
            "to one. Pass --create-project '<name>' to create a project and the client in "
            "it, or --project-id if someone has already made you an administrator of one.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("human", "re-run with --create-project '<project name>'"),
        )

    if len(available) > 1:
        raise CliError(
            f"{CODE_PREFIX}.project_ambiguous",
            "This identity administers more than one Globus Auth project, so which one "
            "should own this deployment's client is a decision rather than a default — and "
            f"a client cannot be moved afterwards. Pass --project-id. {_describe(available)}.",
            exit_code=ExitCode.INVALID,
            remedy=Remedy("human", "re-run with --project-id <uuid>"),
            detail={"available": [p.as_dict() for p in available]},
        )

    return available[0].id


def create_project(auth: Any, *, display_name: str, contact_email: str, admin_id: str) -> Project:
    """Create a project, with the logged-in identity as its administrator.

    `admin_ids` is not optional in effect: Globus does not add the caller
    implicitly, so a project created without it belongs to nobody and neither
    this command nor the operator could put a client in it. That is why the
    identity is read from `userinfo` at login rather than asked for.
    """
    from . import errors

    if not admin_id:
        raise CliError(
            f"{CODE_PREFIX}.no_identity",
            "The login did not report an identity id, so a new project could not be given "
            "an administrator — and Globus does not add the caller implicitly, so the "
            "project would belong to nobody. Nothing was created.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("human", "re-run with --project-id for a project you administer"),
        )
    try:
        response = auth.create_project(display_name, contact_email, admin_ids=[admin_id])
    except Exception as exc:  # pragma: no cover - network failures
        raise errors.globus_error(exc) from exc

    document = _document(response, "project")
    project_id = str(document.get("id") or "").strip()
    if not project_id:
        raise CliError(
            f"{CODE_PREFIX}.project_id_missing",
            "Globus accepted the project but returned no id for it, so this command cannot "
            "say where to create the client. Check the Globus console before re-running: a "
            "project may exist under this name.",
            exit_code=ExitCode.ERROR,
            detail={"display_name": display_name},
        )
    # `admin_ids` is what was *asked for* rather than what came back, because the
    # response shape for a freshly created project is the one thing here that is
    # not worth trusting — and the caller needs this list to add the client to it
    # without dropping the operator.
    return Project(project_id, display_name, (admin_id,))


def register(auth: Any, *, name: str, project_id: str) -> Registration:
    """Create the client and its one credential. Two calls, and both can fail.

    The split is Globus's, not ours: a client exists before it has any credential,
    and the credential call is the only one that ever discloses the secret. So a
    failure of the second call leaves a real client with no way to authenticate —
    reported here with the client id, because that id is what a caller needs in
    order to delete it.
    """
    from . import errors

    try:
        created = auth.create_client(name, project_id, client_type=CLIENT_TYPE)
    except Exception as exc:  # pragma: no cover - network failures
        raise errors.globus_error(exc) from exc

    client_id = str(_document(created, "client").get("id") or "").strip()
    if not client_id:
        raise CliError(
            f"{CODE_PREFIX}.client_id_missing",
            "Globus accepted the client but returned no id for it, so this command can "
            "neither store it nor delete it. Check the Globus console for a client named "
            f"{name!r} before re-running.",
            exit_code=ExitCode.ERROR,
            detail={"name": name, "project_id": project_id},
        )

    try:
        credential = auth.create_client_credential(client_id, CREDENTIAL_NAME)
    except Exception as exc:
        raise CliError(
            f"{CODE_PREFIX}.credential_failed",
            f"Client {client_id} was created, but Globus refused to issue a credential for "
            "it — so the client exists and cannot authenticate. Nothing was stored. Delete "
            "it in the Globus console, or with `globus delete-service-client --client-id` "
            "once the id is recorded.",
            exit_code=ExitCode.ERROR,
            remedy=Remedy("human", f"delete the Globus client {client_id}"),
            detail={"client_id": client_id, "project_id": project_id},
            raw=str(exc),
        ) from exc

    secret = str(_document(credential, "credential").get("secret") or "")
    if not secret:
        raise CliError(
            f"{CODE_PREFIX}.secret_missing",
            f"Globus issued a credential for client {client_id} but returned no secret, and "
            "a secret is disclosed only at creation — so this one can never be read. Delete "
            "the client and register again.",
            exit_code=ExitCode.ERROR,
            remedy=Remedy("human", f"delete the Globus client {client_id}"),
            detail={"client_id": client_id, "project_id": project_id},
        )
    return Registration(client_id=client_id, secret=secret, project_id=project_id, name=name)


def client_identity(client_id: str) -> str:
    """The identity a confidential client authenticates as.

    Globus gives every client an identity in the `clients.auth.globus.org` domain,
    and its local part is the client id — so this is a rename, not a lookup. It is
    here because GCS reports failures under this form while every API call takes the
    bare UUID, and reading one error message against the other is otherwise guesswork.
    """
    return f"{client_id}@clients.auth.globus.org"


def grant_project_admin(
    auth: Any,
    *,
    project_id: str,
    client_id: str,
    current_admin_ids: tuple[str, ...],
    operator_admin_id: str,
) -> tuple[str, ...]:
    """Add the service client to a project's administrators, keeping the existing ones.

    **Why this exists at all.** `globus-connect-server endpoint setup` refuses to run
    as an identity that does not administer an Auth project, and refuses in two
    different ways: with no `--project-id` it says a project "will not be
    automatically created with an Auth client as the sole project admin", and with
    one it says the project "does not exist or … is not an admin on the project and
    can not view it". Task 10.6 established by live run that a confidential client
    must be an **administrator** of the project, and that `--project-id` does not
    substitute for it. Being *in* a project is not the same as administering one.

    **Why it is a read-modify-write.** `admin_ids` on `update_project` replaces the
    list. Sending only the client would remove the operator from their own project,
    which is unrecoverable through this tooling — the identity that could add them
    back is the one just removed. So the current list is required rather than
    optional, and it is passed in instead of fetched so the caller cannot get a
    *different* project's admins than the one it is about to write.

    **`operator_admin_id` is separate from that list on purpose.** If the read ever
    comes back short — a response shape that changes, a field that moves — the
    difference between "an administrator was not added" and "an administrator was
    removed" is the whole risk here, and only one of the two is repairable. Naming
    the logged-in identity explicitly means the identity that could undo a mistake
    survives every mistake. It is a set union, so a normal read where it is already
    present changes nothing.

    `admin_group_ids` is never sent, which is what preserves it: `update_project`
    leaves an unpassed field alone.

    **The privilege this grants is real.** A project administrator may create and
    delete clients in that project, so the endpoint's own credential gains that
    ability. GCS leaves no choice about it, but it is a reason to give a deployment's
    client a project of its own rather than one shared with anything else.
    """
    if not project_id or not client_id:
        raise CliError(
            f"{CODE_PREFIX}.grant_missing_ids",
            "Adding the client to a project's administrators needs both the project id and "
            "the client id, and one of them is empty. Nothing was changed.",
            exit_code=ExitCode.INVALID,
            detail={"project_id": project_id, "client_id": client_id},
        )

    admin_ids = tuple(current_admin_ids)
    for identity in (operator_admin_id, client_id):
        if identity and identity not in admin_ids:
            admin_ids = (*admin_ids, identity)
    if admin_ids == tuple(current_admin_ids):
        return admin_ids

    # Checked here rather than left to the API, because the API's rejection cannot be
    # acted on: it answers with the list truncated mid-element, so "one of these is
    # not a UUID" arrives without saying which. That is what the live run got.
    bad = [identity for identity in admin_ids if not _UUID.match(identity)]
    if bad:
        raise CliError(
            f"{CODE_PREFIX}.grant_invalid_ids",
            "The administrator list for this project contains an entry that is not an "
            f"identity UUID ({', '.join(repr(item) for item in bad)}), and Globus rejects "
            "the whole list if any element is not one. Nothing was changed. Add the client "
            "under the project's Admins in the Globus console instead.",
            exit_code=ExitCode.ERROR,
            remedy=Remedy("human", f"add {client_identity(client_id)} as an admin of the project"),
            detail={"project_id": project_id, "client_id": client_id, "rejected": bad},
        )

    try:
        auth.update_project(project_id, admin_ids=list(admin_ids))
    except Exception as exc:
        raise CliError(
            f"{CODE_PREFIX}.grant_failed",
            f"Globus refused to make client {client_id} an administrator of project "
            f"{project_id}, so `endpoint setup` will still refuse to create an endpoint as "
            "it. Nothing else was changed, and the client is otherwise complete — this can "
            "be retried, or done in the Globus console under the project's Admins.",
            exit_code=ExitCode.ERROR,
            remedy=Remedy(
                "command", f"pixi run globus grant-project-admin --project-id {project_id}"
            ),
            # The list that was sent, because Globus's own rejection truncates it and
            # these are identity UUIDs rather than secrets. Without this the next
            # attempt is as blind as the one that just failed.
            detail={
                "project_id": project_id,
                "client_id": client_id,
                "admin_ids_sent": list(admin_ids),
            },
            raw=str(exc),
        ) from exc
    return admin_ids


def delete_client(auth: Any, client_id: str) -> None:
    """Delete the client, and with it every credential issued to it."""
    from . import errors

    try:
        auth.delete_client(client_id)
    except Exception as exc:  # pragma: no cover - network failures
        raise errors.globus_error(exc) from exc


def _describe(available: tuple[Project, ...]) -> str:
    return ", ".join(f"{p.display_name or '(unnamed)'} ({p.id})" for p in available)


def _iter_projects(response: Any) -> list[dict[str, Any]]:
    """The project records, however the response chooses to present them.

    `get_projects` returns an `IterableResponse` that iterates records directly,
    but a stub in a test — and `GlobusHTTPResponse` itself — is more naturally
    read through `.data`. Both are accepted: the alternative is a test that
    passes against a shape the SDK does not produce.
    """
    data = getattr(response, "data", None)
    if isinstance(data, dict) and isinstance(data.get("projects"), list):
        return [record for record in data["projects"] if isinstance(record, dict)]
    if isinstance(response, dict) and isinstance(response.get("projects"), list):
        return [record for record in response["projects"] if isinstance(record, dict)]
    return [record for record in response if isinstance(record, dict)]


def _admin_ids(record: dict[str, Any]) -> tuple[str, ...]:
    """A project record's administrator identities, from `admin_ids` and only there.

    `get_projects` also returns a nested `admins: {identities: [...], groups: [...]}`
    that documents the same identities a second time, and the first version of this
    read the union of both. The live run rejected that write:

        400 INVALID_PARAMETERS: Invalid value for 'admin_ids' parameter:
        ['cab26c5b-b6d6-4fe1.... 'admin_ids' must be a list of UUIDs

    Globus truncated the list, so which element offended is not recoverable from the
    message — but the operator's own identity had just been accepted by
    `create_project`, so the only suspect was the entry that came from the nested
    field under an assumed shape. Reading one field and writing another is the
    mistake; `admin_ids` is what `update_project` replaces, so `admin_ids` is what
    is read.

    Entries are still normalized rather than trusted: an element may be a bare UUID
    or an object carrying one, and anything that is not a UUID after that is dropped
    instead of being sent. `grant_project_admin` names the logged-in identity
    separately, which is what makes dropping safe — the identity that could repair a
    mistake is never the one at risk from it.
    """
    values = record.get("admin_ids")
    if not isinstance(values, list):
        return ()
    found: list[str] = []
    for item in values:
        identity = _identity_id(item)
        if identity and identity not in found:
            found.append(identity)
    return tuple(found)


def _identity_id(item: Any) -> str:
    """One identity UUID out of an entry that may be a UUID or an object holding one."""
    if isinstance(item, dict):
        item = item.get("id") or item.get("identity_id") or ""
    text = str(item or "").strip()
    return text if _UUID.match(text) else ""


def _document(response: Any, key: str) -> dict[str, Any]:
    """One object out of a Globus Auth response, wrapped or not.

    The Auth API documents these responses as an envelope (`{"client": {…}}`) and
    the SDK's own docstrings show them bare, so both are read rather than one
    being trusted. Getting this wrong does not raise — it yields an empty id, and
    an empty id for a client that really was created is the worst outcome
    available here, because the thing needed to delete it is the thing lost.
    """
    data = getattr(response, "data", None)
    if not isinstance(data, dict):
        data = response if isinstance(response, dict) else {}
    inner = data.get(key)
    if isinstance(inner, dict):
        return inner
    return data


def _memory_storage() -> Any:
    from globus_sdk.token_storage import MemoryTokenStorage

    return MemoryTokenStorage()


def _build_app(*, client_id: str, no_browser: bool, storage: Any) -> Any:
    import globus_sdk

    config = globus_sdk.GlobusAppConfig(
        token_storage=storage,
        login_flow_manager="command-line" if no_browser else "local-server",
        # No refresh token, unlike `login._build_app`. This consent is used for
        # the length of one command and must not outlive it; asking for something
        # renewable would be asking for a credential to look after.
        request_refresh_tokens=False,
    )
    return globus_sdk.UserApp(
        APP_NAME,
        client_id=client_id,
        config=config,
        scope_requirements={
            AUTH_RESOURCE_SERVER: [
                "openid",
                "profile",
                "email",
                globus_sdk.AuthClient.scopes.manage_projects,
            ]
        },
    )


def _build_auth_client(app: Any) -> Any:
    import globus_sdk

    return globus_sdk.AuthClient(app=app)
