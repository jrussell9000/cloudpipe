"""Find and verify the Terraform root to write into.

The public deployer copies `terraform/modules/stack/example/` somewhere outside
the repository — its own `main.tf` says to — so the root is a parameter, not an
assumption (design D6).

Verifying it is the point. A `terraform.tfvars` written into a directory Terraform
never reads produces no error at all: the deployer answers every question, sees a
file appear, and then watches `terraform plan` prompt for all twenty-one values
anyway. So a directory that does not call the stack module is refused with exit
`4`, naming what was looked for.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .exits import CliError, ExitCode, Remedy

#: A `module "<name>" { ... source = "<something>" }` call. The module's *name* is
#: not checked — a deployer may call it anything — and neither is the `source`
#: value, which is a relative path, a registry address or a git URL depending on
#: how they vendored it. What is checked is that a module call exists and that the
#: variables the stack requires are declared, which is the thing that makes
#: terraform.tfvars land somewhere it will be read.
_MODULE_CALL = re.compile(r'^\s*module\s+"[^"]+"\s*\{', re.M)

#: Variables the example root declares and the stack has no default for. Used as
#: the fingerprint of a stack root: a root that declares these is one this
#: wizard's output binds to. Three, not all thirteen, so a deployer who has
#: trimmed the root to the inputs they override is not locked out.
_FINGERPRINT = ("domain", "region", "cloudflare_account_id")


def invocation_dir() -> Path:
    """The directory the deployer ran the command from.

    Not `Path.cwd()` alone. The documented command is `cd <your root>` and then
    `pixi run --manifest-path <clone>/pixi.toml cloudpipe ...`, and pixi runs a
    defined task from the project root (the clone), recording where it was
    invoked in `INIT_CWD`. Reading only the cwd made every default and every
    relative path land in the clone, so the documented command could never write
    to the deployer's root (found in the second-account rerun, 2026-10-12).
    """
    invoked = os.environ.get("INIT_CWD")
    if invoked and Path(invoked).is_dir():
        return Path(invoked)
    return Path.cwd()


def from_invocation(path: str | Path) -> Path:
    """`path` as the deployer meant it: relative to where they ran the command."""
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else invocation_dir() / candidate


def resolve(root: str | Path | None) -> Path:
    """The directory to write into, verified.

    `None` means the directory the command was run from, which is only accepted
    when it looks like a stack root — an unverified default here would be a file
    written into a checkout at random.
    """
    candidate = from_invocation(root) if root is not None else invocation_dir()

    if not candidate.is_dir():
        raise CliError(
            "root.not_a_directory",
            f"{candidate} is not a directory",
            exit_code=ExitCode.INVALID,
            remedy=Remedy(
                "human",
                "Pass --root <dir> pointing at your copy of terraform/modules/stack/example/.",
            ),
            detail={"root": str(candidate)},
        )

    verify(candidate)
    return candidate.resolve()


def verify(candidate: Path) -> None:
    """Raise unless `candidate` is a Terraform root that calls the stack module."""
    tf_files = sorted(candidate.glob("*.tf"))
    if not tf_files:
        raise _refusal(
            candidate,
            "no .tf files",
            "it holds no Terraform configuration at all",
        )

    text = "\n\n".join(path.read_text() for path in tf_files)

    if not _MODULE_CALL.search(text):
        raise _refusal(
            candidate,
            'a `module "..." { ... }` call',
            "its Terraform files declare no module call, so nothing there deploys the stack",
        )

    declared = set(re.findall(r'^variable\s+"([^"]+)"\s*\{', text, re.M))
    absent = [name for name in _FINGERPRINT if name not in declared]
    if absent:
        raise _refusal(
            candidate,
            f"variable declarations for {', '.join(_FINGERPRINT)}",
            f"it declares no {', '.join(absent)} variable, so Terraform would reject the "
            "rendered terraform.tfvars as setting undeclared variables",
        )

    _verify_module_source(candidate, text)


#: The `source` of a module call, when it is the call's first argument, as it is
#: in the example root. A source after a nested block is not found, and then
#: nothing is checked rather than something guessed.
_MODULE_SOURCE = re.compile(r'^\s*module\s+"[^"]+"\s*\{[^}]*?^\s*source\s*=\s*"([^"]+)"', re.M)


def _verify_module_source(candidate: Path, text: str) -> None:
    """A local module `source` must resolve, from this root, to the stack module.

    The example root says `source = "../"`, which is the stack module only while
    the file sits in the clone. Copied out, as Step 2 says to, it names the
    directory above the copy, and every input the call passes is then an
    "Unsupported argument" at the first plan (second-account rerun, 2026-10-13).

    It must also be RELATIVE. Terraform treats an absolute path as a package of
    its own, and the stack's own `../globus`-style calls then "escape" it: `init`
    fails with "Local module path escapes module package". The first fix printed
    an absolute path and hit exactly that.

    Registry and git sources are not checked: they resolve only at `init`.
    """
    match = _MODULE_SOURCE.search(text)
    if not match:
        return
    source = match.group(1)
    absolute = source.startswith("/")
    if not absolute and not source.startswith(("./", "../")) and source not in (".", ".."):
        return
    target = (candidate / source).resolve()
    variables = target / "variables.tf"
    module_text = variables.read_text() if variables.is_file() else ""
    is_stack = all(re.search(rf'^variable\s+"{name}"', module_text, re.M) for name in _FINGERPRINT)
    if is_stack and not absolute:
        return
    from .render import CLONE

    wanted = os.path.relpath(CLONE / "terraform/modules/stack", candidate.resolve())
    if not wanted.startswith("."):
        wanted = f"./{wanted}"
    because = (
        "an absolute path, which Terraform treats as a separate package that the stack's own "
        '"../" module calls escape'
        if absolute
        else f"from here {target}, which is not the stack module"
    )
    raise CliError(
        "root.module_source",
        f'{candidate} calls the stack module with source = "{source}": {because}',
        exit_code=ExitCode.INVALID,
        remedy=Remedy(
            "human",
            f'In {candidate / "main.tf"}, set the module\'s source to the path from this root '
            f'to the stack module in your clone: source = "{wanted}". It has to be relative; '
            'the example root\'s "../" is right only inside the clone.',
        ),
        detail={"root": str(candidate), "source": source, "resolved": str(target)},
    )


def undeclared(root: Path, variables: list[str]) -> list[str]:
    """Which of `variables` the root does not both declare and pass to a module.

    Terraform only warns about a value in terraform.tfvars for a variable the root
    does not declare, and then ignores it. A root copied before the stack gained an
    input therefore drops that input silently: a copy made before `bucket_suffix`
    existed would name every bucket without the suffix, which is to say after
    buckets another deployment owns (second-account rerun, 2026-10-13).
    """
    text = "\n\n".join(path.read_text() for path in sorted(root.glob("*.tf")))
    declared = set(re.findall(r'^variable\s+"([^"]+)"\s*\{', text, re.M))
    return [
        name
        for name in variables
        if name not in declared or not re.search(rf"\bvar\.{name}\b", text)
    ]


def _refusal(candidate: Path, looked_for: str, because: str) -> CliError:
    return CliError(
        "root.not_a_stack_root",
        f"{candidate} is not a CloudPipe stack root: {because}",
        exit_code=ExitCode.INVALID,
        remedy=Remedy(
            "human",
            "Copy terraform/modules/stack/example/ to a directory outside the repository and "
            "pass it as --root. A terraform.tfvars written anywhere else is a file Terraform "
            "never reads.",
        ),
        detail={"root": str(candidate), "looked_for": looked_for},
    )


def loaded_tfvars(root: Path) -> list[Path]:
    """The files Terraform reads variable values from in `root` without a flag.

    In the order it applies them, a later file's value winning: `terraform.tfvars`,
    `terraform.tfvars.json`, then every `*.auto.tfvars` and `*.auto.tfvars.json`
    by file name. That `.auto.tfvars` overrides `terraform.tfvars` is what lets
    `globus init`'s rendered file win over a stale hand-written value.
    """
    fixed = [root / "terraform.tfvars", root / "terraform.tfvars.json"]
    automatic = sorted(
        [*root.glob("*.auto.tfvars"), *root.glob("*.auto.tfvars.json")], key=lambda p: p.name
    )
    return [path for path in [*fixed, *automatic] if path.is_file()]


def tfvars_assignments(root: Path) -> dict[str, tuple[Any, Path]]:
    """Every top-level variable assignment Terraform will load from `root`.

    Maps each variable to `(value, file)` from the file that wins. A quoted HCL
    string comes back as its text; any other HCL value — a number, a bool, a list,
    a map — comes back as its source text, which is enough to tell that it is set.

    Not an HCL parser, and it does not need to be: tfvars hold assignments only.
    What it must get right is "top-level", because a map value can contain a line
    that looks like an assignment — `tags = { globus_client_id = "x" }` must not
    count as setting `globus_client_id`. So bracket depth is tracked outside
    strings and comments. Heredoc values are not understood; nobody writes the
    variables this reads that way.
    """
    found: dict[str, tuple[Any, Path]] = {}
    for path in loaded_tfvars(root):
        try:
            text = path.read_text()
            values = json.loads(text) if path.suffix == ".json" else _hcl_assignments(text)
        except (OSError, ValueError):
            continue
        if isinstance(values, dict):
            for name, value in values.items():
                found[name] = (value, path)
    return found


@dataclass(frozen=True)
class GlobusInputs:
    """Which of `globus init`'s variables are set in a root, and where."""

    expected: tuple[str, ...]
    missing: tuple[str, ...]
    sources: tuple[Path, ...]


def globus_inputs(root: Path, expected: tuple[str, ...]) -> GlobusInputs:
    """Whether each of `expected` is set, in any file Terraform loads, to a value.

    Rendered by `globus init` or written by hand into `terraform.tfvars` are the
    same to Terraform, so they are the same here. Only presence is checked, and an
    empty string counts as absent: the values' shapes are Terraform's own
    `validation` blocks' job, at plan.
    """
    assignments = tfvars_assignments(root)
    missing, sources = [], []
    for name in expected:
        value, source = assignments.get(name, (None, None))
        if value in (None, "", []):
            missing.append(name)
        elif source not in sources:
            sources.append(source)
    return GlobusInputs(tuple(expected), tuple(missing), tuple(sources))


#: Despite the `globus_` prefix, the imaging data bucket: required with or without
#: the ingress, and created by the bootstrap root rather than by `globus init`.
DATA_BUCKET_VARIABLE = "globus_s3_destination_bucket"


def globus_ingress_enabled(root: Path) -> bool:
    """Whether this root stands up the Globus ingress — `globus_enabled`.

    False when unset, which is the stack module's own default. The tfvars reader
    returns a quoted string as its text and any other HCL value as source text,
    so a bool arrives as `"true"`; a JSON tfvars file gives a real bool. Both are
    accepted, and anything else is read as false, because the only value that
    turns 52 resources on is one Terraform itself would read as true.
    """
    value, _ = tfvars_assignments(root).get("globus_enabled", (None, None))
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() == "true"


def wanted_globus_inputs(root: Path, variables_tf: Path) -> tuple[str, ...] | None:
    """The Globus-owned variables THIS root has to set, or None if underivable.

    All eight with the ingress on; only the data bucket with it off, because the
    other seven are required only when `globus_enabled` says so. One function for
    both `cloudpipe setup`'s closing message and `cloudpipe preflight`, which
    disagreed once: preflight learned about the flag and setup went on telling a
    no-ingress deployment that seven variables were missing.
    """
    from . import schema

    expected = schema.globus_variables(variables_tf)
    if expected is None or globus_ingress_enabled(root):
        return expected
    conditional = schema.variables_required_by_another(variables_tf.read_text())
    return tuple(name for name in expected if name not in conditional)


def missing_globus_remedy(root: Path, missing: tuple[str, ...]) -> tuple[str, str]:
    """(kind, text) of the action that sets `missing` — which depends on what is missing.

    `pixi run globus init` renders the ingress's inputs from a Globus answers
    document. It is the wrong advice for the data bucket alone: a deployment
    without the ingress has no reason to write that document, and the bucket's
    name is one the deployer already chose for the bootstrap root.
    """
    if tuple(missing) == (DATA_BUCKET_VARIABLE,):
        return (
            "human",
            f"Write `{DATA_BUCKET_VARIABLE} = \"<name>\"` into {root / 'data-bucket.auto.tfvars'} "
            "(not terraform.tfvars, which a re-render with --force replaces), using the "
            "data_bucket name you gave the bootstrap root (`terraform -chdir="
            f"{root / 'bootstrap'} output` prints it). The prefix is historical: this is the "
            "imaging data bucket, and the stack needs it whether or not Globus is enabled.",
        )
    from .render import cli_command

    return ("command", cli_command("init", task="globus"))


def _hcl_assignments(text: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    depth = 0
    for line in text.splitlines():
        if depth == 0:
            match = _TOP_LEVEL.match(line)
            if match:
                name, rest = match.groups()
                string = _QUOTED.match(rest)
                found[name] = string.group(1) if string else (rest.strip() or None)
        depth = max(0, depth + _depth_change(line))
    return found


_TOP_LEVEL = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.*)$")
_QUOTED = re.compile(r'^"((?:[^"\\]|\\.)*)"\s*(?:(?:#|//).*)?$')


def _depth_change(line: str) -> int:
    """Net bracket depth across `line`, ignoring brackets in strings and comments."""
    change, in_string, index = 0, False, 0
    while index < len(line):
        char = line[index]
        if in_string:
            if char == "\\":
                index += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "#" or line.startswith("//", index):
            break
        elif char in "[{(":
            change += 1
        elif char in "]})":
            change -= 1
        index += 1
    return change
