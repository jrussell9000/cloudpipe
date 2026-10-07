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


def resolve(root: str | Path | None) -> Path:
    """The directory to write into, verified.

    `None` means the current directory, which is only accepted when it looks like
    a stack root — an unverified default here would be a file written into a
    checkout at random.
    """
    candidate = Path(root).expanduser() if root is not None else Path.cwd()

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
