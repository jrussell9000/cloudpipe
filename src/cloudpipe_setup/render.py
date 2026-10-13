"""Answers to Terraform files. Pure functions, text in and text out.

`tfvars()` and `backend_tf()` take a mapping and return a string. They read no
file, no environment variable and no clock, which is what makes "render twice and
compare" a check rather than a hope — see design D5 and
`tests/test_setup_wizard_render.py`.

Field order comes from the schema, not from this file. Sorting the output would
also be deterministic, but it would put a diff between two renders everywhere a
field moved; schema order keeps a diff to what actually changed, and groups the
values the way the deployer answered them.

Two files, and only two. Neither is merged into: writing is refused when the file
exists and `--force` was not given. Parsing and preserving HCL this tool does not
own is the problem `globus_admin/render.py` declined, and it would be worse here —
`terraform.tfvars` is the deployer's own file, and `backend.tf` holds the setting
that decides where their state lives.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from . import schema
from .exits import CliError, ExitCode, Remedy

TFVARS_NAME = "terraform.tfvars"
BACKEND_NAME = "backend.tf"

TFVARS_HEADER = """\
# Rendered by `cloudpipe setup` from the answers document.
#
# Safe to edit, unlike globus.auto.tfvars beside it: this tool refuses to
# overwrite the file once it exists, so an edit here survives a re-run. Prefer
# editing the answers document and re-rendering with --force, so that the two do
# not drift and the next machine you resume on has the same values.
#
# Two kinds of value are NOT here, because this tool does not own them:
#
#   * globus_s3_destination_bucket — the imaging data bucket, despite the name,
#     and required either way. Put it in data-bucket.auto.tfvars beside this
#     file, not here: a re-render with --force drops lines added to this file.
#     The value is the data_bucket name you gave the bootstrap root
#     (`terraform -chdir=bootstrap output data_bucket`).
#   * The Globus ingress's seven inputs, needed only with globus_enabled = true.
#     `globus init`, run from this directory, renders them into
#     globus.auto.tfvars, which Terraform loads after this file.
#
# Next step is not `terraform apply`. A fresh deployment's first apply is phased,
# because the root's providers look up a cluster that does not exist yet. Run
# `bash scripts/stack/install.sh --root <this directory>` from your clone — add
# `--list-phases` to see what it will do, or `--phase N` to take one phase at a
# time. "Bootstrap and install sequence" in docs/infrastructure.md describes each
# phase and the two steps it needs from you.
"""

BACKEND_HEADER = """\
# Rendered by `cloudpipe setup` from the answers document.
#
# The state bucket must exist before the first `terraform init`. This tool does
# not create it — it creates no AWS resource at all — so `cloudpipe setup` prints
# the commands that do. Run them, then `terraform init`.
#
# `use_lockfile` is the S3-native state lock; it needs no DynamoDB table, and it
# needs Terraform >= 1.10. Remote state is not optional in practice: local state
# is one laptop away from a deployment nobody can change, and the bucket is
# versioned so a truncated state can be rolled back. See "State management" in
# docs/infrastructure.md.
#
# The example root also ships this block, commented out. It is left alone rather
# than edited: that file is yours, and this tool does not rewrite HCL it did not
# write. Two live backend blocks are an error, so if you uncomment that one,
# delete this file.
"""


def tfvars(answers: dict[str, Any]) -> str:
    """`terraform.tfvars`, as text. Pure."""
    lines = [TFVARS_HEADER]
    for section in schema.sections():
        fields = [
            field
            for field in schema.terraform_fields()
            if field.section == section.identifier and field.identifier in answers
        ]
        if not fields:
            continue
        lines.append(f"# --- {section.title} ---")
        for field in fields:
            lines.append(f"{field.terraform_variable} = {_hcl(answers[field.identifier])}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def backend_tf(answers: dict[str, Any]) -> str:
    """`backend.tf`, as text. Pure.

    The region is `region`, already collected — a state bucket in another region
    is a thing a deployer can want, but it is not a thing they have ever wanted
    here, and an extra question earns its place or it does not.
    """
    return (
        BACKEND_HEADER
        + "\nterraform {\n"
        + '  backend "s3" {\n'
        + f"    bucket       = {_hcl(answers['state_bucket'])}\n"
        + f"    key          = {_hcl(answers['state_key'])}\n"
        + f"    region       = {_hcl(answers['region'])}\n"
        + "    encrypt      = true\n"
        + "    use_lockfile = true\n"
        + "  }\n"
        + "}\n"
    )


#: The published root that creates every bucket which must exist before the
#: stack's first apply and survive its teardown. Paired with the directory by
#: `tests/test_setup_wizard_render.py`, so a rename cannot leave this naming a
#: root that is not in the deployer's clone.
BOOTSTRAP_ROOT = "terraform/modules/bootstrap"

#: Where the bootstrap root is applied from: a subdirectory of the deployment's
#: own root, never the clone. Terraform loads only the top level of a directory,
#: so the stack root does not read these files.
BOOTSTRAP_DIR = "bootstrap"

#: The clone this package was imported from — the published tree the deployer
#: copies the bootstrap root out of.
CLONE = Path(__file__).resolve().parents[2]


def cli_command(subcommand: str, *, task: str = "cloudpipe") -> str:
    """How to run a wizard command from the deployment root, with this clone's real path.

    A bare `pixi run cloudpipe ...` only works from inside the clone, and the
    deployer runs these from their root, so every printed command carries
    `--manifest-path`. `task="globus"` gives the same for `globus init`, which
    also resolves its paths against the directory it is run from.
    """
    return f"pixi run --manifest-path {shlex.quote(str(CLONE / 'pixi.toml'))} {task} {subcommand}"


def bootstrap_command(answers: dict[str, Any], root: Path, *, clone: Path = CLONE) -> str:
    """The one command that creates the pre-stack buckets, from outside the clone.

    It copies the published root into `<root>/bootstrap` and applies it THERE.
    The previous form applied it in place, `-chdir=<clone>/terraform/modules/
    bootstrap`, which put the only state file for the three buckets that outlive
    every cluster inside a git checkout: `git clean -fdx`, or deleting the clone
    to start again, took it. The deployment root is the directory the deployer
    already keeps, beside `terraform.tfvars` and `backend.tf`.

    Copying the `.tf` files rather than calling the root as a module keeps the
    state addresses the reference deployment already has. The copy is code, not
    state: re-running this overwrites the `.tf` files and the lock, never `terraform.tfstate`.

    This used to be three `aws s3api` calls for the state bucket alone. They are
    gone on purpose: a bucket made that way has no transport-encryption policy and
    no `prevent_destroy`, the data bucket was not created at all, and the metrics
    bucket was created by the stack, which made it destroyable by a teardown.

    The data bucket's name is a placeholder because the wizard does not collect
    it. Whatever the deployer puts there, they then set
    `globus_s3_destination_bucket` to the same name; preflight's `aws.data_bucket`
    and `globus.inputs` both point back at `terraform output` here if they do not.
    """
    source = clone / BOOTSTRAP_ROOT
    target = root / BOOTSTRAP_DIR
    quoted = shlex.quote(str(target))
    # A glob rather than a list of names, so a file added to the root is copied
    # without anyone remembering this function. The lock file is copied too, so
    # the copy resolves the same provider build the published root was tested with.
    files = f"{shlex.quote(str(source))}/*.tf {shlex.quote(str(source / '.terraform.lock.hcl'))}"
    # The license by PATH only: the bootstrap root uploads the file with `source`,
    # so its contents are in neither this command, the answers, nor state.
    license_path = answers.get("freesurfer_license_path")
    license_var = (
        f" -var freesurfer_license_path={shlex.quote(str(license_path))}" if license_path else ""
    )
    return (
        f"mkdir -p {quoted} && cp {files} {quoted}/ && "
        f"terraform -chdir={quoted} init && "
        f"terraform -chdir={quoted} apply "
        f"-var region={answers['region']} -var state_bucket={answers['state_bucket']} "
        "-var data_bucket=<your-data-bucket> "
        "-var metrics_bucket=<name>-metrics" + license_var
    )


def write(path: Path, content: str, *, force: bool) -> Path:
    """Write `content` to `path`, refusing to clobber.

    Refusing rather than merging is design D5. The message names the flag, because
    a deployer who meant to overwrite should not have to guess what it is called.
    """
    if path.exists() and not force:
        # A file an earlier run rendered is the usual case: re-running setup is how
        # a deployer changes an answer. The message used to say "a file it did not
        # write" there too, which was false and hid the real risk — the docs have
        # deployers add a line to terraform.tfvars by hand, and --force drops it
        # (second-account rerun, 2026-10-13).
        ours = path.read_text(errors="replace").startswith(TFVARS_HEADER.splitlines()[0])
        text = (
            f"Re-run with --force to render {path.name} again from the answers document. "
            "That drops any line you added to it by hand: move such lines into a "
            "`<name>.auto.tfvars` file beside it first, which Terraform also loads and this "
            "tool never writes."
            if ours
            else f"Re-run with --force to overwrite it, or move {path.name} aside first. "
            "This tool did not write it and does not merge into it."
        )
        raise CliError(
            "render.exists",
            f"{path} already exists"
            + (", rendered by an earlier `cloudpipe setup` run" if ours else ""),
            exit_code=ExitCode.INVALID,
            remedy=Remedy("command", text),
            detail={"path": str(path)},
        )
    path.write_text(content)
    return path


def _hcl(value: Any, *, indent: int = 0) -> str:
    """An HCL literal for a value the schema allows.

    A string, a list of them, null, or an object — the last being
    `cognito_federation`, whose shape is the institution's and so is nested
    rather than flat. Written over several lines, because a deployer reads this
    file and a one-line object with a nested object inside it is unreadable.

    Null only reaches here for a nullable field the deployer chose it for —
    `domain = null` is port-forward mode — so it is written as HCL's own null,
    never as the string "null", which Terraform would read as a domain.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_hcl(entry) for entry in value) + "]"
    if isinstance(value, dict):
        pad = " " * (indent + 2)
        # Sorted, so two runs of the same answers render the same file and a
        # diff shows what the deployer changed rather than what moved.
        lines = [
            f"{pad}{_hcl_key(key)} = {_hcl(item, indent=indent + 2)}"
            for key, item in sorted(value.items())
        ]
        return "{\n" + "\n".join(lines) + "\n" + " " * indent + "}"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _hcl_key(key: str) -> str:
    """An object key, quoted only when it is not a bare identifier.

    `attribute_mapping`'s keys are a provider's attribute names, which may hold
    a colon (`custom:...`); an unquoted one is a syntax error.
    """
    return key if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key) else _hcl(key)
