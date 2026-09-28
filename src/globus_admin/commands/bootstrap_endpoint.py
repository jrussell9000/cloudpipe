"""`globus bootstrap-endpoint` — create this deployment's Globus endpoint, once.

The operator's end of `images/globus-gcs/reconcile/bootstrap.py`. That module
does the work, on the instance, because only the instance may read the service
client's secret; this decides whether the work should be attempted at all, and
presents what came back.

The asymmetry with `configure` is the thing to understand. `configure` is
idempotent — it plans, shows the plan, applies, and running it twice changes
nothing the second time. This is not idempotent and cannot be made so: a second
`endpoint setup` creates a *second* endpoint, leaves the first orphaned (and
still subscribed, and still billable), and overwrites the only copy of the
first's deployment key. There is no plan to show either, because until the
endpoint exists there is nothing to compare against. So the whole shape of this
command is *refusals before the fact*:

* not staging, because both environments share one endpoint id parameter;
* not when that parameter already holds something;
* not when the answers document points the service-client secret somewhere the
  instance cannot read.

Each refusal is checked before the instance is started, so an operator who has
the wrong deployment selected pays nothing for finding out.

`bootstrap.py` re-checks occupancy on the instance, and that is not redundant:
this check happens before a confirmation is answered and an instance is booted,
which is minutes during which another operator can have run the same command.
Here the check is for the *message* — a refusal that names the endpoint it found,
without having started anything.
"""

from __future__ import annotations

import json
from typing import Any

from .. import cluster, gates, guards, instance, remote
from ..cli import Context, register
from ..environments import PRODUCTION
from ..exits import CliError, ExitCode, Remedy

#: How the SSM document is named in `terraform/modules/globus/bootstrap.tf`.
DOCUMENT_SUFFIX = "-globus-bootstrap"

#: This command's half of the error identifiers `globus_admin.instance` raises.
CODE_PREFIX = "bootstrap_endpoint"

#: The only scheme `service_client_secret_ref` may use for this command. The
#: bootstrap document reads the secret from SSM on the instance, exactly as
#: `reconcile.tf` does, so a reference naming any other store describes a secret
#: nothing involved here can reach.
SSM_SCHEME = "ssm:"

#: Longer than the document's own 1500s + 900s, so SSM's specific "TimedOut"
#: wins over `remote.run_document`'s generic "it may still be running".
#: `endpoint setup` provisions a Let's Encrypt certificate, which is the slow part.
REMOTE_TIMEOUT = 2700.0

#: The gate whose request text this command prints. Nothing else prints it today:
#: the endpoint has to exist before anyone can be asked to subscribe it, and this
#: is the command that makes it exist.
GATE_ID = "endpoint_subscription"


@register("bootstrap-endpoint")
def bootstrap_endpoint(ctx: Context) -> ExitCode:
    _refuse_staging(ctx)
    ctx.require_prereqs(mutating=True)
    secret_param = _require_readable_secret_ref(ctx)
    _refuse_existing_endpoint(ctx)

    instance_id = instance.require_id(ctx, code_prefix=CODE_PREFIX)
    document = f"{ctx.config.deployment_name}{DOCUMENT_SUFFIX}"

    data: dict[str, Any] = {
        "environment": ctx.env.name,
        "endpoint_name": ctx.env.gateway_name,
        "instance_id": instance_id,
        "document": document,
        "service_client_secret_param": secret_param,
        "started_instance": False,
        "report": None,
        "endpoint_id": None,
        "stopped_instance": False,
    }

    # Confirmed before the instance is started, unlike `configure`: there is no
    # plan for the operator to read first, so nothing is gained by booting a host
    # before asking, and a "no" then costs a start and a stop.
    guards.confirm(
        ctx.emitter,
        action=(
            f"create the Globus endpoint {ctx.env.gateway_name!r} for {ctx.config.deployment_name} "
            "(this cannot be undone, and the service client that creates it can never create "
            "it again)"
        ),
        activity=guards.workflow_activity(cluster.probe(ctx.runner), ctx.runner),
        non_interactive=ctx.non_interactive,
        assume_yes=ctx.assume_yes,
    )

    started = instance.ensure_running(ctx, instance_id, code_prefix=CODE_PREFIX)
    data["started_instance"] = started

    failure: CliError | None = None
    try:
        exit_code = _bootstrap(ctx, instance_id, document, data)
    except CliError as err:
        failure, exit_code = err, err.exit_code

    if started:
        data["stopped_instance"] = instance.offer_stop(ctx, instance_id)

    if failure is not None:
        raise failure
    return ctx.emitter.result(data, exit_code=exit_code)


def _bootstrap(ctx: Context, instance_id: str, document: str, data: dict[str, Any]) -> ExitCode:
    ctx.emitter.note(f"creating the endpoint on {instance_id}; this takes a few minutes")
    run = remote.run_document(
        ctx.aws,
        document_name=document,
        instance_id=instance_id,
        parameters=_parameters(ctx),
        emitter=ctx.emitter,
        comment=f"globus bootstrap-endpoint {ctx.config.deployment_name}",
        # Streamed rather than suppressed. `configure` hides the plan's stdout
        # because it renders it instead; here the only structured thing is one
        # JSON line at the end, and the rest is progress an operator waiting
        # several minutes should be able to watch.
        stream_output=True,
        timeout=REMOTE_TIMEOUT,
        sleep=instance._sleep,
        clock=instance._clock,
    )

    report = _parse(run.invocation.stdout)
    data["report"] = report
    if report is not None:
        data["endpoint_id"] = report.get("endpoint_id")

    if not run.ok:
        ctx.emitter.note("the endpoint was not created")
        return run.exit_code

    if report is None:
        # Success without a report is not a success to act on: the endpoint may
        # exist, and the parameters may hold it, but this command cannot say so
        # and must not print a subscription request naming an endpoint it did not
        # read. Verifiable with `globus setup-status`, which reads the parameter.
        raise CliError(
            f"{CODE_PREFIX}.no_report",
            "The bootstrap reported success but produced no JSON report, so which endpoint "
            "was created — if one was — cannot be established from this run.",
            exit_code=ExitCode.ERROR,
            remedy=Remedy("command", "pixi run globus setup-status"),
            detail={"stdout_tail": run.invocation.stdout[-2000:]},
        )

    _present(ctx, report)
    return ExitCode.OK


def _parameters(ctx: Context) -> dict[str, str]:
    """The document's parameters. `ProjectId` is empty unless one was given.

    Empty means the flag is omitted rather than passed blank — whether a service
    client needs `--project-id` at all is settled by task 10.6, and optional is
    the shape that survives either answer.
    """
    return {"ProjectId": ctx.args.project_id or ""}


def _parse(stdout: str) -> dict[str, Any] | None:
    """The report, found by scanning rather than by parsing the whole stream.

    SSM aggregates output across a document's steps, and this document has two —
    the report is one line somewhere in the middle, followed by whatever
    `systemctl` had to say. Scanned from the end so the newest run wins if a
    retry ever put two in one buffer.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "endpoint_id" in parsed:
            return parsed
    return None


def _present(ctx: Context, report: dict[str, Any]) -> None:
    """What was created, then what has to happen next and who has to do it."""
    emitter = ctx.emitter
    endpoint_id = report.get("endpoint_id")
    emitter.note(f"endpoint {endpoint_id} created and recorded in {ctx.env.endpoint_id_param}")
    emitter.note(
        f"deployment key stored in {report.get('deployment_key_param')} "
        f"({report.get('deployment_key_bytes')} bytes, encrypted)"
    )
    # Where the UUID came from, because until 10.6 neither source is a contract:
    # the field GCS writes it under has moved between versions, and the other
    # source is a sentence it printed.
    emitter.note(f"endpoint id read from {report.get('endpoint_id_source')}")

    gate = gates.gate(GATE_ID)
    request = gate.request(**_request_context(ctx, endpoint_id)) if gate else None
    if request is None:  # pragma: no cover - the gate is declared in gates.py
        return

    emitter.note("")
    emitter.note(
        "The endpoint is NOT yet covered by a Globus subscription. Until it is, creating "
        "the High Assurance storage gateway fails with a subscription error — which reads "
        "like a permissions problem and is not one. Send this:"
    )
    emitter.note("")
    for line in request.splitlines():
        emitter.note(f"  {line}")


def _request_context(ctx: Context, endpoint_id: Any) -> dict[str, Any]:
    """The values the subscription request interpolates.

    The same field set `setup_status._request_context` builds, and for the same
    reason: an operator who runs `setup-status` afterwards must be shown the same
    message, not a second draft of it. A value that is not known is left out
    rather than blanked, so `Gate.request` leaves the `{placeholder}` visible
    instead of producing a confident-looking message with a hole in it.
    """
    context: dict[str, Any] = {
        "deployment_name": ctx.config.deployment_name,
        "endpoint_name": ctx.env.gateway_name,
    }
    for key, value in (
        ("endpoint_id", endpoint_id),
        ("contact_email", ctx.config.contact_email),
        ("owner_email", ctx.config.owner_email),
    ):
        if value:
            context[key] = value
    return context


# --- the refusals -----------------------------------------------------------


def _refuse_staging(ctx: Context) -> None:
    """Staging cannot have its own endpoint, so it cannot bootstrap one.

    `endpoint_id_param` is built from `shared_ssm_prefix`: staging and production
    name the same parameter, because staging shares the production endpoint and
    differs only in its gateway, collection, credential and token. A staging
    bootstrap would therefore either be refused for production's endpoint or, on
    a fresh deployment, create production's endpoint under a staging-looking
    command — and the second is worse than a failure.
    """
    if ctx.env.name == PRODUCTION:
        return
    raise CliError(
        f"{CODE_PREFIX}.staging_shares_the_endpoint",
        f"There is no separate {ctx.env.name} endpoint to create: {ctx.env.name} shares the "
        f"production endpoint and only its gateway and collection differ, which "
        "`globus configure --env staging` creates.",
        exit_code=ExitCode.INVALID,
        remedy=Remedy("command", "pixi run globus bootstrap-endpoint  (without --env)"),
        detail={"endpoint_id_param": ctx.env.endpoint_id_param},
    )


def _require_readable_secret_ref(ctx: Context) -> str:
    """The answers document must point at the SSM parameter the instance reads.

    Not a preference. `endpoint setup` runs on the instance under the service
    client's credentials, and the bootstrap document fetches them from SSM the
    way `reconcile.tf` already does. The instance's role is confined to this
    deployment's prefix by an explicit Deny, so a `secretsmanager:` or `env:`
    reference does not name a harder path — it names one that cannot work, and
    the failure would arrive as a Globus authentication error several minutes
    into a remote run.

    Returns the parameter name, so the envelope records which secret was assumed.
    """
    expected = ctx.env.service_client_secret_param
    ref = ctx.config.service_client_secret_ref
    if ref is None:
        raise CliError(
            f"{CODE_PREFIX}.no_secret_ref",
            "The answers document does not say where the Globus service client's secret "
            f"lives, so there is no way to confirm the instance can read it. Set "
            f"`service_client_secret_ref: {SSM_SCHEME}{expected}`.",
            exit_code=ExitCode.INVALID,
            remedy=Remedy("human", f"add service_client_secret_ref: {SSM_SCHEME}{expected}"),
        )

    if not ref.startswith(SSM_SCHEME):
        scheme = ref.split(":", 1)[0]
        raise CliError(
            f"{CODE_PREFIX}.secret_ref_not_ssm",
            f"`service_client_secret_ref` names a {scheme!r} secret, but the bootstrap runs "
            "on the GCS instance and reads the secret from SSM Parameter Store — the "
            "instance's role is confined to this deployment's SSM prefix and can reach "
            f"nothing else. Expected {SSM_SCHEME}{expected}.",
            exit_code=ExitCode.INVALID,
            remedy=Remedy("human", f"set service_client_secret_ref: {SSM_SCHEME}{expected}"),
            detail={"expected": f"{SSM_SCHEME}{expected}"},
        )

    named = ref[len(SSM_SCHEME) :]
    if named != expected:
        raise CliError(
            f"{CODE_PREFIX}.secret_ref_wrong_parameter",
            f"`service_client_secret_ref` names {named}, but the bootstrap document reads "
            f"{expected} — the name Terraform creates from this deployment's SSM prefix. "
            "Seeding the secret under a different name leaves the parameter the instance "
            "reads holding nothing.",
            exit_code=ExitCode.INVALID,
            remedy=Remedy("human", f"set service_client_secret_ref: {SSM_SCHEME}{expected}"),
            detail={"expected": f"{SSM_SCHEME}{expected}", "declared": named},
        )
    return expected


def _refuse_existing_endpoint(ctx: Context) -> None:
    """Refuse when the endpoint-id parameter holds anything but the placeholder.

    Occupancy, not shape, for the same reason `bootstrap.py` uses it: a value
    that is not a UUID is still evidence that something has been here, and
    reading "I cannot interpret this" as "nothing is there" is what would create
    a duplicate endpoint. `parameter_state` rather than `get_parameter` because
    the latter collapses the placeholder and a real value into one answer in the
    other direction — it returns None for the placeholder, which is right for a
    caller that wants a value and useless for one asking whether one exists.
    """
    state = ctx.aws.parameter_state(ctx.env.endpoint_id_param)
    if not state.ok:
        return
    raise CliError(
        f"{CODE_PREFIX}.endpoint_exists",
        f"{ctx.env.endpoint_id_param} already holds {state.value!r}, so this deployment has "
        "an endpoint. Creating another would orphan that one — still subscribed, still "
        "billable — and overwrite the only copy of its deployment key.",
        exit_code=ExitCode.CHECK_FAILED,
        remedy=Remedy("command", "pixi run globus setup-status"),
        detail={"endpoint_id": state.value},
    )
