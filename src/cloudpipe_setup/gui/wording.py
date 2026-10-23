"""The page's words for CLI results whose own words are about the CLI.

The CLI's messages are right for a terminal: someone who passed
`--non-interactive` knows what it means, and "re-run with --force" is the fix.
On the page there is no flag to pass: the same fixes are a box to tick or a
question to go back to. So the page rewords those few results, keyed on the
stable identifiers the JSON contract already publishes (an error's `code`, a
check's `identifier`) and never on the prose, which may change. Everything
else is shown as the CLI wrote it.

No NiceGUI here, so the default environment tests it.
"""

from __future__ import annotations

from typing import Any

from .. import schema
from ..preflight import CHECK_ACCOUNT


def error(error: dict[str, Any]) -> dict[str, Any]:
    """An error envelope's `error`, with the message and remedy the page shows.

    Returns a new mapping; the keys are the CLI's own, so the caller renders it
    the same way whether or not it was reworded.
    """
    code = error.get("code")
    detail = error.get("detail") or {}
    if code == "setup.answers_incomplete":
        reported = [entry["identifier"] for entry in detail.get("field_errors", ())]
        # A derived field whose source is also missing is not a question: it fills
        # itself in once the source is answered. Listing it would send the deployer
        # looking for a box that does not exist (github_oidc_allowed_subs, when
        # github_repo is unanswered).
        missing = [
            identifier for identifier in reported if not _waits_on(identifier, reported)
        ] or reported
        titles = [_title(identifier) for identifier in missing]
        # The per-field records say "would have been prompted for", which is the
        # CLI's view; the remedy below already names every question.
        return {
            **error,
            "detail": {**detail, "field_errors": []},
            "message": (
                "One question is not answered yet."
                if len(missing) == 1
                else f"{len(missing)} questions are not answered yet."
            ),
            "remedy": {
                "kind": "human",
                "text": "Go back and answer: " + "; ".join(titles) + ".",
            },
        }
    if code == "render.exists":
        name = str(detail.get("path", "the file")).rsplit("/", 1)[-1]
        text = (
            f"Tick \"Replace these files if they already exist\" above and write again. "
            f"If you added lines to {name} by hand, move them first into a file named "
            "`<name>.auto.tfvars` beside it: Terraform reads that file too, and this tool "
            "never writes it."
            if detail.get("rendered_by_setup")
            else f"This tool did not write {name}. Move it aside, or tick \"Replace these "
            "files if they already exist\" above to overwrite it, then write again."
        )
        return {**error, "remedy": {"kind": "human", "text": text}}
    return error


def check(record: dict[str, Any]) -> dict[str, Any]:
    """A preflight record, with a remedy the page can act on where the CLI's names a flag.

    Today one: the intended-account check, whose remedy is `--account`, and the
    page has a box for exactly that.
    """
    if record.get("identifier") == CHECK_ACCOUNT and record.get("state") == "skipped":
        remedy = record.get("remedy") or {}
        if remedy.get("kind") == "command":
            return {
                **record,
                "remedy": {
                    "kind": "human",
                    "text": "Type the AWS account ID you mean to deploy into in the box above, "
                    "then run the checks again.",
                },
            }
    return record


def _waits_on(identifier: str, missing: list[str]) -> bool:
    """Whether a missing field is derived from another missing field."""
    try:
        field = schema.field(identifier)
    except KeyError:
        return False
    return field.is_derived and any(source in missing for source in field.derived_from)


def _title(identifier: str) -> str:
    try:
        return schema.field(identifier).title
    except KeyError:
        return identifier
