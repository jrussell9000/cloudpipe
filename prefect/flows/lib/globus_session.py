"""The batch gate: refuse to submit against a Globus session that cannot finish the run.

On 2026-08-17 a 300-subject batch was submitted against a session that had lapsed
the day before. Every workflow ran, reached its ingress step, and failed there.
Nothing between "the operator typed the command" and "300 workflows have failed"
looked at the session at all.

The gate is deliberately in the queue manager and nowhere else. A check inside the
Argo DAG would fire once per workflow — by then the 300 submissions already exist,
which is the failure being prevented. A check here costs one API call and stops
the whole batch before the first submission.

Two questions, because neither alone is enough:

* **How much life is left?** Cheap, and the only one that can warn days ahead.
  Measured against this deployment's 30-day re-authentication rule for
  `ingress-mode=https`. Blind to a revoked session or a missing HTTPS grant.
* **Does the ingress's access actually work right now?** One listing of the
  source with the transfer token and one refresh of the HTTPS token. Catches
  both of those, and nothing else does. Blind to a session that expires an hour
  into a twelve-hour batch.

The margin is what ties them together: a session with four hours left passes the
live check and still cannot carry a batch, so "expires within the margin" is
treated exactly like "already expired".

This duplicates `src/globus_admin/sessions.py` on purpose — the flow-runner image
does not carry that package. `tests/prefect/test_globus_session_gate.py` asserts
the two agree on a table of inputs, so the copy cannot drift unnoticed.

`ingress-mode=presynced` reads nothing from Globus, so it runs no gate at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

SSM_SESSION_PARAM = "/cloudpipe/globus/session-established-at"
SECRET_ID = "globus/refresh-token"

MARGIN_VARIABLE = "cloudpipe-globus-session-margin-hours"
WARN_VARIABLE = "cloudpipe-globus-session-warn-days"
DEFAULT_MARGIN_HOURS = 24.0
DEFAULT_WARN_DAYS = 5.0

#: `ingress-mode=https` holds a login to 30 days. That is this deployment's own
#: re-authentication rule (plan 018 §8), not a gateway's timeout: the source's is a
#: year, and nothing of ours sits in the HTTPS path to declare one. The ingress pod
#: enforces the same number (`images/globus/https_ingress.py`, `MAX_SESSION_DAYS`);
#: a test holds the two equal.
HTTPS_RULE_MINUTES = 30 * 24 * 60
HTTPS_RULE_SOURCE = "this deployment's 30-day re-authentication rule for ingress-mode=https"


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
                f"The Globus session expired at {when}. Every workflow in this batch would "
                "fail at its ingress step: the source listing would be refused, and the "
                "ingress pod refuses a login older than 30 days (exit 78) before any request. "
                f"Run `{LOGIN_COMMAND}`."
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


def source_reachable(transfer_client: Any, collection_id: str, path: str) -> tuple[bool, str]:
    """One listing of the source base path — the `https` gate's Transfer half.

    A 404 here is a refusal. The base path is where every subject in the batch
    is read from, so its absence fails every workflow, and the gate exists to
    say that once instead of N times.
    """
    try:
        transfer_client.operation_ls(collection_id, path=path)
    except Exception as exc:
        if getattr(exc, "http_status", None) == 404:
            return False, (
                f"The source base path {path} does not exist on collection {collection_id}, "
                "so no subject in this batch could be read. Check "
                "/cloudpipe/globus/source-base-path or the flow's globus_source_base_path."
            )
        return False, _explain(exc)
    return True, "source listing succeeded"


def https_token_refreshes(
    auth_client: Any, refresh_token: str | None, collection_id: str
) -> tuple[bool, str]:
    """One refresh of the stored HTTPS token — the `https` gate's second half.

    The listing proves the transfer token; it says nothing about the second
    token, which a login can omit (no source configured then) or Globus can
    revoke on its own. Checks the same thing the ingress pod will need: an
    access token for the source collection's resource server.
    """
    remedy = f"Grant the HTTPS scope again with `{LOGIN_COMMAND}`."
    if not refresh_token:
        return False, (
            f"The stored Globus credential ({SECRET_ID}) has no https-refresh-token, so no "
            f"ingress pod could read the source over HTTPS. {remedy}"
        )
    try:
        response = auth_client.oauth2_refresh_token(refresh_token)
    except Exception as exc:
        return False, f"Globus refused the HTTPS refresh token: {exc}. {remedy}"
    tokens = response.by_resource_server.get(collection_id) or {}
    if not tokens.get("access_token"):
        return False, (
            f"The HTTPS refresh token carries no grant for source collection {collection_id}. "
            f"{remedy}"
        )
    return True, "HTTPS token refresh succeeded"


def _explain(exc: Exception) -> str:
    """Name the lapsed-session case on the source, which does not look like one."""
    raw = str(exc)
    payload = getattr(exc, "raw_json", None)
    auth_params = (payload or {}).get("authorization_parameters") or {} if payload else {}
    if auth_params.get("session_required_single_domain") or auth_params.get(
        "session_required_identities"
    ):
        return (
            "The source collection refused the stored credential because the Globus "
            "session behind it has lapsed. A refresh token cannot fix this — sessions are "
            f"extended by an interactive login. Run `{LOGIN_COMMAND}`. Raw error: {raw}"
        )
    return f"The source collection could not be listed: {raw}"


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
