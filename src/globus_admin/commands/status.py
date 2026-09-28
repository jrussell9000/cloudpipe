"""`globus status` — where things stand, in one screen.

Deliberately read-only and cheap: an operator should be able to run it before a
batch without thinking about consequences. It answers the four questions that
precede every Globus decision here: is the server up, is the login still good,
which collection will be written to, and is anything transferring right now.
"""

from __future__ import annotations

from typing import Any

from .. import cluster, globus_client, sessions
from ..cli import Context, register
from ..exits import ExitCode


@register("status")
def status(ctx: Context) -> ExitCode:
    ctx.require_prereqs()
    aws = ctx.aws
    env = ctx.env
    emitter = ctx.emitter

    data: dict[str, Any] = {"environment": env.name, "gateway": env.gateway_name}

    instance_id = aws.get_parameter(env.instance_id_param)
    if instance_id:
        state = aws.instance_state(instance_id)
        data["instance"] = {"id": instance_id, "state": state}
        emitter.note(f"GCS instance {instance_id}: {state}")
    else:
        data["instance"] = None
        emitter.note("GCS instance: no instance id in SSM (not deployed yet)")

    collection_id = aws.get_parameter(env.collection_id_param)
    data["collection_id"] = collection_id
    emitter.note(f"collection: {collection_id or 'not set up yet'}")

    # Session age against the declared timeout. The timeout comes from the
    # answers for now; once the GCS configuration document exists (task 8.2) it
    # becomes the single source of truth and this reads it from there.
    established = aws.get_parameter(env.session_param)
    state = sessions.evaluate(established, timeout_minutes=ctx.config.session_timeout_minutes)
    data["session"] = state.as_dict()
    emitter.note(sessions.describe(state))

    secret = aws.get_secret_json(env.token_secret)
    data["token_stored"] = bool(secret)
    if not secret:
        emitter.note(f"token: none stored in {env.token_secret}")

    # Active transfers are a Globus fact, not a cluster fact, so this works
    # without WARP. The cluster-side view (pods, workflows) belongs to `doctor`.
    if secret:
        transfer_client = globus_client.build_transfer_client(secret, env=env.name)
        tasks = list(globus_client.iter_pipeline_tasks(transfer_client))
        data["active_tasks"] = [task.as_dict() for task in tasks]
        emitter.note(f"pipeline transfer tasks active or inactive: {len(tasks)}")
    else:
        data["active_tasks"] = None

    access = cluster.probe(ctx.runner)
    data["cluster_reachable"] = access.reachable
    if not access.reachable:
        emitter.note(
            "cluster: unreachable — connect Cloudflare WARP for the Kubernetes-side checks "
            "in `globus doctor`."
        )

    return emitter.result(data)
