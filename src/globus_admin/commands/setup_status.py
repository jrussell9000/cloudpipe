"""`globus setup-status` — what is done, what is next, and what is in the way.

The sequence and the state derivation live in `globus_admin.setup`; this module
observes reality once, synthesises the two pieces of evidence no `doctor` check
covers, and presents the result.

Observing once matters. `setup-status` and `doctor` ask different questions of
the same deployment, and a second independent pass could answer them
inconsistently — a session that expires between two runs is enough. So this
runs the `doctor` checklist and derives every step from that one snapshot.

Like `doctor`, and unlike every other command, this does not refuse to run when
a prerequisite fails: reporting that failure in order is the whole job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .. import answers as answers_module
from .. import doctor as doctor_module
from .. import setup as setup_module
from ..cli import Context, register
from ..exits import ExitCode, Remedy
from ..prereqs import FAIL, PASS, CheckResult

_MARKS = {
    setup_module.DONE: "done",
    setup_module.READY: "next",
    setup_module.BLOCKED: "wait",
    setup_module.FAILED: "FAIL",
}


@register("setup-status")
def setup_status(ctx: Context) -> ExitCode:
    diag = doctor_module.Diagnostics(
        config=ctx.config,
        env=ctx.env,
        aws=ctx.aws,
        runner=ctx.runner,
        session_factory=ctx.session_factory,
    )
    results = doctor_module.run(diag)

    raw, unreadable = _raw_answers(ctx)
    observed = setup_module.evidence(
        results,
        prerequisites=diag.prerequisites,
        synthesized=[_answers_evidence(raw, unreadable), _endpoint_evidence(diag)],
    )
    steps = setup_module.derive(observed, raw)

    context = _request_context(ctx, diag)
    for index, step in enumerate(steps, start=1):
        _print(ctx, index, step)

    nxt = setup_module.first_actionable(steps)
    _summarize(ctx, steps, nxt)

    data: dict[str, Any] = {
        "environment": ctx.env.name,
        "steps": [step.as_dict(**context) for step in steps],
        "next": nxt.id if nxt else None,
        "summary": setup_module.summarize(steps),
    }
    return ctx.emitter.result(data, exit_code=_exit_code(steps))


def _exit_code(steps: list[setup_module.Step]) -> ExitCode:
    """Setup's state as one number.

    A failed step is a real fault (CHECK_FAILED) and outranks a blocked one,
    because a step blocked on a human is a normal, expected state of a setup in
    progress — BLOCKED is what a caller polls on, not what it alerts on. All
    done is OK, which is what makes `setup-status` usable as a readiness gate.
    """
    states = {step.state for step in steps}
    if setup_module.FAILED in states:
        return ExitCode.CHECK_FAILED
    if setup_module.BLOCKED in states or setup_module.READY in states:
        return ExitCode.BLOCKED
    return ExitCode.OK


def _print(ctx: Context, index: int, step: setup_module.Step) -> None:
    ctx.emitter.note(f"{index:2}. [{_MARKS[step.state]}] {step.spec.title}")
    if step.state == setup_module.DONE:
        return
    ctx.emitter.note(f"      {step.reason}")
    # Only the identifiers the reason does not already state in words. For a
    # step held up by a predecessor the reason names it by title, and repeating
    # it as an id is noise for the person reading; for a gate or an un-run check
    # the identifier is the only handle there is. The JSON carries all of them
    # either way, which is what a program reads.
    opaque = [name for name in step.waiting_on if name not in setup_module.step_ids()]
    if opaque:
        ctx.emitter.note(f"      waiting on: {', '.join(opaque)}")
    if step.spec.action:
        verb = "run" if step.spec.action.kind == setup_module.COMMAND else "do"
        ctx.emitter.note(f"      → {verb}: {step.spec.action.text}")


def _summarize(ctx: Context, steps: list[setup_module.Step], nxt: setup_module.Step | None) -> None:
    counts = setup_module.summarize(steps)
    ctx.emitter.note(
        f"{counts[setup_module.DONE]}/{len(steps)} done, "
        f"{counts[setup_module.READY]} ready, "
        f"{counts[setup_module.BLOCKED]} waiting, "
        f"{counts[setup_module.FAILED]} failed"
    )
    if nxt is None:
        ctx.emitter.note("Setup is complete.")
        return
    ctx.emitter.note(f"Next: {nxt.spec.title} ({nxt.id})")


def _request_context(ctx: Context, diag: doctor_module.Diagnostics) -> dict[str, Any]:
    """Values the gate request templates interpolate.

    The endpoint UUID is the one that matters: the subscription request is
    useless without it, and the guided-setup spec requires it to be populated.
    A value that is not known yet is left out rather than blanked, so
    `Gate.request` leaves the `{placeholder}` visible instead of producing a
    confident-looking message with a hole in it.
    """
    endpoint_id = diag.parameter(ctx.env.endpoint_id_param)
    context: dict[str, Any] = {
        "deployment_name": ctx.config.deployment_name,
        "endpoint_name": ctx.env.gateway_name,
    }
    for key, value in (
        ("endpoint_id", endpoint_id),
        ("contact_email", ctx.config.contact_email),
        ("owner_email", ctx.config.owner_email),
    ):
        if value:
            context[key] = value
    return context


def _raw_answers(ctx: Context) -> tuple[dict[str, Any], str | None]:
    """The answers document as written, or why it could not be read.

    `init` raises on a missing or malformed answers document, which is right for
    a command that exists to render from it. Here it is a *state*: a deployment
    that has not been set up yet has no answers document, and a status command
    that dies on the most common starting condition is no use to the person in
    it. So this reports rather than raises.
    """
    path = ctx.config.answers_path
    if path is None:
        return {}, "no answers document was found"
    try:
        document = yaml.safe_load(Path(path).read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        return {}, f"could not read {path}: {exc}"
    if not isinstance(document, dict):
        return {}, f"{path} must contain a mapping of answers, not {type(document).__name__}"
    return document, None


def _answers_evidence(raw: dict[str, Any], unreadable: str | None) -> CheckResult:
    """Does the answers document validate? No `doctor` check covers this.

    `doctor` reads configuration through the lenient loader, which is right for
    a checklist that must work on a half-built deployment. Setup needs the
    strict answer, because `init` will refuse on exactly these problems.
    """
    if unreadable is not None:
        return CheckResult(
            setup_module.EVIDENCE_ANSWERS,
            "Answers document",
            FAIL,
            unreadable,
            Remedy("human", "create globus-answers.yaml, or pass --answers with its path"),
        )

    problems = answers_module.validate(raw)
    if not problems:
        return CheckResult(
            setup_module.EVIDENCE_ANSWERS,
            "Answers document",
            PASS,
            "every required answer is present and well-formed",
        )
    first = problems[0]
    more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
    return CheckResult(
        setup_module.EVIDENCE_ANSWERS,
        "Answers document",
        FAIL,
        f"{first.field}: {first.message}{more}",
        Remedy("human", "correct the answers document, then re-run `pixi run globus init`"),
        detail={"problems": [p.as_dict() for p in problems]},
    )


def _endpoint_evidence(diag: doctor_module.Diagnostics) -> CheckResult:
    """Is an endpoint UUID recorded for this deployment?

    Separate from `doctor.ssm_parameters`, which reports all five parameters
    together. Setup needs this one on its own: the endpoint is created by a
    distinct step, and a step cannot be `done` on evidence that also covers
    things belonging to other steps.
    """
    endpoint_id = diag.parameter(diag.env.endpoint_id_param)
    if endpoint_id:
        return CheckResult(
            setup_module.EVIDENCE_ENDPOINT,
            "Endpoint recorded",
            PASS,
            f"endpoint {endpoint_id}",
            detail={"endpoint_id": endpoint_id},
        )
    return CheckResult(
        setup_module.EVIDENCE_ENDPOINT,
        "Endpoint recorded",
        FAIL,
        f"no endpoint UUID is recorded at {diag.env.endpoint_id_param}",
        Remedy("command", "pixi run globus bootstrap-endpoint"),
    )
