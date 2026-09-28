"""The batch gate: refuse to submit against a Globus session that cannot finish the run.

On 2026-08-17 a 300-subject batch was submitted against a session that had lapsed
the day before. Every workflow ran, reached the Globus transfer step, and failed
at the destination pre-flight. Nothing between "the operator typed the command"
and "300 workflows have failed" looked at the session at all.

The gate is deliberately in the queue manager and nowhere else. A check inside the
Argo DAG would fire once per workflow — by then the 300 submissions already exist,
which is the failure being prevented. A check here costs one API call and stops
the whole batch before the first submission.

Two questions, because neither alone is enough:

* **How much life is left?** Cheap, and the only one that can warn days ahead.
  Blind to a revoked session or a timeout somebody lowered.
* **Does a listing actually work right now?** Catches both of those, and nothing
  else does. Blind to a session that expires an hour into a twelve-hour batch.

The margin is what ties them together: a session with four hours left passes the
live check and still cannot carry a batch, so "expires within the margin" is
treated exactly like "already expired".

This duplicates `src/globus_admin/sessions.py` on purpose — the flow-runner image
does not carry that package. `tests/prefect/test_globus_session_gate.py` asserts
the two agree on a table of inputs, so the copy cannot drift unnoticed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

SSM_SESSION_PARAM = "/cloudpipe/globus/session-established-at"
SSM_CONFIG_PARAM = "/cloudpipe/globus/config"
#: Which gateway's declared timeout the gate measures against. Published by
#: Terraform rather than derived — see #506 and the comment on the resource.
SSM_GATEWAY_NAME_PARAM = "/cloudpipe/globus/gateway-name"
SECRET_ID = "globus/refresh-token"

MARGIN_VARIABLE = "cloudpipe-globus-session-margin-hours"
WARN_VARIABLE = "cloudpipe-globus-session-warn-days"
DEFAULT_MARGIN_HOURS = 24.0
DEFAULT_WARN_DAYS = 5.0

# Only used when nothing declares a timeout. Matches the declared production
# value; if the two ever disagree, the declaration wins.
DEFAULT_SESSION_TIMEOUT_MINUTES = 30 * 24 * 60


class AmbiguousGatewayError(RuntimeError):
    """More than one gateway is declared and nothing said which one applies.

    Raised rather than defaulted: the caller is asking how long this session has
    left, and answering from a guess is how #506 went unnoticed — the guess and
    the declaration agreed, so nothing looked wrong until they would not have.
    """


OK = "ok"
WARN = "warn"
EXPIRED = "expired"
UNKNOWN = "unknown"

LOGIN_COMMAND = "pixi run globus login"


@dataclass(frozen=True)
class GateDecision:
    """Whether the batch may start, and the sentence to log either way."""

    allowed: bool
    status: str
    reason: str
    remaining_hours: float | None = None
    expires_at: datetime | None = None


def evaluate(
    established_at: str | datetime | None,
    *,
    timeout_minutes: int,
    now: datetime | None = None,
    margin_hours: float = DEFAULT_MARGIN_HOURS,
    warn_days: float = DEFAULT_WARN_DAYS,
) -> GateDecision:
    """Pure: how much life is left, and whether that is enough to start a batch."""
    moment = now or datetime.now(UTC)
    parsed = _parse(established_at)

    if parsed is None:
        return GateDecision(
            allowed=False,
            status=UNKNOWN,
            reason=(
                "No Globus login time is recorded, so there is no way to tell whether the "
                f"stored session will outlive this batch. Run `{LOGIN_COMMAND}`."
            ),
        )

    expires_at = parsed + timedelta(minutes=timeout_minutes)
    remaining = expires_at - moment
    hours = remaining.total_seconds() / 3600
    when = expires_at.strftime("%Y-%m-%d %H:%MZ")

    if hours <= 0:
        return GateDecision(
            allowed=False,
            status=EXPIRED,
            reason=(
                f"The Globus session expired at {when}. Every transfer in this batch would "
                f"fail at the destination pre-flight. Run `{LOGIN_COMMAND}`."
            ),
            remaining_hours=hours,
            expires_at=expires_at,
        )

    if hours <= margin_hours:
        return GateDecision(
            allowed=False,
            status=EXPIRED,
            reason=(
                f"The Globus session expires at {when}, in {hours:.1f}h — inside the "
                f"{margin_hours:.0f}h safety margin, so this batch would not finish before "
                f"it lapses. Run `{LOGIN_COMMAND}`."
            ),
            remaining_hours=hours,
            expires_at=expires_at,
        )

    if hours <= warn_days * 24:
        return GateDecision(
            allowed=True,
            status=WARN,
            reason=(
                f"Globus session expires at {when}, in {hours / 24:.1f} days. Submitting, "
                f"but renew soon with `{LOGIN_COMMAND}`."
            ),
            remaining_hours=hours,
            expires_at=expires_at,
        )

    return GateDecision(
        allowed=True,
        status=OK,
        reason=f"Globus session expires at {when}, in {hours / 24:.1f} days.",
        remaining_hours=hours,
        expires_at=expires_at,
    )


def timeout_minutes_from_config(document: Any, gateway_name: str | None = None) -> tuple[int, str]:
    """The declared `authentication_timeout_mins`, and where it came from.

    The declaration is the number the endpoint was actually configured with. The
    fallback exists so a deployment that has not rendered a configuration
    document yet still gets a gate rather than no gate — but it says so, because
    a gate measuring against the wrong timeout is worse than one that admits it
    is guessing.
    """
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except json.JSONDecodeError:
            return DEFAULT_SESSION_TIMEOUT_MINUTES, "the default (the configuration is not JSON)"

    gateways = document.get("storage_gateways") if isinstance(document, dict) else None
    if not isinstance(gateways, list) or not gateways:
        return DEFAULT_SESSION_TIMEOUT_MINUTES, "the default (nothing declares a timeout)"

    candidates = [g for g in gateways if isinstance(g, dict)]
    if gateway_name:
        candidates = [g for g in candidates if g.get("display_name") == gateway_name]
    elif len(candidates) > 1:
        # Ambiguous: more than one gateway declared and nothing said which. This
        # used to return DEFAULT_SESSION_TIMEOUT_MINUTES with an explanatory
        # source string, which is how production spent months measuring against
        # a default it happened to agree with (#506). A gate that cannot tell
        # which timeout applies is not a gate, so it refuses instead of guessing.
        declared = ", ".join(
            str(g.get("display_name")) for g in candidates if g.get("display_name")
        )
        raise AmbiguousGatewayError(
            f"{len(candidates)} storage gateways are declared ({declared}) and none was "
            f"named, so there is no way to tell which timeout this session must outlive. "
            f"Pass the gateway name — the deployment publishes it at "
            f"{SSM_GATEWAY_NAME_PARAM}."
        )

    for gateway in candidates:
        declared = gateway.get("authentication_timeout_mins")
        # `True` is an `int` in Python, and a declaration of `true` here would
        # otherwise be read as a one-minute timeout.
        if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
            return declared, f"declared for {gateway.get('display_name')}"

    return DEFAULT_SESSION_TIMEOUT_MINUTES, "the default (no gateway declares a timeout)"


def destination_reachable(transfer_client: Any, collection_id: str, path: str) -> tuple[bool, str]:
    """One listing through the destination collection, with the stored credential.

    A 404 counts as reachable: the path may legitimately not exist yet, and the
    request still proved the session and the gateway credential are good. Only an
    authorization or connection failure is a refusal.
    """
    try:
        transfer_client.operation_ls(collection_id, path=path)
    except Exception as exc:
        if getattr(exc, "http_status", None) == 404:
            return True, "listing returned 404 (path absent), so the session is still good"
        return False, _explain(exc)
    return True, "destination listing succeeded"


def _explain(exc: Exception) -> str:
    """Name the lapsed-session case, which does not look like one."""
    raw = str(exc)
    payload = getattr(exc, "raw_json", None)
    auth_params = (payload or {}).get("authorization_parameters") or {} if payload else {}
    if auth_params.get("session_required_single_domain") or auth_params.get(
        "session_required_identities"
    ):
        return (
            "The destination collection refused the stored credential because the Globus "
            "session behind it has lapsed. A refresh token cannot fix this — sessions are "
            f"extended by an interactive login. Run `{LOGIN_COMMAND}`. Raw error: {raw}"
        )
    return f"The destination collection could not be listed: {raw}"


def _parse(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    # Terraform creates the parameter with a placeholder; it means "no login yet".
    if not text or text == "REPLACE_AFTER_GCS_SETUP":
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
