"""The answers document's published schema, read as data.

`schemas/answers.schema.json` is the contract; this module is the way the rest of
the code reads it, so nothing restates a field name, a prompt, or the gate list in
Python. `globus init` renders from it (task 8.10), `setup-status` names gates from
it, and a future wizard can render a form from the schema alone.

Validation lives here too, and is driven by the schema rather than restating it:
every rule a problem can report comes from reading the same file a form
generator reads, so the two can never disagree about what is valid. `init`
decides how to present the problems; it does not decide what they are.

`jsonschema` is deliberately not used. It is not a declared dependency, and its
errors name JSON Pointers and failing subschemas — accurate, and the wrong thing
to show someone filling in a form. The rules here are few enough to check
directly, and the result is one sentence per field.

There is no accessor for an AWS credential, because the schema has no field for
one: AWS credentials come from the operator's environment through an SSO profile,
and `aws_account_id` exists only so a command can refuse to act against the wrong
account.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).with_name("schemas") / "answers.schema.json"

#: How a secret is referred to rather than written down. `secretsmanager:<name>`
#: reads from AWS Secrets Manager; `ssm:<parameter-name>` reads a SecureString from
#: SSM Parameter Store; `env:<VARIABLE>` reads from the environment.
#:
#: `ssm:` exists because production's GCS service-client secret is already an SSM
#: SecureString at `/cloudpipe/globus/gcs-client-secret`, alongside its client id and
#: deployment key. Without this scheme an answers document could not truthfully say
#: where that secret lives, and the only alternative was moving a live secret between
#: stores to satisfy the schema — a migration with real blast radius, to fix a
#: description. Note the consequence: Globus secrets now live in two stores, so
#: anything that enumerates them must consult both.
#:
#: Kept in step with the schema's `pattern` by
#: `tests/globus/test_answers_schema.py::test_the_pattern_and_the_scheme_list_agree`.
#: The two encodings are independent — the pattern gates the document, this tuple
#: gates `is_secret_reference` — so a scheme added to one alone splits them silently.
SECRET_SCHEMES = ("secretsmanager", "ssm", "env")


@dataclass(frozen=True)
class Field:
    """One answer, described well enough to render an input for it.

    `gate` is the human gate that produces the value, where one applies — the
    difference between an empty box and "waiting on the data provider". `derived`
    marks a value that has a working default and must not be presented as a
    question; it is here so a deployment that needs an override has somewhere to
    put it, not so anyone is asked.
    """

    name: str
    type: str
    required: bool
    prompt: str
    help: str
    pattern: str | None = None
    gate: str | None = None
    derived: bool = False

    @property
    def secret_reference(self) -> bool:
        """Whether this field holds a pointer to a secret rather than a value."""
        return self.name.endswith("_secret_ref")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "prompt": self.prompt,
            "help": self.help,
            "pattern": self.pattern,
            "gate": self.gate,
            "derived": self.derived,
        }


@lru_cache(maxsize=1)
def schema() -> dict[str, Any]:
    """The published schema. Cached: it is a file that cannot change mid-run."""
    return json.loads(SCHEMA_PATH.read_text())


def gates() -> tuple[str, ...]:
    """The human-gate vocabulary, from the schema rather than from a second list."""
    return tuple(schema()["$defs"]["gate"]["enum"])


def fields() -> tuple[Field, ...]:
    """Every field, in the schema's own order.

    Property order in the file is the order a form should ask in — required
    values first, then the ones that can wait, then the overrides — so it is
    preserved rather than sorted. `json.loads` keeps insertion order, and the
    test suite asserts required fields come before optional ones.
    """
    document = schema()
    required = set(document.get("required") or ())
    # A field named only under `anyOf` is required in the sense that matters to
    # someone filling the form in: exactly one of the alternatives must be given.
    conditional = {
        name for branch in document.get("anyOf") or () for name in branch.get("required") or ()
    }

    built = []
    for name, spec in document["properties"].items():
        built.append(
            Field(
                name=name,
                type=str(spec.get("type", "string")),
                required=name in required or name in conditional,
                prompt=str(spec.get("title") or name),
                help=str(spec.get("description") or ""),
                pattern=spec.get("pattern"),
                gate=spec.get("x-gate"),
                derived=bool(spec.get("x-derived", False)),
            )
        )
    return tuple(built)


def field(name: str) -> Field | None:
    return next((f for f in fields() if f.name == name), None)


def required_names() -> tuple[str, ...]:
    """Fields with no default and no way to be derived. The setup's real cost."""
    return tuple(f.name for f in fields() if f.required)


@dataclass(frozen=True)
class Problem:
    """One thing wrong with one field.

    Field-level rather than document-level on purpose: someone filling in a dozen
    values wants every mistake at once, not the first one, and a report that names
    the field can be shown next to the input that produced it.
    """

    field: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "message": self.message}


_TYPE_NAMES = {
    "string": (str,),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
}


def validate(document: Any) -> list[Problem]:
    """Every problem with `document`, one per offending field.

    Ordered by the schema's field order rather than by discovery, so the report
    reads in the same order as the form.
    """
    if not isinstance(document, dict):
        return [Problem("", f"the answers must be a mapping, not {type(document).__name__}")]

    problems: list[Problem] = []
    by_name = {f.name: f for f in fields()}

    for name in document:
        if name not in by_name:
            problems.append(Problem(name, f"{name}: not a field this schema defines"))

    document_schema = schema()
    for field_ in fields():
        spec = document_schema["properties"][field_.name]
        value = document.get(field_.name)

        if field_.name not in document or value is None or value == "":
            # An `anyOf` alternative is required as a group, and is reported once
            # against the group rather than as two separate missing fields.
            if field_.name in (document_schema.get("required") or ()):
                problems.append(Problem(field_.name, f"{field_.name}: required, and missing"))
            continue

        problems.extend(_field_problems(field_, spec, value))

    problems.extend(_group_problems(document, document_schema))
    return problems


def _field_problems(field_: Field, spec: dict[str, Any], value: Any) -> list[Problem]:
    expected = _TYPE_NAMES.get(field_.type, (str,))
    # `bool` is an `int` in Python, so a stray `true` would pass an integer check.
    if not isinstance(value, expected) or (field_.type == "integer" and isinstance(value, bool)):
        return [
            Problem(
                field_.name,
                f"{field_.name}: expected {field_.type}, got {type(value).__name__}",
            )
        ]

    problems = []
    pattern = spec.get("pattern")
    if pattern and isinstance(value, str) and not re.match(pattern, value):
        problems.append(
            Problem(
                field_.name,
                f"{field_.name}: {value!r} is not a valid {field_.prompt.lower()} "
                f"(expected {pattern})",
            )
        )
    minimum = spec.get("minimum")
    if minimum is not None and isinstance(value, int) and value < minimum:
        problems.append(Problem(field_.name, f"{field_.name}: must be {minimum} or greater"))
    if isinstance(value, list):
        problems.extend(_item_problems(field_, spec, value))
    return problems


def _item_problems(field_: Field, spec: dict[str, Any], value: list[Any]) -> list[Problem]:
    problems = []
    if len(value) < int(spec.get("minItems", 0)):
        problems.append(Problem(field_.name, f"{field_.name}: needs at least one entry"))
    item_pattern = (spec.get("items") or {}).get("pattern")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            problems.append(
                Problem(
                    field_.name, f"{field_.name}[{index}]: expected text, got {type(item).__name__}"
                )
            )
        elif item_pattern and not re.match(item_pattern, item):
            problems.append(
                Problem(field_.name, f"{field_.name}[{index}]: {item!r} is not valid here")
            )
    return problems


def _group_problems(document: dict[str, Any], document_schema: dict[str, Any]) -> list[Problem]:
    """Rules about a set of fields rather than about one.

    Today that is only "give an identity domain in one form or the other", which
    no single field can check: each one is individually optional, and the mistake
    is supplying neither.
    """
    branches = [
        tuple(branch.get("required") or ()) for branch in document_schema.get("anyOf") or ()
    ]
    branches = [names for names in branches if names]
    if not branches:
        return []

    # `anyOf` is satisfied by ONE branch, so the failure is "none of them", and it
    # is reported once. Reporting per branch would tell someone who supplied
    # neither form that they have two separate problems.
    if any(all(document.get(name) for name in names) for names in branches):
        return []

    alternatives = " or ".join(f"`{name}`" for names in branches for name in names)
    return [
        Problem(
            branches[0][0],
            f"{alternatives}: one of these is required, and none is set",
        )
    ]


def is_secret_reference(value: str) -> bool:
    """Whether `value` points at a secret instead of being one.

    Used to keep a literal out of the answers document. The check is on the shape
    of the reference, not on whether the target resolves: a wrong pointer is a
    mistake someone can see and fix, while a pasted secret has to be rotated.
    """
    scheme, _, rest = value.partition(":")
    return scheme in SECRET_SCHEMES and bool(rest)
