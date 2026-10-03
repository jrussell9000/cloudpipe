"""Read and validate an answers document.

The answers document is the stored artifact and `terraform.tfvars` is derived from
it (design D5). So this module is where a prepared document meets the published
rules, and the one behaviour that matters here is that it reports **every**
offending field rather than stopping at the first: a deployer who pasted in a
document with four faults should learn all four in one run, not go four rounds.

It writes nothing. `save()` is the only function that touches the disk, and only
the answers document.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from . import schema
from .exits import CliError, ExitCode, Remedy
from .output import Record

DEFAULT_ANSWERS_NAME = "cloudpipe-answers.yaml"

#: Keys that would mean someone is trying to hand us a credential. Checked by
#: name, before anything else, and never echoed back: the rule is that AWS
#: credentials come from the environment through an SSO profile, and a tool that
#: silently accepted a key here would be the reason someone put one in a file.
CREDENTIAL_KEYS = frozenset(
    {
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "access_key",
        "secret_key",
        "secret_access_key",
        "cloudflare_api_token",
        "cloudflare_api_key",
    }
)


def load(path: str | Path | None) -> tuple[dict[str, Any], Path | None]:
    """The answers document, or an empty one when there is nothing to read.

    Returns the answers and the path they came from. A `None` path means no
    document existed, which is the normal first interactive run — it is not an
    error, and it is the caller that decides whether the absence is fatal.
    """
    resolved = resolve_path(path)
    if resolved is None or not resolved.exists():
        return {}, None

    text = resolved.read_text()
    try:
        # `yaml.safe_load` reads JSON too — JSON is a YAML subset — so a caller
        # that would rather write JSON needs no flag and no second code path.
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise CliError(
            "answers.unparsable",
            f"{resolved} is not valid YAML or JSON",
            exit_code=ExitCode.INVALID,
            raw=str(err),
            remedy=Remedy("human", f"Fix the syntax in {resolved}, or delete it and start again."),
        ) from err

    if parsed is None:
        return {}, resolved
    if not isinstance(parsed, dict):
        raise CliError(
            "answers.not_a_mapping",
            f"{resolved} must hold a mapping of field names to values, not a "
            f"{type(parsed).__name__}",
            exit_code=ExitCode.INVALID,
            remedy=Remedy("human", f"Rewrite {resolved} as `field: value` lines."),
        )
    return parsed, resolved


def resolve_path(path: str | Path | None) -> Path | None:
    if path is not None:
        return Path(path).expanduser()
    return Path.cwd() / DEFAULT_ANSWERS_NAME


def reject_credentials(answers: dict[str, Any]) -> None:
    """Refuse a document carrying a credential, naming no value.

    Separate from `validate` and run before it, because this is not a validation
    failure to be collected alongside others — it is a document that must not be
    read any further. The offending value is never put in a message.
    """
    offending = sorted(set(answers) & CREDENTIAL_KEYS)
    if not offending:
        return
    raise CliError(
        "answers.credential_supplied",
        f"the answers document contains credential fields ({', '.join(offending)}). This tool "
        "never takes credentials: AWS credentials come from an AWS CLI v2 SSO profile in your "
        "environment, and the Cloudflare token from CLOUDFLARE_API_TOKEN.",
        exit_code=ExitCode.INVALID,
        remedy=Remedy(
            "human",
            "Remove those fields from the answers document, rotate the credential they held "
            "because it has been written to disk, and run `aws sso login` instead.",
        ),
        detail={"fields": offending},
    )


def derive(answers: dict[str, Any]) -> dict[str, Any]:
    """Fill every derived field that is absent, leaving any override alone.

    Pure: returns a new mapping. A derived field already present in the document
    is an override and is never recomputed — that is the half of design D13 that
    makes deriving safe for the three cases needing a different value.
    """
    filled = dict(answers)
    for field in schema.fields():
        if not field.is_derived or filled.get(field.identifier) not in (None, "", []):
            continue
        computed = field.derive(filled)
        if computed is not None:
            filled[field.identifier] = computed
    return filled


def overridden_derived(answers: dict[str, Any]) -> list[tuple[schema.Field, Any]]:
    """Derived fields whose stored value is not what the sources would compute.

    Reported, never corrected. An override is legitimate — three real cases need
    one — but so is a stale value left behind when its source changed, and the two
    are indistinguishable from the document alone. Saying so is the whole remedy:
    a wrong OIDC subject claim fails nowhere, it just silently costs a build its
    credentials.
    """
    stale = []
    for field in schema.fields():
        if not field.is_derived:
            continue
        computed = field.derive(answers)
        stored = answers.get(field.identifier)
        if computed is not None and stored not in (None, "", []) and stored != computed:
            stale.append((field, computed))
    return stale


def refresh_derived(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Recompute derived fields that `before` had left at their derived value.

    The case this exists for: a deployer re-runs and changes `github_repo`. The
    stored subject claim was derived from the old repository, so keeping it would
    leave the trust policy naming a repository nobody pushes to. A value that did
    NOT match the old derivation is an override and is left alone.
    """
    updated = dict(after)
    for field in schema.fields():
        if not field.is_derived:
            continue
        was_derived = field.derive(before)
        stored = updated.get(field.identifier)
        if stored not in (None, "", []) and stored != was_derived:
            continue  # an override, not a stale derivation
        computed = field.derive(updated)
        if computed is not None:
            updated[field.identifier] = computed
    return updated


def validate(answers: dict[str, Any], *, with_backend: bool = True) -> list[Record]:
    """Every problem with these answers, as one record per offending field.

    Returns an empty list when the document is usable. The caller decides what to
    do with a non-empty one; nothing here raises on a field-level fault, because
    raising is what makes a validator stop at the first.
    """
    problems: list[Record] = []
    known = {field.identifier for field in schema.fields()}

    for unknown in sorted(set(answers) - known):
        problems.append(
            Record(
                identifier=unknown,
                title="Unknown field",
                state="fail",
                message=f"{unknown} is not a field in the published input schema.",
                remedy=Remedy("human", f"Remove {unknown} from the answers document."),
            )
        )

    for field in schema.fields():
        problems.extend(_check(field, answers, with_backend=with_backend))

    return problems


def _check(field: schema.Field, answers: dict[str, Any], *, with_backend: bool) -> list[Record]:
    value = answers.get(field.identifier)
    missing = value in (None, "", [])

    if missing:
        if _is_required(field, with_backend=with_backend):
            return [
                Record(
                    identifier=field.identifier,
                    title=field.title,
                    state="fail",
                    message=f"{field.identifier} is required and is not set.",
                    remedy=Remedy("human", field.description.split("\n", 1)[0]),
                )
            ]
        return []

    return validate_field(field, value)


def validate_field(field: schema.Field, value: Any) -> list[Record]:
    """Every problem with one value for one field.

    Public because the prompt needs exactly this: a document's faults are
    collected and reported together, while a person at a terminal is re-asked
    immediately. Both have to apply the same rules, and there is only one place
    those rules live.
    """
    if field.is_list:
        return _check_list(field, value)
    return _check_scalar(field, value)


def _is_required(field: schema.Field, *, with_backend: bool) -> bool:
    if field.required:
        return True
    # `x-required-when` is the schema's way of saying "required under a condition
    # the schema cannot express". Today there is exactly one: state_bucket, when
    # the backend block is being rendered. See design D12.
    return field.required_when is not None and with_backend and field.default is None


def _check_scalar(field: schema.Field, value: Any) -> list[Record]:
    if not isinstance(value, str):
        return [
            Record(
                identifier=field.identifier,
                title=field.title,
                state="fail",
                message=f"{field.identifier} must be a string, not a {type(value).__name__}.",
                remedy=Remedy("human", f"Quote the value of {field.identifier}."),
            )
        ]
    # Every failing rule, not the first: two Terraform validation blocks on one
    # variable are two independent faults, and a value can break both.
    return [
        Record(
            identifier=field.identifier,
            title=field.title,
            state="fail",
            message=rule.message,
            remedy=Remedy("human", field.description.split("\n", 1)[0]),
        )
        for rule in field.rules
        if not rule.accepts(value)
    ]


def _check_list(field: schema.Field, value: Any) -> list[Record]:
    if not isinstance(value, list):
        return [
            Record(
                identifier=field.identifier,
                title=field.title,
                state="fail",
                message=f"{field.identifier} must be a list, not a {type(value).__name__}.",
                remedy=Remedy("human", f"Write {field.identifier} as a YAML or JSON list."),
            )
        ]
    if field.min_items is not None and len(value) < field.min_items:
        return [
            Record(
                identifier=field.identifier,
                title=field.title,
                state="fail",
                message=field.error_message
                or f"{field.identifier} must hold at least {field.min_items} entries.",
                remedy=Remedy("human", field.description.split("\n", 1)[0]),
            )
        ]
    non_strings = [entry for entry in value if not isinstance(entry, str)]
    if non_strings:
        return [
            Record(
                identifier=field.identifier,
                title=field.title,
                state="fail",
                message=f"{field.identifier} must hold strings; found {non_strings!r}.",
                remedy=Remedy("human", f"Quote every entry of {field.identifier}."),
            )
        ]
    return []


def invalid(problems: list[Record]) -> CliError:
    """One error carrying every field-level problem, for the caller to emit.

    The problems go in `detail["field_errors"]` rather than being concatenated
    into the message, because a consumer must be able to attach each one to its
    own input without splitting prose.
    """
    count = len(problems)
    return CliError(
        "answers.invalid",
        f"{count} problem{'s' if count != 1 else ''} with the answers document",
        exit_code=ExitCode.INVALID,
        detail={"field_errors": [problem.as_dict() for problem in problems]},
    )


def missing_required(answers: dict[str, Any], *, with_backend: bool = True) -> list[schema.Field]:
    """The fields a non-interactive run would have had to prompt for.

    Named separately from `validate` because the two have different exit codes: an
    absent answer under `--non-interactive` is `BLOCKED` — a human has not decided
    something — while a malformed one is `INVALID`.
    """
    return [
        field
        for field in schema.fields()
        if _is_required(field, with_backend=with_backend)
        and answers.get(field.identifier) in (None, "", [])
    ]


def save(answers: dict[str, Any], path: Path) -> None:
    """Write the answers document.

    Sorted keys and block style, so two runs of the same answers produce the same
    file and a diff shows what a deployer changed rather than what moved.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".json":
        path.write_text(json.dumps(answers, indent=2, sort_keys=True) + "\n")
        return
    path.write_text(
        yaml.safe_dump(answers, default_flow_style=False, sort_keys=True, width=88),
    )
