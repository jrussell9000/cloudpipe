"""Globus session arithmetic — how long the stored login is still good for.

The pipeline's failure mode this exists to prevent: on 2026-08-17 a 300-subject
batch was submitted against a session that had lapsed the day before, and all
300 died at the destination pre-flight. Nothing surfaced the session's age, so
nobody looked.

Two rules are baked in:

* Age is measured from the recorded login time (`session-established-at`), never
  from the secret's `LastChangedDate`, which also moves when the secret is edited
  for an unrelated reason and would silently reset the clock.
* An unknown age is `unknown`, never `ok`. A missing parameter means nobody has
  logged in through this tool yet, which is exactly when a batch must not start.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

OK = "ok"
WARN = "warn"
EXPIRED = "expired"
UNKNOWN = "unknown"

DEFAULT_GATE_MARGIN = timedelta(hours=24)
DEFAULT_WARN_THRESHOLD = timedelta(days=5)


@dataclass(frozen=True)
class SessionState:
    """What is known about the stored Globus session."""

    status: str
    established_at: datetime | None
    expires_at: datetime | None
    remaining: timedelta | None
    timeout_minutes: int

    @property
    def remaining_hours(self) -> float | None:
        return None if self.remaining is None else self.remaining.total_seconds() / 3600

    @property
    def usable(self) -> bool:
        """Safe to start a batch: known, not expired, and not inside the gate margin."""
        return self.status in (OK, WARN)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "established_at": self.established_at.isoformat() if self.established_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "remaining_hours": round(self.remaining_hours, 2) if self.remaining_hours else None,
            "timeout_minutes": self.timeout_minutes,
        }


def evaluate(
    established_at: datetime | str | None,
    *,
    timeout_minutes: int,
    now: datetime | None = None,
    gate_margin: timedelta = DEFAULT_GATE_MARGIN,
    warn_threshold: timedelta = DEFAULT_WARN_THRESHOLD,
) -> SessionState:
    """Classify the session: ok, warn (expiring soon), expired, or unknown.

    `expired` covers "already lapsed" *and* "lapses within the gate margin": a
    batch that cannot finish before the session dies is the same failure, just
    later.
    """
    moment = now or datetime.now(UTC)
    parsed = _parse(established_at)
    if parsed is None:
        return SessionState(UNKNOWN, None, None, None, timeout_minutes)

    expires_at = parsed + timedelta(minutes=timeout_minutes)
    remaining = expires_at - moment
    if remaining <= gate_margin:
        status = EXPIRED
    elif remaining <= warn_threshold:
        status = WARN
    else:
        status = OK
    return SessionState(status, parsed, expires_at, remaining, timeout_minutes)


def describe(state: SessionState) -> str:
    """One line an operator can act on."""
    if state.status == UNKNOWN:
        return (
            "Globus session: unknown — no login has been recorded. "
            "Run `pixi run globus login` before submitting a batch."
        )
    assert state.expires_at is not None and state.remaining is not None
    when = state.expires_at.strftime("%Y-%m-%d %H:%MZ")
    hours = state.remaining.total_seconds() / 3600
    if state.status == EXPIRED:
        if hours <= 0:
            return f"Globus session: EXPIRED (lapsed {when}). Run `pixi run globus login`."
        return (
            f"Globus session: expires {when}, in {hours:.1f}h — inside the safety margin, "
            "so a batch must not start. Run `pixi run globus login`."
        )
    if state.status == WARN:
        return f"Globus session: expires {when}, in {hours / 24:.1f} days — renew soon."
    return f"Globus session: valid until {when} ({hours / 24:.1f} days)."


def _parse(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
