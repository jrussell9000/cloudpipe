"""`cloudpipe preflight` — check the world against the collected answers.

Thin on purpose. Every decision lives in `preflight.py`, which takes a mapping and
returns records; this file resolves the root and the answers, prints, and picks the
exit code. That split is what lets the whole check set be tested without a CLI, and
lets a future front end render the same records.

Read-only, and the output says so. A deployer must be able to run this on someone
else's deployment, paste the result into a ticket, and run it again an hour later
without having changed anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import config, preflight, render, roots
from ..cli import register
from ..exits import CliError, ExitCode, Remedy

if TYPE_CHECKING:
    from pathlib import Path

    from ..cli import Context
    from ..output import Emitter, Record


@register("preflight")
def preflight_command(ctx: Context) -> ExitCode:
    emitter = ctx.emitter

    root = roots.resolve(ctx.args.root)
    answers, answers_path = config.load(ctx.args.answers)
    config.reject_credentials(answers)
    if answers_path is None:
        raise _no_answers(ctx.args.answers)

    _preamble(emitter, root, answers_path)

    records = preflight.run(
        answers,
        root,
        expected_account=ctx.args.account,
    )
    _report(emitter, records)

    code = preflight.exit_code(records)
    return emitter.result(
        {
            "root": str(root),
            "answers": str(answers_path),
            "checks": [record.as_dict() for record in records],
        },
        exit_code=code,
    )


def _preamble(emitter: Emitter, root: Path, answers_path: Path) -> None:
    """Say what is being read and what will not be touched, before reading anything.

    The second half is not reassurance for its own sake. Someone is going to run
    this against a production deployment they did not build, and the guarantee that
    it creates nothing is the reason they can.
    """
    emitter.note(f"root:    {root}")
    emitter.note(f"answers: {answers_path}")
    if not (root / render.TFVARS_NAME).exists():
        emitter.note(
            f"note: no {render.TFVARS_NAME} in this root yet. Checking the answers against the "
            "world anyway — but `cloudpipe setup` has not rendered them."
        )
    emitter.note(
        "Read-only: nothing in AWS, Cloudflare, Kubernetes or Globus is created or changed."
    )


#: Four characters each, so the messages line up. Words rather than symbols: the
#: skipped mark was `????`, which a deployer reasonably read as a rendering fault
#: rather than as "this check could not look" — the one state they most need to
#: tell apart from `ok`.
MARKS = {"pass": " ok ", "fail": "FAIL", "skipped": "skip", "blocked": "WAIT"}


def _report(emitter: Emitter, records: list[Record]) -> None:
    emitter.note()
    emitter.note("Preflight")
    for record in records:
        mark = MARKS[record.state]
        emitter.note(f"  [{mark}] {record.title}: {record.message}")
        if record.remedy and record.state != "pass":
            label = "run" if record.remedy.kind == "command" else "do"
            emitter.note(f"         {label}: {record.remedy.text}")

    failed = [record for record in records if record.state == "fail"]
    skipped = [record for record in records if record.state == "skipped"]
    blocked = [record for record in records if record.state == "blocked"]

    emitter.note()
    if failed:
        emitter.note(f"{len(failed)} check(s) failed. Nothing was changed.")
    else:
        emitter.note("No check failed. Nothing was changed.")
    if skipped:
        # Said plainly, because a run that skipped half its checks is not a green
        # run and the exit code alone cannot carry that.
        emitter.note(
            f"{len(skipped)} check(s) could not be performed and are reported as skipped, not "
            "passed. Each names what was missing."
        )
    if blocked:
        # Not failures and they do not change the exit code: each is something the
        # deployer still owes, at a point the check names — the GPU node image,
        # which is baked after the install, is the case this exists for.
        emitter.note(
            f"{len(blocked)} check(s) marked WAIT name a step that comes later; they do not "
            "stop the install."
        )


def _no_answers(given: str | None) -> CliError:
    """Exit 4 rather than checking an empty document.

    Every check against absent answers would report `skipped`, and a report of
    eight skips reads like a permissions problem rather than a missing file.
    """
    looked_for = given or f"./{config.DEFAULT_ANSWERS_NAME}"
    return CliError(
        "answers.absent",
        f"no answers document at {looked_for}, so there is nothing to check the world against",
        exit_code=ExitCode.INVALID,
        remedy=Remedy(
            "command",
            "pixi run cloudpipe setup  (or pass --answers <path> if your document is elsewhere)",
        ),
        detail={"looked_for": str(looked_for)},
    )
