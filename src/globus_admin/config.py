"""Deployment configuration: the few human-supplied values the CLI needs.

This is the *reader*. The published answers schema, field-level validation, and
`globus init`'s rendering of `terraform.tfvars` and the GCS configuration are
task 8.9/8.10 of the change; until those land this module reads the same file
shape leniently, so the commands built first are usable against the existing
deployment.

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

from .exits import CliError, ExitCode, Remedy

DEFAULT_ANSWERS_NAMES = ("globus-answers.yaml", "globus-answers.yml", "globus-answers.json")
DEFAULT_DEPLOYMENT_NAME = "cloudpipe"
DEFAULT_STAGING_PREFIX = "scratch/globus-staging"
DEFAULT_SESSION_TIMEOUT_DAYS = 30

#: Every field the published answers schema defines, which is exactly what this
#: module reads. Anything else lands in `extra`.
#:
#: `tests/globus/test_answers_schema.py` fails if the schema publishes a field
#: this set omits. That is the failure worth catching: because this reader is
#: lenient, an unread field does not raise — it is silently ignored, so a
#: documented answer quietly stops having any effect.
KNOWN_ANSWER_FIELDS = frozenset(
    {
        "deployment_name",
        "aws_account_id",
        "aws_region",
        "bucket",
        "identity_domain",
        "identity_domains",
        "contact_email",
        "owner_email",
        "service_client_id",
        "service_client_secret_ref",
        "native_app_client_id",
        "source_collection_id",
        "source_base_path",
        "subscription_id",
        "admin_prefix_list_id",
        "collection_name",
        "org_name",
        "session_timeout_days",
        "gateway_name",
        "iam_user_name",
        # Read by `render`, not by this module: it decides what the rendered
        # files say, and no command acts on it directly.
        "production_managed",
        "staging_enabled",
        "staging_prefix",
    }
)


@dataclass(frozen=True)
class DeploymentConfig:
    """Resolved deployment inputs, with derived names filled in."""

    deployment_name: str = DEFAULT_DEPLOYMENT_NAME
    aws_account_id: str | None = None
    aws_region: str | None = None
    bucket: str | None = None
    identity_domains: tuple[str, ...] = ()
    contact_email: str | None = None
    owner_email: str | None = None
    service_client_id: str | None = None
    service_client_secret_ref: str | None = None
    """WHERE the service client's secret lives, never the secret. The schema
    rejects a literal, and nothing here ever resolves one into this object."""

    native_app_client_id: str | None = None
    source_collection_id: str | None = None
    source_base_path: str | None = None
    subscription_id: str | None = None
    """None until someone adds the endpoint to an institutional subscription —
    a gate, not a mistake. High Assurance does not work until it is set."""

    admin_prefix_list_id: str | None = None
    collection_name: str = ""
    org_name: str = ""
    """The organization shown on the endpoint's public listing.

    Empty when the answers do not set one, rather than defaulted to the
    deployment name here: `render` owns that fallback, and a second copy of it
    would be a second place for the two to disagree."""

    session_timeout_days: int = DEFAULT_SESSION_TIMEOUT_DAYS
    gateway_name: str = ""
    """The production gateway's display name, when it is not the collection's.

    Empty means derive it, which is what a deployment this tool built will do.
    See `environments.resolve`."""

    iam_user_name: str = ""
    """The existing IAM user whose access key the production S3 gateway registers.

    A name, never a credential. Empty is legitimate — an organization SCP denies
    `iam:CreateUser`, so "none granted yet" is a state an operator reaches and
    cannot fix alone — and `doctor` check 12 reports it rather than anything
    refusing to run."""

    staging_enabled: bool = False
    staging_prefix: str = DEFAULT_STAGING_PREFIX
    answers_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def session_timeout_minutes(self) -> int:
        return self.session_timeout_days * 24 * 60

    def require_account_id(self) -> str:
        """The account the operator says they meant. Required before mutating anything."""
        if not self.aws_account_id:
            raise CliError(
                "config.account_id_missing",
                "No AWS account id is configured, so the account these credentials point at "
                "cannot be verified before changing anything.",
                exit_code=ExitCode.INVALID,
                remedy=Remedy(
                    "command",
                    "pixi run globus init --answers globus-answers.yaml  "
                    "(or add aws_account_id to the answers document)",
                ),
            )
        return self.aws_account_id

    def require_bucket(self) -> str:
        if not self.bucket:
            raise CliError(
                "config.bucket_missing",
                "No S3 bucket is configured for this deployment.",
                exit_code=ExitCode.INVALID,
                remedy=Remedy("human", "add `bucket` to the answers document"),
            )
        return self.bucket


def find_answers(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Locate the answers document: explicit path, then `$GLOBUS_ANSWERS`, then the cwd."""
    if explicit is not None:
        path = Path(explicit)
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
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate
    return None


def load(explicit: str | os.PathLike[str] | None = None) -> DeploymentConfig:
    """Read the answers document if there is one, else fall back to environment defaults.

    Falling back rather than failing is deliberate at this stage: read-only
    commands (`status`, `doctor`, `tasks`) stay usable against the deployment that
    exists today, while anything that mutates goes through `require_account_id()`
    and so still refuses to run without a named account.
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
        if not isinstance(raw, dict):
            raise CliError(
                "config.answers_unparseable",
                f"{path} must contain a mapping of answers, not {type(raw).__name__}.",
                exit_code=ExitCode.INVALID,
                remedy=Remedy("human", f"rewrite {path} as `key: value` pairs"),
            )

    deployment_name = (
        raw.get("deployment_name")
        or os.environ.get("GLOBUS_DEPLOYMENT_NAME")
        or DEFAULT_DEPLOYMENT_NAME
    )
    domains = raw.get("identity_domains") or raw.get("identity_domain") or ()
    if isinstance(domains, str):
        domains = (domains,)

    staging_enabled = raw.get("staging_enabled")

    return DeploymentConfig(
        deployment_name=str(deployment_name),
        aws_account_id=_as_str(raw.get("aws_account_id")),
        aws_region=_as_str(raw.get("aws_region")) or _region_from_env(),
        bucket=_as_str(raw.get("bucket")),
        identity_domains=tuple(str(d) for d in domains),
        contact_email=_as_str(raw.get("contact_email")),
        owner_email=_as_str(raw.get("owner_email")),
        service_client_id=_as_str(raw.get("service_client_id")),
        service_client_secret_ref=_as_str(raw.get("service_client_secret_ref")),
        native_app_client_id=_as_str(raw.get("native_app_client_id")),
        source_collection_id=_as_str(raw.get("source_collection_id")),
        source_base_path=_as_str(raw.get("source_base_path")),
        subscription_id=_as_str(raw.get("subscription_id")),
        admin_prefix_list_id=_as_str(raw.get("admin_prefix_list_id")),
        # The production gateway and collection share one display name, derived
        # from the deployment name so a fresh deployment needs no answer for it.
        collection_name=str(raw.get("collection_name") or f"{deployment_name}-s3"),
        gateway_name=str(raw.get("gateway_name") or ""),
        iam_user_name=str(raw.get("iam_user_name") or ""),
        org_name=str(raw.get("org_name") or ""),
        session_timeout_days=int(raw.get("session_timeout_days") or DEFAULT_SESSION_TIMEOUT_DAYS),
        # Opt-in: a fresh deployment has no staging gateway unless it asks for one.
        # See `render._staging_enabled` for why the Terraform default disagrees.
        staging_enabled=bool(staging_enabled),
        staging_prefix=str(raw.get("staging_prefix") or DEFAULT_STAGING_PREFIX),
        answers_path=path,
        extra={k: v for k, v in raw.items() if k not in KNOWN_ANSWER_FIELDS},
    )


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _region_from_env() -> str | None:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
