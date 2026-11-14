"""Thin AWS accessors, so commands never build a client or a name themselves.

Everything here takes an already-resolved boto3 session — credentials come from
the operator's environment (`prereqs.check_aws_sso`), and nothing in this package
constructs credentials of its own.

Contract 2.0 left SSM, Secrets Manager and one EKS read. The EC2, SSM Run
Command and IAM simulation accessors went with the Globus Connect Server host
(openspec retire-globus-host).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import errors
from .exits import CliError, ExitCode, Remedy

#: What Terraform writes into a parameter nobody has filled in yet.
PLACEHOLDER = "REPLACE_AFTER_GCS_SETUP"

MISSING = "missing"
PLACEHOLDER_STATE = "placeholder"
SET = "set"


@dataclass(frozen=True)
class ParameterState:
    """Whether a parameter is absent, still the Terraform placeholder, or real.

    `get_parameter` collapses the first two to None, which is right for a caller
    that just wants a value. `doctor` needs them apart: "Terraform has not run"
    and "Terraform ran but nothing has filled it in" have different next steps.
    """

    name: str
    state: str
    value: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == SET


@dataclass
class AwsGateway:
    """The SSM, Secrets Manager and EKS reads and writes the CLI needs."""

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

    def cluster_endpoint(self, name: str) -> str | None:
        """The EKS API endpoint of the cluster named `name`, or None if there is none.

        `doctor` compares it with kubectl's current server, so a Kubernetes check
        can never read a cluster other than the deployment's own (#523): the
        shared 10.0.0.0/16 means a sandbox and production endpoint are
        indistinguishable by address alone.
        """
        client = self.session.client("eks")
        try:
            response = client.describe_cluster(name=name)
        except Exception as exc:
            if _is_not_found(exc, "ResourceNotFoundException"):
                return None
            raise errors.aws_error(exc) from exc
        endpoint = (response.get("cluster") or {}).get("endpoint")
        return str(endpoint) if endpoint else None


def _is_not_found(exc: Exception, code: str) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return (response.get("Error") or {}).get("Code") == code
    return type(exc).__name__ == code
