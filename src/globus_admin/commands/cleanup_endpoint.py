"""`globus cleanup-endpoint` — delete this deployment's Globus endpoint.

The inverse of `bootstrap-endpoint`, and the only command in this package that
destroys something no `terraform apply` can put back. It exists because task 10.6
bootstraps a real endpoint under a throwaway service client to prove the
bootstrap path works, and a test that cannot clean up after itself leaves an
endpoint counting against a subscription forever.

`bootstrap-endpoint` refuses in order to protect an endpoint that already exists.
This refuses in order to protect an endpoint that is *in use*, which is a harder
thing to establish, and the guards are built so that none of them trusts the
answers document alone:

* **The operator has to name the endpoint.** `--endpoint-id` is required and must
  equal what `endpoint_id_param` holds. Guards derived from the answers document
  — "refuse the production deployment name", "refuse unless `--env` says test" —
  all fail the same way: the answers document is the thing most likely to be
  wrong, because pointing at the wrong one is *how* this command gets run by
  mistake. Typing a UUID cannot be done by accident.
* **A recorded collection stops it.** A collection id under either environment's
  prefix is evidence that something has been built on this endpoint and that
  transfers may depend on it. That evidence comes from the deployment's own SSM
  parameters, not from a file on the operator's laptop.
* **And then it asks**, with running-workflow activity in the prompt, exactly as
  `configure` does before an apply.

**The service client is spent afterwards.** GCS does not allow the client id that
created an endpoint to create another one, so this is not reversible even by
re-running `bootstrap-endpoint`: a re-bootstrap needs a *new* service client,
which is `delete-service-client` followed by `register-service-client`. Both are
commands rather than a portal visit, but that makes the recovery scriptable rather
than cheap — it still destroys a credential and issues another, under a second
`manage_projects` login. That is the reason for `--endpoint-id` rather than a
`--force` flag: a flag would be one character of protection in front of it.
"""

from __future__ import annotations

import json
from typing import Any

from .. import cluster, environments, guards, instance, remote
from ..cli import Context, register
from ..exits import CliError, ExitCode, Remedy

#: How the SSM document is named in `terraform/modules/globus/teardown.tf`.
DOCUMENT_SUFFIX = "-globus-teardown"

#: This command's half of the error identifiers `globus_admin.instance` raises.
CODE_PREFIX = "cleanup_endpoint"

#: Longer than the document's own 600s, so SSM's specific "TimedOut" wins over
#: `remote.run_document`'s generic "it may still be running". Shorter than
#: `bootstrap-endpoint`'s because nothing here waits on a certificate.
REMOTE_TIMEOUT = 1200.0


@register("cleanup-endpoint")
def cleanup_endpoint(ctx: Context) -> ExitCode:
    _refuse_staging(ctx)
    ctx.require_prereqs(mutating=True)
    endpoint_id = _require_matching_endpoint_id(ctx)
    _refuse_live_collections(ctx)

    instance_id = instance.require_id(ctx, code_prefix=CODE_PREFIX)
    document = f"{ctx.config.deployment_name}{DOCUMENT_SUFFIX}"

    data: dict[str, Any] = {
        "environment": ctx.env.name,
        "endpoint_id": endpoint_id,
        "instance_id": instance_id,
        "document": document,
        "started_instance": False,
        "report": None,
        "deleted": False,
        "stopped_instance": False,
    }

    # Before the instance is started, as in `bootstrap-endpoint`: there is nothing
    # to show the operator first, so booting a host before asking buys nothing.
    guards.confirm(
        ctx.emitter,
        action=(
            f"permanently delete the Globus endpoint {endpoint_id} "
            f"({ctx.env.gateway_name}, {ctx.config.deployment_name}). This cannot be undone, "
            "and the service client that created it can never create another one — a "
            "re-bootstrap would need a new service client, which means deleting this "
            "deployment's credential and issuing another"
        ),
        activity=guards.workflow_activity(cluster.probe(ctx.runner), ctx.runner),
        non_interactive=ctx.non_interactive,
        assume_yes=ctx.assume_yes,
    )

    started = instance.ensure_running(ctx, instance_id, code_prefix=CODE_PREFIX)
    data["started_instance"] = started

    failure: CliError | None = None
    try:
        exit_code = _teardown(ctx, instance_id, document, endpoint_id, data)
    except CliError as err:
        failure, exit_code = err, err.exit_code

    if started:
        data["stopped_instance"] = instance.offer_stop(ctx, instance_id)

    if failure is not None:
        raise failure
    return ctx.emitter.result(data, exit_code=exit_code)


def _teardown(
    ctx: Context, instance_id: str, document: str, endpoint_id: str, data: dict[str, Any]
) -> ExitCode:
    ctx.emitter.note(f"deleting endpoint {endpoint_id} from {instance_id}")
    run = remote.run_document(
        ctx.aws,
        document_name=document,
        instance_id=instance_id,
        parameters={"ExpectEndpointId": endpoint_id},
        emitter=ctx.emitter,
        comment=f"globus cleanup-endpoint {ctx.config.deployment_name}",
        # Streamed: the only structured output is one JSON line at the end, and
        # the rest is the GCS CLI saying what it removed, which is what an
        # operator watching a deletion wants to see.
        stream_output=True,
        timeout=REMOTE_TIMEOUT,
        sleep=instance._sleep,
        clock=instance._clock,
    )

    report = _parse(run.invocation.stdout)
    data["report"] = report

    if not run.ok:
        # Deliberately not "nothing was deleted". The instance refuses before
        # touching anything when its own checks fail, but a failure *after*
        # `endpoint cleanup` succeeded is also possible — the parameter resets
        # come last — and this command cannot tell those apart from the exit code.
        ctx.emitter.note(
            "the teardown did not complete; check the output above for whether the endpoint "
            "was deleted before it stopped"
        )
        return run.exit_code

    if report is None:
        raise CliError(
            f"{CODE_PREFIX}.no_report",
            "The teardown reported success but produced no JSON report, so whether the "
            "endpoint was deleted cannot be established from this run.",
            exit_code=ExitCode.ERROR,
            remedy=Remedy("command", "pixi run globus setup-status"),
            detail={"stdout_tail": run.invocation.stdout[-2000:]},
        )

    data["deleted"] = True
    _present(ctx, report)
    return ExitCode.OK


def _parse(stdout: str) -> dict[str, Any] | None:
    """The report, found by scanning from the end.

    Same shape as `bootstrap_endpoint._parse` and for the same reason: SSM
    aggregates stdout, and the GCS CLI writes prose around the one JSON line.
    Keyed on `parameters_reset` rather than `endpoint_id`, because the bootstrap
    report also carries an `endpoint_id` and the two must not be confused if a
    transcript ever contains both.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "parameters_reset" in parsed:
            return parsed
    return None


def _present(ctx: Context, report: dict[str, Any]) -> None:
    """What was deleted, and the two things that are now stale because of it."""
    emitter = ctx.emitter
    emitter.note(f"endpoint {report.get('endpoint_id')} deleted")

    node = report.get("node_cleanup") or {}
    if not node.get("ok"):
        # Not a failure of this command — the endpoint is gone, which is what was
        # asked — but the node record may survive it, and whoever holds the
        # subscription is the only one who can see that.
        emitter.note(
            f"note: `node cleanup` exited {node.get('exit_status')}, so a node record may "
            "remain on the Globus side. It cannot be removed now that the endpoint is gone; "
            "mention it if the subscription holder reports a stale node."
        )

    reset = ", ".join(report.get("parameters_reset") or [])
    emitter.note(f"reset to the placeholder: {reset}")
    emitter.note("")
    emitter.note(
        "The GCS instance still holds local configuration for the deleted endpoint, so its "
        "boot unit will fail to register on the next start. Destroy it rather than reusing "
        "it, and register a new Globus service client before any further bootstrap — the one "
        "that created this endpoint cannot create another."
    )
    emitter.note(
        "  pixi run globus delete-service-client --client-id <the recorded id>, then "
        "pixi run globus register-service-client"
    )


# --- the refusals -----------------------------------------------------------


def _refuse_staging(ctx: Context) -> None:
    """There is no separate staging endpoint, so `--env staging` names nothing.

    `endpoint_id_param` comes from `shared_ssm_prefix`: both environments name the
    same parameter. A staging cleanup would therefore delete the endpoint staging
    shares with production, which is the opposite of what asking for staging
    means.
    """
    if ctx.env.name == environments.PRODUCTION:
        return
    raise CliError(
        f"{CODE_PREFIX}.staging_shares_the_endpoint",
        f"There is no separate {ctx.env.name} endpoint to delete: {ctx.env.name} shares one "
        "endpoint with production and differs only in its gateway and collection. Deleting "
        f"it would delete production's endpoint too. To remove just the {ctx.env.name} "
        "gateway, take it out of the configuration document and run `globus configure`.",
        exit_code=ExitCode.INVALID,
        detail={"endpoint_id_param": ctx.env.endpoint_id_param},
    )


def _require_matching_endpoint_id(ctx: Context) -> str:
    """The operator's `--endpoint-id` must be the one this deployment records.

    The guard that does the real work. Every cheaper alternative — refusing a
    deployment name, an `--env`, a hard-coded production UUID — reads its answer
    out of the answers document, and the answers document is exactly what is wrong
    in the scenario worth defending against: an operator in the wrong directory,
    or with `GLOBUS_ANSWERS` still set from earlier, running a command they have
    run successfully before against a test deployment. A required UUID makes that
    mistake impossible to make silently, because production's UUID is not the one
    they would have typed.
    """
    declared = (getattr(ctx.args, "endpoint_id", None) or "").strip()
    state = ctx.aws.parameter_state(ctx.env.endpoint_id_param)
    if not state.ok:
        raise CliError(
            f"{CODE_PREFIX}.no_endpoint",
            f"{ctx.env.endpoint_id_param} records no endpoint for {ctx.config.deployment_name} "
            f"({state.state}), so there is nothing here to delete. If an endpoint exists that "
            "this deployment has lost track of, it has to be deleted through the Globus web "
            "app — this command only deletes what the deployment records.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("command", "pixi run globus setup-status"),
            detail={"parameter_state": state.state},
        )

    recorded = (state.value or "").strip()
    if declared != recorded:
        raise CliError(
            f"{CODE_PREFIX}.endpoint_id_mismatch",
            f"--endpoint-id names {declared!r}, but {ctx.config.deployment_name} records "
            f"{recorded!r}. Nothing was deleted. Either the answers document points at a "
            "different deployment than you meant, or the endpoint id is not the one you "
            "believe it is — and the two are worth telling apart before deleting anything.",
            exit_code=ExitCode.INVALID,
            remedy=Remedy("command", "pixi run globus setup-status"),
            detail={"declared": declared, "recorded": recorded},
        )
    return recorded


def _refuse_live_collections(ctx: Context) -> None:
    """A recorded collection means something has been built on this endpoint.

    Read from the deployment's parameters rather than from the answers document,
    which is the point: this is evidence, not a declaration. Both environments are
    checked because they share the endpoint — a deployment whose staging
    collection exists while production's does not is a perfectly ordinary
    half-built state, and deleting the endpoint under it would take staging with
    it.

    The endpoint 10.6 creates never reaches this state: its High Assurance
    gateway create is expected to fail for want of a subscription, so no
    collection is ever recorded, so a test endpoint is always deletable while one
    that carries a collection never is. That asymmetry is doing the work — the
    check needs no flag to distinguish the two cases.
    """
    for env_name in environments.ENV_NAMES:
        env = environments.resolve(
            env_name,
            deployment_name=ctx.config.deployment_name,
            collection_name=ctx.config.collection_name,
        )
        state = ctx.aws.parameter_state(env.collection_id_param)
        if not state.ok:
            continue
        raise CliError(
            f"{CODE_PREFIX}.collection_in_service",
            f"{env.collection_id_param} records collection {state.value}, so this endpoint "
            f"carries a {env_name} collection that transfers may be using. Deleting the "
            "endpoint would delete the collection with it, and neither can be recreated "
            "under the same service client. Remove the collection through `globus configure` "
            "first if this endpoint really is meant to go.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("command", "pixi run globus status"),
            detail={"environment": env_name, "collection_id": state.value},
        )
