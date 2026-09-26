"""`plan` and `apply` for the on-instance reconcile.

    python3 -m reconcile plan  --config /path/to/config.json
    python3 -m reconcile apply --config -          # read the document from stdin
    python3 -m reconcile plan  --config - --only cloudpipe-s3-staging

Exit codes match the operator CLI's vocabulary (`globus_admin.exits`) so a
wrapper can pass them straight through:

    0  nothing to do, or apply succeeded
    1  something unexpected failed
    3  a plan was produced but something blocks it (an immutable difference)
    4  the configuration document is invalid

`plan` exits 0 whether or not it found work: a plan is a report, and a non-zero
exit would make "there are changes to make" indistinguishable from "the check
failed" to any automation running it on a schedule.

With `--publish-to <ssm-parameter> --region <region>` the plan is also recorded in
SSM for `globus doctor` check 7 to read back later — see `plan_report`. That write
can never change the exit code: it is a diagnostic, and its reason goes to stderr
because stdout in `--json` mode is the plan, which `globus configure` parses whole.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import gcs, plan_report, planner

OK = 0
ERROR = 1
CHECK_FAILED = 3
INVALID = 4


def load_document(source: str) -> dict[str, Any]:
    text = sys.stdin.read() if source == "-" else Path(source).read_text()
    document = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError(f"the configuration must be a JSON object, not {type(document).__name__}")
    return document


def validate(document: dict[str, Any]) -> list[str]:
    """Validate with `globus_admin.configdoc` when it is importable.

    On the instance only this package is installed, so the import fails and the
    reconcile proceeds — the document came from SSM, where Terraform rendered it,
    and `globus doctor` already checks it from the operator's side. In the
    repository and in CI the import succeeds and the same rules apply here too.
    """
    try:
        from globus_admin import configdoc
    except ImportError:
        return []
    return configdoc.validate(document)


def narrow(document: dict[str, Any], gateway_name: str) -> dict[str, Any]:
    """The part of `document` that belongs to one storage gateway.

    `globus configure --env staging` must act on the staging gateway and nothing
    else. That scoping is applied here, on the instance, rather than by the
    caller: it is the last point before the changes happen, so a scoped run
    cannot reach production even if production is declared `managed: true`.

    Roles that name no collection are endpoint-wide rather than the gateway's, so
    a scoped run drops them. Granting one would be doing something outside the
    scope the operator asked for, which is exactly what scoping is meant to stop.
    """
    gateways = [
        gateway
        for gateway in document.get("storage_gateways") or []
        if isinstance(gateway, dict) and gateway.get("display_name") == gateway_name
    ]
    collections = [
        collection
        for collection in document.get("collections") or []
        if isinstance(collection, dict) and collection.get("gateway") == gateway_name
    ]
    kept = {collection.get("display_name") for collection in collections}
    roles = [
        role
        for role in document.get("roles") or []
        if isinstance(role, dict) and role.get("collection") in kept
    ]
    return {"storage_gateways": gateways, "collections": collections, "roles": roles}


def declared_gateways(document: dict[str, Any]) -> list[str]:
    return [
        str(gateway.get("display_name"))
        for gateway in document.get("storage_gateways") or []
        if isinstance(gateway, dict) and gateway.get("display_name")
    ]


def build_plan(document: dict[str, Any], *, runner=None) -> planner.Plan:
    live = gcs.read_live_state(**({"runner": runner} if runner else {}))
    return planner.plan(document, live)


def apply(plan: planner.Plan, *, document: dict[str, Any], runner, log=print) -> int:
    """Run the plan's actions. Refuses outright if anything is blocked.

    Refusing the WHOLE plan rather than skipping the blocked object is deliberate:
    the actions are not independent. A collection create that follows a gateway
    the operator has not resolved yet would fail halfway, leaving the endpoint in
    a state neither the document nor the previous run describes.

    `document` is here because a CREATE action carries only a name: `Action.fields`
    holds what DIFFERS, and nothing differs about an object that does not exist.
    The declared fields are looked up by name rather than copied into the action,
    so the published plan keeps reporting decisions instead of turning into an
    object dump.

    **Create-time flags are only ever those that have been run by hand first.**
    The gateway's are in the appendix of `docs/globus-setup.md` and in
    `retire-globus-s3-access-keys` 5.2; the collection's in 5.2a. A flag invented
    here would be baked into an AMI and first exercised against a live endpoint,
    which is the one rehearsal this deployment cannot do over.

    Order within a create matters and is the planner's, not ours: gateways are
    planned before collections, and a collection needs its gateway's id — which,
    for a gateway created moments ago, exists nowhere but that create's response.
    """
    if plan.blocked:
        for problem in plan.problems:
            log(f"ERROR  {problem.describe()}")
        log("Refusing to apply: resolve the differences above first. Nothing was changed.")
        return CHECK_FAILED

    declared_gateways = _declared(document, "storage_gateways")
    declared_collections = _declared(document, "collections")
    # Ids of gateways created during THIS apply, which no listing has seen yet.
    created_gateways: dict[str, str] = {}

    for action in plan.actions:
        log(f"APPLY  {action.describe()}")

        if action.object_type == planner.GATEWAY and action.kind == planner.UPDATE:
            fields = {key: value for key, (_, value) in action.fields.items()}
            gateway_id = _live_id(plan, planner.GATEWAY, action.name)
            gcs.run(gcs.gateway_update_command(gateway_id, fields), runner=runner)

        elif action.object_type == planner.COLLECTION and action.kind == planner.UPDATE:
            # Visibility drift, since 11.10. Without this branch a planned
            # collection update fell through to "not implemented" and printed
            # SKIP — which would have reported drift and then not fixed it,
            # the same shape of half-truth 11.10 exists to remove.
            fields = {key: value for key, (_, value) in action.fields.items()}
            collection_id = _live_id(plan, planner.COLLECTION, action.name)
            gcs.run(gcs.collection_update_command(collection_id, fields), runner=runner)

        elif action.object_type == planner.GATEWAY and action.kind == planner.CREATE:
            declared = _declared_object(declared_gateways, planner.GATEWAY, action.name)
            response = gcs.run(gcs.gateway_create_command(declared), runner=runner)
            new_id = gcs.created_id(response)
            if new_id:
                created_gateways[action.name] = new_id
            log(
                f"       created storage gateway {action.name!r}"
                + (f" as {new_id}" if new_id else "")
                + " — it has no credential until one is registered by hand"
            )

        elif action.object_type == planner.COLLECTION and action.kind == planner.CREATE:
            declared = _declared_object(declared_collections, planner.COLLECTION, action.name)
            gateway_name = str(declared.get("gateway") or "")
            gateway_id = created_gateways.get(gateway_name) or plan.live_ids.get(
                planner.GATEWAY, {}
            ).get(gateway_name)
            if not gateway_id:
                raise gcs.GcsError(
                    f"collection {action.name!r} names gateway {gateway_name!r}, whose id is "
                    f"neither live nor created by this run, so it cannot be created"
                )
            response = gcs.run(gcs.collection_create_command(gateway_id, declared), runner=runner)
            new_id = gcs.created_id(response)
            log(f"       created collection {action.name!r}" + (f" as {new_id}" if new_id else ""))

        else:
            # Roles are the remaining case. They are create-only and the CLI's
            # grant is a different shape again; left to an operator rather than
            # guessed at, and said out loud so a plan is not read as applied.
            log(f"SKIP   {action.describe()} — not implemented here; do this by hand")
    return OK


def _declared(document: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    return {
        obj["display_name"]: obj
        for obj in document.get(key) or []
        if isinstance(obj, dict) and isinstance(obj.get("display_name"), str)
    }


def _declared_object(
    declared: dict[str, dict[str, Any]], object_type: str, name: str
) -> dict[str, Any]:
    """The declaration a CREATE was planned from.

    A miss is an internal inconsistency — the planner only ever plans a create for
    a name it read out of this same document — so it raises rather than skipping.
    Skipping would report the action as applied and change nothing.
    """
    obj = declared.get(name)
    if obj is None:
        raise gcs.GcsError(
            f"{object_type} {name!r} was planned for creation but is not in the configuration"
        )
    return obj


def _live_id(plan: planner.Plan, object_type: str, name: str) -> str:
    """Globus's id for a declared object, which every `update` takes instead of a name.

    The planner matches on display names, but the CLI's `update` subcommands take
    UUIDs, so the apply has to translate — and `plan.live_ids` is already carrying
    exactly that, because the operator's side needs the collection id anyway.

    An UPDATE is only ever planned for an object the planner found live, so a
    missing id here is an internal inconsistency rather than anything the operator
    can fix. Raise instead of falling back to the name: the name is precisely what
    Globus rejects, and `main` turns this into a recorded "cannot say" rather than
    a silent no-op.
    """
    live_id = plan.live_ids.get(object_type, {}).get(name)
    if not live_id:
        raise gcs.GcsError(
            f"no live id was recorded for {object_type} {name!r}, so it cannot be updated"
        )
    return live_id


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


def settled(
    document: dict[str, Any], plan: planner.Plan, *, runner=None
) -> tuple[planner.Plan | None, str | None]:
    """The plan to PUBLISH after an apply, which is not the plan that was applied.

    The applied plan describes the endpoint as it was *before*, so recording it
    would leave `doctor` reporting drift the operator has just fixed — clearable
    only by running the very command they ran. Re-reading the live state costs one
    more round of listings on an action a person initiated deliberately, and it
    doubles as a convergence check: an apply that did not converge publishes what
    is left over, which is the honest answer.

    Nothing to re-read when nothing changed. A blocked plan is refused whole, and
    an empty one had no actions, so in both cases the plan in hand is already
    current.
    """
    if plan.blocked or not plan.actions:
        return plan, None
    try:
        return build_plan(document, runner=runner), None
    except gcs.GcsError as exc:
        return None, f"the apply ran, but the endpoint could not be re-read afterwards: {exc}"


def record(
    plan: planner.Plan | None,
    *,
    param: str | None,
    region: str | None,
    mode: str,
    gateway: str | None,
    unavailable: str | None = None,
    runner=None,
    host_id=plan_report.host_instance_id,
    err=_stderr,
) -> None:
    """Record the plan in SSM, when the caller asked for that. Never raises.

    Called on every path that reached an answer about the endpoint — *including*
    the path where the answer is "it could not be read". Skipping the write there
    would leave an OLDER plan in place for `doctor` to read as current, which is
    the trap task 10.5 recorded for the node report and is the same trap here.

    Not called when the configuration document itself is unreadable or invalid.
    That says nothing about the endpoint, and `doctor` check 3 already reports it;
    a second copy under a second heading is how a checklist stops being read.
    """
    if not param:
        return
    if not region:
        err("--publish-to needs --region; the plan was not recorded")
        return

    report = plan_report.build(
        plan,
        mode=mode,
        gateway=gateway,
        instance_id=host_id(),
        unavailable=unavailable,
    )
    reason = plan_report.publish(
        report, param=param, region=region, runner=runner or subprocess.run
    )
    err(
        f"the plan was not recorded in {param}: {reason}" if reason else f"plan recorded in {param}"
    )


def main(
    argv: list[str] | None = None,
    *,
    runner=None,
    log=print,
    err=_stderr,
    host_id=plan_report.host_instance_id,
) -> int:
    parser = argparse.ArgumentParser(prog="reconcile", description=__doc__)
    parser.add_argument("mode", choices=("plan", "apply"))
    parser.add_argument(
        "--config",
        required=True,
        help="path to the configuration document, or - for stdin",
    )
    parser.add_argument(
        "--only",
        metavar="GATEWAY",
        help="act on this storage gateway and its collections only",
    )
    parser.add_argument("--json", action="store_true", help="emit the plan as JSON")
    parser.add_argument(
        "--publish-to",
        metavar="PARAM",
        help="also record the plan in this SSM parameter, for `globus doctor` to read later",
    )
    parser.add_argument(
        "--region",
        help="AWS region for the --publish-to write (no default: the CLI's own would be a guess)",
    )
    parser.add_argument(
        "--wait-for-gcs",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "wait up to this long for GCS to start serving before reading the endpoint. "
            "For the boot-triggered association, which otherwise wins the race against "
            "cloudpipe-gcs-boot by seconds and publishes a cannot-compare. Zero by default: "
            "an operator running this by hand wants an endpoint that is down reported now."
        ),
    )
    args = parser.parse_args(argv)

    # Bound once, so every `record` call below publishes under the same scope and
    # destination. `--only` absent means the whole document, which only the
    # Terraform association runs; everything an operator runs is scoped.
    published = {
        "param": args.publish_to,
        "region": args.region,
        "gateway": args.only or None,
        "runner": runner,
        "host_id": host_id,
        "err": err,
    }

    try:
        document = load_document(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        log(f"Could not read the configuration: {exc}")
        return INVALID

    problems = validate(document)
    if problems:
        for problem in problems:
            log(f"INVALID  {problem}")
        return INVALID

    # After validation, never before: the referential rules span the whole
    # document, so a narrowed one would hide a collection pointing at a gateway
    # that is not declared.
    if args.only:
        if args.only not in declared_gateways(document):
            names = ", ".join(declared_gateways(document)) or "none"
            log(f"No storage gateway named {args.only!r} is declared. Declared: {names}.")
            return INVALID
        document = narrow(document, args.only)

    try:
        # Before the first listing, so a boot-time run waits rather than
        # publishing "cannot compare" about an endpoint that is seconds away.
        # Inside the same `try`: exhausting the wait is a failure to read the
        # endpoint, which is exactly what the branch below already records.
        gcs.wait_until_serving(
            timeout_s=args.wait_for_gcs, runner=runner or subprocess.run, log=err
        )
        plan = build_plan(document, runner=runner)
    except gcs.GcsError as exc:
        log(f"Could not read the endpoint's live state: {exc}")
        # Recorded as a failure to compare, not left absent: an absent write leaves
        # the previous plan looking current.
        record(None, mode=args.mode, unavailable=str(exc), **published)
        return ERROR

    if args.json:
        log(json.dumps(plan.as_dict(), indent=2))
    else:
        log(plan.render())

    if args.mode == "plan":
        record(plan, mode="plan", **published)
        # 0 even with actions pending — see the module docstring.
        return CHECK_FAILED if plan.blocked else OK

    try:
        code = apply(plan, document=document, runner=runner or subprocess.run, log=log)
    except gcs.GcsError as exc:
        # Stopped partway, so the endpoint is in neither the state the plan
        # described nor the one it was aiming for. Re-raised unchanged — but the
        # pre-apply plan must not be left standing as a current description.
        record(None, mode="apply", unavailable=f"the apply stopped partway: {exc}", **published)
        raise
    current, unavailable = settled(document, plan, runner=runner)
    record(current, mode="apply", unavailable=unavailable, **published)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
