"""`globus login` — establish a Globus session and put it where the pipeline reads it.

The order of the writes is the part to preserve:

1. the tokens go into Secrets Manager,
2. `session-established-at` is written **last**,
3. the cluster is told to re-sync.

If step 2 never happens, the recorded session stays unknown and the batch gate
refuses to submit. That is the safe direction: a login nobody recorded looks
expired, where the reverse — a recorded session with no token behind it — would
wave a 300-subject batch through.

Step 3 is an annotation on the ExternalSecret, never `kubectl delete secret`. The
old procedure deleted it, which leaves every pod that mounts it without a
credential until the operator re-creates it.

A login always needs a person at a browser, so `--non-interactive` exits 2 before
asking for anything, naming the browser login as the step a human must take.
"""

from __future__ import annotations

import os
from typing import Any

from .. import cluster, guards, sessions
from .. import login as login_module
from ..cli import Context, register
from ..exits import CliError, ExitCode, Remedy

CLIENT_ID_ENV = "GLOBUS_NATIVE_APP_CLIENT_ID"


@register("login")
def login(ctx: Context) -> ExitCode:
    if ctx.non_interactive:
        raise CliError(
            "login.needs_browser",
            "A Globus login needs a person to consent in a browser, which no unattended run "
            "can do.",
            exit_code=ExitCode.BLOCKED,
            remedy=Remedy("human", "run `pixi run globus login` in a terminal with a browser"),
            gate="globus_login",
        )

    # Every login grants the HTTPS scope, so it needs to know whose. Refused up
    # front rather than noted: a login without it stores a credential every
    # ingress pod then refuses (exit 78), which is worse than no login at all.
    https_collection_id = ctx.config.source_collection_id
    if not https_collection_id:
        raise CliError(
            "login.no_source_collection",
            "No source_collection_id is configured, so this login could not grant the HTTPS "
            "scope that ingress-mode=https needs.",
            exit_code=ExitCode.INVALID,
            remedy=Remedy("human", "add source_collection_id to the answers document"),
        )

    ctx.require_prereqs(mutating=True)
    env = ctx.env
    emitter = ctx.emitter
    aws = ctx.aws

    stored = aws.get_secret_json(env.token_secret) or {}
    client_id = login_module.resolve_client_id(
        getattr(ctx.args, "client_id", None),
        ctx.config.native_app_client_id,
        os.environ.get(CLIENT_ID_ENV),
        stored.get("native-app-client-id"),
    )

    domains = ctx.config.identity_domains
    if domains:
        emitter.note(f"Sign in with an identity in: {', '.join(domains)}")
    else:
        # Without a declared domain the login cannot demand one, and a High
        # Assurance collection will reject the resulting session later.
        emitter.note(
            "warning: no identity_domains are configured, so this login cannot require a "
            "session in a particular domain. A High Assurance collection may reject it."
        )

    # Not a confirmation gate — a login is how an outage gets fixed, and making
    # it slower under pressure is its own failure. But a transfer that is running
    # right now is worth knowing about before the credential moves.
    activity = guards.workflow_activity(cluster.probe(ctx.runner), ctx.runner)
    if activity.known and activity.busy:
        emitter.note(
            f"note: {len(activity.workflows)} workflow(s) and {activity.transfer_pods} "
            "transfer pod(s) are running; they keep using the token they already hold."
        )

    result = login_module.LoginSession(
        client_id=client_id,
        domains=domains,
        no_browser=bool(getattr(ctx.args, "no_browser", False)),
        https_collection_id=https_collection_id,
    ).run()
    emitter.note(f"Logged in as {result.username}")

    payload = result.secret_payload()
    aws.put_secret_json(env.token_secret, payload)
    emitter.note(f"Stored the credential in {env.token_secret}")

    # Last, and only after the tokens are safely stored.
    aws.put_parameter(env.session_param, result.established_at.isoformat())

    state = sessions.evaluate(
        result.established_at, timeout_minutes=ctx.config.session_timeout_minutes
    )
    emitter.note(sessions.describe(state))

    data: dict[str, Any] = {
        "environment": env.name,
        "username": result.username,
        "identity_id": result.identity_id,
        "secret": env.token_secret,
        "session": state.as_dict(),
        "cluster_sync": _sync_cluster(ctx, payload),
    }
    return emitter.result(data)


def _sync_cluster(ctx: Context, payload: dict[str, str]) -> dict[str, Any]:
    """Push the new credential into the cluster, and say plainly if it did not land.

    A failure here is never fatal: the tokens are already in Secrets Manager, and
    External Secrets converges on its own interval. Reporting this as a failed
    login would send the operator through the browser flow again for nothing.

    Waits for every key the ExternalSecret syncs, not only `refresh-token`: the
    ingress pods read `https-refresh-token`, and a wait on one key reported the
    Secret up to date while the other was still the old value.
    """
    secret_name = ctx.env.k8s_secret
    access = cluster.probe(ctx.runner)
    if not access.reachable:
        interval = cluster.eso_refresh_interval()
        ctx.emitter.note(
            f"The cluster was unreachable, so `{secret_name}` was not refreshed now. "
            f"External Secrets will pick the new tokens up within {interval}. "
            "Connect Cloudflare WARP and re-run to refresh it immediately."
        )
        return {"attempted": False, "synced": False, "reason": "cluster unreachable"}

    error = cluster.force_external_secret_sync(ctx.runner, name=secret_name)
    if error is not None:
        ctx.emitter.note(f"warning: {error.message}")
        return {"attempted": True, "synced": False, "reason": error.code}

    expected = {key: payload[key] for key in cluster.SYNCED_KEYS if key in payload}
    ctx.emitter.note(f"Waiting for `{secret_name}` in the cluster to carry the new tokens…")
    if cluster.wait_for_secret(ctx.runner, name=secret_name, expected=expected):
        ctx.emitter.note(f"`{secret_name}` is up to date.")
        return {"attempted": True, "synced": True}

    interval = cluster.eso_refresh_interval()
    ctx.emitter.note(
        f"`{secret_name}` has not picked up the new tokens yet. Nothing is wrong with the "
        f"login — the credential is stored, and External Secrets re-reads it every "
        f"{interval}. Check with `pixi run globus doctor` before starting a batch."
    )
    return {"attempted": True, "synced": False, "reason": "timed out waiting for the sync"}
