"""The human gates: the steps no command can perform, declared as data.

A gate is something the tooling can *detect* but cannot *satisfy*. Some need
another organization and take weeks; some take the operator thirty seconds in a
browser. Either way the failure mode is the same and it is the reason this file
exists: a setup that stops at one of these with a generic error leaves someone
guessing whether they are stuck, waiting, or doing it wrong.

So each gate carries who grants it, one paragraph saying what it is and why
nothing here can do it, how to verify it is satisfied, and — where the action is
"send someone a message" — the message, with the deployment's own values filled
in. A wizard can render that without knowing anything about any institution.

**Every `gate` identifier the CLI can emit is declared here.** `CliError.gate`
appears in the JSON contract, so an identifier a consumer cannot look up is a
dangling reference; `tests/globus/test_gates.py` scans the source for
`gate="..."` and fails on one that is missing. That is why
`operator_confirmation` is in the list even though nobody grants it: it is a
gate in the sense that matters to a caller — the run stopped and a human has to
decide — and `blocks_setup` is how the ones that hold up setup are told apart.

Stored as Python rather than JSON because the content is prose: a paragraph and
an email template per entry, which JSON turns into unreadable `\\n`-spliced
strings. The published form is the JSON that `setup-status` emits, not the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Someone else has to act, and it can take days or weeks.
ORGANIZATIONAL = "organizational"
#: The operator can satisfy it themselves, now.
OPERATOR = "operator"

CHECK = "check"
COMMAND = "command"


@dataclass(frozen=True)
class Verification:
    """How to tell whether the gate is satisfied.

    `kind="check"` names a `doctor` check identifier, which is the better answer
    when one exists: the checklist already reports it, so a wizard shows one
    source of truth rather than a second opinion. `kind="command"` is for the
    gates no check covers yet.
    """

    kind: str
    text: str

    def __post_init__(self) -> None:
        if self.kind not in (CHECK, COMMAND):
            raise ValueError(f"verification kind must be {CHECK!r} or {COMMAND!r}")

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "text": self.text}


@dataclass(frozen=True)
class Gate:
    """One thing a human must do, described well enough to act on."""

    id: str
    title: str
    grantor: str
    explanation: str
    verification: Verification
    kind: str = ORGANIZATIONAL
    request_template: str | None = None
    """The message to send, with `{placeholders}`. None where the action is not
    a request — an application form, or a browser login."""

    recurring: bool = False
    """True where satisfying it once is not enough: a session that expires, a
    login that lapses. A wizard shows these differently from a one-off."""

    blocks_setup: bool = True

    def request(self, **context: Any) -> str | None:
        """The request text with this deployment's values filled in.

        Returns None where there is nothing to send. A placeholder with no value
        is left as-is rather than raising: half a draft an operator can finish is
        more useful than an exception, and the gap is visible in the text.
        """
        if self.request_template is None:
            return None
        return self.request_template.format_map(_Blanks(context))

    def as_dict(self, **context: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "grantor": self.grantor,
            "explanation": self.explanation,
            "verification": self.verification.as_dict(),
            "kind": self.kind,
            "recurring": self.recurring,
            "blocks_setup": self.blocks_setup,
        }
        record["request_text"] = self.request(**context)
        return record


class _Blanks(dict):
    """Leaves an unknown `{placeholder}` visible instead of raising."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


SUBSCRIPTION_REQUEST = """\
Subject: Globus subscription with High Assurance — {deployment_name}

Hello,

I am setting up a Globus Connect Server endpoint to move research data into AWS
S3 for the {deployment_name} pipeline, and I need to know whether our institution
holds a Globus subscription that includes High Assurance.

High Assurance is not optional for this deployment: the S3 storage gateway the
pipeline depends on can only be created under a High Assurance subscription, and
the data involved is subject to a use agreement that requires it.

Could you tell me whether such a subscription exists, and if so, who manages it?
If we do not have one, I would like to know what would be involved in obtaining
one.

Thank you,
{contact_email}
"""

ENDPOINT_SUBSCRIPTION_REQUEST = """\
Subject: Please add endpoint {endpoint_id} to our Globus subscription

Hello,

Could you add this Globus Connect Server endpoint to our institutional
subscription?

  Endpoint UUID: {endpoint_id}
  Endpoint name: {endpoint_name}
  Owner:         {owner_email}
  Contact:       {contact_email}

The endpoint exists and is running, but until it is associated with the
subscription it cannot use High Assurance features, which means its S3 storage
gateway cannot be created and no transfers can run.

In the Globus web app this is done from the subscription's management page by
adding the endpoint UUID above.

Thank you,
{contact_email}
"""


GATES: tuple[Gate, ...] = (
    Gate(
        id="nda_duc",
        title="Data use agreement with the source data provider",
        grantor="the organization that holds the source data",
        explanation=(
            "Access to the source collection is granted by whoever holds the data, under "
            "a use agreement that has to be applied for and approved. Nothing in this "
            "tooling can create, accelerate, or work around it, and approval is commonly "
            "measured in weeks. Until it is granted the source collection's UUID is not "
            "known, so setup cannot be completed and no transfer can be attempted. Start "
            "this first: it is almost always the longest-running item, and everything "
            "else can be done while it is pending."
        ),
        verification=Verification(CHECK, "doctor.source_listing"),
        kind=ORGANIZATIONAL,
    ),
    Gate(
        id="globus_app_registration",
        title="Register the Globus native app",
        grantor="you, in the Globus Developers Portal",
        explanation=(
            "Two registrations are needed and they are not interchangeable, but only one is "
            "yours to make in a browser. A native app, which has no secret, is what the "
            "browser login uses to obtain the refresh token the pipeline transfers with — "
            "and a browser flow is precisely what cannot be scripted, because it is a person "
            "logging in. The other registration is the confidential client that owns the "
            "endpoint, so the endpoint belongs to a service identity rather than to a person, "
            "survives that person leaving, and can be associated with an institutional "
            "subscription: `pixi run globus register-service-client` creates it. Both are "
            "free, take a few minutes, and need no approval from anyone."
        ),
        verification=Verification(COMMAND, "pixi run globus init"),
        kind=OPERATOR,
    ),
    Gate(
        id="globus_subscription",
        title="An institutional Globus subscription with High Assurance",
        grantor="your institution's Globus subscription manager",
        explanation=(
            "High Assurance is a subscription feature, and the S3 storage gateway this "
            "pipeline writes through requires it. Without a subscription the gateway "
            "cannot be created at all — this is not a setting that can be relaxed or "
            "deferred. Most institutions that run Globus already hold a subscription, so "
            "the first step is usually finding out rather than buying one."
        ),
        verification=Verification(CHECK, "doctor.subscription"),
        kind=ORGANIZATIONAL,
        request_template=SUBSCRIPTION_REQUEST,
    ),
    Gate(
        id="endpoint_subscription",
        title="Add this endpoint to the subscription",
        grantor="your institution's Globus subscription manager",
        explanation=(
            "A subscription existing is not the same as this endpoint being covered by it: "
            "each endpoint is added individually, by UUID, and the endpoint has to exist "
            "before anyone can add it. Until that happens the endpoint reports itself as "
            "unsubscribed and creating a High Assurance storage gateway fails with a "
            "subscription error — which reads like a permissions problem and is not one."
        ),
        verification=Verification(CHECK, "doctor.subscription"),
        kind=ORGANIZATIONAL,
        request_template=ENDPOINT_SUBSCRIPTION_REQUEST,
    ),
    Gate(
        id="globus_login",
        title="Log in to Globus in a browser",
        grantor="you",
        explanation=(
            "High Assurance sessions are extended only by an interactive login, with a "
            "forced re-authentication — refreshing a token does not extend one, which is "
            "why this recurs no matter how the credential is stored. When it lapses, "
            "transfers fail with `not_from_allowed_domain`, which reads like a gateway "
            "misconfiguration and has sent people looking in the wrong place for hours. "
            "How often depends on the session timeout this deployment declares."
        ),
        verification=Verification(CHECK, "doctor.session"),
        kind=OPERATOR,
        recurring=True,
    ),
    Gate(
        id="aws_sso_login",
        title="Log in to AWS with an SSO profile",
        grantor="you, with a profile from your AWS administrators",
        explanation=(
            "Every command reads AWS credentials from your environment through an AWS CLI "
            "v2 SSO profile, and nothing here accepts an access key or writes AWS "
            "configuration on your behalf. The login expires on your organization's "
            "schedule; when it has, commands stop before doing anything rather than "
            "failing partway through."
        ),
        verification=Verification(CHECK, "doctor.prerequisites"),
        kind=OPERATOR,
        recurring=True,
    ),
    Gate(
        id="cluster_access",
        title="Connect Cloudflare WARP",
        grantor="you, once enrolled by whoever administers the cluster",
        explanation=(
            "The Kubernetes API is private and reached over Cloudflare WARP. Only the "
            "cluster-dependent steps need it — syncing the credential immediately, the "
            "running-workflow guard, and the Kubernetes check in `doctor` — so Globus "
            "setup itself proceeds without it. The session lasts 24 hours and a lapsed one "
            "presents as a TLS handshake timeout, which reads like a cluster outage."
        ),
        verification=Verification(CHECK, "doctor.prerequisites"),
        kind=OPERATOR,
        recurring=True,
    ),
    Gate(
        id="operator_confirmation",
        title="Confirm an action that changes things",
        grantor="you",
        explanation=(
            "Not a gate anyone grants: a command that would change production state asked "
            "for confirmation and did not get one, because it ran with --non-interactive "
            "and without --yes. It is declared here so that every gate identifier the CLI "
            "emits can be looked up, rather than leaving a consumer with a name and "
            "nowhere to resolve it."
        ),
        verification=Verification(COMMAND, "re-run with --yes"),
        kind=OPERATOR,
        blocks_setup=False,
    ),
)

_BY_ID = {gate.id: gate for gate in GATES}


def gate(gate_id: str) -> Gate | None:
    return _BY_ID.get(gate_id)


def identifiers() -> tuple[str, ...]:
    return tuple(_BY_ID)


def setup_gates() -> tuple[Gate, ...]:
    """The gates that hold up setup, in the order they are usually hit."""
    return tuple(g for g in GATES if g.blocks_setup)


def as_list(**context: Any) -> list[dict[str, Any]]:
    """The whole list as JSON-ready records, request text filled in from `context`."""
    return [g.as_dict(**context) for g in GATES]
