"""The names one deployment's commands may touch, resolved in one place.

There is no environment selector since contract 2.0: the staging gateway went
with the Globus Connect Server host (openspec retire-globus-host), and one
deployment has one source, one token secret and one Kubernetes Secret. Every
name here is derived from the deployment, so no command can read another
deployment's objects (#523).
"""

from __future__ import annotations

from dataclasses import dataclass

#: The only name this table ever carries. Kept as a field of the envelope
#: (`environment`) so a 1.x consumer reading it keeps working.
PRODUCTION = "production"


@dataclass(frozen=True)
class Environment:
    """Resolved names for one deployment."""

    name: str
    deployment_name: str
    ssm_prefix: str
    """Where this deployment's Globus parameters live: `/<deployment>/globus`."""

    token_secret: str
    """The Secrets Manager secret `login` writes and the ExternalSecret reads.

    One name per account, the same in the ExternalSecret, ESO's IAM policy and the
    Prefect worker's, so it is not derived from the deployment name: a deployment
    is its own account. #523's throwaway shared production's account, which is how
    it inherited production's names; with no host to stand up, nothing does that.
    """

    @property
    def is_production(self) -> bool:
        return True

    @property
    def session_param(self) -> str:
        """When the Globus session behind the stored tokens was established.

        Deliberately not the secret's `LastChangedDate`: that also moves when the
        secret is edited for an unrelated reason, which would silently reset the
        clock a batch depends on.
        """
        return f"{self.ssm_prefix}/session-established-at"

    @property
    def source_collection_id_param(self) -> str:
        return f"{self.ssm_prefix}/source-collection-id"

    @property
    def source_base_path_param(self) -> str:
        return f"{self.ssm_prefix}/source-base-path"

    @property
    def cluster_name(self) -> str:
        """The deployment's EKS cluster, which is named for it (`local.name`)."""
        return self.deployment_name

    @property
    def k8s_secret(self) -> str:
        """The Kubernetes Secret External Secrets syncs the tokens into.

        A fixed name inside the deployment's own cluster: it is the pipeline's
        contract with its WorkflowTemplates. `doctor` reads it only through the
        cluster named for this deployment, and reports SKIP when that cluster has
        none, rather than comparing another deployment's (#523).
        """
        return "globus-credentials"

    def owned_ssm_params(self) -> tuple[str, ...]:
        """Parameters this deployment's commands may write."""
        return (self.session_param,)

    def expected_ssm_params(self) -> tuple[ExpectedParam, ...]:
        """Parameters `doctor.ssm_parameters` checks, and why each matters.

        `placeholder_ok` marks the session clock: Terraform creates it holding a
        placeholder until the first login, and `doctor.session` reports that state
        itself, so reporting it here too would name one cause twice.
        """
        return (
            ExpectedParam(
                self.session_param, "when the stored Globus login was made", placeholder_ok=True
            ),
            ExpectedParam(
                self.source_collection_id_param, "the collection HTTPS ingress copies from"
            ),
            ExpectedParam(self.source_base_path_param, "the path under the source collection"),
        )


@dataclass(frozen=True)
class ExpectedParam:
    """An SSM parameter `doctor` expects to find, and why it matters."""

    name: str
    purpose: str
    placeholder_ok: bool = False


def resolve(*, deployment_name: str) -> Environment:
    """Build the name table for `deployment_name`."""
    return Environment(
        name=PRODUCTION,
        deployment_name=deployment_name,
        ssm_prefix=f"/{deployment_name}/globus",
        token_secret="globus/refresh-token",
    )
