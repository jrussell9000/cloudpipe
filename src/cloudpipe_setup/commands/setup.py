"""`cloudpipe setup` — collect the inputs and render the Terraform files.

The order here is load, derive, collect, validate, render, report, and every step
before `render` touches nothing on disk. That matters for the interrupted case: a
run that dies partway leaves either a complete answers document or the one it
started with, never a half-written `terraform.tfvars`.

Nothing in this file knows what the questions are. It walks
`schema.fields()`, so a new input is a schema entry and no change here (design
D1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import config, render, roots, schema
from ..cli import register
from ..exits import CliError, ExitCode, Remedy

if TYPE_CHECKING:
    from pathlib import Path

    from ..cli import Context

#: The published installer, as a deployer invokes it from their clone. A path
#: rather than a documentation section, because this is the command the closing
#: output has to name. `tests/test_setup_wizard_render.py` pairs it with the file
#: it points at, so a rename of a published interface name cannot leave this
#: message naming a script that is not there.
INSTALLER = "scripts/stack/install.sh"


@register("setup")
def setup(ctx: Context) -> ExitCode:
    emitter = ctx.emitter
    with_backend = not ctx.args.no_backend

    root = roots.resolve(ctx.args.root)
    emitter.note(f"target root: {root}")

    answers, answers_path = config.load(ctx.args.answers)
    config.reject_credentials(answers)
    if answers_path is not None:
        emitter.note(f"answers: {answers_path}")

    answers = _apply_defaults(answers)
    target = config.resolve_path(ctx.args.answers)

    if ctx.non_interactive:
        answers = config.derive(answers)
        outstanding = config.missing_required(answers, with_backend=with_backend)
        if outstanding:
            raise _blocked(outstanding)
    else:
        # Imported here rather than at the top so that the non-interactive path —
        # the one another program drives — reaches none of the terminal handling.
        from .. import prompt

        answers = prompt.collect(
            answers,
            emitter=emitter,
            with_backend=with_backend,
            # Written after every field, so an interrupted run resumes from the
            # answers document alone. There is no separate progress file.
            on_answer=lambda current: config.save(current, target),
        )

    problems = config.validate(answers, with_backend=with_backend)
    if problems:
        raise config.invalid(problems)

    config.save(answers, target)
    emitter.note(f"wrote {target}")

    written = [_render(root, render.TFVARS_NAME, render.tfvars(answers), ctx)]
    if with_backend:
        written.append(_render(root, render.BACKEND_NAME, render.backend_tf(answers), ctx))
    for path in written:
        emitter.note(f"wrote {path}")

    outstanding = _outstanding(root, answers, with_backend=with_backend)
    _report(emitter, written, outstanding, config.overridden_derived(answers))

    return emitter.result(
        {
            "root": str(root),
            "answers": str(target),
            "files_written": [str(path) for path in written],
            "fields": [
                {
                    "identifier": field.identifier,
                    "terraform_variable": field.terraform_variable,
                    "derived": field.is_derived,
                    "state": "pass",
                }
                for field in schema.fields()
                if field.identifier in answers
            ],
            "outstanding": outstanding,
        }
    )


def _report(
    emitter,
    written: list[Path],
    outstanding: list[dict],
    overrides: list[tuple[schema.Field, object]],
) -> None:
    """The closing output, which says what has NOT been done.

    This is not a summary for its own sake. "Setup complete" after collecting
    inputs would read as "now run terraform apply", and that fails: the root's
    providers look up a cluster that does not exist yet. So the heading is what was
    collected, and the body is the remaining work with a command or an instruction
    against each item.
    """
    emitter.note()
    emitter.note(f"Inputs collected. {len(written)} file(s) written. Nothing was deployed.")

    for field, computed in overrides:
        emitter.note()
        emitter.note(
            f"note: {field.identifier} does not match what "
            f"{', '.join(field.derived_from)} would derive ({computed!r}). Keeping your value — "
            "delete the field from the answers document to go back to the derived one."
        )

    emitter.note()
    emitter.note("Still to do:")
    for step in outstanding:
        mark = "done" if step["state"] == "pass" else "todo"
        emitter.note(f"  [{mark}] {step['title']} — {step['message']}")
        remedy = step["remedy"]
        if remedy:
            label = "run" if remedy["kind"] == "command" else "do"
            emitter.note(f"         {label}: {remedy['text']}")


def _render(root: Path, name: str, content: str, ctx: Context) -> Path:
    return render.write(root / name, content, force=ctx.args.force)


def _apply_defaults(answers: dict) -> dict:
    """Fill schema defaults for fields the deployer has not set.

    Only `state_key` has one today. Applied here rather than in `config.load` so
    that the answers document a deployer carries between machines records the
    value that was actually used, not an empty slot that a future default change
    would silently fill differently.
    """
    filled = dict(answers)
    for field in schema.fields():
        if field.default is not None and filled.get(field.identifier) in (None, ""):
            filled[field.identifier] = field.default
    return filled


def _blocked(outstanding: list[schema.Field]) -> CliError:
    """Exit 2, naming every field a prompt would have been needed for.

    Every one, not the first: a caller preparing an answers document wants the
    whole list in one run. And `BLOCKED` rather than `INVALID`, because nothing is
    wrong with what they supplied — a human has not decided these yet.
    """
    names = [field.identifier for field in outstanding]
    return CliError(
        "setup.answers_incomplete",
        f"--non-interactive was set and {len(names)} required field"
        f"{'s are' if len(names) != 1 else ' is'} absent from the answers document",
        exit_code=ExitCode.BLOCKED,
        remedy=Remedy(
            "human",
            "Add these fields to the answers document, or run without --non-interactive to be "
            "asked for them: " + ", ".join(names),
        ),
        detail={
            "field_errors": [
                {
                    "identifier": field.identifier,
                    "title": field.title,
                    "state": "blocked",
                    "message": f"{field.identifier} is required and would have been prompted for.",
                    "remedy": {"kind": "human", "text": field.description.split("\n", 1)[0]},
                }
                for field in outstanding
            ]
        },
    )


def _outstanding(root: Path, answers: dict, *, with_backend: bool) -> list[dict]:
    """What is still to be done, as records a consumer can render.

    This is the list the closing output is built from. It exists because a wizard
    that reports success without it implies the deployment is ready to run, and it
    is not: the inputs are collected, nothing is deployed, and the first apply is
    phased.
    """
    steps = []

    # Set anywhere Terraform loads — rendered by `globus init` or written by hand
    # into terraform.tfvars — counts as done. See preflight's `globus.inputs`.
    expected = schema.globus_variables() or ()
    globus = roots.globus_inputs(root, expected)
    done = bool(expected) and not globus.missing
    steps.append(
        {
            "identifier": "globus.inputs",
            "title": "Globus Terraform inputs",
            "state": "pass" if done else "blocked",
            "message": (
                f"all {len(expected)} set, in {', '.join(p.name for p in globus.sources)}"
                if done
                else f"{len(globus.missing) or 'the'} Globus variable(s) not set: "
                f"{', '.join(globus.missing) or 'unknown'}; this tool does not own them"
            ),
            "remedy": None
            if done
            else {
                "kind": "command",
                "text": "pixi run globus init  (read docs/globus-prerequisites.md first — "
                "several of its inputs come from gates that take days)",
            },
        }
    )

    if with_backend:
        steps.append(
            {
                "identifier": "state.bucket",
                "title": "Pre-stack buckets",
                "state": "blocked",
                "message": "backend.tf names a bucket that this tool has not created, because it "
                "creates no AWS resource. Two more buckets have to exist before the stack's first "
                "apply and survive its teardown: the imaging data bucket and the metrics bucket.",
                "remedy": {
                    "kind": "command",
                    "text": render.bootstrap_command(answers),
                },
            }
        )

    # The phased install is a published script, not a procedure to work by hand:
    # scripts/stack/install.sh is in the tree the deployer cloned, and the reference
    # deployment runs that same copy. Naming the command is what the capability
    # requires, and it is also what keeps this message true as the phases change —
    # the script's own --list-phases is the phase list, and nothing here restates it.
    steps.append(
        {
            "identifier": "install.phased",
            "title": "Phased first apply",
            "state": "blocked",
            "message": "a bare `terraform apply` against an empty account fails in the root's "
            "cluster lookup: the providers read a cluster that does not exist yet.",
            "remedy": {
                "kind": "command",
                "text": f"bash {INSTALLER} --root {root}   "
                f"(or one phase at a time with --phase N, which a first install wants; "
                f"`bash {INSTALLER} --list-phases` prints them)",
            },
        }
    )

    # Two things the install needs from a human. Named here because the installer
    # cannot supply either and both stop it part-way: the token at the Phase 4
    # apply, and an operator before the phase that closes the public endpoint. The
    # installer refuses rather than locking anyone out, but by then the deployer has
    # a half-built cluster and a reason to wonder what they did wrong.
    steps.append(
        {
            "identifier": "install.cloudflare_token",
            "title": "Cloudflare API token, in the environment",
            "state": "blocked",
            "message": "the installer reads CLOUDFLARE_API_TOKEN from the environment and never "
            "as a Terraform variable, which would persist it in state.",
            "remedy": {
                "kind": "human",
                "text": "Export an account-scoped token with Zero Trust: Edit, plus Tunnel and "
                "Access permissions, in the shell you install from. "
                "`pixi run cloudpipe preflight` checks it before you start.",
            },
        }
    )
    steps.append(
        {
            "identifier": "install.operator_accounts",
            "title": "An operator who can sign in",
            "state": "blocked",
            "message": "the phase that closes the public Kubernetes endpoint refuses to run "
            "against an empty user pool: after it the cluster is reached only over WARP, and "
            "WARP admits only identities the pool knows.",
            "remedy": {
                "kind": "human",
                "text": "Once the pool exists, create one account per operator_emails entry — "
                "the installer prints the command, and docs/deployer-first-hour.md covers what "
                "each operator does at their first sign-in.",
            },
        }
    )
    return steps
