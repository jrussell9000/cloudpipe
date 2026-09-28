"""The environment table: every name a command may touch, resolved in one place.

`--env staging` must not be able to reach a production secret, parameter, or
gateway by accident, so no module builds these names itself — they come from
here, and `tests/globus/test_environment_isolation.py` asserts that a staging run
touches nothing production owns.
"""

from __future__ import annotations

from dataclasses import dataclass

PRODUCTION = "production"
STAGING = "staging"
ENV_NAMES = (PRODUCTION, STAGING)


@dataclass(frozen=True)
class Environment:
    """Resolved names for one environment."""

    name: str
    ssm_prefix: str
    """Where this environment's own parameters live."""

    shared_ssm_prefix: str
    """Where deployment-wide parameters live (instance id, source collection).

    Staging shares the production endpoint, its EC2 instance, and the upstream
    source collection — only the gateway, collection, credential, and token
    differ. Splitting these two prefixes is what keeps that honest: a staging
    command reads the shared instance id but can never read or write the
    production collection id or token.
    """

    gateway_name: str
    collection_name: str
    token_secret: str
    credential_secret: str

    @property
    def is_production(self) -> bool:
        return self.name == PRODUCTION

    @property
    def collection_id_param(self) -> str:
        return f"{self.ssm_prefix}/collection-id"

    @property
    def session_param(self) -> str:
        """When the Globus session behind the stored token was established.

        Deliberately not the secret's `LastChangedDate`: that also moves when the
        secret is edited for an unrelated reason, which would silently reset the
        clock a batch depends on.
        """
        return f"{self.ssm_prefix}/session-established-at"

    @property
    def config_param(self) -> str:
        """The rendered GCS configuration document (shared by both environments)."""
        return f"{self.shared_ssm_prefix}/config"

    @property
    def instance_id_param(self) -> str:
        return f"{self.shared_ssm_prefix}/instance-id"

    @property
    def endpoint_id_param(self) -> str:
        return f"{self.shared_ssm_prefix}/endpoint-id"

    @property
    def service_client_id_param(self) -> str:
        """The GCS service client's UUID, as the instance reads it.

        Deployment-wide, beside its secret and for the same reason: one service
        client per deployment. Written by `globus register-service-client` and read
        by the three SSM documents that export `GCS_CLI_CLIENT_ID`; a SecureString
        only because Terraform created it as one, not because a client id is
        secret.
        """
        return f"{self.shared_ssm_prefix}/gcs-client-id"

    @property
    def service_client_secret_param(self) -> str:
        """The GCS service client's secret, as the instance reads it.

        Deployment-wide: there is one service client per deployment, and both
        environments' gateways are created through it. **Written** here by
        `globus register-service-client`, which is the only moment the secret
        exists outside SSM — Globus discloses it once, at creation — and never
        read back: `bootstrap-endpoint` compares the answers document's
        `service_client_secret_ref` against this name, which is a different thing
        from reading the secret, and is the point. Nothing but the instance's own
        SSM read ever resolves the value, so it reaches no terminal, no SSM
        command parameter, and no CloudTrail record.
        """
        return f"{self.shared_ssm_prefix}/gcs-client-secret"

    @property
    def node_report_param(self) -> str:
        """What node records the endpoint held when the instance last registered.

        Deployment-wide, like `instance_id_param`: there is one endpoint and one
        host, so both environments read the same report. Written by the host
        (`cloudpipe-gcs-boot`), never by this CLI — hence absent from
        `owned_ssm_params()` — and absent from `expected_ssm_params()` too,
        because "no report yet" is a normal state with its own check rather than a
        blocked transfer.
        """
        return f"{self.shared_ssm_prefix}/node-report"

    @property
    def reconcile_plan_param(self) -> str:
        """Whether the endpoint matched its configuration, as of the last reconcile.

        Deployment-wide for the same reason as `node_report_param`: one endpoint,
        one host, one reconcile. The *scope* of a plan is not in the name but in the
        report — `globus configure` always runs `--only <this environment's
        gateway>`, so most plans stored here describe one gateway, and check 7
        refuses one written for the other environment rather than reading it as
        clean.

        Written by the reconcile on the instance (`reconcile.plan_report`), never by
        this CLI — hence absent from `owned_ssm_params()` — and absent from
        `expected_ssm_params()` too, because "no plan recorded yet" is a normal
        state with its own check rather than a blocked transfer.
        """
        return f"{self.shared_ssm_prefix}/reconcile-plan"

    @property
    def listener_report_param(self) -> str:
        """Whether each gateway's signing listener was serving, at the last install.

        Deployment-wide for the same reason as the two reports above: one host, one
        set of listeners. Both environments read it, and each picks out its OWN
        gateway — the report describes every declared listener, because the host
        cannot know which environment is asking.

        Written by the listener install document on the instance
        (`reconcile.listener_report`), never by this CLI — hence absent from
        `owned_ssm_params()` — and absent from `expected_ssm_params()` too, because
        "no listener report yet" is a normal state with its own check rather than a
        blocked transfer.
        """
        return f"{self.shared_ssm_prefix}/listener-report"

    @property
    def destination_listing_path_param(self) -> str:
        """Where to list *through this environment's collection* to prove writes work.

        Per-environment, and that is the whole point: the two collections are rooted
        differently, so one rule cannot serve both. Production's collection is rooted
        at the bucket, so its root is an empty S3 prefix that the confined writer role
        denies; staging's is rooted inside its own prefix, so its root is already the
        permitted prefix. Terraform computes the value from the same expression that
        scopes the IAM condition, so the path checked and the prefix permitted cannot
        drift apart.

        Read, never written, by this CLI — hence absent from `owned_ssm_params()`.
        """
        return f"{self.ssm_prefix}/destination-listing-path"

    @property
    def source_collection_id_param(self) -> str:
        return f"{self.shared_ssm_prefix}/source-collection-id"

    @property
    def source_base_path_param(self) -> str:
        return f"{self.shared_ssm_prefix}/source-base-path"

    @property
    def k8s_secret(self) -> str:
        """The Kubernetes Secret ESO syncs the token into."""
        return "globus-credentials" if self.is_production else "globus-credentials-staging"

    def owned_ssm_params(self) -> tuple[str, ...]:
        """Parameters this environment may write. Used by the isolation test."""
        return (self.collection_id_param, self.session_param)

    def expected_ssm_params(self) -> tuple[ExpectedParam, ...]:
        """Parameters that must hold a real value before a transfer can run.

        Deliberately excludes `session-established-at` and `config`: each has its
        own `doctor` check, and reporting a missing value twice under two
        different headings is how a checklist stops being read.
        """
        return (
            ExpectedParam(
                self.instance_id_param, "the GCS EC2 instance to start before a transfer"
            ),
            ExpectedParam(self.endpoint_id_param, "the Globus endpoint this deployment owns"),
            ExpectedParam(self.collection_id_param, "the collection workflows write into"),
            ExpectedParam(
                self.source_collection_id_param, "the upstream collection data comes from"
            ),
            ExpectedParam(self.source_base_path_param, "the path under the source collection"),
            ExpectedParam(
                self.destination_listing_path_param,
                "the path to list through the destination collection",
            ),
        )


@dataclass(frozen=True)
class ExpectedParam:
    """An SSM parameter `doctor` expects to find, and why it matters."""

    name: str
    purpose: str


def resolve(
    env_name: str,
    *,
    deployment_name: str,
    collection_name: str,
    gateway_name: str | None = None,
) -> Environment:
    """Build the environment table for `env_name`.

    `collection_name` is the production collection's display name (the
    `globus_collection_name` Terraform variable, e.g. `cloudpipe-s3`); staging
    derives from it with a `-staging` suffix, matching
    `terraform/modules/globus/staging.tf`.

    `gateway_name` overrides the production *gateway's* display name, which
    otherwise equals the collection's. It exists because a gateway created
    before this tool need not follow the derivation: this deployment's is
    `cloudpipe-s3-gateway` beside a `cloudpipe-s3` collection. The reconcile
    matches objects by display name, so a derived-but-wrong name declares a
    gateway that does not exist and says nothing about the one that does.
    Staging's gateway is created by this tool, so it stays derived.
    """
    if env_name not in ENV_NAMES:
        raise ValueError(f"unknown environment {env_name!r}; expected one of {ENV_NAMES}")

    shared_prefix = f"/{deployment_name}/globus"
    if env_name == PRODUCTION:
        return Environment(
            name=PRODUCTION,
            ssm_prefix=shared_prefix,
            shared_ssm_prefix=shared_prefix,
            gateway_name=(gateway_name or "").strip() or collection_name,
            collection_name=collection_name,
            token_secret="globus/refresh-token",
            credential_secret=f"globus/s3-gateway/{collection_name}",
        )

    staging_name = f"{collection_name}-staging"
    return Environment(
        name=STAGING,
        ssm_prefix=f"{shared_prefix}/staging",
        shared_ssm_prefix=shared_prefix,
        gateway_name=staging_name,
        collection_name=staging_name,
        token_secret="globus/refresh-token-staging",
        credential_secret=f"globus/s3-gateway/{staging_name}",
    )
