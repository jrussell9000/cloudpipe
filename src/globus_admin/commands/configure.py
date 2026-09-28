"""`globus configure` — make the Globus endpoint match the declared configuration.

The work itself happens on the GCS instance, in `images/globus-gcs/reconcile`:
only the instance holds the service credentials and can talk to the endpoint's
management API. This command is the operator's end of that — it starts the
instance if it has to, runs the reconcile in `plan` mode through SSM, shows what
would change, and runs it again in `apply` mode once a human has agreed.

Three decisions shape it:

* **Plan, then apply, always.** Never one call that decides for itself. The plan
  is a separate remote run whose output is shown in full, because an operator
  agreeing to "apply the configuration" without seeing it is not agreeing to
  anything in particular.
* **The scope is the environment's gateway, passed to the reconcile.** A staging
  run cannot touch production even if production were declared `managed: true`,
  because the narrowing happens on the instance rather than here.
* **Only an instance this command started is offered to be stopped.** One it
  found running may be running for someone else — a batch, another operator —
  and stopping it would kill their transfers.

It also records the environment's collection id in SSM, where the pipeline,
`doctor` and `cleanup-endpoint`'s in-service guard all read it — but only over
Terraform's placeholder. See `_record_collection_id` for why it never overwrites.
"""

from __future__ import annotations

import json
from typing import Any

from .. import cluster, guards, instance, remote
from ..aws import MISSING
from ..cli import Context, register
from ..exits import CliError, ExitCode

PLAN = "plan"
APPLY = "apply"

#: The plan's key for collections, as `reconcile/planner.py` names it.
COLLECTION = "collection"

#: What `_record_collection_id` did, in `data["collection_id"]["outcome"]`.
RECORDED = "recorded"
WOULD_RECORD = "would_record"
UNCHANGED = "unchanged"
CONFLICT = "conflict"
NOT_LIVE = "not_live"
NO_PARAMETER = "no_parameter"

#: How the SSM document is named in `terraform/modules/globus/reconcile.tf`.
DOCUMENT_SUFFIX = "-globus-reconcile"

#: This command's half of the error identifiers `globus_admin.instance` raises.
CODE_PREFIX = "configure"


@register("configure")
def configure(ctx: Context) -> ExitCode:
    plan_only = bool(ctx.args.plan_only)
    ctx.require_prereqs(mutating=not plan_only)

    instance_id = instance.require_id(ctx, code_prefix=CODE_PREFIX)
    document = f"{ctx.config.deployment_name}{DOCUMENT_SUFFIX}"

    data: dict[str, Any] = {
        "environment": ctx.env.name,
        "gateway": ctx.env.gateway_name,
        "instance_id": instance_id,
        "document": document,
        "started_instance": False,
        "plan": None,
        "applied": False,
        "collection_id": None,
        "stopped_instance": False,
    }

    started = instance.ensure_running(ctx, instance_id, code_prefix=CODE_PREFIX, await_gridftp=True)
    data["started_instance"] = started

    failure: CliError | None = None
    try:
        exit_code = _plan_and_apply(ctx, instance_id, document, data)
    except CliError as err:
        failure, exit_code = err, err.exit_code

    # Before emitting, not after: the envelope has to say what happened to the
    # instance, and a `finally` would run after the result was already rendered.
    if started:
        data["stopped_instance"] = instance.offer_stop(ctx, instance_id)

    if failure is not None:
        raise failure
    return ctx.emitter.result(data, exit_code=exit_code)


def _plan_and_apply(
    ctx: Context, instance_id: str, document: str, data: dict[str, Any]
) -> ExitCode:
    emitter = ctx.emitter

    plan_run = _run(ctx, instance_id, document, PLAN, stream_output=False)
    plan = _parse(plan_run.invocation.stdout)
    data["plan"] = plan
    if plan is not None:
        _render(emitter, plan)
    else:
        # Not the JSON plan that was asked for, so there is nothing to render.
        # The raw output is shown verbatim instead — suppressing the stream and
        # then showing nothing would leave the operator with no way to see why.
        for line in plan_run.invocation.stdout.splitlines():
            emitter.note(f"  {line}")

    if not plan_run.ok:
        emitter.note("the plan did not complete; nothing was applied")
        return plan_run.exit_code

    if ctx.args.plan_only:
        _record_collection_id(ctx, plan, data, write=False)
        return ExitCode.OK

    if plan is not None and not plan.get("actions"):
        if _has_suppressed_drift(plan):
            emitter.note(
                "nothing to apply, but the endpoint does not match the configuration; "
                "the notes above say what differs"
            )
        else:
            emitter.note("nothing to apply: the endpoint already matches the configuration")
        _record_collection_id(ctx, plan, data, write=True)
        return ExitCode.OK

    if plan is None:
        # The reconcile's output was not the JSON plan it was asked for. Applying
        # is still allowed — the operator has seen the raw output, which is the
        # thing they are agreeing to — but it must not look like it was read.
        emitter.note("could not read the plan as JSON; the output above is all there is")

    guards.confirm(
        emitter,
        action=f"apply the configuration to {ctx.env.gateway_name} ({ctx.env.name})",
        activity=guards.workflow_activity(cluster.probe(ctx.runner), ctx.runner),
        non_interactive=ctx.non_interactive,
        assume_yes=ctx.assume_yes,
    )

    apply_run = _run(ctx, instance_id, document, APPLY)
    data["applied"] = apply_run.ok
    if apply_run.ok:
        # The plan's ids are still true after an apply, because an apply never
        # deletes. A collection the apply itself created is not among them; the
        # next run records it.
        _record_collection_id(ctx, plan, data, write=True)
    return apply_run.exit_code


def _record_collection_id(
    ctx: Context, plan: dict[str, Any] | None, data: dict[str, Any], *, write: bool
) -> None:
    """Record the live collection's id in SSM, if the parameter is still unset.

    Only ever over the placeholder. A parameter that already holds a different id
    is reported, never overwritten: the pipeline writes wherever it points, so a
    silent overwrite would move every future transfer to another collection on
    the strength of a display-name match. And a parameter that does not exist is
    left alone too — Terraform creates it, and one created here would be a
    parameter Terraform does not know about.

    No confirmation is asked. It changes nothing in Globus, and it only fills in
    a value that is already true; the confirmation `configure` asks for is about
    changing the endpoint.
    """
    param = ctx.env.collection_id_param
    ids = ((plan or {}).get("live_ids") or {}).get(COLLECTION) or {}
    live_id = ids.get(ctx.env.collection_name)
    record: dict[str, Any] = {"parameter": param, "live": live_id, "outcome": NOT_LIVE}
    data["collection_id"] = record
    note = ctx.emitter.note

    if not live_id:
        # Includes a plan that could not be read: no id is known, so none is written.
        return

    state = ctx.aws.parameter_state(param)
    if state.ok:
        record["recorded"] = state.value
        if state.value == live_id:
            record["outcome"] = UNCHANGED
            return
        record["outcome"] = CONFLICT
        note(
            f"{param} records {state.value}, but the live collection "
            f"{ctx.env.collection_name!r} is {live_id}. Left as it is: transfers go "
            "wherever that parameter points, so changing it is a decision for a person."
        )
        return
    if state.state == MISSING:
        record["outcome"] = NO_PARAMETER
        note(f"{param} does not exist; run `terraform apply -target=module.globus` to create it")
        return
    if not write:
        record["outcome"] = WOULD_RECORD
        note(f"would record the collection id {live_id} in {param}")
        return
    ctx.aws.put_parameter(param, live_id)
    record["outcome"] = RECORDED
    note(f"recorded the collection id {live_id} in {param}")


def _run(
    ctx: Context, instance_id: str, document: str, mode: str, *, stream_output: bool = True
) -> remote.RemoteRun:
    ctx.emitter.note(f"running the reconcile in {mode} mode on {instance_id}")
    return remote.run_document(
        ctx.aws,
        document_name=document,
        instance_id=instance_id,
        parameters={
            "Mode": mode,
            "Gateway": ctx.env.gateway_name,
            # The plan comes back as JSON and is rendered here: that way one run
            # can both be read by a person and be returned in the envelope, and
            # the decision "is there anything to apply" rests on parsed data
            # rather than on matching a sentence in some prose.
            #
            # The apply comes back as text, because there is nothing to decide
            # from it — it is progress, and progress is for reading.
            "Format": "json" if mode == PLAN else "text",
        },
        emitter=ctx.emitter,
        comment=f"globus configure {mode} {ctx.env.name}",
        stream_output=stream_output,
        # Through the module rather than imported by name, so the one seam in
        # `instance` covers the polling here as well as the instance waits.
        sleep=instance._sleep,
        clock=instance._clock,
    )


def _parse(stdout: str) -> dict[str, Any] | None:
    """The plan, if the remote run produced one. `None` covers every other case."""
    try:
        parsed = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) and "actions" in parsed else None


def _render(emitter: Any, plan: dict[str, Any]) -> None:
    """The plan as lines a person reads, from the structured form.

    Problems first: they decide whether anything can happen at all, and an
    operator who reads the action list first has already started thinking about
    the wrong question.
    """
    for problem in plan.get("problems") or []:
        emitter.note(
            f"  BLOCKED  {problem.get('object_type')} {problem.get('name')!r}: "
            f"{problem.get('field')} is {problem.get('live')!r} live but "
            f"{problem.get('declared')!r} in the configuration, and Globus cannot change it"
        )
    for action in plan.get("actions") or []:
        fields = action.get("fields") or {}
        changes = ", ".join(
            f"{key}: {value.get('live')!r} -> {value.get('declared')!r}"
            for key, value in sorted(fields.items())
        )
        suffix = f" ({changes})" if changes else ""
        emitter.note(
            f"  {action.get('kind', '').upper():7} {action.get('object_type')} "
            f"{action.get('name')!r}{suffix}"
        )
    for note in plan.get("notes") or []:
        emitter.note(
            f"  note     {note.get('object_type')} {note.get('name')!r}: {note.get('reason')}"
        )
    if not (plan.get("problems") or plan.get("actions")):
        if _has_suppressed_drift(plan):
            emitter.note(
                "  no actions to take, but the endpoint does NOT match the configuration "
                "— see the notes above for what differs and was left alone"
            )
        else:
            emitter.note("  no changes — the endpoint matches the configuration")


def _has_suppressed_drift(plan: dict[str, Any]) -> bool:
    """Whether the plan found differences it deliberately will not act on.

    That is what a note is: an object declared `managed: false` whose live value
    differs, or a field the endpoint does not report and so cannot be checked.
    Both mean the endpoint does not match the configuration even though there is
    nothing to apply, so "nothing to do" and "already matches" are not the same
    statement and must not share a message.

    The planner is explicit about why it reports these rather than staying quiet —
    silence "would make a read-only declaration look like an unchanged endpoint" —
    and claiming a match here would put that silence back one layer up, where the
    operator actually reads it. Production is declared `managed: false`, so a plan
    of notes and nothing else is the ordinary case, not an edge one.
    """
    return bool(plan.get("notes"))
