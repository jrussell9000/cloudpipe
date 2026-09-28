"""`globus tasks` — list or cancel this pipeline's Globus transfer tasks.

Replaces `images/globus/cancel_transfers.py` and the inline Python snippet in
`docs/globus.md`. Both existed because a terminated workflow leaves its Globus
task running, and a surviving task can be adopted by a later run — so the first
thing to do after terminating a batch is to look for stragglers.

Cancelling is a mutation: it needs the same confirmation and running-workflow
guard as anything else, because cancelling a task belonging to a live workflow
fails that workflow.
"""

from __future__ import annotations

from typing import Any

from .. import cluster, globus_client, guards
from ..cli import Context, register
from ..exits import ExitCode


@register("tasks")
def tasks(ctx: Context) -> ExitCode:
    cancelling = bool(ctx.args.cancel)
    ctx.require_prereqs(mutating=cancelling)

    env = ctx.env
    emitter = ctx.emitter
    secret = ctx.aws.get_secret_json(env.token_secret)
    transfer_client = globus_client.build_transfer_client(secret, env=env.name)

    found = list(globus_client.iter_pipeline_tasks(transfer_client))
    if ctx.args.subject:
        found = [task for task in found if task.subject == ctx.args.subject]

    if not found:
        emitter.note("no active or inactive pipeline transfer tasks")
        return emitter.result({"tasks": [], "cancelled": []})

    for task in found:
        emitter.note(f"  {task.task_id}  {task.status:8}  {task.label}  {task.request_time}")

    data: dict[str, Any] = {"tasks": [task.as_dict() for task in found], "cancelled": []}
    if not cancelling:
        return emitter.result(data)

    access = cluster.probe(ctx.runner)
    guards.confirm(
        emitter,
        action=f"cancel {len(found)} Globus transfer task(s)",
        activity=guards.workflow_activity(access, ctx.runner),
        non_interactive=ctx.non_interactive,
        assume_yes=ctx.assume_yes,
    )

    cancelled = []
    for task in found:
        code = globus_client.cancel(transfer_client, task.task_id)
        emitter.note(f"cancelled {task.task_id}: {code}")
        cancelled.append({"task_id": task.task_id, "label": task.label, "code": code})

    data["cancelled"] = cancelled
    return emitter.result(data)
