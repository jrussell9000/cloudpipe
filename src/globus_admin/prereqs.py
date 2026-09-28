"""Prerequisite checks, run before every command.

The contract with the operator (and with a future wizard) is narrow on purpose:
they install AWS CLI v2 and log in with SSO, they install Terraform and pixi, and
this tool does nothing else about credentials. It never accepts an access key,
never writes AWS config, and never runs a login itself — the AWS CLI owns that
browser flow, and wrapping it would add a second place for it to break.

SSO specifically, not "any credentials": long-lived IAM user keys on a laptop are
what this setup should not teach. botocore reports how a credential was resolved,
and an SSO profile resolves with method `sso`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from . import errors
from .config import DeploymentConfig
from .exits import CliError, ExitCode, Remedy
from .shell import Completed, Runner, subprocess_runner

PASS = "pass"
WARN = "warn"
FAIL = "fail"
SKIPPED = "skipped"

# Check identifiers are part of the integration contract: stable across releases,
# because a wizard keys its display on them.
CHECK_AWS_CLI = "prereq.aws_cli"
CHECK_AWS_SSO = "prereq.aws_sso"
CHECK_AWS_ACCOUNT = "prereq.aws_account"
CHECK_TERRAFORM = "prereq.terraform"
CHECK_PIXI = "prereq.pixi"


@dataclass(frozen=True)
class CheckResult:
    id: str
    title: str
    severity: str
    message: str
    remedy: Remedy | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    exit_code: ExitCode | None = None
    """How a FAIL should be classified for the caller, when it is not simply CHECK_FAILED.

    A check knows things the caller cannot infer from the severity: an expired SSO
    login is a human gate (BLOCKED), a wrong account is bad input (INVALID), and a
    failed listing is just a failed check. `doctor` reads this to pick its exit
    code; it is routing information, not part of the JSON record.
    """

    @property
    def blocking(self) -> bool:
        return self.severity == FAIL

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "severity": self.severity,
            "message": self.message,
            "remedy": self.remedy.as_dict() if self.remedy else None,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AwsIdentity:
    account: str
    arn: str
    region: str | None
    profile: str | None
    credential_method: str


def check_aws_cli(runner: Runner = subprocess_runner) -> CheckResult:
    """AWS CLI v2 must be installed: v1 has no `aws sso login`."""
    result = runner(["aws", "--version"])
    if not result.found:
        return CheckResult(
            CHECK_AWS_CLI,
            "AWS CLI v2 installed",
            FAIL,
            "The `aws` command is not on PATH.",
            Remedy(
                "human",
                "install AWS CLI v2: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html",
            ),
        )

    version = _first_match(r"aws-cli/(\d+\.\d+\.\d+)", result.output)
    if version is None:
        return CheckResult(
            CHECK_AWS_CLI,
            "AWS CLI v2 installed",
            WARN,
            "Could not read the AWS CLI version from its output.",
            detail={"raw": result.output[:200]},
        )
    if not version.startswith("2."):
        return CheckResult(
            CHECK_AWS_CLI,
            "AWS CLI v2 installed",
            FAIL,
            f"AWS CLI {version} is installed; v2 is required for SSO logins.",
            Remedy("human", "upgrade to AWS CLI v2"),
            detail={"version": version},
        )
    return CheckResult(
        CHECK_AWS_CLI,
        "AWS CLI v2 installed",
        PASS,
        f"aws-cli {version}",
        detail={"version": version},
    )


def check_aws_sso(
    session_factory: Callable[[], Any] | None = None,
) -> tuple[CheckResult, AwsIdentity | None]:
    """Credentials must resolve from an SSO profile in this environment, and be current."""
    try:
        session = (session_factory or _default_session)()
    except Exception as exc:  # pragma: no cover - boto3 import/config failures
        return _from_cli_error(CHECK_AWS_SSO, "AWS SSO login", errors.aws_error(exc)), None

    profile = getattr(session, "profile_name", None)
    if profile == "default":
        profile = None

    try:
        credentials = session.get_credentials()
        method = getattr(credentials, "method", "") if credentials else ""
        # Resolving the key is what actually loads (and validates) the SSO token.
        if credentials is not None:
            credentials.get_frozen_credentials()
    except Exception as exc:
        return _from_cli_error(
            CHECK_AWS_SSO, "AWS SSO login", errors.aws_error(exc, profile=profile)
        ), None

    if credentials is None:
        err = errors.aws_error(
            type("NoCredentialsError", (Exception,), {})("Unable to locate credentials"),
            profile=profile,
        )
        return _from_cli_error(CHECK_AWS_SSO, "AWS SSO login", err), None

    if method != "sso":
        return (
            CheckResult(
                CHECK_AWS_SSO,
                "AWS SSO login",
                FAIL,
                f"These credentials resolved from `{method or 'unknown'}`, not from an AWS SSO "
                "profile. Setup requires SSO: static keys are not supported.",
                Remedy("command", "aws configure sso"),
                detail={"credential_method": method, "profile": profile},
            ),
            None,
        )

    try:
        caller = session.client("sts").get_caller_identity()
    except Exception as exc:
        return _from_cli_error(
            CHECK_AWS_SSO, "AWS SSO login", errors.aws_error(exc, profile=profile)
        ), None

    identity = AwsIdentity(
        account=str(caller.get("Account", "")),
        arn=str(caller.get("Arn", "")),
        region=getattr(session, "region_name", None),
        profile=profile,
        credential_method=method,
    )
    return (
        CheckResult(
            CHECK_AWS_SSO,
            "AWS SSO login",
            PASS,
            f"SSO credentials for account {identity.account} in "
            f"{identity.region or 'no region set'} ({identity.arn})",
            detail={
                "account": identity.account,
                "arn": identity.arn,
                "region": identity.region,
                "profile": profile,
            },
        ),
        identity,
    )


def check_aws_account(identity: AwsIdentity | None, config: DeploymentConfig) -> CheckResult:
    """The environment decides which credentials; the answers decide which account is intended."""
    if identity is None:
        return CheckResult(
            CHECK_AWS_ACCOUNT,
            "AWS account is the intended one",
            SKIPPED,
            "Not run: AWS credentials could not be resolved.",
        )
    if not config.aws_account_id:
        return CheckResult(
            CHECK_AWS_ACCOUNT,
            "AWS account is the intended one",
            WARN,
            f"Logged in to account {identity.account}, but no `aws_account_id` is configured, "
            "so nothing verifies it is the intended one. Commands that change state will refuse.",
            Remedy("human", "add `aws_account_id` to the answers document"),
            detail={"logged_in_account": identity.account},
        )
    if identity.account != config.aws_account_id:
        return CheckResult(
            CHECK_AWS_ACCOUNT,
            "AWS account is the intended one",
            FAIL,
            f"These credentials are for account {identity.account}, but this deployment's "
            f"answers name {config.aws_account_id}.",
            Remedy("command", "aws sso login --profile <the profile for the intended account>"),
            detail={
                "logged_in_account": identity.account,
                "expected_account": config.aws_account_id,
            },
        )
    return CheckResult(
        CHECK_AWS_ACCOUNT,
        "AWS account is the intended one",
        PASS,
        f"account {identity.account}",
        detail={"account": identity.account},
    )


def check_terraform(
    runner: Runner = subprocess_runner, *, versions_file: Path | None = None
) -> CheckResult:
    """Terraform must satisfy the repository's own `required_version`.

    The constraint is read from `terraform/versions.tf` rather than duplicated
    here, so this check can never disagree with Terraform about what is
    acceptable.
    """
    constraint = read_required_version(versions_file)
    result = runner(["terraform", "version", "-json"])
    if not result.found:
        return CheckResult(
            CHECK_TERRAFORM,
            "Terraform installed",
            FAIL,
            f"The `terraform` command is not on PATH; this deployment needs {constraint or 'it'}.",
            Remedy("human", "install Terraform: https://developer.hashicorp.com/terraform/install"),
            detail={"required": constraint},
        )

    version = _terraform_version(result)
    if version is None:
        return CheckResult(
            CHECK_TERRAFORM,
            "Terraform installed",
            WARN,
            "Could not read the Terraform version.",
            detail={"raw": result.output[:200], "required": constraint},
        )

    if constraint:
        try:
            satisfied = Version(version) in SpecifierSet(constraint)
        except (InvalidVersion, InvalidSpecifier):
            return CheckResult(
                CHECK_TERRAFORM,
                "Terraform installed",
                WARN,
                f"Terraform {version} is installed; could not interpret the required_version "
                f"constraint {constraint!r}.",
                detail={"version": version, "required": constraint},
            )
        if not satisfied:
            return CheckResult(
                CHECK_TERRAFORM,
                "Terraform installed",
                FAIL,
                f"Terraform {version} does not satisfy {constraint} from terraform/versions.tf.",
                Remedy("human", f"install a Terraform version matching {constraint}"),
                detail={"version": version, "required": constraint},
            )

    return CheckResult(
        CHECK_TERRAFORM,
        "Terraform installed",
        PASS,
        f"terraform {version}" + (f" (satisfies {constraint})" if constraint else ""),
        detail={"version": version, "required": constraint},
    )


def check_pixi(runner: Runner = subprocess_runner) -> CheckResult:
    """pixi runs this CLI, so its absence is usually academic — but a wizard may check first."""
    result = runner(["pixi", "--version"])
    if not result.found:
        return CheckResult(
            CHECK_PIXI,
            "pixi installed",
            FAIL,
            "The `pixi` command is not on PATH; the CLI runs as `pixi run globus`.",
            Remedy("human", "install pixi: https://pixi.sh"),
        )
    version = _first_match(r"(\d+\.\d+\.\d+)", result.output)
    return CheckResult(
        CHECK_PIXI,
        "pixi installed",
        PASS,
        result.output.strip() or "installed",
        detail={"version": version},
    )


def read_required_version(versions_file: Path | None = None) -> str | None:
    """Extract `required_version` from terraform/versions.tf, if it can be found."""
    path = versions_file or _repo_root() / "terraform" / "versions.tf"
    try:
        text = path.read_text()
    except OSError:
        return None
    match = re.search(r'required_version\s*=\s*"([^"]+)"', text)
    if not match:
        return None
    # Terraform writes ">= 1.10"; packaging wants no space.
    return match.group(1).replace(" ", "")


def run_all(
    config: DeploymentConfig,
    *,
    runner: Runner = subprocess_runner,
    session_factory: Callable[[], Any] | None = None,
) -> tuple[list[CheckResult], AwsIdentity | None]:
    """Every prerequisite check, in dependency order."""
    results = [check_aws_cli(runner)]
    sso_result, identity = check_aws_sso(session_factory)
    results.append(sso_result)
    results.append(check_aws_account(identity, config))
    results.append(check_terraform(runner))
    results.append(check_pixi(runner))
    return results, identity


def require(
    config: DeploymentConfig,
    *,
    mutating: bool = False,
    runner: Runner = subprocess_runner,
    session_factory: Callable[[], Any] | None = None,
) -> AwsIdentity:
    """Run the prerequisites and raise on the first blocking failure.

    A mutating command additionally requires a *known* intended account: acting
    on an account nobody named is exactly the mistake the check exists to stop.
    """
    results, identity = run_all(config, runner=runner, session_factory=session_factory)
    for result in results:
        if result.blocking:
            raise to_cli_error(result)

    if mutating:
        config.require_account_id()
    if identity is None:  # pragma: no cover - a non-blocking path cannot reach this
        raise CliError(
            "aws.identity_unknown",
            "AWS identity could not be resolved.",
            exit_code=ExitCode.BLOCKED,
            remedy=Remedy("command", "aws sso login"),
            gate="aws_sso_login",
        )
    return identity


def to_cli_error(result: CheckResult) -> CliError:
    """Classify a failed prerequisite check. Also used by `doctor` for its exit code.

    Prerequisites that need a human action are BLOCKED; a wrong account is the
    operator's answers being wrong, which is INVALID.
    """
    blocked_gates = {CHECK_AWS_CLI, CHECK_AWS_SSO, CHECK_TERRAFORM, CHECK_PIXI}
    exit_code = ExitCode.BLOCKED if result.id in blocked_gates else ExitCode.INVALID
    if result.id == CHECK_AWS_SSO and result.detail.get("credential_method") not in (
        None,
        "",
        "sso",
    ):
        # Non-SSO credentials are a configuration choice, not a pending human step.
        exit_code = ExitCode.INVALID
    return CliError(
        result.id,
        result.message,
        exit_code=exit_code,
        remedy=result.remedy,
        gate=result.id,
        detail=result.detail,
    )


def _from_cli_error(check_id: str, title: str, err: CliError) -> CheckResult:
    return CheckResult(
        check_id,
        title,
        FAIL,
        err.message,
        err.remedy,
        detail={"code": err.code, "raw": err.raw} if err.raw else {"code": err.code},
    )


def _terraform_version(result: Completed) -> str | None:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return _first_match(r"Terraform v(\d+\.\d+\.\d+)", result.output)
    version = payload.get("terraform_version")
    return str(version) if version else None


def _first_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _default_session() -> Any:
    import boto3

    return boto3.Session()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]
