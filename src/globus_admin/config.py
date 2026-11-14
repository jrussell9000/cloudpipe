"""Deployment configuration: the few human-supplied values the CLI needs.

This is the reader of the answers document (`schemas/answers.schema.json`,
validated through `answers.validate`). Contract 2.0 cut it to what `login` and
`doctor` read: the Globus Connect Server host, its endpoint and `globus init`
are retired (openspec retire-globus-host), so nothing here describes an endpoint
and nothing is rendered from it.

What it deliberately does not hold: anything about AWS credentials. There is no
profile, key, or role field, because credentials come from the environment
(`AWS_PROFILE` and the AWS CLI's SSO login) and nowhere else. `aws_account_id`
is here only so the CLI can refuse to act against an account the operator did
not name.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import answers
from .exits import CliError, ExitCode, Remedy

DEFAULT_ANSWERS_NAMES = ("globus-answers.yaml", "globus-answers.yml", "globus-answers.json")
DEFAULT_DEPLOYMENT_NAME = "cloudpipe"

#: This deployment's re-authentication rule for `ingress-mode=https`, in days.
#: Fixed, not an answer: the ingress pod (`images/globus/https_ingress.py`,
#: `MAX_SESSION_DAYS`) and the Prefect gate (`HTTPS_RULE_MINUTES`) enforce the
#: same number, and a test holds the three equal.
SESSION_RULE_DAYS = 30


@dataclass(frozen=True)
class DeploymentConfig:
    """Resolved deployment inputs, with derived names filled in."""

    deployment_name: str = DEFAULT_DEPLOYMENT_NAME
    aws_account_id: str | None = None
    aws_region: str | None = None
    identity_domains: tuple[str, ...] = ()
    native_app_client_id: str | None = None
    source_collection_id: str | None = None
    source_base_path: str | None = None
    answers_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def session_timeout_minutes(self) -> int:
        return SESSION_RULE_DAYS * 24 * 60

    def require_account_id(self) -> str:
        """The account the operator says they meant. Required before mutating anything."""
        if not self.aws_account_id:
            raise CliError(
                "config.account_id_missing",
                "No AWS account id is configured, so the account these credentials point at "
                "cannot be verified before changing anything.",
                exit_code=ExitCode.INVALID,
                remedy=Remedy("human", "add aws_account_id to the answers document"),
            )
        return self.aws_account_id


def invocation_dir() -> Path:
    """The directory the operator ran the command from.

    pixi runs a defined task, `globus` included, from the project root and records
    where it was invoked in `INIT_CWD`. From the reference checkout the two are the
    same directory. A deployer runs `pixi run --manifest-path <clone>/pixi.toml
    globus ...` from their own Terraform root, and reading only the cwd sent the
    answers lookup into the clone. The same defect, and the same fix, as
    `cloudpipe_setup.roots.invocation_dir` (second-account rerun, 2026-10-12).
    """
    invoked = os.environ.get("INIT_CWD")
    if invoked and Path(invoked).is_dir():
        return Path(invoked)
    return Path.cwd()


def from_invocation(path: str | os.PathLike[str]) -> Path:
    """`path` as the operator meant it: relative to where they ran the command."""
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else invocation_dir() / candidate


def find_answers(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Locate the answers document: explicit path, then `$GLOBUS_ANSWERS`, then where
    the command was run from."""
    if explicit is not None:
        path = from_invocation(explicit)
        if not path.is_file():
            raise CliError(
                "config.answers_not_found",
                f"Answers document not found: {path}",
                exit_code=ExitCode.INVALID,
                remedy=Remedy("human", f"create {path}, or pass --answers with the right path"),
            )
        return path

    from_env = os.environ.get("GLOBUS_ANSWERS")
    if from_env:
        return find_answers(from_env)

    for name in DEFAULT_ANSWERS_NAMES:
        candidate = invocation_dir() / name
        if candidate.is_file():
            return candidate
    return None


def load(explicit: str | os.PathLike[str] | None = None) -> DeploymentConfig:
    """Read and validate the answers document if there is one, else environment defaults.

    A document that exists is validated before anything acts, and every problem
    is reported at once, one per field (exit 4). With no document at all the
    defaults stand: `doctor` still runs against the deployment the environment's
    credentials point at, and `login` refuses on its own for the values it needs.
    """
    path = find_answers(explicit)
    raw: dict[str, Any] = {}
    if path is not None:
        text = path.read_text()
        try:
            raw = json.loads(text) if path.suffix == ".json" else (yaml.safe_load(text) or {})
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            raise CliError(
                "config.answers_unparseable",
                f"Could not parse {path}: {exc}",
                exit_code=ExitCode.INVALID,
                remedy=Remedy("human", f"fix the syntax in {path}"),
                raw=str(exc),
            ) from exc
        problems = answers.validate(raw)
        if problems:
            raise CliError(
                "config.answers_invalid",
                f"{path} has {len(problems)} problem(s): "
                + "; ".join(problem.message for problem in problems),
                exit_code=ExitCode.INVALID,
                remedy=Remedy(
                    "human",
                    f"fix each field named above in {path}. Fields the retired Globus host "
                    "needed (bucket, gateway and collection names, the service client, "
                    "subscription, staging, session_timeout_days) are no longer read: delete "
                    "them.",
                ),
                detail={"field_errors": [problem.as_dict() for problem in problems]},
            )

    deployment_name = (
        raw.get("deployment_name")
        or os.environ.get("GLOBUS_DEPLOYMENT_NAME")
        or DEFAULT_DEPLOYMENT_NAME
    )
    domains = raw.get("identity_domains") or raw.get("identity_domain") or ()
    if isinstance(domains, str):
        domains = (domains,)

    return DeploymentConfig(
        deployment_name=str(deployment_name),
        aws_account_id=_as_str(raw.get("aws_account_id")),
        aws_region=_as_str(raw.get("aws_region")) or _region_from_env(),
        identity_domains=tuple(str(d) for d in domains),
        native_app_client_id=_as_str(raw.get("native_app_client_id")),
        source_collection_id=_as_str(raw.get("source_collection_id")),
        source_base_path=_as_str(raw.get("source_base_path")),
        answers_path=path,
    )


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _region_from_env() -> str | None:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
