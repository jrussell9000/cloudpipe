"""The ordered health checklist behind `globus doctor`.

Why an ordered list with dependency skipping, rather than independent checks:
almost every symptom here has the same few root causes, and an unordered report
shows the same cause several times. When the SSO login has expired, "the source
listing failed" is noise — the operator needs one line that says to run
`aws sso login`, and the rest saying "not run" so nobody goes hunting.

So each check names what it depends on. A dependency that failed produces
`skipped` with the reason, never `fail` and never `pass`. Nothing here reports a
fact it could not observe.

The checks, in order (the order and the ids are the contract, `docs/globus-
contract.md`; contract 2.0 kept each surviving check's 1.x id):

 1. `doctor.prerequisites` — AWS CLI v2, an SSO login, the intended account and
    region, and cluster reachability (a WARN: only check 5 needs the cluster)
 2. `doctor.ssm_parameters` — the session clock and the two source parameters
    exist, and the source parameters are not Terraform placeholders
 3. `doctor.session` — the stored login's age against this deployment's 30-day
    re-authentication rule
 4. `doctor.source_listing` — the source base path lists with the transfer token,
    as the batch gate lists it
 5. `doctor.kubernetes_secret` — this deployment's credential Secret holds every
    key a login writes, each matching Secrets Manager
 6. `doctor.https_token` — one refresh of the HTTPS token for the source
    collection's scope, as the batch gate and every ingress pod need

The endpoint checks of contract 1.x (configuration, instance, GridFTP,
subscription, drift, destination listing, gateway credential, node records,
signing listener) went with the Globus Connect Server host (openspec
retire-globus-host). New checks are appended, never inserted.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import cluster as cluster_module
from . import environments, globus_client, prereqs, sessions
from .aws import MISSING, PLACEHOLDER_STATE, AwsGateway, ParameterState
from .config import SESSION_RULE_DAYS, DeploymentConfig
from .exits import CliError, ExitCode, Remedy
from .prereqs import FAIL, PASS, SKIPPED, WARN, CheckResult

CHECK_PREREQUISITES = "doctor.prerequisites"
CHECK_SSM = "doctor.ssm_parameters"
CHECK_SESSION = "doctor.session"
CHECK_SOURCE = "doctor.source_listing"
CHECK_KUBERNETES_SECRET = "doctor.kubernetes_secret"
CHECK_HTTPS_TOKEN = "doctor.https_token"

#: The order is part of the contract: a caller may rely on it and on the ids.
CHECK_ORDER = (
    CHECK_PREREQUISITES,
    CHECK_SSM,
    CHECK_SESSION,
    CHECK_SOURCE,
    CHECK_KUBERNETES_SECRET,
    CHECK_HTTPS_TOKEN,
)

_TITLES = {
    CHECK_PREREQUISITES: "Prerequisites",
    CHECK_SSM: "Ingress parameters in SSM",
    CHECK_SESSION: "Globus session",
    CHECK_SOURCE: "Source collection listing",
    CHECK_KUBERNETES_SECRET: "Kubernetes credential Secret",
    CHECK_HTTPS_TOKEN: "HTTPS token",
}

LOGIN = "pixi run globus login"
_NO_LOGIN = "Not run: no Globus login is stored for this deployment (see the session check)."


@dataclass
class Diagnostics:
    """Everything the checklist talks to, injected so each check is testable."""

    config: DeploymentConfig
    env: environments.Environment
    aws: AwsGateway
    runner: Any
    session_factory: Callable[[], Any] | None = None
    """How the prerequisite check resolves AWS credentials. `None` means boto3's
    default chain, which is what an operator's shell provides."""

    # Overridable seams, resolved where they are used rather than bound here: a
    # default bound at class-definition time would capture the original function,
    # and a test that replaced the module attribute would keep using the real one.
    build_transfer_client: Callable[..., Any] | None = None
    auth_client: Any = None
    sleep: Callable[[float], None] | None = None

    # Resolved as the checklist runs; later checks read what earlier ones found.
    identity: prereqs.AwsIdentity | None = None
    cluster: cluster_module.ClusterAccess | None = None
    parameters: dict[str, ParameterState] = field(default_factory=dict)
    _token: dict[str, Any] | None = None
    _token_read: bool = False

    def token(self) -> dict[str, Any] | None:
        """The stored credential, read at most once."""
        if not self._token_read:
            self._token_read = True
            self._token = self.aws.get_secret_json(self.env.token_secret)
        return self._token

    def transfer(self) -> Any:
        build = self.build_transfer_client or globus_client.build_transfer_client
        return build(self.token())

    def pause(self, seconds: float) -> None:
        (self.sleep or time.sleep)(seconds)

    def parameter(self, name: str) -> str | None:
        state = self.parameters.get(name)
        return state.value if state and state.ok else None


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def check_prerequisites(diag: Diagnostics) -> CheckResult:
    """Check 1: the operator's own machine, and where its credentials point.

    Reuses the prerequisite module rather than re-checking, so `doctor` and the
    refusal `login` raises can never disagree.
    """
    results, identity = prereqs.run_all(
        diag.config, runner=diag.runner, session_factory=diag.session_factory
    )
    diag.identity = identity

    access = cluster_module.probe(diag.runner)
    diag.cluster = access

    failed = [r for r in results if r.severity == FAIL]
    warned = [r for r in results if r.severity == WARN]

    detail: dict[str, Any] = {
        "checks": [r.as_dict() for r in results],
        "cluster_reachable": access.reachable,
    }
    if identity is not None:
        detail |= {
            "account": identity.account,
            "role": identity.arn,
            "region": identity.region,
            "profile": identity.profile,
        }

    if failed:
        first = failed[0]
        return CheckResult(
            CHECK_PREREQUISITES,
            _TITLES[CHECK_PREREQUISITES],
            FAIL,
            first.message,
            first.remedy,
            detail=detail | {"failed": [r.id for r in failed]},
            exit_code=prereqs.to_cli_error(first).exit_code,
        )

    # The resolved account, role, and region are printed even on success: acting
    # on the wrong account is the mistake with the widest blast radius, and it is
    # invisible unless something says which one is in use.
    identity_text = (
        f"account {identity.account} as {identity.arn} in {identity.region or 'no region set'}"
        if identity is not None
        else "no AWS identity resolved"
    )

    if not access.reachable:
        return CheckResult(
            CHECK_PREREQUISITES,
            _TITLES[CHECK_PREREQUISITES],
            WARN,
            f"{identity_text}. The Kubernetes API did not answer, so the cluster-side "
            "check is skipped; every other check runs through AWS and Globus APIs.",
            (access.error.remedy if access.error else None)
            or Remedy("command", "warp-cli debug access-reauth"),
            detail=detail,
        )

    warn_text = f" ({'; '.join(r.message for r in warned)})" if warned else ""
    return CheckResult(
        CHECK_PREREQUISITES,
        _TITLES[CHECK_PREREQUISITES],
        WARN if warned else PASS,
        f"{identity_text}; the cluster is reachable{warn_text}",
        warned[0].remedy if warned else None,
        detail=detail,
    )


def check_ssm_parameters(diag: Diagnostics) -> CheckResult:
    """Check 2: the parameters exist, and the source ones hold real values."""
    expected = diag.env.expected_ssm_params()
    states = {param.name: diag.aws.parameter_state(param.name) for param in expected}
    diag.parameters = states

    missing = [p for p in expected if states[p.name].state == MISSING]
    placeholders = [
        p for p in expected if states[p.name].state == PLACEHOLDER_STATE and not p.placeholder_ok
    ]
    detail = {"parameters": {name: state.state for name, state in states.items()}}
    title = _TITLES[CHECK_SSM]

    if missing:
        names = ", ".join(p.name for p in missing)
        return CheckResult(
            CHECK_SSM,
            title,
            FAIL,
            f"{len(missing)} parameter(s) do not exist: {names}. Terraform creates these "
            "(terraform/modules/stack/ingress_session.tf), so the stack has not been applied "
            "in this account and region.",
            Remedy("human", "apply the stack; see docs/infrastructure.md"),
            detail=detail | {"missing": names},
            exit_code=ExitCode.CHECK_FAILED,
        )

    if placeholders:
        names = ", ".join(p.name for p in placeholders)
        return CheckResult(
            CHECK_SSM,
            title,
            FAIL,
            f"{len(placeholders)} parameter(s) still hold the Terraform placeholder: {names}. "
            "No source collection is configured, so HTTPS ingress has nothing to copy from.",
            Remedy(
                "human",
                "set globus_source_collection_id and globus_source_base_path in the stack's "
                "tfvars and apply",
            ),
            detail=detail | {"placeholders": names},
            exit_code=ExitCode.CHECK_FAILED,
        )

    return CheckResult(
        CHECK_SSM,
        title,
        PASS,
        f"{len(expected)} parameter(s) present under {diag.env.ssm_prefix}",
        detail=detail,
    )


def check_session(diag: Diagnostics) -> CheckResult:
    """Check 3: how much life the stored login has left, under the 30-day rule.

    Measured against this deployment's own rule, not a collection's timeout: the
    source's is a year, and the ingress pod refuses a login older than 30 days
    before it sends any request (`images/globus/https_ingress.py`).
    """
    timeout_minutes = diag.config.session_timeout_minutes
    established = diag.aws.get_parameter(diag.env.session_param)
    state = sessions.evaluate(established, timeout_minutes=timeout_minutes)
    detail = state.as_dict() | {
        "rule_days": SESSION_RULE_DAYS,
        "parameter": diag.env.session_param,
    }
    title = _TITLES[CHECK_SESSION]

    if state.status == sessions.UNKNOWN:
        return CheckResult(
            CHECK_SESSION,
            title,
            FAIL,
            "No login time is recorded, so how long the stored session has left is unknown. "
            "An unknown session is treated as unusable: this is exactly the state in which "
            "a batch must not start.",
            Remedy("command", LOGIN),
            detail=detail,
            exit_code=ExitCode.BLOCKED,
        )
    described = (
        f"{sessions.describe(state)} Rule: {SESSION_RULE_DAYS} days, this deployment's "
        "re-authentication rule for ingress-mode=https."
    )
    if state.status == sessions.EXPIRED:
        return CheckResult(
            CHECK_SESSION,
            title,
            FAIL,
            described,
            Remedy("command", LOGIN),
            detail=detail,
            exit_code=ExitCode.BLOCKED,
        )
    if state.status == sessions.WARN:
        return CheckResult(
            CHECK_SESSION, title, WARN, described, Remedy("command", LOGIN), detail=detail
        )
    return CheckResult(CHECK_SESSION, title, PASS, described, detail=detail)


def check_source_listing(diag: Diagnostics) -> CheckResult:
    """Check 4: a listing of the source base path, with the stored transfer token."""
    collection_id = diag.parameter(diag.env.source_collection_id_param)
    base_path = diag.parameter(diag.env.source_base_path_param) or "/"
    assert collection_id  # guarded by the dependency rule in `run`
    title = _TITLES[CHECK_SOURCE]
    try:
        attempts = globus_client.source_listable(
            diag.transfer(), collection_id, base_path, sleep=diag.pause
        )
    except CliError as err:
        return _from_cli_error(CHECK_SOURCE, title, err)
    retried = f" after {attempts - 1} retried node failure(s)" if attempts > 1 else ""
    return CheckResult(
        CHECK_SOURCE,
        title,
        PASS,
        f"listed {base_path} on {collection_id}{retried}",
        detail={"collection_id": collection_id, "path": base_path, "attempts": attempts},
    )


def check_kubernetes_secret(diag: Diagnostics) -> CheckResult:
    """Check 5: this deployment's Secret holds what a login wrote.

    Only through this deployment's own cluster (#523). The cluster named for the
    deployment is looked up in EKS, and its endpoint must be kubectl's current
    server: otherwise the check would compare another deployment's Secret — the
    shared 10.0.0.0/16 makes a sandbox and production indistinguishable by
    address. No such cluster, or no such Secret in it, is SKIP with the reason.

    Compared by hash; no token is ever put in a message or a record. Every synced
    key is compared, `https-refresh-token` included, which the ingress pods read.
    """
    title = _TITLES[CHECK_KUBERNETES_SECRET]
    secret_name = diag.env.k8s_secret
    cluster_name = diag.env.cluster_name
    endpoint = diag.aws.cluster_endpoint(cluster_name)
    if endpoint is None:
        return CheckResult(
            CHECK_KUBERNETES_SECRET,
            title,
            SKIPPED,
            f"Not run: this deployment has no EKS cluster named `{cluster_name}` in this "
            "account and region, so it has no credential Secret of its own to check.",
            detail={"cluster": cluster_name},
        )
    server = cluster_module.current_server(diag.runner)
    if (server or "").rstrip("/") != endpoint.rstrip("/"):
        return CheckResult(
            CHECK_KUBERNETES_SECRET,
            title,
            FAIL,
            f"kubectl's current context points at {server or 'no server'}, not at "
            f"`{cluster_name}` ({endpoint}), so the Secret it can read is not this "
            "deployment's.",
            Remedy(
                "command",
                f"aws eks update-kubeconfig --name {cluster_name} --region "
                f"{diag.identity.region if diag.identity else '<region>'}",
            ),
            detail={"cluster": cluster_name, "endpoint": endpoint, "kubectl_server": server},
            exit_code=ExitCode.INVALID,
        )

    read = cluster_module.read_secret(diag.runner, name=secret_name)
    namespace = cluster_module.ARGO_NAMESPACE
    if not read.exists:
        return CheckResult(
            CHECK_KUBERNETES_SECRET,
            title,
            SKIPPED,
            f"Not run: `{cluster_name}` has no `{secret_name}` Secret in {namespace}, so this "
            "deployment has no credential Secret to compare.",
            detail={"cluster": cluster_name, "secret": secret_name},
        )
    if not read.ok:
        return CheckResult(
            CHECK_KUBERNETES_SECRET,
            title,
            FAIL,
            f"Could not read the `{secret_name}` Secret in {namespace}.",
            Remedy("human", "check the ExternalSecret and the namespace"),
            detail={"secret": secret_name, "error": read.error},
            exit_code=ExitCode.CHECK_FAILED,
        )

    stored = diag.token() or {}
    detail: dict[str, Any] = {"cluster": cluster_name, "secret": secret_name, "keys": {}}
    absent, stale = [], []
    for key in cluster_module.SYNCED_KEYS:
        in_cluster = read.values.get(key) or ""
        in_aws = str(stored.get(key) or "").strip()
        detail["keys"][key] = {
            "cluster_fingerprint": _fingerprint(in_cluster),
            "secrets_manager_fingerprint": _fingerprint(in_aws),
        }
        if not in_cluster:
            absent.append(key)
        elif in_cluster != in_aws:
            stale.append(key)

    if absent:
        return CheckResult(
            CHECK_KUBERNETES_SECRET,
            title,
            FAIL,
            f"The `{secret_name}` Secret has no {', '.join(absent)}, so every ingress pod "
            "would fail to authenticate.",
            Remedy("command", LOGIN),
            detail=detail | {"absent": absent},
            exit_code=ExitCode.CHECK_FAILED,
        )
    if stale:
        interval = cluster_module.eso_refresh_interval()
        return CheckResult(
            CHECK_KUBERNETES_SECRET,
            title,
            WARN,
            f"The `{secret_name}` Secret holds a different {', '.join(stale)} than Secrets "
            f"Manager. External Secrets re-syncs on {interval}, so this is expected shortly "
            "after a login and a problem if it persists.",
            Remedy("human", f"wait for the {interval} sync, or force it with `{LOGIN}`"),
            detail=detail | {"stale": stale},
        )
    return CheckResult(
        CHECK_KUBERNETES_SECRET,
        title,
        PASS,
        f"`{secret_name}` in `{cluster_name}` matches Secrets Manager on all "
        f"{len(cluster_module.SYNCED_KEYS)} keys",
        detail=detail,
    )


def check_https_token(diag: Diagnostics) -> CheckResult:
    """Check 6: one refresh of the stored HTTPS token, for the source's scope."""
    collection_id = diag.parameter(diag.env.source_collection_id_param)
    assert collection_id  # guarded by the dependency rule in `run`
    title = _TITLES[CHECK_HTTPS_TOKEN]
    try:
        globus_client.https_token_refreshes(
            diag.token(), collection_id, auth_client=diag.auth_client
        )
    except CliError as err:
        return _from_cli_error(CHECK_HTTPS_TOKEN, title, err)
    return CheckResult(
        CHECK_HTTPS_TOKEN,
        title,
        PASS,
        f"the HTTPS token refreshes for {collection_id}",
        detail={"collection_id": collection_id},
    )


# ---------------------------------------------------------------------------
# The order
# ---------------------------------------------------------------------------


def run(diag: Diagnostics) -> list[CheckResult]:
    """Run the checklist in order, skipping whatever its dependencies invalidated."""
    results: list[CheckResult] = []

    prereq = check_prerequisites(diag)
    results.append(prereq)
    if prereq.severity == FAIL:
        # Every remaining check needs AWS. Naming the cause once and marking the
        # rest "not run" is the whole point of the ordering.
        reason = "Not run: the prerequisite check failed, so nothing else could be read."
        results.extend(_skip_rest(CHECK_SSM, reason))
        return results

    results.append(check_ssm_parameters(diag))
    results.append(check_session(diag))

    token = diag.token()
    source_id = diag.parameter(diag.env.source_collection_id_param)
    no_source = "Not run: no source collection id is recorded in SSM (see the parameter check)."

    if not token:
        results.append(_skipped(CHECK_SOURCE, _NO_LOGIN))
    elif not source_id:
        results.append(_skipped(CHECK_SOURCE, no_source))
    else:
        results.append(check_source_listing(diag))

    if diag.cluster is not None and diag.cluster.reachable:
        results.append(check_kubernetes_secret(diag))
    else:
        results.append(
            _skipped(
                CHECK_KUBERNETES_SECRET,
                "Not run: the Kubernetes API was unreachable. Connect Cloudflare WARP "
                "(`warp-cli debug access-reauth`) to include this check.",
            )
        )

    if not token:
        results.append(_skipped(CHECK_HTTPS_TOKEN, _NO_LOGIN))
    elif not source_id:
        results.append(_skipped(CHECK_HTTPS_TOKEN, no_source))
    else:
        results.append(check_https_token(diag))

    return results


def exit_code_for(results: list[CheckResult]) -> ExitCode:
    """The first failure decides.

    Checks run in dependency order, so the first FAIL is the root cause and its
    classification is the one a caller should act on.
    """
    for result in results:
        if result.severity == FAIL:
            return result.exit_code or ExitCode.CHECK_FAILED
    return ExitCode.OK


def _skip_rest(from_id: str, reason: str) -> list[CheckResult]:
    start = CHECK_ORDER.index(from_id)
    return [_skipped(check_id, reason) for check_id in CHECK_ORDER[start:]]


def _skipped(check_id: str, reason: str) -> CheckResult:
    return CheckResult(check_id, _TITLES[check_id], SKIPPED, reason)


def _from_cli_error(check_id: str, title: str, err: CliError) -> CheckResult:
    return CheckResult(
        check_id,
        title,
        FAIL,
        err.message,
        err.remedy,
        detail={"code": err.code, "raw": err.raw, **err.detail},
        exit_code=err.exit_code,
    )


def _fingerprint(value: str) -> str | None:
    """A short hash, so two tokens can be compared in a report without printing either."""
    if not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:12]
