"""`globus register-service-client` and `globus delete-service-client`.

The operator's end of `globus_admin.service_client`. That module talks to Globus
Auth; this one decides whether it should be asked to, and where the result is
stored.

**What these replace.** `terraform/modules/globus/main.tf` creates
`gcs-client-id` and `gcs-client-secret` as placeholders and a comment used to send
the operator to the Globus Developers Portal with instructions to register a
confidential client and paste both values in by hand. Every step of that is an API
call, so the portal visit was a habit rather than a requirement — and it was the
reason the endpoint bootstrap was described as gated on a human.

**The guards are the same ones `bootstrap-endpoint` and `cleanup-endpoint` use,
for the same reasons.** Not staging, because both parameters live under the shared
prefix and a staging run would write production's credentials. Not when either
parameter already holds a value, because the client an existing endpoint
authenticates as is reachable only through them: overwriting one leaves the real
client unreferenced, unusable and undeletable by this tooling. And deletion
requires the operator to type the client id, which cannot be done by accident.

**The write order is chosen for the state a crash leaves behind.** The id lands
first and the secret second, which is the opposite of `bootstrap.py`'s ordering,
and the reason is the same principle: leave the half-done state that the recovery
tool can act on. `delete-service-client` needs the id and nothing else, so
id-without-secret is repairable in one command, while secret-without-id would be a
live client whose UUID exists only in a scrollback buffer.
"""

from __future__ import annotations

from typing import Any

from .. import aws as aws_module
from .. import cluster, environments, guards
from .. import service_client as service_client_module
from ..cli import Context, register
from ..exits import CliError, ExitCode, Remedy

CODE_PREFIX = "service_client"

#: How the client is named in the Globus console when `--name` is not given.
#: Derived from the deployment name so two deployments in one Auth project are
#: distinguishable, which is the only thing that console shows.
NAME_SUFFIX = "-gcs"


@register("register-service-client")
def register_service_client(ctx: Context) -> ExitCode:
    _refuse_staging(ctx, "register")
    ctx.require_prereqs(mutating=True)
    _refuse_occupied(ctx)

    name = (getattr(ctx.args, "name", None) or "").strip() or (
        f"{ctx.config.deployment_name}{NAME_SUFFIX}"
    )
    env = ctx.env
    data: dict[str, Any] = {
        "environment": env.name,
        "name": name,
        "client_id": None,
        "project_id": None,
        "project_created": False,
        "project_admin_granted": False,
        "identity": None,
        "id_param": env.service_client_id_param,
        "secret_param": env.service_client_secret_param,
        "stored": False,
        "rollback": None,
    }

    guards.confirm(
        ctx.emitter,
        action=(
            f"register a Globus confidential client named {name!r} for "
            f"{ctx.config.deployment_name} and store its id and secret in "
            f"{env.service_client_id_param} and {env.service_client_secret_param}"
        ),
        activity=guards.workflow_activity(cluster.probe(ctx.runner), ctx.runner),
        non_interactive=ctx.non_interactive,
        assume_yes=ctx.assume_yes,
    )

    session = _login(ctx)
    data["identity"] = session.username

    project, created = _resolve_project(ctx, session)
    data["project_id"] = project.id
    data["project_created"] = created

    registration = service_client_module.register(session.auth, name=name, project_id=project.id)
    data["client_id"] = registration.client_id
    ctx.emitter.note(f"registered client {registration.client_id} in project {project.id}")

    _store(ctx, session, registration, data)

    # Last, and after the store, for the reason the module docstring gives about
    # write order: the secret is disclosed once, so anything that could fail must
    # fail on the side of the state a recovery command can act on. A stored client
    # that is not yet a project administrator is repaired by one command
    # (`grant-project-admin`); an unstored secret is not repairable at all.
    _grant_admin(ctx, session, project, registration.client_id, data)
    _present(ctx, registration)
    return ctx.emitter.result(data)


@register("grant-project-admin")
def grant_project_admin(ctx: Context) -> ExitCode:
    """Repair a deployment whose client is registered but not a project administrator.

    `register-service-client` does this as its last step, so on a fresh deployment
    this command is never needed. It exists for the two states that step cannot
    cover: a registration whose grant failed after the credentials were stored (the
    store comes first on purpose), and a client registered before this tooling knew
    the grant was required — which is every client registered through the Globus
    Developers Portal, including the T6 throwaway that found the requirement.
    """
    _refuse_staging(ctx, "grant project administration for")
    ctx.require_prereqs(mutating=True)

    project_id = (getattr(ctx.args, "project_id", None) or "").strip()
    client_id = _recorded_client_id(ctx)
    env = ctx.env
    data: dict[str, Any] = {
        "environment": env.name,
        "client_id": client_id,
        "project_id": project_id,
        "identity": None,
        "already_admin": False,
        "granted": False,
    }

    guards.confirm(
        ctx.emitter,
        action=(
            f"make the Globus client {client_id} an administrator of Auth project "
            f"{project_id}, which lets it create and delete clients in that project"
        ),
        activity=guards.workflow_activity(cluster.probe(ctx.runner), ctx.runner),
        non_interactive=ctx.non_interactive,
        assume_yes=ctx.assume_yes,
    )

    session = _login(ctx)
    data["identity"] = session.username

    available = service_client_module.projects(session.auth)
    # `choose_project` is reused for its refusal rather than its choice: it is the
    # one place that says "you do not administer that project" with the list of the
    # ones you do, and a caller who does not administer the project cannot write to
    # its administrators either.
    chosen = service_client_module.choose_project(available, requested=project_id)
    project = next(p for p in available if p.id == chosen)

    if client_id in project.admin_ids:
        data["already_admin"] = True
        ctx.emitter.note(
            f"client {client_id} already administers project {chosen}; nothing to change"
        )
        return ctx.emitter.result(data)

    admins = service_client_module.grant_project_admin(
        session.auth,
        project_id=chosen,
        client_id=client_id,
        current_admin_ids=project.admin_ids,
        operator_admin_id=session.identity_id or "",
    )
    data["granted"] = True
    data["project_admin_count"] = len(admins)
    ctx.emitter.note(
        f"client {client_id} now administers project {chosen} "
        f"({service_client_module.client_identity(client_id)})"
    )
    return ctx.emitter.result(data)


@register("delete-service-client")
def delete_service_client(ctx: Context) -> ExitCode:
    _refuse_staging(ctx, "delete")
    ctx.require_prereqs(mutating=True)
    client_id = _require_matching_client_id(ctx)
    _refuse_while_endpoint_exists(ctx, client_id)

    env = ctx.env
    data: dict[str, Any] = {
        "environment": env.name,
        "client_id": client_id,
        "identity": None,
        "id_param": env.service_client_id_param,
        "secret_param": env.service_client_secret_param,
        "deleted": False,
        "parameters_reset": [],
    }

    guards.confirm(
        ctx.emitter,
        action=(
            f"permanently delete the Globus client {client_id} and every credential issued "
            f"to it, and reset {env.service_client_id_param} and "
            f"{env.service_client_secret_param} to the placeholder"
        ),
        activity=guards.workflow_activity(cluster.probe(ctx.runner), ctx.runner),
        non_interactive=ctx.non_interactive,
        assume_yes=ctx.assume_yes,
    )

    session = _login(ctx)
    data["identity"] = session.username

    service_client_module.delete_client(session.auth, client_id)
    data["deleted"] = True
    ctx.emitter.note(f"client {client_id} deleted")

    # After the deletion, not before: a parameter reset that outlived a failed
    # delete would lose the id needed to try again.
    data["parameters_reset"] = _reset(
        ctx, (env.service_client_id_param, env.service_client_secret_param)
    )
    ctx.emitter.note(f"reset to the placeholder: {', '.join(data['parameters_reset'])}")
    return ctx.emitter.result(data)


# --- the steps --------------------------------------------------------------


def _login(ctx: Context) -> service_client_module.AuthSession:
    """The `manage_projects` consent, which is not the pipeline's consent.

    Announced before the browser opens, because an operator who has just run
    `globus login` will otherwise reasonably wonder why they are being asked
    again. The answer is in the scope: this one may create and delete Globus Auth
    clients, and it is thrown away when the command ends.
    """
    domains = ctx.config.identity_domains
    if domains:
        ctx.emitter.note(f"Sign in with an identity in: {', '.join(domains)}")
    ctx.emitter.note(
        "This login asks for the `manage_projects` scope, which the pipeline's own "
        "credential deliberately does not hold. Nothing from it is stored."
    )
    return service_client_module.ManageProjectsSession(
        client_id=_native_app_client_id(ctx),
        domains=domains,
        no_browser=bool(getattr(ctx.args, "no_browser", False)),
    ).run()


def _native_app_client_id(ctx: Context) -> str:
    """Which registered application performs the browser flow.

    The native app, the same one `globus login` uses — and the one registration
    that stays manual, because a native app is what a human logs in *through*.
    Deliberately not the service client: on `register` it does not exist yet, and a
    confidential client cannot run a browser flow on a person's behalf in any case.

    Taken from the answers document or the environment, and **not** from a flag.
    `delete-service-client --client-id` already means the service client being
    deleted, and one `--client-id` meaning the app to log in through on one command
    and the credential to destroy on the other is exactly the confusion the
    typed-UUID guard exists to prevent.
    """
    import os

    from .. import login as login_module
    from .login import CLIENT_ID_ENV

    return login_module.resolve_client_id(
        None, ctx.config.native_app_client_id, os.environ.get(CLIENT_ID_ENV), None
    )


def _resolve_project(
    ctx: Context, session: service_client_module.AuthSession
) -> tuple[service_client_module.Project, bool]:
    """Which Auth project owns the client, creating one only when asked outright.

    `--create-project` is a separate flag rather than a fallback for "no projects
    found": a project is an organizational object with an administrator and a
    contact address, it outlives the client inside it, and guessing that one should
    exist is not a default worth having.

    Returns the whole `Project` rather than its id because the client has to be
    added to the project's administrators afterwards, and that write replaces the
    administrator list — so the list as it stands now is part of what this resolves.
    """
    requested = (getattr(ctx.args, "project_id", None) or "").strip()
    create = (getattr(ctx.args, "create_project", None) or "").strip()

    if create and requested:
        raise CliError(
            f"{CODE_PREFIX}.project_both_named",
            "--project-id and --create-project both name where the client should go, and "
            "they cannot both be right. Pass one.",
            exit_code=ExitCode.INVALID,
        )

    if create:
        contact = ctx.config.contact_email or ""
        if not contact:
            raise CliError(
                f"{CODE_PREFIX}.no_contact_email",
                "Creating a Globus Auth project needs a contact email address, and the "
                "answers document declares none. It appears on the project in the Globus "
                "console as its point of contact.",
                exit_code=ExitCode.INVALID,
                remedy=Remedy("human", "add contact_email to the answers document"),
            )
        project = service_client_module.create_project(
            session.auth,
            display_name=create,
            contact_email=contact,
            admin_id=session.identity_id or "",
        )
        ctx.emitter.note(f"created Globus Auth project {project.display_name} ({project.id})")
        return project, True

    available = service_client_module.projects(session.auth)
    chosen = service_client_module.choose_project(available, requested=requested)
    # `choose_project` answers with an id because every one of its branches is a
    # judgement about which id, not about which record; the record is recovered here
    # rather than threaded through it.
    for project in available:
        if project.id == chosen:
            return project, False
    raise CliError(  # pragma: no cover - `choose_project` only ever returns a listed id
        f"{CODE_PREFIX}.project_vanished",
        f"Project {chosen} was chosen from the administered projects but is not in that "
        "listing, which should not be reachable. Nothing was created.",
        exit_code=ExitCode.ERROR,
    )


def _grant_admin(
    ctx: Context,
    session: service_client_module.AuthSession,
    project: service_client_module.Project,
    client_id: str,
    data: dict[str, Any],
) -> None:
    """Make the client an administrator of its own project, which GCS requires.

    Task 10.6 established this by live run: `endpoint setup` as a client that
    administers no project exits 1, and `--project-id` does not stand in for the
    role. So this is not a convenience — without it the next command in the
    documented sequence cannot succeed.

    The existing administrators come from the project record because the write
    replaces the list; see `service_client.grant_project_admin`.
    """
    admins = service_client_module.grant_project_admin(
        session.auth,
        project_id=project.id,
        client_id=client_id,
        current_admin_ids=project.admin_ids,
        operator_admin_id=session.identity_id or "",
    )
    data["project_admin_granted"] = True
    data["project_admin_count"] = len(admins)
    ctx.emitter.note(
        f"made the client an administrator of project {project.id}, which "
        "`endpoint setup` requires of the identity that creates an endpoint"
    )


def _store(
    ctx: Context,
    session: service_client_module.AuthSession,
    registration: service_client_module.Registration,
    data: dict[str, Any],
) -> None:
    """Both parameters, id first — and on any failure, undo the registration.

    A secret Globus disclosed once and this command failed to store is worse than
    no client at all: it names a live confidential client nobody can authenticate
    as and nothing here can find again. So the failure path deletes the client
    rather than leaving the operator to clean up in the console, and it says which
    of the two happened.
    """
    written: list[str] = []
    try:
        ctx.aws.put_parameter(ctx.env.service_client_id_param, registration.client_id, secure=True)
        written.append(ctx.env.service_client_id_param)
        ctx.aws.put_parameter(ctx.env.service_client_secret_param, registration.secret, secure=True)
        written.append(ctx.env.service_client_secret_param)
    except CliError as err:
        rollback = _rollback(ctx, session, registration, written)
        data["rollback"] = rollback
        undone = (
            "the client was deleted"
            if rollback["client_deleted"]
            else (f"the client {registration.client_id} could NOT be deleted and is still live")
        )
        raise CliError(
            f"{CODE_PREFIX}.store_failed",
            f"The client was registered, but storing it failed: {err.message} A client "
            "secret is disclosed only at creation, so it cannot be stored on a later run — "
            f"so the registration was rolled back, and {undone}.",
            exit_code=ExitCode.ERROR,
            remedy=Remedy("command", "pixi run globus setup-status"),
            detail={"client_id": registration.client_id, "rollback": rollback},
        ) from err

    data["stored"] = True
    ctx.emitter.note(f"stored the client id in {ctx.env.service_client_id_param}")
    ctx.emitter.note(
        f"stored the secret in {ctx.env.service_client_secret_param} (SecureString; "
        "its value is not printed, here or anywhere)"
    )


def _rollback(
    ctx: Context,
    session: service_client_module.AuthSession,
    registration: service_client_module.Registration,
    written: list[str],
) -> dict[str, Any]:
    """Best effort, and honest about what it could not do.

    Every step is attempted even if an earlier one failed: a client left behind in
    Globus and a parameter left holding a dead id are separate problems, and the
    operator needs to know which of them they still have.
    """
    record: dict[str, Any] = {"client_deleted": False, "parameters_reset": [], "errors": []}
    try:
        service_client_module.delete_client(session.auth, registration.client_id)
        record["client_deleted"] = True
    except CliError as err:
        record["errors"].append(err.message)

    try:
        record["parameters_reset"] = _reset(ctx, tuple(written))
    except CliError as err:  # pragma: no cover - a second SSM failure
        record["errors"].append(err.message)
    return record


def _reset(ctx: Context, names: tuple[str, ...]) -> list[str]:
    """Put the Terraform placeholder back, so the occupancy guards read free again."""
    reset: list[str] = []
    for name in names:
        ctx.aws.put_parameter(name, aws_module.PLACEHOLDER, secure=True)
        reset.append(name)
    return reset


def _present(ctx: Context, registration: service_client_module.Registration) -> None:
    """The client id, and the one thing that is not done yet.

    The remaining step is the operator's and is not a portal visit: the answers
    document is a file they own. The *project administrator* role used to be listed
    here as a later GCS command, which task 10.6 disproved — GCS refuses to create
    an endpoint as a client that administers no project, so the role has to exist
    before `bootstrap-endpoint`, and this command has already granted it.
    """
    emitter = ctx.emitter
    emitter.note("")
    emitter.note(
        f"Record this in the answers document:  service_client_id: {registration.client_id}"
    )
    emitter.note(
        f"                                     service_client_secret_ref: "
        f"ssm:{ctx.env.service_client_secret_param}"
    )
    emitter.note("")
    emitter.note(
        "Next: `pixi run globus bootstrap-endpoint`, which creates the endpoint as this "
        "client. The client is the endpoint's owner from then on, so it is not a credential "
        "to reuse for a second deployment."
    )


# --- the refusals -----------------------------------------------------------


def _refuse_staging(ctx: Context, verb: str) -> None:
    """Both parameters are deployment-wide, so `--env staging` names production's.

    The same refusal `bootstrap-endpoint` and `cleanup-endpoint` carry, and the
    same reason: `service_client_id_param` is built from `shared_ssm_prefix`.
    Staging shares the production endpoint and therefore the client that owns it.
    """
    if ctx.env.name == environments.PRODUCTION:
        return
    raise CliError(
        f"{CODE_PREFIX}.staging_shares_the_client",
        f"There is no separate {ctx.env.name} service client to {verb}: {ctx.env.name} shares "
        "the production endpoint, and the client that owns that endpoint is recorded once "
        f"per deployment. A {ctx.env.name} run here would act on production's client.",
        exit_code=ExitCode.INVALID,
        detail={"id_param": ctx.env.service_client_id_param},
    )


def _refuse_occupied(ctx: Context) -> None:
    """Refuse when either parameter holds anything but the Terraform placeholder.

    Occupancy rather than shape, as in `bootstrap_endpoint._refuse_existing_endpoint`:
    a value that is not UUID-shaped is still evidence that something has been here,
    and reading "I cannot interpret this" as "nothing is there" is what would
    overwrite a live client's credentials. Either parameter is enough to refuse —
    a half-written pair is a state to repair, not to write over.

    Both parameters are read **with decryption**, for the reason
    `_require_matching_client_id` gives: Terraform declares them `SecureString`, so
    an undecrypted read yields KMS ciphertext, which cannot equal the placeholder.
    There is no ciphertext to compare against either — KMS salts each put, so the
    same placeholder encrypts differently every time. Without this the guard reads
    every fresh deployment as occupied and refuses the one command that could fill
    it in, which is exactly what it did on the T6 throwaway. The decrypted value is
    never read here, printed, or put in `detail` — only `state.ok` is.
    """
    for name in (ctx.env.service_client_id_param, ctx.env.service_client_secret_param):
        state = ctx.aws.parameter_state(name, decrypt=True)
        if not state.ok:
            continue
        raise CliError(
            f"{CODE_PREFIX}.client_exists",
            f"{name} already holds a value, so this deployment has a service client. "
            "Registering another would overwrite the credentials of the client the "
            "endpoint authenticates as, leaving it unusable and unfindable — and the new "
            "client could not create an endpoint here either, because one already exists. "
            "Delete the recorded client first if it really is meant to go.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("command", "pixi run globus setup-status"),
            # The id is not a secret; the secret parameter's value is never read.
            detail={"parameter": name},
        )


def _recorded_client_id(ctx: Context) -> str:
    """The client this deployment records, read with decryption.

    No `--client-id` to match against, unlike `delete-service-client`: that guard
    exists because deletion is irreversible and the typed id is the one check the
    answers document cannot answer for you. This command adds a role to a project
    the operator must already administer, and the confirmation prompt names both
    ids — so requiring the id to be typed twice would buy nothing.

    Decryption for the reason `_refuse_occupied` records: Terraform declares the
    parameter a `SecureString`, and an undecrypted read is KMS ciphertext.
    """
    param = ctx.env.service_client_id_param
    state = ctx.aws.parameter_state(param, decrypt=True)
    if not state.ok:
        raise CliError(
            f"{CODE_PREFIX}.no_client",
            f"{param} records no service client for {ctx.config.deployment_name} "
            f"({state.state}), so there is no client to make an administrator. Run "
            "`register-service-client` first — it performs this grant itself.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("command", "pixi run globus setup-status"),
            detail={"parameter_state": state.state},
        )
    return (state.value or "").strip()


def _require_matching_client_id(ctx: Context) -> str:
    """The operator's `--client-id` must be the one this deployment records.

    `cleanup_endpoint._require_matching_endpoint_id`'s guard, applied to the other
    identifier, and for the identical reason: the way this command gets run by
    mistake is an answers document pointing at a deployment the operator did not
    mean, so every guard derived from that document is answered by the very thing
    that is wrong. The parameter is read **with decryption** because Terraform made
    it a SecureString; without that the comparison is against ciphertext and can
    only ever fail.
    """
    declared = (getattr(ctx.args, "client_id", None) or "").strip()
    param = ctx.env.service_client_id_param
    state = ctx.aws.parameter_state(param, decrypt=True)
    if not state.ok:
        raise CliError(
            f"{CODE_PREFIX}.no_client",
            f"{param} records no service client for {ctx.config.deployment_name} "
            f"({state.state}), so there is nothing here to delete. A client this deployment "
            "has lost track of has to be deleted in the Globus console — this command only "
            "deletes what the deployment records.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("command", "pixi run globus setup-status"),
            detail={"parameter_state": state.state},
        )

    recorded = (state.value or "").strip()
    if declared != recorded:
        raise CliError(
            f"{CODE_PREFIX}.client_id_mismatch",
            f"--client-id names {declared!r}, but {ctx.config.deployment_name} records a "
            "different client. Nothing was deleted. Either the answers document points at "
            "another deployment than you meant, or the client id is not the one you believe "
            "it is — and the two are worth telling apart before deleting a credential.",
            exit_code=ExitCode.INVALID,
            remedy=Remedy("command", "pixi run globus setup-status"),
            # The recorded value is not echoed: an operator who mistyped learns
            # nothing from it, and an operator in the wrong deployment should not
            # be handed the right answer to a guard that just caught them.
            detail={"declared": declared},
        )
    return recorded


def _refuse_while_endpoint_exists(ctx: Context, client_id: str) -> None:
    """An endpoint this client owns outranks any reason to delete the client.

    Deleting it would leave an endpoint — subscribed, billable, serving — that no
    credential in this deployment can manage: the reconcile authenticates as this
    client, and GCS has no way to hand an existing endpoint to a new one. So the
    order is fixed, and it is the reverse of creation: `cleanup-endpoint` first.
    """
    state = ctx.aws.parameter_state(ctx.env.endpoint_id_param)
    if not state.ok:
        return
    raise CliError(
        f"{CODE_PREFIX}.endpoint_in_service",
        f"{ctx.env.endpoint_id_param} records endpoint {state.value}, which this client "
        "owns and which nothing else can manage. Deleting the client would leave that "
        "endpoint unmanageable — it could not even be deleted afterwards. Run "
        "`globus cleanup-endpoint` first.",
        exit_code=ExitCode.CHECK_FAILED,
        remedy=Remedy("command", f"pixi run globus cleanup-endpoint --endpoint-id {state.value}"),
        detail={"endpoint_id": state.value, "client_id": client_id},
    )
