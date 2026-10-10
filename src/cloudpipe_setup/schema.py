"""The published input schema, as objects the rest of the package reads.

This is the only module that knows the schema's file layout. Everything else asks
for `fields()` and gets prompts, help text, rules and groupings — which is the
point of design D1: a second front end replaces `prompt.py`, not this.

Nothing here hard-codes a field, a prompt, a rule or an order. A new input is a
new entry in `schemas/inputs.schema.json` and no change to any Python file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).parent / "schemas" / "inputs.schema.json"


@dataclass(frozen=True)
class Rule:
    """One pattern a value must match, and the message to show when it does not.

    `message` is the Terraform `error_message` verbatim — see design D9's
    clarification and `tests/test_setup_wizard_schema.py`. Holding the two equal
    is what stops the wizard and `terraform plan` wording the same rule
    differently.
    """

    pattern: str
    message: str

    def accepts(self, value: str) -> bool:
        return re.search(self.pattern, value) is not None


@dataclass(frozen=True)
class Field:
    """One input, in the shape a prompter or a form generator needs."""

    identifier: str
    title: str
    description: str
    type: str
    rules: tuple[Rule, ...]
    section: str
    source: str
    required: bool
    default: Any | None
    min_items: int | None
    #: The field's own `x-error-message`, for a rule that is not a pattern. Today
    #: that is `minItems` on the one list field, whose Terraform condition is
    #: `length(var.x) > 0` — so the message lives on the field rather than beside a
    #: pattern, and a reader that only looked at `rules` would lose Terraform's
    #: wording and substitute its own.
    error_message: str | None
    terraform_variable: str | None
    derived_from: tuple[str, ...]
    derived_template: str | None
    required_when: str | None
    #: The rules every entry of a list field must satisfy, from `items`.
    item_rules: tuple[Rule, ...] = ()
    #: A field whose JSON Schema `type` admits `null`. Null is then an answer, not
    #: an absence: it renders as `null` in tfvars, and a required nullable field
    #: is satisfied by it. `null_meaning` is what choosing it does, for the prompt.
    nullable: bool = False
    null_meaning: str | None = None
    #: Why this field is never prompted for, or None when it is. An object whose
    #: shape belongs to someone else — `cognito_federation` — is written into the
    #: answers document by hand, then validated and rendered like any other.
    not_asked: str | None = None
    #: The nested schema of an object field, for the validation that cannot be a
    #: pattern on a scalar. A tuple of (name, subschema) rather than a dict, so
    #: the dataclass stays hashable and needs no mutable default.
    properties: tuple[tuple[str, Any], ...] = ()
    required_properties: tuple[str, ...] = ()

    @property
    def property_schemas(self) -> dict[str, Any]:
        return dict(self.properties)

    @property
    def is_object(self) -> bool:
        return self.type == "object"

    @property
    def is_derived(self) -> bool:
        return bool(self.derived_template)

    @property
    def is_list(self) -> bool:
        return self.type == "array"

    def derive(self, answers: dict[str, Any]) -> list[str] | None:
        """The computed value, or None when a source field is still missing.

        A derived field is displayed and overridable, never asked — the
        `deployment-setup-wizard` spec requires both halves. Returning None rather
        than raising lets collection reach the field before its source is
        answered, which an out-of-order answers document can do.
        """
        if not self.derived_template:
            return None
        if any(
            source not in answers or answers[source] in (None, "") for source in self.derived_from
        ):
            return None
        rendered = self.derived_template.format(
            **{source: answers[source] for source in self.derived_from}
        )
        return [rendered] if self.is_list else rendered


@dataclass(frozen=True)
class Section:
    identifier: str
    title: str


@lru_cache(maxsize=1)
def document() -> dict[str, Any]:
    """The raw schema. Cached: it is a published file, constant within a run."""
    return json.loads(SCHEMA_PATH.read_text())


def version() -> str:
    """The schema's declared contract version, which equals `CONTRACT_VERSION`.

    Spelled `x-schema-version` in the file rather than `schema_version`, because
    JSON Schema reserves unprefixed keywords and this document already carries
    eight `x-` extensions. The JSON envelopes use the unprefixed `schema_version`
    for the same value; they are not JSON Schema documents and have no such
    reservation.

    One value, declared in two places, which is why `tests/test_setup_wizard_contract.py`
    holds them equal: a consumer that pinned the schema it validates against must
    be able to tell from the envelope alone whether this run speaks the same
    contract.
    """
    return str(document()["x-schema-version"])


@lru_cache(maxsize=1)
def sections() -> tuple[Section, ...]:
    """Groupings in presentation order, as declared in `$defs.section`."""
    return tuple(
        Section(entry["identifier"], entry["title"])
        for entry in document()["$defs"]["section"]["x-order"]
    )


def _rules(body: dict[str, Any]) -> tuple[Rule, ...]:
    """Every pattern on a field, from `pattern` and from `allOf`.

    A JSON Schema field takes one `pattern`, so a variable with two Terraform
    `validation` blocks is an `allOf` of single-pattern subschemas. Both are
    collected here so a caller never has to know which form a field used.
    """
    collected = []
    for carrier in (body, *body.get("allOf", ())):
        if "pattern" in carrier:
            collected.append(Rule(carrier["pattern"], carrier["x-error-message"]))
    return tuple(collected)


@lru_cache(maxsize=1)
def fields() -> tuple[Field, ...]:
    """Every input, in schema order, grouped by section for presentation.

    Schema order within a section is deliberate rather than sorted: the sections
    run AWS, identity, source control, Cloudflare, state, which is roughly the
    order a deployer can answer them in.
    """
    schema = document()
    required = set(schema["required"])
    required_when = schema.get("x-required-when", {})
    by_section = {section.identifier: [] for section in sections()}

    for identifier, body in schema["properties"].items():
        types = body["type"] if isinstance(body["type"], list) else [body["type"]]
        (base_type,) = [t for t in types if t != "null"]
        field = Field(
            identifier=identifier,
            title=body["title"],
            description=body["description"],
            type=base_type,
            nullable="null" in types,
            null_meaning=body.get("x-null-meaning"),
            not_asked=body.get("x-not-asked"),
            properties=tuple(body.get("properties", {}).items()),
            required_properties=tuple(body.get("required", ())),
            item_rules=_rules(body.get("items", {})),
            rules=_rules(body),
            section=body["x-section"],
            source=body["x-source"],
            required=identifier in required,
            default=body.get("default"),
            min_items=body.get("minItems"),
            error_message=body.get("x-error-message"),
            terraform_variable=body["x-terraform-variable"],
            derived_from=tuple(body.get("x-derived-from", ())),
            derived_template=body.get("x-derived-template"),
            required_when=required_when.get(identifier),
        )
        by_section[field.section].append(field)

    return tuple(field for section in sections() for field in by_section[section.identifier])


def field(identifier: str) -> Field:
    for candidate in fields():
        if candidate.identifier == identifier:
            return candidate
    raise KeyError(identifier)


def terraform_fields() -> tuple[Field, ...]:
    """The fields that bind to a Terraform variable, i.e. what tfvars holds."""
    return tuple(f for f in fields() if f.terraform_variable is not None)


def backend_fields() -> tuple[Field, ...]:
    """The fields that bind to no variable: the backend block takes none (D12)."""
    return tuple(f for f in fields() if f.terraform_variable is None)


#: The stack's variable declarations. Read, never restated — the same rule as the
#: Terraform floor and the Access secret's name.
STACK_VARIABLES = Path(__file__).resolve().parents[2] / "terraform/modules/stack/variables.tf"


def globus_variables(variables_tf: Path = STACK_VARIABLES) -> tuple[str, ...] | None:
    """The stack variables a deployer must supply that this wizard does not own.

    Derived as every variable the stack requires, minus the ones this schema
    binds. That is the ownership split itself (design D4), which
    `tests/test_setup_wizard_schema.py` holds exact: every required variable has
    exactly one owner. So the result is the Globus set without listing it here,
    and without importing `globus_admin`, whose package pulls in `globus-sdk`.

    `None` when the declarations cannot be read, so a caller reports that rather
    than guessing at a list.
    """
    try:
        text = variables_tf.read_text()
    except OSError:
        return None
    owned = {field.terraform_variable for field in terraform_fields()}
    return tuple(sorted(required_variables(text) - owned))


def required_variables(text: str) -> set[str]:
    """Every variable a deployer may have to supply: unconditional and conditional.

    "No default" alone stopped being the whole answer when the Globus ingress
    became opt-in. Its seven inputs now default to the empty string and are
    rejected as empty only when `globus_enabled` is true, so by the old reading
    they are optional — and a deployer who turned the ingress on would be told
    nothing about them until `terraform plan` refused the empty values.

    Terraform has no "conditionally required" keyword, so the cross-variable
    validation IS the declaration, and reading it is reading the source of truth
    rather than keeping a parallel list in step with it.
    """
    return variables_without_default(text) | variables_required_by_another(text)


def variables_without_default(text: str) -> set[str]:
    """Names of the `variable` blocks in `text` that declare no `default`.

    Brace-matched rather than regexed whole, because a body holds nested
    `validation { ... }` blocks and a non-greedy match would stop at the first
    closing brace. Braces inside strings are skipped, since an `error_message` can
    quote one.
    """
    names = set()
    for name, body in _variable_bodies(text):
        if not re.search(r"^\s*default\s*=", body, re.M):
            names.add(name)
    return names


def variables_required_by_another(text: str) -> set[str]:
    """Variables whose own validation turns on the value of a different variable.

    A `validation` condition of the form `!var.flag || <rule>` says the rule
    applies only when `flag` is set — which is how a defaulted variable declares
    that it is required under some condition. Matched by the cross-reference
    rather than by the `!var.x ||` spelling, so a condition written
    `var.x == false || ...` or `var.x ? <rule> : true` counts the same.

    Self-references are excluded: every condition names its own variable.
    """
    names = set()
    for name, body in _variable_bodies(text):
        for condition in re.findall(r"^\s*condition\s*=\s*(.+)$", body, re.M):
            if {ref for ref in re.findall(r"var\.([a-z0-9_]+)", condition) if ref != name}:
                names.add(name)
    return names


def _variable_bodies(text: str) -> list[tuple[str, str]]:
    """Each `variable "name" { ... }` as (name, body), brace-matched."""
    return [
        (match.group(1), _block_body(text, match.end()))
        for match in re.finditer(r'^variable\s+"([^"]+)"\s*\{', text, re.M)
    ]


def _block_body(text: str, start: int) -> str:
    """The text from `start` (just past an opening brace) to its matching close."""
    depth, index, in_string = 1, start, False
    while index < len(text) and depth:
        char = text[index]
        if in_string:
            if char == "\\":
                index += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return text[start : index - 1]
