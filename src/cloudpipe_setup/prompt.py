"""Line-oriented collection. One field at a time, over plain stdin.

Deliberately boring (design D10): no raw mode, no key handling, no cursor
positioning, no alternate screen. What that buys is a collection path that
survives WSL's terminal, `tmux`, a plain SSH session, and a test harness feeding
stdin from a file — the last being the reason a pseudo-terminal is ruled out.

Two details are load-bearing rather than cosmetic:

Prompts go to **stderr**, through the emitter, and the answer is read with a bare
`input()`. `input("prompt")` writes its prompt to stdout, which would put prose in
the middle of the JSON envelope. Keeping the two streams separate is what lets one
run be both watched by a person and parsed by a program.

Nothing here knows a field name, a question, a rule or an order. Every one comes
from `schema.fields()`, so a new input is a schema entry and no change to this
file. A second front end replaces this module and keeps everything else.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from . import config, schema
from .exits import CliError, ExitCode, Remedy
from .output import Emitter

#: Typed on its own to reprint the field's help. Documented in the preamble, and
#: cheap to discover by accident, which is the point: the help text is long and a
#: deployer who scrolled past it needs it back without losing their place.
HELP_TOKEN = "?"

#: Typed on its own to leave a field unanswered for now. The run then stops with
#: exit 2 rather than writing a partial `terraform.tfvars`: a half-rendered file
#: that Terraform then prompts for the rest of is worse than no file.
SKIP_TOKEN = "skip"


def collect(
    answers: dict[str, Any],
    *,
    emitter: Emitter,
    with_backend: bool = True,
    on_answer: Callable[[dict[str, Any]], None] | None = None,
    reader: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Ask for every field that is not derived, and return the answers.

    `on_answer` is called with the whole document after each accepted answer, so
    an interrupted run resumes from the answers document alone. `reader` is
    resolved at call time rather than as a default argument: a default binds
    `input` when this module is imported, so a caller replacing it afterwards
    would be ignored and the prompt would block on a terminal that is not there.
    """
    current = config.derive(dict(answers))
    pending = [field for field in schema.fields() if _is_asked(field, with_backend=with_backend)]
    style = _Style(emitter)

    _preamble(emitter, style, total=len(pending))

    section = None
    for position, field in enumerate(pending, start=1):
        if field.section != section:
            section = field.section
            _section_header(emitter, style, section)
        before = dict(current)
        current[field.identifier] = _ask(
            field, current, emitter, style, position, len(pending), reader
        )
        current = config.refresh_derived(before, current)
        if on_answer is not None:
            on_answer(current)

    _show_derived(emitter, style, current)
    return current


def _is_asked(field: schema.Field, *, with_backend: bool) -> bool:
    """Whether this field becomes a question.

    Derived fields are shown and overridable but never asked — both halves are
    required by the spec. A field that is required only when the backend is being
    rendered is not asked when it is not: a question with no effect reads as a
    setting that was ignored.
    """
    if field.is_derived:
        return False
    if field.required:
        return True
    return field.required_when is None or with_backend


def _ask(
    field: schema.Field,
    answers: dict[str, Any],
    emitter: Emitter,
    style: _Style,
    position: int,
    total: int,
    reader: Callable[[], str] | None,
) -> Any:
    """Ask until the answer satisfies every rule on the field.

    Re-asking rather than collecting the fault and moving on, which is the
    opposite of what `config.validate` does for a prepared document — and right
    for both. A person is here to fix it now; a document's author is not.
    """
    default = answers.get(field.identifier)
    if default in (None, "", []):
        default = field.default

    emitter.note()
    emitter.note(f"{style.dim(f'[{position}/{total}]')} {style.bold(field.title)}")
    _help(emitter, style, field)

    while True:
        raw = _read(field, emitter, style, default, reader)

        if raw == HELP_TOKEN:
            _help(emitter, style, field, full=True)
            continue

        if raw == SKIP_TOKEN:
            raise _skipped(field)

        if raw == "":
            if default in (None, "", []):
                emitter.note(style.warn("  This one has no default — it has to be answered."))
                continue
            return default

        value = (
            [entry.strip() for entry in raw.split(",") if entry.strip()] if field.is_list else raw
        )

        problems = config.validate_field(field, value)
        if not problems:
            return value
        for problem in problems:
            emitter.note(style.warn(f"  {problem.message}"))


def _read(
    field: schema.Field,
    emitter: Emitter,
    style: _Style,
    default: Any,
    reader: Callable[[], str] | None,
) -> str:
    """One line from the deployer, or a refusal when the input has run out.

    EOF is an error rather than an empty answer. Collection driven from a file
    that stops early would otherwise write a `terraform.tfvars` built from
    defaults nobody chose.
    """
    shown = _format_default(default)
    emitter.note(f"  {field.identifier}{style.dim(shown)}: ", end="")
    try:
        return (reader or input)().strip()
    except EOFError as err:
        raise CliError(
            "prompt.input_exhausted",
            f"input ended while asking for {field.identifier}",
            exit_code=ExitCode.BLOCKED,
            remedy=Remedy(
                "human",
                "Answer the remaining fields, or prepare an answers document and use "
                "--non-interactive. Everything answered so far has been saved.",
            ),
            detail={"field_errors": [{"identifier": field.identifier, "message": "unanswered"}]},
        ) from err


def _format_default(default: Any) -> str:
    if default in (None, "", []):
        return ""
    if isinstance(default, list):
        return f" [{', '.join(str(entry) for entry in default)}]"
    return f" [{default}]"


def _help(emitter: Emitter, style: _Style, field: schema.Field, *, full: bool = False) -> None:
    """The field's help text: its first paragraph, or all of it on `?`.

    Truncated by default because several of these run to three paragraphs, and a
    wall of text before every question is text people stop reading. The `?` hint
    is only shown when there is more to see.
    """
    paragraphs = field.description.split("\n\n")
    emitter.note(f"  {style.dim(_SOURCE_LABELS[field.source])}")
    for paragraph in paragraphs if full else paragraphs[:1]:
        emitter.note(f"  {paragraph}")
    if not full and len(paragraphs) > 1:
        emitter.note(style.dim(f"  Type {HELP_TOKEN} for the rest."))


def _preamble(emitter: Emitter, style: _Style, *, total: int) -> None:
    emitter.note(style.bold(f"Collecting {total} values for a CloudPipe deployment."))
    emitter.note(
        f"  Press Enter to accept a value in brackets. Type {HELP_TOKEN} for more help on a "
        f"field, or {SKIP_TOKEN} to stop and come back."
    )
    emitter.note("  Nothing is created in any cloud account, and no credential is asked for.")
    emitter.note(
        "  The Globus values are not here: `pixi run globus init` owns them and renders them "
        "separately."
    )


def _section_header(emitter: Emitter, style: _Style, identifier: str) -> None:
    title = next(
        (section.title for section in schema.sections() if section.identifier == identifier),
        identifier,
    )
    emitter.note()
    emitter.note(style.bold(f"--- {title} ---"))


def _show_derived(emitter: Emitter, style: _Style, answers: dict[str, Any]) -> None:
    """Show every derived value, so none of them is a surprise at `plan` time.

    Shown rather than asked, and the message says where to change it. A value
    computed silently is one the deployer did not agree to.
    """
    derived = [field for field in schema.fields() if field.is_derived]
    if not derived:
        return
    emitter.note()
    emitter.note(style.bold("--- Derived ---"))
    for field in derived:
        value = answers.get(field.identifier)
        emitter.note(f"  {field.identifier} = {value!r}")
        emitter.note(
            style.dim(
                f"    computed from {', '.join(field.derived_from)}. Edit it in the answers "
                "document to override; see its description in the input schema for when to."
            )
        )


def _skipped(field: schema.Field) -> CliError:
    return CliError(
        "prompt.skipped",
        f"{field.identifier} was skipped, so the Terraform files were not written",
        exit_code=ExitCode.BLOCKED,
        remedy=Remedy(
            "human",
            "Find the value and run `pixi run cloudpipe setup` again. Everything answered so "
            "far has been saved and is offered back as the default.",
        ),
        detail={"field_errors": [{"identifier": field.identifier, "message": "skipped"}]},
    )


#: What `x-source` means to a person standing in front of the question. Keyed by
#: the schema's own enum, so a new source value fails loudly here rather than
#: being shown as a bare identifier.
_SOURCE_LABELS = {
    "aws-console": "Look this up in the AWS console.",
    "cloudflare-dashboard": "Look this up in the Cloudflare dashboard.",
    "identity-provider": "Your identity provider's administrator knows this.",
    "source-control-host": "This comes from your source-control host.",
    "institution": "This comes from your institution.",
    "deployer-choice": "This one is yours to decide.",
    "derived": "Computed from another answer.",
}


class _Style:
    """ANSI styling, or nothing at all.

    Colour is enabled only on a terminal with `NO_COLOR` unset, and never carries
    meaning on its own — every warning reads the same with the codes stripped. The
    stream asked is the one the emitter actually writes to, so a captured run
    produces plain text rather than escape codes in a buffer.
    """

    def __init__(self, emitter: Emitter) -> None:
        stream = emitter.stderr
        self.enabled = bool(
            getattr(stream, "isatty", None) and stream.isatty() and not os.environ.get("NO_COLOR")
        )

    def _wrap(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap(text, "1")

    def dim(self, text: str) -> str:
        return self._wrap(text, "2")

    def warn(self, text: str) -> str:
        return self._wrap(text, "33")
