"""What the browser form does with a value, with no NiceGUI anywhere in it.

Three jobs. Turn the text in a box into the value the answers document holds
(`parse`, `to_text`). Apply it the way the terminal prompt does, re-deriving
what was derived and keeping what was overridden (`update`). And drive the CLI
for everything else (`cli`, `phases`), reading its JSON envelope rather than
importing its commands, so the browser and a terminal produce the same files
from the same answers by construction.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config, render, schema
from ..commands.setup import INSTALLER

#: Long enough for preflight, whose checks make network calls to AWS and
#: Cloudflare one after another. A hung call becomes an error the page can show,
#: rather than a spinner that never stops.
CLI_TIMEOUT_S = 600


def shown(section: str, *, with_backend: bool) -> list[schema.Field]:
    """The fields a section displays, in schema order.

    Questions, derived values and the one field written by hand. The form shows
    all three where the prompt asks only the first, because a page can show a
    computed value beside the question it comes from, and the prompt can only
    list it at the end.
    """
    return [
        field
        for field in schema.fields()
        if field.section == section
        and (
            config.is_asked(field, with_backend=with_backend)
            or field.not_asked
            or (field.is_derived and (field.required_when is None or with_backend))
        )
    ]


def literal_markdown(text: str) -> str:
    """Schema or CLI text, made safe to render as Markdown without losing a `<placeholder>`.

    The help text and remedies write placeholders as `<your domain>`, `<name>`,
    `<code>`. Rendered as Markdown each is an HTML tag: the sanitizer drops it, so
    "grafana.<your domain>" showed as "grafana.", and `<code>` started a real code
    element. Inside a backtick code span `<` is already literal, so only the
    text between spans is escaped.
    """
    parts = re.split(r"(`[^`]*`)", text)
    return "".join(
        part if part.startswith("`") else part.replace("&", "&amp;").replace("<", "&lt;")
        for part in parts
    )


def to_text(value: Any) -> str:
    """A stored value as the text a box shows. A list is comma-separated, as the prompt takes it."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(entry) for entry in value)
    return str(value)


def initial_text(field: schema.Field, answers: dict[str, Any]) -> str:
    """What a box starts with: the stored answer, a suggestion, or the schema's default."""
    stored = answers.get(field.identifier)
    if stored not in (None, "", []):
        return to_text(stored)
    suggested = field.suggest(answers)
    return to_text(suggested if suggested is not None else field.default)


def is_none(field: schema.Field, answers: dict[str, Any]) -> bool:
    """Whether the document holds an explicit null for a nullable field."""
    return field.nullable and field.identifier in answers and answers[field.identifier] is None


def parse(field: schema.Field, text: str, *, none: bool = False) -> Any:
    """The value a box's text stands for.

    `none` is the "None" box beside a nullable field, which is the form's
    spelling of the prompt's `none`. An empty box is the empty string or the
    empty list, which `update` reads as "not answered".
    """
    if none and field.nullable:
        return None
    text = text.strip()
    if field.is_list:
        return [entry.strip() for entry in text.split(",") if entry.strip()]
    return text


def problems(field: schema.Field, value: Any) -> list[str]:
    """Every rule the value breaks, in Terraform's own words.

    An empty value reports nothing here. Whether a field is still missing is a
    question about the whole document, asked when the files are written; asking
    it on every keystroke would paint a fresh form red.
    """
    if value in ("", []):
        return []
    return [record.message for record in config.validate_field(field, value)]


def update(answers: dict[str, Any], field: schema.Field, value: Any) -> dict[str, Any]:
    """The answers with `value` applied, and the derived and suggested fields brought up to date.

    An empty value removes the field rather than storing an empty string. For a
    derived or suggested field that is the way back to the computed value: delete
    the override and it is filled again. Suggestions first, because a derived
    field may read one (`github_user_url` from `gitops_repo_url`).
    """
    after = dict(answers)
    if value in ("", []):
        after.pop(field.identifier, None)
    else:
        after[field.identifier] = value
    suggested = config.refresh_suggested(answers, after)
    return config.derive(config.refresh_derived(answers, suggested))


@dataclass(frozen=True)
class Envelope:
    """One CLI run: its JSON envelope, and the human text it wrote to stderr."""

    exit_code: int
    ok: bool
    data: dict[str, Any]
    error: dict[str, Any] | None
    notes: str

    @property
    def field_errors(self) -> dict[str, list[str]]:
        """Each field's messages, so the form can put them under the box they belong to."""
        grouped: dict[str, list[str]] = {}
        for entry in (self.error or {}).get("detail", {}).get("field_errors", ()):
            grouped.setdefault(entry["identifier"], []).append(entry["message"])
        return grouped


def cli(
    subcommand: str,
    *,
    answers_path: Path,
    root: Path,
    extra: tuple[str, ...] = (),
) -> Envelope:
    """Run `cloudpipe <subcommand>` non-interactively and return what it reported.

    A subprocess rather than a call into `cli.main`: the envelope is the published
    contract, and this is the cheapest proof that a front end can be built on it
    alone. Absolute paths throughout, so `INIT_CWD` cannot move either one.
    """
    argv = [
        sys.executable,
        "-m",
        "cloudpipe_setup",
        subcommand,
        "--json",
        "--non-interactive",
        "--answers",
        str(answers_path.resolve()),
        "--root",
        str(root.resolve()),
        *extra,
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(render.CLONE / "src"), env.get("PYTHONPATH")) if part
    )
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=env,
            cwd=render.CLONE,
            timeout=CLI_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        return _broken(
            "gui.timeout",
            f"`{subcommand}` did not finish in {CLI_TIMEOUT_S} s",
            notes=_text(err.stderr),
        )

    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError:
        # No envelope is a bug in the CLI, not a state to render. Keep the raw
        # output, because translating an error must never hide it.
        return _broken(
            "gui.no_envelope",
            f"`{subcommand}` exited {completed.returncode} without a JSON envelope",
            notes=completed.stderr,
            raw=completed.stdout,
            exit_code=completed.returncode,
        )
    return Envelope(
        exit_code=envelope["exit_code"],
        ok=envelope["ok"],
        data=envelope["data"],
        error=envelope["error"],
        notes=completed.stderr,
    )


def _broken(
    code: str, message: str, *, notes: str, raw: str | None = None, exit_code: int = 1
) -> Envelope:
    return Envelope(
        exit_code=exit_code,
        ok=False,
        data={},
        error={"code": code, "message": message, "remedy": None, "raw": raw, "detail": {}},
        notes=notes,
    )


def _text(stream: str | bytes | None) -> str:
    if isinstance(stream, bytes):
        return stream.decode(errors="replace")
    return stream or ""


def installer() -> Path:
    return render.CLONE / INSTALLER


def phases() -> list[dict[str, Any]]:
    """The installer's phases, from `--list-phases --json`, which reads no state or credentials.

    Never restated here: design D2 of publish-phased-installer makes that list the
    only place the phases are enumerated.
    """
    completed = subprocess.run(
        ["bash", str(installer()), "--list-phases", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def phase_command(root: Path, phase: int | None = None) -> str:
    """The command that runs one phase, or all of them, from the deployer's terminal."""
    command = f"bash {shlex.quote(str(installer()))} --root {shlex.quote(str(root))}"
    return command if phase is None else f"{command} --phase {phase}"
