"""The setup sequence as data: what to do, in what order, and what each waits on.

`doctor` answers "is anything wrong?". This answers a different question — "what
do I do next?" — and the two are deliberately not the same list. A checklist is
flat and diagnostic; setup is ordered and has dependencies, so a single failure
early on makes most of the later entries unanswerable rather than failing.

**State is derived, never stored.** There is no progress file, because the two
ways setup actually goes wrong both defeat one: it is resumed on a different
machine, and it is half-done by someone else. A file would confidently report
progress that the account does not have. So every step's state comes from the
same live observations `doctor` makes, and running `setup-status` from a fresh
clone against a finished deployment reports everything `done`.

Three rules hold the derivation honest, and each exists because the opposite is
a plausible-looking bug:

1. **Unknown is not done.** A step whose evidence could not be read is `blocked`,
   never `done`. A Kubernetes check that could not reach the cluster says nothing
   about the credential, and reporting it as healthy is the specific failure the
   guided-setup spec forbids.
2. **Blocked on a human is not failed.** A failing check that is a gate's
   verification means someone else has to act; that is `blocked`, with the gate
   named, so a wizard can show the request text instead of an error.
3. **A step is only as good as what precedes it.** Once a predecessor is not
   `done`, everything after it is `blocked` on that predecessor, whatever its own
   evidence happens to say. Evidence gathered on top of an unsatisfied
   dependency is not trustworthy enough to report as progress.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import gates as gates_module
from . import prereqs as prereqs_module
from .exits import ExitCode
from .prereqs import FAIL, PASS, SKIPPED, WARN, CheckResult

#: Step states. A wizard keys its display on these, so they are contract.
DONE = "done"
READY = "ready"
BLOCKED = "blocked"
FAILED = "failed"
STATES = (DONE, READY, BLOCKED, FAILED)

COMMAND = "command"
HUMAN = "human"

# Step identifiers are contract: stable across releases, because a caller keys
# its display and its resume logic on them.
STEP_AWS_CLI = "setup.aws_cli"
STEP_AWS_SSO = "setup.aws_sso"
STEP_TERRAFORM = "setup.terraform"
STEP_PIXI = "setup.pixi"
STEP_DATA_AGREEMENT = "setup.data_agreement"
STEP_GLOBUS_APPS = "setup.globus_apps"
STEP_ANSWERS = "setup.answers"
STEP_INFRASTRUCTURE = "setup.infrastructure"
STEP_ENDPOINT = "setup.endpoint"
STEP_SUBSCRIPTION = "setup.subscription"
STEP_CONFIGURE = "setup.configure"
STEP_LOGIN = "setup.login"
STEP_CREDENTIAL_SYNC = "setup.credential_sync"
STEP_TRANSFER_READY = "setup.transfer_ready"

#: Evidence this module synthesises, because no `doctor` check covers it.
#: Namespaced under `setup.` so it can never collide with a `doctor.` check id.
EVIDENCE_ANSWERS = "setup.evidence.answers_document"
EVIDENCE_ENDPOINT = "setup.evidence.endpoint_recorded"


@dataclass(frozen=True)
class Action:
    """What to do next: a command to run, or something a person must do."""

    kind: str
    text: str

    def __post_init__(self) -> None:
        if self.kind not in (COMMAND, HUMAN):
            raise ValueError(f"action kind must be {COMMAND!r} or {HUMAN!r}")

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "text": self.text}


@dataclass(frozen=True)
class StepSpec:
    """One step, declared. What it *is*, separate from what state it is in.

    `checks` and `answers` are the two kinds of evidence. Both must be satisfied
    for the step to be `done`: a step can depend on a live observation, on a
    value the operator supplied, or on both.
    """

    id: str
    title: str
    after: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    answers: tuple[str, ...] = ()
    gate: str | None = None
    action: Action | None = None
    """What to do when the step is not yet done. Omitted where the step has no
    action of its own — the last step is a consequence of the ones before it."""

    strict: tuple[str, ...] = ()
    """Checks where a warning is not good enough to call this step done.

    `doctor`'s WARN is overloaded, and the two meanings diverge here. For a
    diagnostic checklist "no configuration document exists" is a warning worth
    noting. For an ordered sequence it means the step has not been performed at
    all, and letting it pass as `done` would report a deployment as configured
    when nothing had configured it. Naming the check here says: for this step, a
    caveat is not a completion."""

    prerequisite: bool = False
    """Something the operator's machine must have, rather than work to perform.

    An unmet one reports `blocked`, not `failed`: a missing Terraform is a
    normal state of a machine nobody has set up yet, and calling it a failure
    would put every first run in the same bucket as a broken deployment."""


@dataclass(frozen=True)
class Step:
    """A declared step, resolved against what was observed."""

    spec: StepSpec
    state: str
    waiting_on: tuple[str, ...]
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.spec.id

    def as_dict(self, **context: Any) -> dict[str, Any]:
        gate = gates_module.gate(self.spec.gate) if self.spec.gate else None
        return {
            "id": self.spec.id,
            "title": self.spec.title,
            "state": self.state,
            "waiting_on": list(self.waiting_on),
            "reason": self.reason,
            "action": self.spec.action.as_dict() if self.spec.action else None,
            "gate": gate.as_dict(**context) if gate else None,
            "detail": self.detail,
        }


SEQUENCE: tuple[StepSpec, ...] = (
    # -- prerequisites -------------------------------------------------------
    # First, and with no `after` between them: they are independent, so a
    # missing Terraform must not hide a missing AWS CLI behind it. Reporting all
    # four at once is what lets someone fix their machine in one pass.
    StepSpec(
        id=STEP_AWS_CLI,
        title="AWS CLI v2 is installed",
        checks=("prereq.aws_cli",),
        action=Action(
            HUMAN,
            "install AWS CLI v2: "
            "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html",
        ),
        prerequisite=True,
    ),
    StepSpec(
        id=STEP_AWS_SSO,
        title="Logged in to AWS with an SSO profile",
        after=(STEP_AWS_CLI,),
        checks=("prereq.aws_sso", "prereq.aws_account"),
        gate="aws_sso_login",
        action=Action(COMMAND, "aws sso login"),
        prerequisite=True,
    ),
    StepSpec(
        id=STEP_TERRAFORM,
        title="Terraform is installed",
        checks=("prereq.terraform",),
        action=Action(
            HUMAN, "install Terraform: https://developer.hashicorp.com/terraform/install"
        ),
        prerequisite=True,
    ),
    StepSpec(
        id=STEP_PIXI,
        title="pixi is installed",
        checks=("prereq.pixi",),
        action=Action(HUMAN, "install pixi: https://pixi.sh"),
        prerequisite=True,
    ),
    # -- the things other people grant ---------------------------------------
    # Neither depends on the prerequisites: both take weeks and should be
    # started on day one, while the machine is still being set up.
    StepSpec(
        id=STEP_DATA_AGREEMENT,
        title="Data use agreement with the source data provider",
        answers=("source_collection_id", "source_base_path"),
        gate="nda_duc",
        action=Action(
            HUMAN,
            "apply for access to the source data, then record the collection UUID and base "
            "path it grants in the answers document",
        ),
    ),
    StepSpec(
        id=STEP_GLOBUS_APPS,
        title="Globus applications are registered",
        answers=("service_client_id", "service_client_secret_ref", "native_app_client_id"),
        gate="globus_app_registration",
        # Only the native app is a browser job: a native app is what a person logs
        # in *through*, and a browser flow cannot be scripted. The confidential
        # client is `globus register-service-client` — but it writes two SSM
        # parameters Terraform creates, so it cannot run before
        # `STEP_INFRASTRUCTURE`, while `service_client_id` is a required answer
        # that `init` renders into `globus_client_id` before it. Registering in the
        # portal is the way out of that ordering today; see task 10.6.
        action=Action(
            HUMAN,
            "register a native app at https://app.globus.org/settings/developers and record "
            "its UUID in the answers document; register the confidential client in the same "
            "place, or with `pixi run globus register-service-client` once the AWS "
            "infrastructure exists",
        ),
    ),
    # -- the parts this tooling performs -------------------------------------
    StepSpec(
        id=STEP_ANSWERS,
        title="Answers document is complete and valid",
        after=(STEP_DATA_AGREEMENT, STEP_GLOBUS_APPS),
        checks=(EVIDENCE_ANSWERS,),
        action=Action(COMMAND, "pixi run globus init"),
    ),
    StepSpec(
        id=STEP_INFRASTRUCTURE,
        title="AWS infrastructure is applied",
        after=(STEP_AWS_SSO, STEP_TERRAFORM, STEP_ANSWERS),
        checks=("doctor.ssm_parameters", "doctor.instance"),
        action=Action(COMMAND, "terraform apply -target=module.globus"),
    ),
    StepSpec(
        id=STEP_ENDPOINT,
        title="Globus endpoint exists",
        after=(STEP_INFRASTRUCTURE,),
        checks=(EVIDENCE_ENDPOINT,),
        action=Action(COMMAND, "pixi run globus bootstrap-endpoint"),
    ),
    StepSpec(
        id=STEP_SUBSCRIPTION,
        title="Endpoint is covered by a High Assurance subscription",
        after=(STEP_ENDPOINT,),
        checks=("doctor.subscription",),
        gate="endpoint_subscription",
        action=Action(
            HUMAN, "send your subscription manager the request text attached to this step"
        ),
    ),
    StepSpec(
        id=STEP_CONFIGURE,
        title="Storage gateway and collection are configured",
        after=(STEP_SUBSCRIPTION,),
        # `doctor.config_drift` is deliberately NOT evidence here, though it is
        # the check that would prove this best. It reports `skipped`
        # unconditionally until the on-instance reconcile ships in the AMI, and
        # rule 1 says unknown is never done — so naming it would leave this step
        # blocked forever on a healthy deployment. Adding it once the reconcile
        # is deployed is an additive change the contract allows.
        checks=("doctor.configuration",),
        strict=("doctor.configuration",),
        action=Action(COMMAND, "pixi run globus configure"),
    ),
    StepSpec(
        id=STEP_LOGIN,
        title="A Globus session is established",
        after=(STEP_GLOBUS_APPS, STEP_ENDPOINT),
        checks=("doctor.session",),
        gate="globus_login",
        action=Action(COMMAND, "pixi run globus login"),
    ),
    StepSpec(
        id=STEP_CREDENTIAL_SYNC,
        title="The pipeline's credential is present in the cluster",
        after=(STEP_LOGIN,),
        checks=("doctor.kubernetes_secret",),
        gate="cluster_access",
        action=Action(
            HUMAN,
            "connect Cloudflare WARP, then allow External Secrets to sync the token "
            "(it does so without intervention)",
        ),
    ),
    StepSpec(
        id=STEP_TRANSFER_READY,
        title="Both collections can be listed",
        after=(STEP_DATA_AGREEMENT, STEP_CONFIGURE, STEP_LOGIN),
        checks=("doctor.destination_listing", "doctor.source_listing"),
    ),
)

_SEQUENCE_BY_ID = {spec.id: spec for spec in SEQUENCE}


def step_ids() -> tuple[str, ...]:
    return tuple(_SEQUENCE_BY_ID)


def evidence(
    results: list[CheckResult],
    *,
    prerequisites: list[CheckResult] | None = None,
    synthesized: list[CheckResult] | None = None,
) -> dict[str, CheckResult]:
    """Flatten every observation into one `{check_id: CheckResult}` lookup.

    The prerequisite results are passed separately because `doctor` folds them
    into check 1 as plain dicts, which drops `exit_code` — and `exit_code` is
    what separates "a human must act" from "this is broken".
    """
    found: dict[str, CheckResult] = {}
    for group in (results, prerequisites or [], synthesized or []):
        for result in group:
            found[result.id] = result
    return found


def derive(observed: dict[str, CheckResult], answers: dict[str, Any]) -> list[Step]:
    """Resolve every declared step against what was observed.

    Walks `SEQUENCE` in order, so a step can read the states of the steps it
    declares in `after` — which is why `after` may only name an earlier step, and
    why `test_every_dependency_is_declared_earlier` asserts it.
    """
    states: dict[str, str] = {}
    steps: list[Step] = []

    for spec in SEQUENCE:
        step = _resolve(spec, observed, answers, states)
        states[spec.id] = step.state
        steps.append(step)
    return steps


def first_actionable(steps: list[Step]) -> Step | None:
    """The one step to tell the operator about, or None when setup is complete.

    Not simply the first non-`done` step. Two orderings matter and they pull in
    different directions:

    A failure outranks everything, because a step is often `blocked` *because* of
    something broken further down the list, and pointing at the symptom sends
    someone to the wrong place.

    Then `ready` outranks `blocked`, which is the opposite of what the list order
    suggests. A blocked step is by definition one that cannot be acted on, so it
    is only the answer when nothing is actionable at all — that is the genuine
    "waiting on someone else" state, and it is worth saying plainly rather than
    burying under a step the operator could get on with.
    """
    for wanted in (FAILED, READY, BLOCKED):
        for step in steps:
            if step.state == wanted:
                return step
    return None


def summarize(steps: list[Step]) -> dict[str, int]:
    counts = dict.fromkeys(STATES, 0)
    for step in steps:
        counts[step.state] += 1
    return counts


def _resolve(
    spec: StepSpec,
    observed: dict[str, CheckResult],
    answers: dict[str, Any],
    states: dict[str, str],
) -> Step:
    # Rule 3, applied first: an unsatisfied predecessor settles the state
    # regardless of this step's own evidence. Checking evidence first and
    # dependencies second would let a stale PASS mask a broken chain.
    unmet = tuple(dep for dep in spec.after if states.get(dep) != DONE)
    if unmet:
        return Step(
            spec,
            BLOCKED,
            unmet,
            _needs(unmet),
            {"blocked_by": list(unmet)},
        )

    missing_answers = tuple(name for name in spec.answers if not _present(answers, name))
    checks = [observed.get(name) for name in spec.checks]

    failed = [c for c in checks if c is not None and c.severity == FAIL]
    unknown = tuple(
        name
        for name, c in zip(spec.checks, checks, strict=True)
        if c is None or c.severity == SKIPPED
    )

    detail: dict[str, Any] = {"checks": [c.id for c in checks if c is not None]}
    if missing_answers:
        detail["missing_answers"] = list(missing_answers)
    if unknown:
        detail["not_observed"] = list(unknown)

    if failed:
        first = failed[0]
        # Rule 2: a gate being unmet is not a fault. Two ways to recognise one,
        # and both are needed. The gate may name this very check as its
        # verification — the direct pairing, declared in `gates.py`. Or the check
        # may classify its own failure as BLOCKED, which is how an expired SSO
        # login is recognised: the `aws_sso_login` gate verifies through the
        # aggregate `doctor.prerequisites`, so the pairing alone would miss it
        # and report a routine re-login as a broken deployment.
        if spec.gate and (_verifies(spec.gate, first.id) or _human_blocked(first)):
            return Step(
                spec, BLOCKED, (spec.gate,), first.message, detail | {"failed_check": first.id}
            )
        # A prerequisite the machine lacks is blocked on the operator installing
        # it, which the step's action states. `waiting_on` stays empty because
        # there is nothing else to name: the thing it waits on is the action.
        if spec.prerequisite and _human_blocked(first):
            return Step(spec, BLOCKED, (), first.message, detail | {"failed_check": first.id})
        return Step(spec, FAILED, (), first.message, detail | {"failed_check": first.id})

    if missing_answers:
        # `ready`, not `blocked`, even when a gate produces the value. The
        # distinction this vocabulary has to carry is "can I act on this now?",
        # and a value nobody has supplied yet is precisely something to act on —
        # go apply for the agreement, go register the app. `blocked` is reserved
        # for the two cases where acting is pointless: a predecessor that is not
        # done, and a gate observed to be unsatisfied. The gate is still named in
        # `waiting_on` and carried in the record, so a caller can show what is
        # being asked for and of whom.
        joined = ", ".join(missing_answers)
        return Step(
            spec,
            READY,
            (spec.gate,) if spec.gate else (),
            f"the answers document does not yet give {joined}",
            detail,
        )

    if unknown:
        # Rule 1. Say which observation is missing, because "not observed" and
        # "observed as bad" send an operator to completely different places.
        return Step(
            spec,
            BLOCKED,
            unknown,
            f"could not be determined: {', '.join(unknown)} did not run",
            detail,
        )

    weak = [c for c in checks if c is not None and c.severity == WARN and c.id in spec.strict]
    if weak:
        return Step(spec, READY, (), weak[0].message, detail | {"not_satisfied": [weak[0].id]})

    if checks or spec.answers:
        warned = [c for c in checks if c is not None and c.severity == WARN]
        reason = warned[0].message if warned else _done_reason(checks)
        return Step(spec, DONE, (), reason, detail)

    # No evidence declared at all: nothing can call it done, so it is the next
    # thing to do. No step in SEQUENCE is currently in this shape; it is here so
    # that adding one cannot silently report `done`.
    return Step(spec, READY, (), "no evidence is declared for this step", detail)


def _done_reason(checks: list[CheckResult | None]) -> str:
    messages = [c.message for c in checks if c is not None and c.severity == PASS]
    return "; ".join(messages) if messages else "satisfied"


def _needs(unmet: tuple[str, ...]) -> str:
    # Titles as written. Lower-casing them to fit the sentence turns "High
    # Assurance" into "high assurance", which is a product name here.
    titles = [_SEQUENCE_BY_ID[dep].title for dep in unmet if dep in _SEQUENCE_BY_ID]
    return "waiting on " + ("; ".join(titles) if titles else ", ".join(unmet))


def _human_blocked(result: CheckResult) -> bool:
    """Does this failure mean a person must act, rather than that something broke?

    `CheckResult.exit_code` carries that routing where a check set it. The
    prerequisite checks do not set it — `prereqs.to_cli_error` is what classifies
    them, and reusing it here rather than restating the rule is what stops
    `setup-status` from disagreeing with the refusal every other command raises.
    A wrong AWS account classifies as INVALID, not BLOCKED, and stays `failed`:
    nobody grants it, the answers are simply wrong.
    """
    code = result.exit_code
    if code is None and result.id.startswith("prereq."):
        code = prereqs_module.to_cli_error(result).exit_code
    return code is ExitCode.BLOCKED


def _verifies(gate_id: str, check_id: str) -> bool:
    gate = gates_module.gate(gate_id)
    if gate is None:  # pragma: no cover - test_gates asserts every id resolves
        return False
    return gate.verification.kind == gates_module.CHECK and gate.verification.text == check_id


def _present(answers: dict[str, Any], name: str) -> bool:
    value = answers.get(name)
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True
