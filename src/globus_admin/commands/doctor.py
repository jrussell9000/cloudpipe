"""`globus doctor` — the ordered checklist, printed for a person or a program.

The checks themselves live in `globus_admin.doctor`; this module is only their
presentation and the command's exit code. Keeping them apart is what lets each
check be unit-tested against fakes without going through argument parsing.

Unlike every other command, `doctor` does not refuse to run when a prerequisite
fails: reporting that failure *is* its job. It calls the checks directly rather
than through `Context.require_prereqs`.
"""

from __future__ import annotations

from typing import Any

from .. import doctor as doctor_module
from ..cli import Context, register
from ..exits import ExitCode
from ..prereqs import FAIL, PASS, SKIPPED, WARN, CheckResult

_MARKS = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL", SKIPPED: "----"}


@register("doctor")
def doctor(ctx: Context) -> ExitCode:
    diag = doctor_module.Diagnostics(
        config=ctx.config,
        env=ctx.env,
        aws=ctx.aws,
        runner=ctx.runner,
        session_factory=ctx.session_factory,
        start_instance=bool(getattr(ctx.args, "start_instance", False)),
        # Progress goes through the emitter like everything else, so it lands on
        # stderr in both modes and cannot corrupt the JSON envelope on stdout.
        notify=ctx.emitter.note,
    )

    results = doctor_module.run(diag)
    for index, result in enumerate(results, start=1):
        _print(ctx, index, result)

    exit_code = doctor_module.exit_code_for(results)
    _summarize(ctx, results, exit_code)

    data: dict[str, Any] = {
        "environment": ctx.env.name,
        "checks": [result.as_dict() for result in results],
        "summary": _counts(results),
    }
    return ctx.emitter.result(data, exit_code=exit_code)


def _print(ctx: Context, index: int, result: CheckResult) -> None:
    ctx.emitter.note(f"{index:2}. {_MARKS[result.severity]}  {result.title}")
    ctx.emitter.note(f"      {result.message}")
    # One next step per non-PASS check, and nothing to read past on a PASS.
    if result.severity != PASS and result.remedy:
        verb = "run" if result.remedy.kind == "command" else "do"
        ctx.emitter.note(f"      → {verb}: {result.remedy.text}")


def _summarize(ctx: Context, results: list[CheckResult], exit_code: ExitCode) -> None:
    counts = _counts(results)
    ctx.emitter.note(
        f"{counts['pass']} passed, {counts['warn']} warning(s), {counts['fail']} failed, "
        f"{counts['skipped']} not run"
    )
    if exit_code is ExitCode.OK and counts["skipped"]:
        ctx.emitter.note("Nothing failed. The checks marked ---- were not run, not passed.")


def _counts(results: list[CheckResult]) -> dict[str, int]:
    counts = {PASS: 0, WARN: 0, FAIL: 0, SKIPPED: 0}
    for result in results:
        counts[result.severity] += 1
    return counts
