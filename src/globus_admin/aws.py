"""Thin AWS accessors, so commands never build a client or a name themselves.

Everything here takes an already-resolved boto3 session — credentials come from
the operator's environment (`prereqs.check_aws_sso`), and nothing in this package
constructs credentials of its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import errors
from .exits import CliError, ExitCode, Remedy

PLACEHOLDER = "REPLACE_AFTER_GCS_SETUP"

MISSING = "missing"
PLACEHOLDER_STATE = "placeholder"
SET = "set"

IAM_PRESENT = "present"
IAM_ABSENT = "absent"
IAM_UNREADABLE = "unreadable"

ALLOWED = "allowed"

#: What an S3 storage gateway's credential has to be able to do to the objects it
#: writes. Read as well as write: GridFTP reads back what it uploaded.
S3_WRITE_ACTIONS = ("s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload")

#: A key no gateway prefix contains, used to ask the simulator the confinement
#: question: may this role write OUTSIDE its prefix? It is never written — the
#: simulator evaluates policy without touching the bucket.
CONFINEMENT_PROBE_KEY = "cloudpipe-doctor-confinement-probe"

ASSUME_ROLE = "sts:AssumeRole"

#: SSM Run Command statuses that will not change again.
COMMAND_TERMINAL = ("Success", "Cancelled", "TimedOut", "Failed")

#: What `send_command` reports before the agent has picked the command up. Not an
#: SSM status — `GetCommandInvocation` raises until the invocation exists, and
#: this is what that is reported as.
COMMAND_PENDING = "Pending"


@dataclass(frozen=True)
class ParameterState:
    """Whether a parameter is absent, still the Terraform placeholder, or real.

    `get_parameter` collapses the first two to None, which is right for a caller
    that just wants a value. `doctor` needs them apart: "Terraform has not run"
    and "Terraform ran but GCS setup never finished" have different next steps.
    """

    name: str
    state: str
    value: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == SET


@dataclass(frozen=True)
class RoleReport:
    """What can be seen of an IAM role, including "nothing, and here is why".

    Three states rather than a boolean: absent and unreadable have different next
    steps — one is a Terraform apply, the other is a permission the operator's own
    role is missing — and a check that collapsed them would send people to the
    wrong place.

    `trusted_principals` is read out of the trust policy rather than simulated:
    the simulator never retrieves resource-based policies, and AWS documents that
    it cannot simulate one for a role at all — so "does this role accept that
    principal" has no simulator answer. Only Allow statements naming an AWS
    principal for `sts:AssumeRole` count; conditions are not evaluated, which is
    safe here because Terraform writes none (`s3_gateway_roles.tf`).
    """

    name: str
    state: str
    arn: str | None = None
    trusted_principals: tuple[str, ...] = ()
    error: str | None = None

    @property
    def present(self) -> bool:
        return self.state == IAM_PRESENT

    def trusts(self, principal_arn: str) -> bool:
        return principal_arn in self.trusted_principals

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "role_name": self.name,
            "state": self.state,
            "arn": self.arn,
            "trusted_principals": list(self.trusted_principals),
        }
        if self.error:
            record["error"] = self.error
        return record


@dataclass(frozen=True)
class InstanceRole:
    """The role behind an instance's profile — the principal a listener assumes FROM.

    Read from the instance itself rather than from Terraform's naming, because the
    question is whether the role trusts the host that is actually running.
    """

    arn: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PermissionCheck:
    """The simulator's answer for one resource, per action.

    `evaluated` is false when the simulation itself could not run — the operator's
    role may not hold `iam:SimulatePrincipalPolicy`. That is not the same answer as
    "denied", and is never reported as one.
    """

    resource: str
    decisions: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def evaluated(self) -> bool:
        return self.error is None and bool(self.decisions)

    @property
    def allowed(self) -> bool:
        return self.evaluated and all(d == ALLOWED for d in self.decisions.values())

    @property
    def denied_actions(self) -> tuple[str, ...]:
        return tuple(action for action, d in sorted(self.decisions.items()) if d != ALLOWED)

    @property
    def allowed_actions(self) -> tuple[str, ...]:
        """What was allowed — the failure list when the question is confinement."""
        if not self.evaluated:
            return ()
        return tuple(action for action, d in sorted(self.decisions.items()) if d == ALLOWED)

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {"resource": self.resource, "decisions": dict(self.decisions)}
        if self.error:
            record["error"] = self.error
        return record


@dataclass(frozen=True)
class CommandInvocation:
    """One SSM Run Command invocation, as far as it has got.

    `response_code` is the exit status of the script on the instance. It is kept
    apart from `status` because SSM collapses every non-zero exit into the single
    status `Failed`, and the number is what says *which* failure: the reconcile
    speaks the same exit-code vocabulary as this CLI (`exits.ExitCode`), so a
    blocked plan and a crash are distinguishable only here.
    """

    command_id: str
    instance_id: str
    status: str
    response_code: int | None = None
    stdout: str = ""
    stderr: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in COMMAND_TERMINAL

    @property
    def succeeded(self) -> bool:
        return self.status == "Success"

    def as_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "instance_id": self.instance_id,
            "status": self.status,
            "response_code": self.response_code,
        }


@dataclass
class AwsGateway:
    """SSM, Secrets Manager, EC2, and read-only IAM the CLI needs."""

    session: Any

    def get_parameter(self, name: str, *, decrypt: bool = False) -> str | None:
        """Parameter value, or None when absent or still a setup placeholder."""
        client = self.session.client("ssm")
        try:
            response = client.get_parameter(Name=name, WithDecryption=decrypt)
        except Exception as exc:
            if _is_not_found(exc, "ParameterNotFound"):
                return None
            raise errors.aws_error(exc) from exc
        value = (response.get("Parameter") or {}).get("Value")
        if not value or value == PLACEHOLDER:
            return None
        return str(value)

    def parameter_state(self, name: str, *, decrypt: bool = False) -> ParameterState:
        """Absent, placeholder, or set — for reporting rather than for using."""
        client = self.session.client("ssm")
        try:
            response = client.get_parameter(Name=name, WithDecryption=decrypt)
        except Exception as exc:
            if _is_not_found(exc, "ParameterNotFound"):
                return ParameterState(name, MISSING)
            raise errors.aws_error(exc) from exc
        value = (response.get("Parameter") or {}).get("Value")
        if not value:
            return ParameterState(name, MISSING)
        if value == PLACEHOLDER:
            return ParameterState(name, PLACEHOLDER_STATE)
        return ParameterState(name, SET, str(value))

    def put_parameter(self, name: str, value: str, *, secure: bool = False) -> None:
        client = self.session.client("ssm")
        try:
            client.put_parameter(
                Name=name,
                Value=value,
                Type="SecureString" if secure else "String",
                Overwrite=True,
            )
        except Exception as exc:
            raise errors.aws_error(exc) from exc

    def get_secret_json(self, secret_id: str) -> dict[str, Any] | None:
        """Secret contents as a dict, or None when the secret is absent or empty."""
        client = self.session.client("secretsmanager")
        try:
            response = client.get_secret_value(SecretId=secret_id)
        except Exception as exc:
            if _is_not_found(exc, "ResourceNotFoundException"):
                return None
            raise errors.aws_error(exc) from exc
        payload = response.get("SecretString")
        if not payload:
            return None
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise CliError(
                "aws.secret_unparseable",
                f"The secret {secret_id} does not contain JSON.",
                exit_code=ExitCode.CHECK_FAILED,
                remedy=Remedy("command", "pixi run globus login"),
                raw=str(exc),
            ) from exc
        return parsed if isinstance(parsed, dict) else None

    def put_secret_json(self, secret_id: str, payload: dict[str, Any]) -> None:
        client = self.session.client("secretsmanager")
        body = json.dumps(payload)
        try:
            client.put_secret_value(SecretId=secret_id, SecretString=body)
        except Exception as exc:
            if _is_not_found(exc, "ResourceNotFoundException"):
                try:
                    client.create_secret(Name=secret_id, SecretString=body)
                    return
                except Exception as create_exc:
                    raise errors.aws_error(create_exc) from create_exc
            raise errors.aws_error(exc) from exc

    def instance_state(self, instance_id: str) -> str:
        client = self.session.client("ec2")
        try:
            response = client.describe_instance_status(
                InstanceIds=[instance_id], IncludeAllInstances=True
            )
        except Exception as exc:
            raise errors.aws_error(exc) from exc
        statuses = response.get("InstanceStatuses") or []
        if not statuses:
            return "unknown"
        return str((statuses[0].get("InstanceState") or {}).get("Name", "unknown"))

    def instance_address(self, instance_id: str) -> str | None:
        """Where GridFTP would answer: public DNS name, else public IP, else None.

        The instance is reached over the public internet on 443 because Globus
        Transfer connects to it from outside the VPC — so this is the address the
        port check must use, not a private one.
        """
        client = self.session.client("ec2")
        try:
            response = client.describe_instances(InstanceIds=[instance_id])
        except Exception as exc:
            raise errors.aws_error(exc) from exc
        for reservation in response.get("Reservations") or []:
            for instance in reservation.get("Instances") or []:
                return (
                    instance.get("PublicDnsName") or instance.get("PublicIpAddress") or None
                ) or None
        return None

    def start_instance(self, instance_id: str) -> None:
        client = self.session.client("ec2")
        try:
            client.start_instances(InstanceIds=[instance_id])
        except Exception as exc:
            raise errors.aws_error(exc) from exc

    def stop_instance(self, instance_id: str) -> None:
        client = self.session.client("ec2")
        try:
            client.stop_instances(InstanceIds=[instance_id])
        except Exception as exc:
            raise errors.aws_error(exc) from exc

    def send_command(
        self,
        document_name: str,
        instance_id: str,
        parameters: dict[str, str],
        *,
        comment: str = "",
    ) -> str:
        """Start an SSM document on one instance. Returns the command id.

        SSM Run Command rather than SSH: the instance has no inbound admin path,
        the operator needs no key material, and the call is recorded in CloudTrail
        against the identity that made it.
        """
        client = self.session.client("ssm")
        try:
            response = client.send_command(
                DocumentName=document_name,
                InstanceIds=[instance_id],
                Parameters={key: [value] for key, value in parameters.items()},
                # 100 characters is SSM's limit, and it truncates rather than
                # rejecting — so this is cut here, where it is visible.
                Comment=comment[:100],
            )
        except Exception as exc:
            raise _send_error(exc, document_name, instance_id) from exc
        return str((response.get("Command") or {}).get("CommandId") or "")

    def command_invocation(self, command_id: str, instance_id: str) -> CommandInvocation:
        """This invocation's state and the output collected so far.

        SSM returns partial output while a command is still running, which is what
        makes streaming possible without an agent-side channel. It also raises
        until the agent has accepted the command — reported here as `Pending`,
        because to a caller polling for progress that is what it means.
        """
        client = self.session.client("ssm")
        try:
            response = client.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except Exception as exc:
            if _is_not_found(exc, "InvocationDoesNotExist"):
                return CommandInvocation(command_id, instance_id, COMMAND_PENDING)
            raise errors.aws_error(exc) from exc

        code = response.get("ResponseCode")
        return CommandInvocation(
            command_id,
            instance_id,
            str(response.get("Status") or COMMAND_PENDING),
            # -1 is SSM's "the script never ran", which is not an exit status.
            response_code=int(code) if isinstance(code, int) and code >= 0 else None,
            stdout=str(response.get("StandardOutputContent") or ""),
            stderr=str(response.get("StandardErrorContent") or ""),
        )

    def role(self, name: str) -> RoleReport:
        """What can be read of role `name`, degrading rather than raising.

        One call: `GetRole` returns the trust policy inline (boto3 has already
        URL-decoded it into a dict), which is the only part of the role this
        report needs that the simulator cannot supply.
        """
        client = self.session.client("iam")
        try:
            response = client.get_role(RoleName=name)
        except Exception as exc:
            if _is_not_found(exc, "NoSuchEntity"):
                return RoleReport(name, IAM_ABSENT)
            if _is_access_denied(exc):
                return RoleReport(name, IAM_UNREADABLE, error=str(exc))
            raise errors.aws_error(exc) from exc

        role = response.get("Role") or {}
        return RoleReport(
            name,
            IAM_PRESENT,
            arn=role.get("Arn") or None,
            trusted_principals=_assume_role_principals(role.get("AssumeRolePolicyDocument")),
        )

    def instance_role(self, instance_id: str) -> InstanceRole:
        """The role behind `instance_id`'s instance profile, or why it is unknown.

        Two reads — the instance names its profile, the profile names its role —
        and either can be refused. Refusal is reported, not raised: it limits what
        this operator can see, and says nothing about the deployment.
        """
        try:
            response = self.session.client("ec2").describe_instances(InstanceIds=[instance_id])
        except Exception as exc:
            if _is_access_denied(exc):
                return InstanceRole(error=str(exc))
            raise errors.aws_error(exc) from exc

        profile_arn = None
        for reservation in response.get("Reservations") or []:
            for instance in reservation.get("Instances") or []:
                profile_arn = (instance.get("IamInstanceProfile") or {}).get("Arn")
        if not profile_arn:
            return InstanceRole(error=f"instance {instance_id} has no instance profile")

        # An instance profile ARN ends `instance-profile/<path/>name`; the API wants the name.
        profile_name = str(profile_arn).rsplit("/", 1)[-1]
        try:
            profile = self.session.client("iam").get_instance_profile(
                InstanceProfileName=profile_name
            )
        except Exception as exc:
            if _is_access_denied(exc) or _is_not_found(exc, "NoSuchEntity"):
                return InstanceRole(error=str(exc))
            raise errors.aws_error(exc) from exc

        roles = (profile.get("InstanceProfile") or {}).get("Roles") or []
        if not roles or not roles[0].get("Arn"):
            return InstanceRole(error=f"instance profile {profile_name} holds no role")
        return InstanceRole(arn=str(roles[0]["Arn"]))

    def can_assume(self, principal_arn: str, role_arn: str) -> PermissionCheck:
        """The identity half of an `sts:AssumeRole`: may `principal_arn` ask?

        The other half — does the role accept the request — is its trust policy,
        which the simulator cannot evaluate; see `RoleReport`.
        """
        return self._simulate(principal_arn, (ASSUME_ROLE,), role_arn)

    def can_write_prefix(self, principal_arn: str, bucket: str, prefix: str) -> PermissionCheck:
        """Ask IAM's simulator whether `principal_arn` may write under `prefix`.

        Read-only: the simulator evaluates policies, including the organization's,
        without touching a single object. That matters here because the alternative
        way to answer this question is to write a probe object into the destination
        bucket with a credential the operator may not hold.
        """
        under = f"{prefix.strip('/')}/*" if prefix.strip("/") else "*"
        return self._simulate(principal_arn, S3_WRITE_ACTIONS, f"arn:aws:s3:::{bucket}/{under}")

    def can_write_outside_prefix(self, principal_arn: str, bucket: str) -> PermissionCheck:
        """The confinement question: may `principal_arn` write a key no prefix contains?

        `allowed` here is the failure. A role that can write its own prefix and
        also the bucket root is not confined, and confinement is the property that
        keeps the listener out of the security path (D3).
        """
        return self._simulate(
            principal_arn, S3_WRITE_ACTIONS, f"arn:aws:s3:::{bucket}/{CONFINEMENT_PROBE_KEY}"
        )

    def _simulate(
        self, principal_arn: str, actions: tuple[str, ...], resource: str
    ) -> PermissionCheck:
        client = self.session.client("iam")
        try:
            response = client.simulate_principal_policy(
                PolicySourceArn=principal_arn,
                ActionNames=list(actions),
                ResourceArns=[resource],
            )
        except Exception as exc:
            if _is_access_denied(exc) or _is_not_found(exc, "NoSuchEntity"):
                return PermissionCheck(resource, error=str(exc))
            raise errors.aws_error(exc) from exc

        decisions = {
            str(result.get("EvalActionName")): str(result.get("EvalDecision") or "unknown")
            for result in response.get("EvaluationResults") or []
            if result.get("EvalActionName")
        }
        return PermissionCheck(resource, decisions=decisions)


def _send_error(exc: Exception, document_name: str, instance_id: str) -> CliError:
    """Translate the two refusals that actually happen when sending a command.

    Both read as something else. `InvalidInstanceId` is SSM's answer for an
    instance that is stopped, still booting, or whose agent has not registered —
    it says nothing about the id being wrong, which is how it reads. A missing
    document means Terraform has not been applied since the reconcile landed,
    not that the operator mistyped anything.
    """
    if _is_not_found(exc, "InvalidInstanceId"):
        return CliError(
            "ssm.instance_unavailable",
            f"SSM will not accept a command for {instance_id}. The instance is stopped, "
            "still booting, or its SSM agent has not registered yet.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("human", "wait for the instance to finish booting, then re-run"),
            raw=str(exc),
        )
    if _is_not_found(exc, "InvalidDocument"):
        return CliError(
            "ssm.document_missing",
            f"No SSM document named {document_name} exists in this account and region.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("command", "terraform apply"),
            raw=str(exc),
        )
    return errors.aws_error(exc)


def _assume_role_principals(document: Any) -> tuple[str, ...]:
    """AWS principals a trust policy allows to `sts:AssumeRole`, in order, deduplicated.

    Accepts the document as boto3 returns it (a dict) or as raw JSON text. A
    single statement, action or principal may be a bare value instead of a list —
    IAM allows both, and Terraform's `jsonencode` emits a bare string for one.
    """
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except json.JSONDecodeError:
            return ()
    if not isinstance(document, dict):
        return ()

    principals: list[str] = []
    for statement in _as_list(document.get("Statement")):
        if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
            continue
        actions = _as_list(statement.get("Action"))
        if not any(a in (ASSUME_ROLE, "sts:*", "*") for a in actions):
            continue
        principal = statement.get("Principal")
        if not isinstance(principal, dict):
            continue
        for arn in _as_list(principal.get("AWS")):
            if isinstance(arn, str) and arn not in principals:
                principals.append(arn)
    return tuple(principals)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _is_access_denied(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        return code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
    return type(exc).__name__ in {"AccessDenied", "AccessDeniedException"}


def _is_not_found(exc: Exception, code: str) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return (response.get("Error") or {}).get("Code") == code
    return type(exc).__name__ == code
