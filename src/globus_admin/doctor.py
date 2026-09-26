"""The ordered health checklist behind `globus doctor`.

Why an ordered list with dependency skipping, rather than a dozen independent
checks: almost every Globus symptom here has the same few root causes, and an
unordered report shows the same cause a dozen times. When the SSO login has
expired, "the collection listing failed" is noise — the operator needs one line
that says to run `aws sso login`, and eleven lines saying "not run" so nobody
goes hunting.

So each check names what it depends on. A dependency that failed produces
`skipped` with the reason, never `fail` and never `pass`. Nothing here reports a
fact it could not observe.

The checks, in order (the order is the contract; a wizard renders them as a
checklist):

 1. prerequisites — AWS CLI v2, an SSO login, the intended account, Terraform,
    pixi, and cluster reachability (a WARN: only checks 11 needs the cluster)
 2. the SSM parameters exist and are not Terraform placeholders
 3. the GCS configuration document is valid
 4. the GCS instance's state (stopped is informational, not a failure)
 5. GridFTP answers on 443 (needs the instance running)
 6. the endpoint is on a subscription (High Assurance depends on it)
 7. declared-versus-live configuration drift
 8. the Globus session's remaining life against the declared timeout
 9. a listing of the destination collection
10. a listing of the source collection
11. the Kubernetes `globus-credentials` Secret matches Secrets Manager
12. the role the gateway's signing listener assumes exists, trusts the GCS
    instance, can write where transfers land, and nothing outside it (skipped for
    a gateway not yet cut over from a static key)
13. the endpoint holds no node record for a host that no longer exists (read from
    a report the instance writes when it registers, since only the node itself can
    ask that question and it is stopped most of the time)
14. the gateway's signing listener is actually serving on its loopback port (read
    from a report the instance writes when the listeners are installed, since the
    listener binds `127.0.0.1` and nothing off-host can reach it)

Checks 12 and 14 are deliberately two lines rather than one. They answer the two
halves of the same 403: an unassumable role and a listener that is not running are
indistinguishable from the client, and an operator who cannot tell them apart
starts by suspecting the credential — which is the one of the two that is fine.

New checks are appended, never inserted: the ids and their order are a contract
(`docs/globus-contract.md`), so a caller reading the list by index keeps working.
"""

from __future__ import annotations

import hashlib
import json
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from . import cluster as cluster_module
from . import configdoc, environments, globus_client, prereqs, sessions
from .aws import (
    IAM_UNREADABLE,
    MISSING,
    PLACEHOLDER_STATE,
    SET,
    AwsGateway,
    InstanceRole,
    ParameterState,
)
from .config import DeploymentConfig
from .exits import CliError, ExitCode, Remedy
from .prereqs import FAIL, PASS, SKIPPED, WARN, CheckResult

CHECK_PREREQUISITES = "doctor.prerequisites"
CHECK_SSM = "doctor.ssm_parameters"
CHECK_CONFIGURATION = "doctor.configuration"
CHECK_INSTANCE = "doctor.instance"
CHECK_GRIDFTP = "doctor.gridftp"
CHECK_SUBSCRIPTION = "doctor.subscription"
CHECK_DRIFT = "doctor.config_drift"
CHECK_SESSION = "doctor.session"
CHECK_DESTINATION = "doctor.destination_listing"
CHECK_SOURCE = "doctor.source_listing"
CHECK_KUBERNETES_SECRET = "doctor.kubernetes_secret"
CHECK_S3_CREDENTIAL = "doctor.s3_gateway_credential"
CHECK_STALE_NODES = "doctor.stale_nodes"
CHECK_S3_LISTENER = "doctor.s3_listener"

#: The order is part of the contract: a caller may rely on it and on the ids.
CHECK_ORDER = (
    CHECK_PREREQUISITES,
    CHECK_SSM,
    CHECK_CONFIGURATION,
    CHECK_INSTANCE,
    CHECK_GRIDFTP,
    CHECK_SUBSCRIPTION,
    CHECK_DRIFT,
    CHECK_SESSION,
    CHECK_DESTINATION,
    CHECK_SOURCE,
    CHECK_KUBERNETES_SECRET,
    CHECK_S3_CREDENTIAL,
    CHECK_STALE_NODES,
    CHECK_S3_LISTENER,
)

GRIDFTP_PORT = 443
INSTANCE_START_TIMEOUT = 300.0
GRIDFTP_WAIT_TIMEOUT = 180.0
POLL_INTERVAL = 10.0

_NO_LOGIN = "Not run: no Globus login is stored for this environment (see the session check)."

_TITLES = {
    CHECK_PREREQUISITES: "Prerequisites",
    CHECK_SSM: "Globus parameters in SSM",
    CHECK_CONFIGURATION: "GCS configuration document",
    CHECK_INSTANCE: "GCS instance",
    CHECK_GRIDFTP: f"GridFTP on port {GRIDFTP_PORT}",
    CHECK_SUBSCRIPTION: "Endpoint subscription",
    CHECK_DRIFT: "Declared configuration matches the endpoint",
    CHECK_SESSION: "Globus session",
    CHECK_DESTINATION: "Destination collection listing",
    CHECK_SOURCE: "Source collection listing",
    CHECK_KUBERNETES_SECRET: "Kubernetes credential Secret",
    CHECK_S3_CREDENTIAL: "S3 gateway credential",
    CHECK_STALE_NODES: "Globus node records",
    CHECK_S3_LISTENER: "S3 signing listener",
}


def tcp_probe(host: str, port: int, timeout: float = 5.0) -> tuple[bool, str]:
    """Can a TCP connection be opened? Returns (reachable, detail)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "connected"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


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

    start_instance: bool = False

    # Overridable seams. Each defaults to `None` and is resolved where it is
    # used, not here: a default bound at class-definition time would capture the
    # original function object, and a test that replaced the module attribute
    # would silently keep talking to the real network.
    probe_tcp: Callable[[str, int, float], tuple[bool, str]] | None = None
    build_transfer_client: Callable[..., Any] | None = None
    sleep: Callable[[float], None] | None = None
    now: Callable[[], float] | None = None

    notify: Callable[[str], None] | None = None
    """Where to announce a wait that is about to happen, as it happens.

    `run` collects every result before the command prints any of them, so a check
    that blocks prints nothing until the whole checklist is done. With
    `--start-instance` that silence can run to `INSTANCE_START_TIMEOUT` plus
    `GRIDFTP_WAIT_TIMEOUT` — minutes of a dead terminal, which reads as a hang
    and invites a Ctrl-C partway through a boot. This is the seam that lets the
    checks say what they are waiting for without importing the emitter and
    without deciding where the text goes. `None` means say nothing, which is what
    the unit tests want."""

    # Resolved as the checklist runs; later checks read what earlier ones found.
    identity: prereqs.AwsIdentity | None = None
    cluster: cluster_module.ClusterAccess | None = None
    prerequisites: list[CheckResult] = field(default_factory=list)
    """The individual prerequisite results, kept as objects rather than only as
    the dicts folded into check 1's `detail`.

    `setup-status` reports one step per prerequisite, and to classify a failure
    it needs `CheckResult.exit_code` — the routing information that says whether
    a failure is a human gate or bad input. `as_dict()` drops that field on
    purpose, since it is not part of the published record."""

    parameters: dict[str, ParameterState] = field(default_factory=dict)
    document: Any = None
    instance_id: str | None = None
    instance_running: bool = False
    _token: dict[str, Any] | None = None
    _token_read: bool = False
    _transfer: Any = None

    # ---- shared lookups -------------------------------------------------

    def token(self) -> dict[str, Any] | None:
        """The stored `{native-app-client-id, refresh-token}`, read at most once."""
        if not self._token_read:
            self._token_read = True
            self._token = self.aws.get_secret_json(self.env.token_secret)
        return self._token

    def transfer(self) -> Any:
        if self._transfer is None:
            build = self.build_transfer_client or globus_client.build_transfer_client
            self._transfer = build(self.token(), env=self.env.name)
        return self._transfer

    def connect(self, host: str, port: int, timeout: float) -> tuple[bool, str]:
        return (self.probe_tcp or tcp_probe)(host, port, timeout)

    def pause(self, seconds: float) -> None:
        (self.sleep or time.sleep)(seconds)

    def say(self, text: str) -> None:
        """Announce a wait. A no-op unless a caller wired `notify`."""
        if self.notify is not None:
            self.notify(text)

    def clock(self) -> float:
        return (self.now or time.monotonic)()

    def parameter(self, name: str) -> str | None:
        state = self.parameters.get(name)
        return state.value if state and state.ok else None

    def gateway_declaration(self) -> dict[str, Any] | None:
        """This environment's storage gateway, as the configuration document declares it.

        None here means only "no declaration matched". It does NOT say which of
        the two reasons applies, and callers must not treat them alike — see
        `declared_gateway_names`.
        """
        if not isinstance(self.document, dict):
            return None
        for gateway in self.document.get("storage_gateways") or []:
            if isinstance(gateway, dict) and gateway.get("display_name") == self.env.gateway_name:
                return gateway
        return None

    def declared_gateway_names(self) -> list[str]:
        """Every gateway display name in the document, so a miss can be explained.

        A document declaring nothing and a document declaring gateways under
        other names look identical through `gateway_declaration`, and treating
        them alike is how a real failure disappears: on 2026-09-23 this
        deployment's answers omitted `gateway_name`, so the derived name was
        `cloudpipe-s3` while the document declared `cloudpipe-s3-gateway`, and
        check 12 — which had been reporting a genuine missing `iam_user` —
        silently became "not run".

        The matching is by display name throughout (the reconcile does the same),
        so a name that matches nothing is never "nothing to check": it means the
        object actually serving transfers is the one nobody is looking at.
        """
        if not isinstance(self.document, dict):
            return []
        return [
            str(gateway.get("display_name") or "")
            for gateway in self.document.get("storage_gateways") or []
            if isinstance(gateway, dict) and str(gateway.get("display_name") or "").strip()
        ]

    def declared_timeout_minutes(self) -> tuple[int, str]:
        """What the configuration ASKS the endpoint to enforce, and where it is written.

        Not what the endpoint enforces — see `effective_timeout_minutes`, which
        is what a session check must measure against.
        """
        gateway = self.gateway_declaration()
        if gateway is not None:
            declared = gateway.get("authentication_timeout_mins")
            if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
                return declared, f"declared for {self.env.gateway_name}"
        return self.config.session_timeout_minutes, "the answers document"

    def effective_timeout_minutes(self) -> tuple[int, str, int | None]:
        """The timeout in force, where it came from, and the declared value if it differs.

        **Live wins.** The declared value is what the endpoint has been asked
        for, and asking is not enforcing: production is deliberately
        `managed: false` here, so its gateway still enforces what it was set to
        by hand. Reading the declaration instead reported a session as lasting
        30 days when the gateway enforced 7 — a fourfold overstatement in the
        one check whose purpose is to stop a batch the session cannot outlive
        (2026-09-23).

        The declared value is still returned when the two disagree, so the
        message can say so: a mismatch is a real finding about this deployment,
        not noise to hide.
        """
        declared, source = self.declared_timeout_minutes()
        collection_id = self.parameter(self.env.collection_id_param)
        if not collection_id:
            return declared, source, None
        live = globus_client.live_timeout_minutes(self.transfer(), collection_id)
        if live is None:
            return declared, f"{source} (the endpoint did not report one)", None
        return live, "the live gateway", declared if live != declared else None


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def check_prerequisites(diag: Diagnostics) -> CheckResult:
    """Check 1: the operator's own machine, and where its credentials point.

    Reuses the prerequisite module rather than re-checking, so `doctor` and the
    refusal every other command raises can never disagree.
    """
    results, identity = prereqs.run_all(
        diag.config, runner=diag.runner, session_factory=diag.session_factory
    )
    diag.identity = identity
    diag.prerequisites = list(results)

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
            "Prerequisites",
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
            "Prerequisites",
            WARN,
            f"{identity_text}. The Kubernetes API did not answer, so the cluster-side "
            "check is skipped; Globus setup itself does not need it.",
            (access.error.remedy if access.error else None)
            or Remedy("command", "warp-cli debug access-reauth"),
            detail=detail,
        )

    warn_text = f" ({'; '.join(r.message for r in warned)})" if warned else ""
    return CheckResult(
        CHECK_PREREQUISITES,
        "Prerequisites",
        WARN if warned else PASS,
        f"{identity_text}; Terraform, pixi, and the cluster all reachable{warn_text}",
        warned[0].remedy if warned else None,
        detail=detail,
    )


def check_ssm_parameters(diag: Diagnostics) -> CheckResult:
    """Check 2: the parameters exist and hold real values, not Terraform placeholders."""
    expected = diag.env.expected_ssm_params()
    states = {}
    for param in expected:
        states[param.name] = diag.aws.parameter_state(param.name)
    diag.parameters = states

    missing = [p for p in expected if states[p.name].state == MISSING]
    placeholders = [p for p in expected if states[p.name].state == PLACEHOLDER_STATE]

    detail = {
        "parameters": {name: state.state for name, state in states.items()},
    }

    if missing:
        names = ", ".join(p.name for p in missing)
        return CheckResult(
            CHECK_SSM,
            "Globus parameters in SSM",
            FAIL,
            f"{len(missing)} parameter(s) do not exist: {names}. Terraform creates these, "
            "so this usually means the Globus module has not been applied.",
            Remedy("command", "terraform apply -target=module.globus"),
            detail=detail | {"missing": names},
            exit_code=ExitCode.CHECK_FAILED,
        )

    if placeholders:
        names = ", ".join(p.name for p in placeholders)
        return CheckResult(
            CHECK_SSM,
            "Globus parameters in SSM",
            FAIL,
            f"{len(placeholders)} parameter(s) still hold the Terraform placeholder: {names}. "
            "Terraform has run, but the Globus endpoint setup that fills them in has not "
            "finished.",
            Remedy("command", f"pixi run globus bootstrap-endpoint --env {diag.env.name}"),
            detail=detail | {"placeholders": names},
            exit_code=ExitCode.CHECK_FAILED,
        )

    return CheckResult(
        CHECK_SSM,
        "Globus parameters in SSM",
        PASS,
        f"{len(expected)} parameter(s) set under {diag.env.ssm_prefix}",
        detail=detail,
    )


def check_configuration(diag: Diagnostics) -> CheckResult:
    """Check 3: the declared configuration document is present and valid."""
    state = diag.aws.parameter_state(diag.env.config_param)
    if state.state != SET:
        return CheckResult(
            CHECK_CONFIGURATION,
            "GCS configuration document",
            WARN,
            f"No configuration document is stored at {diag.env.config_param}, so the "
            "endpoint's gateways and collections are not declared anywhere and drift "
            "cannot be detected.",
            Remedy("human", "apply the Terraform that renders the configuration document"),
            detail={"parameter": diag.env.config_param, "state": state.state},
        )

    try:
        document = json.loads(state.value or "")
    except json.JSONDecodeError as exc:
        return CheckResult(
            CHECK_CONFIGURATION,
            "GCS configuration document",
            FAIL,
            f"The configuration document at {diag.env.config_param} is not valid JSON.",
            Remedy("human", "re-apply the Terraform that renders the document"),
            detail={"parameter": diag.env.config_param, "error": str(exc)},
            exit_code=ExitCode.INVALID,
        )

    problems = configdoc.validate(document)
    if problems:
        diag.document = None
        return CheckResult(
            CHECK_CONFIGURATION,
            "GCS configuration document",
            FAIL,
            f"The configuration document has {len(problems)} problem(s): "
            + "; ".join(problems[:3])
            + ("; …" if len(problems) > 3 else ""),
            Remedy("human", "fix the Terraform inputs that render the document"),
            detail={"problems": problems},
            exit_code=ExitCode.INVALID,
        )

    diag.document = document
    return CheckResult(
        CHECK_CONFIGURATION,
        "GCS configuration document",
        PASS,
        "valid",
        detail=configdoc.summarize(document),
    )


def check_instance(diag: Diagnostics) -> CheckResult:
    """Check 4: the GCS instance's state. Stopped is normal — it runs only for transfers."""
    instance_id = diag.parameter(diag.env.instance_id_param)
    diag.instance_id = instance_id
    if not instance_id:
        return CheckResult(
            CHECK_INSTANCE,
            "GCS instance",
            SKIPPED,
            "Not run: no instance id is recorded in SSM.",
            detail={},
        )

    state = diag.aws.instance_state(instance_id)
    diag.instance_running = state == "running"

    if not diag.instance_running and diag.start_instance:
        # Said before the call, not after: the point is to be on screen while the
        # wait happens. The minutes are the real timeouts, so nobody has to guess
        # whether a quiet terminal is progress or a hang.
        diag.say(
            f"{instance_id} is {state}; starting it. This usually takes about a minute, "
            f"and the checks that need it wait up to {INSTANCE_START_TIMEOUT / 60:.0f} minutes "
            "for the instance and another "
            f"{GRIDFTP_WAIT_TIMEOUT / 60:.0f} for GridFTP to answer. Please wait — the "
            "checklist prints once every check has run."
        )
        diag.aws.start_instance(instance_id)
        state = _wait_for_running(diag, instance_id)
        diag.instance_running = state == "running"
        if diag.instance_running:
            diag.say(f"{instance_id} is running; waiting for GridFTP to accept connections.")

    detail = {"instance_id": instance_id, "state": state, "started_by_doctor": diag.start_instance}

    if diag.instance_running:
        return CheckResult(
            CHECK_INSTANCE, "GCS instance", PASS, f"{instance_id} is running", detail=detail
        )

    # A stopped instance is the steady state: the schedule stops it to save
    # money, and the pipeline starts it per batch. Reporting that as a failure
    # would teach the operator to ignore failures.
    return CheckResult(
        CHECK_INSTANCE,
        "GCS instance",
        PASS,
        f"{instance_id} is {state} — normal between batches. The checks that need it "
        "running are skipped; re-run with --start-instance to include them.",
        detail=detail,
    )


def check_gridftp(diag: Diagnostics) -> CheckResult:
    """Check 5: GridFTP answers on 443, from outside the VPC."""
    assert diag.instance_id  # guarded by the dependency rule in `run`
    address = diag.aws.instance_address(diag.instance_id)
    if not address:
        return CheckResult(
            CHECK_GRIDFTP,
            f"GridFTP on port {GRIDFTP_PORT}",
            FAIL,
            f"Instance {diag.instance_id} is running but has no public address, so Globus "
            "cannot reach it.",
            Remedy("human", "check the instance's public IP and security group"),
            detail={"instance_id": diag.instance_id},
            exit_code=ExitCode.CHECK_FAILED,
        )

    # Only wait when doctor started the instance itself: GridFTP takes a moment
    # to come up. Otherwise one attempt, so a healthy run stays fast.
    deadline = diag.clock() + (GRIDFTP_WAIT_TIMEOUT if diag.start_instance else 0)
    attempts = 0
    while True:
        attempts += 1
        reachable, detail_text = diag.connect(address, GRIDFTP_PORT, 5.0)
        if reachable or diag.clock() >= deadline:
            break
        diag.pause(POLL_INTERVAL)

    if reachable:
        return CheckResult(
            CHECK_GRIDFTP,
            f"GridFTP on port {GRIDFTP_PORT}",
            PASS,
            f"{address}:{GRIDFTP_PORT} answers",
            detail={"address": address, "attempts": attempts},
        )

    return CheckResult(
        CHECK_GRIDFTP,
        f"GridFTP on port {GRIDFTP_PORT}",
        FAIL,
        f"Nothing answered on {address}:{GRIDFTP_PORT}. Globus Transfer connects to the "
        "instance from outside AWS, so this port has to be open to the internet.",
        Remedy("command", f"pixi run globus configure --env {diag.env.name}"),
        detail={"address": address, "attempts": attempts, "error": detail_text},
        exit_code=ExitCode.CHECK_FAILED,
    )


def check_subscription(diag: Diagnostics) -> CheckResult:
    """Check 6: the endpoint is on a subscription — High Assurance depends on it."""
    endpoint_id = diag.parameter(diag.env.endpoint_id_param)
    assert endpoint_id  # guarded by the dependency rule in `run`
    try:
        endpoint = diag.transfer().get_endpoint(endpoint_id)
    except CliError as err:
        return _from_cli_error(CHECK_SUBSCRIPTION, "Endpoint subscription", err)
    except Exception as exc:
        return _from_cli_error(
            CHECK_SUBSCRIPTION,
            "Endpoint subscription",
            _translate(exc, diag),
        )

    subscription_id = _get(endpoint, "subscription_id")
    if not subscription_id:
        return CheckResult(
            CHECK_SUBSCRIPTION,
            "Endpoint subscription",
            FAIL,
            "This endpoint is not on a Globus subscription. High Assurance collections and "
            "the managed S3 connector both require one, and nobody outside Globus can set "
            "this — a subscription manager has to add the endpoint.",
            Remedy("human", "ask your Globus subscription manager to add this endpoint"),
            detail={"endpoint_id": endpoint_id},
            exit_code=ExitCode.BLOCKED,
        )

    return CheckResult(
        CHECK_SUBSCRIPTION,
        "Endpoint subscription",
        PASS,
        f"endpoint {endpoint_id} is subscribed",
        detail={"endpoint_id": endpoint_id, "subscription_id": str(subscription_id)},
    )


def check_drift(diag: Diagnostics) -> CheckResult:
    """Check 7: declared versus live configuration.

    Nothing on a workstation can answer this. The comparison needs
    `globus-connect-server` listings, which only the GCS Manager running on the
    node serves, and the node is stopped between batches. So this check observes
    nothing itself: the reconcile records its plan at `<prefix>/reconcile-plan`
    every time it runs, and the rule is applied here, where correcting it is a
    code change rather than an AMI rebuild — the same split check 13 uses.

    Until 2026-09-23 the plan went to the SSM command's output and nowhere else:
    aged out, addressed by command id rather than by "the current plan", and
    present only for runs that happened. That is why this reported `skipped`.

    Three refusals are load-bearing, and all three exist so that a question
    nobody asked cannot render as a clean endpoint.

    The report's `instance_id` must be the deployment's current instance. That
    matters *more* here than for node records, because an instance is replaced by
    pinning a new AMI — and the AMI is where the planner's rules live. A plan from
    a replaced host compared an older configuration using older rules, so it is
    not a statement about what is declared now.

    The report's `gateway` must cover this environment. `globus configure` always
    runs `--only <that environment's gateway>`, so most recorded plans looked at
    one gateway and say nothing whatever about the other. Accepting one written
    for the wrong environment is how "no drift" gets reported for the gateway
    nobody examined — the failure 11.7 found in check 12, where matching by
    display name let a wrong name read as good news.

    And `actions: null` is the host saying it could not compare, never "nothing
    differs".
    """
    detail: dict[str, Any] = {}
    param = diag.env.reconcile_plan_param
    report, refusal = _believable_host_report(
        diag, param, _plan_refusals(param, diag.env.name), _drift_result, detail
    )
    if refusal is not None:
        return refusal
    assert report is not None

    generated = detail["generated_at"]
    current = detail["instance_id"]
    replan = Remedy("command", f"pixi run globus configure --env {diag.env.name} --plan-only")

    gateway = report.get("gateway")
    detail |= {"gateway": gateway, "mode": str(report.get("mode") or "")}
    if gateway and str(gateway) != diag.env.gateway_name:
        return _drift_result(
            SKIPPED,
            f"Not run: the recorded plan covers storage gateway {str(gateway)!r}, and this "
            f"environment's is {diag.env.gateway_name!r}. Every `globus configure` run is "
            "scoped to one gateway, so that plan never looked at this one, and calling it "
            "clean would be reporting on a gateway nobody examined.",
            replan,
            detail=detail,
        )
    scope = f"gateway {str(gateway)!r}" if gateway else "the whole configuration"

    if report.get("actions") is None:
        return _drift_result(
            SKIPPED,
            f"Not run: instance {current} could not compare the endpoint with its "
            f"configuration at {generated}: "
            f"{report.get('unavailable') or 'no reason was recorded'}.",
            detail=detail,
        )

    counts = _plan_counts(report)
    if counts is None:
        return _drift_result(
            WARN,
            f"The plan at {param} does not report how much it found, so nothing can be "
            "concluded from it. Something other than the reconcile has written to this "
            "parameter.",
            Remedy("human", f"delete {param}; the reconcile rewrites it on its next run"),
            detail=detail,
        )
    detail["counts"] = counts
    actions, problems, notes = counts["actions"], counts["problems"], counts["notes"]

    # Truncation loses which objects, never how many — `plan_report` measures the
    # counts before it trims — so it qualifies every message rather than changing
    # any severity.
    cut = (
        " The recorded plan was trimmed to fit one SSM parameter, so not every object is "
        "named here."
        if report.get("truncated")
        else ""
    )
    as_of = f"Compared by {current} at {generated}, over {scope}."

    if problems:
        return _drift_result(
            FAIL,
            f"{problems} difference(s) between the configuration and the endpoint cannot be "
            f"reconciled: {_plan_summary(report, 'problems')}. Globus cannot change these "
            "fields in place and the reconcile never deletes and recreates, so each needs a "
            f"person. {as_of}{cut}",
            Remedy(
                "human",
                "resolve each difference by hand — either change the declaration to match "
                "the endpoint, or delete and recreate the object deliberately",
            ),
            detail=detail,
            exit_code=ExitCode.CHECK_FAILED,
        )

    if actions:
        return _drift_result(
            WARN,
            f"The endpoint does not match its configuration: {actions} change(s) are "
            f"pending — {_plan_summary(report, 'actions')}. {as_of}{cut}",
            Remedy("command", f"pixi run globus configure --env {diag.env.name}"),
            detail=detail,
        )

    if notes:
        # A note is a difference the reconcile deliberately leaves alone: an object
        # declared `managed: false`, or a field the endpoint does not report. Routine
        # for this deployment, whose production gateway is unmanaged on purpose — so
        # it is stated rather than warned about, matching how `configure` reports it.
        return _drift_result(
            PASS,
            f"Nothing to apply, though {notes} difference(s) were observed and left alone "
            f"(unmanaged objects, or fields the endpoint does not report). {as_of}{cut}",
            detail=detail,
        )

    return _drift_result(PASS, f"No differences. {as_of}{cut}", detail=detail)


def _drift_result(
    severity: str,
    message: str,
    remedy: Remedy | None = None,
    *,
    detail: dict[str, Any],
    exit_code: ExitCode | None = None,
) -> CheckResult:
    """One `CheckResult` shape, as check 13 has — check 7 now has nine of them."""
    return CheckResult(
        CHECK_DRIFT,
        _TITLES[CHECK_DRIFT],
        severity,
        message,
        remedy,
        detail=detail,
        exit_code=exit_code,
    )


def _plan_counts(report: dict[str, Any]) -> dict[str, int] | None:
    """The three counts as integers, or `None` if the report does not carry them.

    Read from `counts` rather than measured from the lists, because the lists may
    have been trimmed to fit the parameter while the counts never are. Measuring
    here would silently under-report exactly the plans with the most drift in them.
    """
    counts = report.get("counts")
    if not isinstance(counts, dict):
        return None
    read: dict[str, int] = {}
    for key in ("actions", "problems", "notes"):
        value = counts.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        read[key] = value
    return read


def _plan_summary(report: dict[str, Any], key: str, limit: int = 3) -> str:
    """A few of the objects named in the plan, for a one-line message."""
    items = report.get(key)
    if not isinstance(items, list) or not items:
        return "none were named in the recorded plan"
    named = [
        f"{entry.get('object_type') or 'object'} {str(entry.get('name') or 'unnamed')!r}"
        for entry in items[:limit]
        if isinstance(entry, dict)
    ]
    if not named:
        return "none were named in the recorded plan"
    more = len(items) - len(named)
    return ", ".join(named) + (f", and {more} more" if more > 0 else "")


def _plan_refusals(param: str, env_name: str) -> _ReportRefusals:
    return _ReportRefusals(
        missing=(
            f"Not run: the reconcile has not recorded a plan at {param}. It records one on "
            "every run, including `--plan-only`, which changes nothing — so this check "
            "starts reporting after the next one. Reported as `skipped` rather than `pass` "
            "on purpose: an unanswered question is not a clean endpoint."
        ),
        malformed=(
            f"The plan at {param} is not a JSON object, so the endpoint could not be "
            "compared with its configuration. Something other than the reconcile has "
            "written to this parameter."
        ),
        unknown_instance=(
            "Not run: this deployment's instance id is not recorded in SSM (see the "
            "parameter check), so there is no way to tell whether the recorded plan came "
            "from the host that is running now."
        ),
        replaced=lambda written_by, generated, current: (
            f"Not run: the plan was recorded by instance {written_by or 'an unnamed host'} "
            f"at {generated}, and this deployment's instance is now {current}. An instance "
            "is replaced by pinning a new AMI, which is also where the reconcile's own "
            "rules live — so that plan compared an older configuration using older rules, "
            f"and says nothing about what is declared now. Run `pixi run globus configure "
            f"--env {env_name} --plan-only` for a current one."
        ),
        rewrite=Remedy("human", f"delete {param}; the reconcile rewrites it on its next run"),
    )


def check_session(diag: Diagnostics) -> CheckResult:
    """Check 8: how much life the stored Globus login has left."""
    timeout_minutes, source, declared_differs = diag.effective_timeout_minutes()
    established = diag.aws.get_parameter(diag.env.session_param)
    state = sessions.evaluate(established, timeout_minutes=timeout_minutes)
    detail = state.as_dict() | {"timeout_source": source, "parameter": diag.env.session_param}
    if declared_differs is not None:
        detail["declared_timeout_minutes"] = declared_differs
    login = f"pixi run globus login{'' if diag.env.is_production else f' --env {diag.env.name}'}"

    if state.status == sessions.UNKNOWN:
        return CheckResult(
            CHECK_SESSION,
            "Globus session",
            FAIL,
            "No login time is recorded, so how long the stored session has left is unknown. "
            "An unknown session is treated as unusable: this is exactly the state in which "
            "a batch must not start.",
            Remedy("command", login),
            detail=detail,
            exit_code=ExitCode.BLOCKED,
        )
    # The timeout is named in every message, not just in the detail. How long a
    # session lasts here was invisible for months, and it turned out to be a
    # value this deployment had set itself — an operator who never sees the
    # number has no reason to question it.
    described = (
        f"{sessions.describe(state)} Timeout: {timeout_minutes} minutes "
        f"({timeout_minutes / 1440:.0f} days), from {source}."
    )
    if declared_differs is not None:
        # Said here rather than left to `--json`: the gap between what the
        # endpoint enforces and what the configuration asks for is the whole
        # reason this check reads the live value, and an operator who sees only
        # one number has no reason to wonder which one it is.
        described += (
            f" The configuration declares {declared_differs} minutes "
            f"({declared_differs / 1440:.0f} days), which is not in force — the live "
            "value above is what the session is measured against."
        )

    if state.status == sessions.EXPIRED:
        return CheckResult(
            CHECK_SESSION,
            "Globus session",
            FAIL,
            described,
            Remedy("command", login),
            detail=detail,
            exit_code=ExitCode.BLOCKED,
        )
    if state.status == sessions.WARN:
        return CheckResult(
            CHECK_SESSION,
            "Globus session",
            WARN,
            described,
            Remedy("command", login),
            detail=detail,
        )
    return CheckResult(CHECK_SESSION, "Globus session", PASS, described, detail=detail)


def check_destination_listing(diag: Diagnostics) -> CheckResult:
    """Check 9: a real listing through the destination collection.

    The one check that exercises the whole path at once: the session, the
    gateway's S3 credential, and the collection's base path. A batch that passes
    this and nothing else will still start.

    The path is read rather than hard-coded, because `/` was wrong for production
    and right for staging. `/` is the *collection* root, and production's collection
    is rooted at the bucket — so `/` reaches S3 as an empty prefix, which the writer
    role denies once it is confined to a key prefix. Staging's collection is rooted
    inside its own prefix, so its root is already the permitted one and it passed for
    a reason unrelated to being correct. Terraform publishes the right path per
    environment from the same expression that scopes the IAM condition; deriving it
    here would mean re-inferring how each collection is rooted, which is the mistake
    being fixed.

    The `/` fallback keeps a deployment that has not applied that parameter yet
    behaving exactly as before rather than erroring on a missing value — and check 2
    is what reports the parameter as absent, so the gap is visible in one place
    instead of being guessed at twice.
    """
    collection_id = diag.parameter(diag.env.collection_id_param)
    assert collection_id  # guarded by the dependency rule in `run`
    path = diag.parameter(diag.env.destination_listing_path_param) or "/"
    return _listing_check(
        diag,
        CHECK_DESTINATION,
        "Destination collection listing",
        collection_id,
        path,
    )


def check_source_listing(diag: Diagnostics) -> CheckResult:
    """Check 10: a listing of the source collection, at the configured base path."""
    collection_id = diag.parameter(diag.env.source_collection_id_param)
    base_path = diag.parameter(diag.env.source_base_path_param) or "/"
    assert collection_id  # guarded by the dependency rule in `run`
    return _listing_check(diag, CHECK_SOURCE, "Source collection listing", collection_id, base_path)


def check_kubernetes_secret(diag: Diagnostics) -> CheckResult:
    """Check 11: the Secret the pipeline reads matches the one a login writes.

    These drift because External Secrets syncs on an interval: a login updates
    Secrets Manager immediately, and until the next sync the transfer pods still
    mount the old token. Compared by hash — the token itself is never read into a
    message or a record.
    """
    secret_name = diag.env.k8s_secret
    stored = diag.token()
    read = cluster_module.read_secret_token(diag.runner, name=secret_name)
    if not read.ok:
        return CheckResult(
            CHECK_KUBERNETES_SECRET,
            "Kubernetes credential Secret",
            FAIL,
            f"Could not read the `{secret_name}` Secret in {cluster_module.ARGO_NAMESPACE}.",
            Remedy("human", "check the ExternalSecret and the namespace"),
            detail={"secret": secret_name, "error": read.error},
            exit_code=ExitCode.CHECK_FAILED,
        )

    in_cluster = read.token or ""
    in_aws = str((stored or {}).get("refresh-token") or "").strip()
    detail = {
        "secret": secret_name,
        "cluster_token_fingerprint": _fingerprint(in_cluster),
        "secrets_manager_fingerprint": _fingerprint(in_aws),
    }

    if not in_cluster:
        return CheckResult(
            CHECK_KUBERNETES_SECRET,
            "Kubernetes credential Secret",
            FAIL,
            f"The `{secret_name}` Secret has no refresh-token, so every transfer pod will "
            "fail to authenticate.",
            Remedy("command", "pixi run globus login"),
            detail=detail,
            exit_code=ExitCode.CHECK_FAILED,
        )

    if in_cluster != in_aws:
        interval = cluster_module.eso_refresh_interval()
        return CheckResult(
            CHECK_KUBERNETES_SECRET,
            "Kubernetes credential Secret",
            WARN,
            f"The `{secret_name}` Secret holds a different token than Secrets Manager. "
            f"External Secrets re-syncs on {interval}, so this is expected shortly after a "
            "login and a problem if it persists.",
            Remedy("human", f"wait for the {interval} sync, or force it with `globus login`"),
            detail=detail,
        )

    return CheckResult(
        CHECK_KUBERNETES_SECRET,
        "Kubernetes credential Secret",
        PASS,
        f"`{secret_name}` matches Secrets Manager",
        detail=detail,
    )


def check_s3_gateway_credential(diag: Diagnostics) -> CheckResult:
    """Check 12: the role this gateway's listener signs as, and what it can reach.

    Re-pointed rather than replaced (`retire-globus-s3-access-keys` D7): same id,
    same position, new question. The key Globus holds authenticates nothing once a
    gateway is cut over — the listener discards its signature and re-signs as a
    prefix-scoped role — so the credential worth checking is that role. Three
    things must hold, and each is a FAIL whose remedy is a Terraform apply, never
    a request to a person, because Terraform creates every piece of it:

    * the role exists and its trust policy names the GCS instance's role;
    * the instance's role may call `sts:AssumeRole` on it;
    * it may write under the prefix transfers land in, and NOT outside it.

    The last is the one that matters most. Confinement is what keeps the listener
    out of the security path (D3), and an unassumable role is not merely a broken
    transfer: the listener forwards the request unsigned and S3 answers a plain
    403, indistinguishable from a wrong prefix (measured in 3.1).

    A gateway that declares no listener is not yet cut over and still signs with a
    static key. That is SKIPPED, not FAIL: the old question was withdrawn with the
    key (D4), and the new one has nothing to ask of it.

    Conditions that limit what *this operator* can see — a refused `GetRole`,
    `DescribeInstances` or simulation — are WARN. They say nothing about whether
    transfers work.
    """
    title = _TITLES[CHECK_S3_CREDENTIAL]
    gateway = diag.gateway_declaration()
    assert gateway is not None  # guarded by the dependency rule in `run`

    bucket = str(gateway.get("bucket") or "").strip()
    listener = gateway.get("s3_listener")
    detail: dict[str, Any] = {
        "gateway": diag.env.gateway_name,
        "bucket": bucket or None,
        "s3_listener": listener if isinstance(listener, dict) else None,
    }

    if not isinstance(listener, dict):
        return CheckResult(
            CHECK_S3_CREDENTIAL,
            title,
            SKIPPED,
            f"Not run: the {diag.env.gateway_name} gateway is not cut over to a signing "
            "listener yet, so it still signs with a static access key and there is no role "
            "to check. Its cutover is `retire-globus-s3-access-keys` 8.3.",
            detail=detail,
        )

    role_name = str(listener.get("writer_role") or "").strip()
    role = diag.aws.role(role_name)
    detail["role"] = role.as_dict()

    if role.state == IAM_UNREADABLE:
        return CheckResult(
            CHECK_S3_CREDENTIAL,
            title,
            WARN,
            f"The role `{role_name}` could not be read, so this check could not run. That is "
            "a limit on your own role, not necessarily a problem with the gateway.",
            Remedy("human", "ask for `iam:GetRole` on this account, or check by hand"),
            detail=detail,
        )

    if not role.present or not role.arn:
        return CheckResult(
            CHECK_S3_CREDENTIAL,
            title,
            FAIL,
            f"The {diag.env.gateway_name} gateway's listener signs as `{role_name}`, which "
            "does not exist in this account — so every transfer is forwarded unsigned and "
            "S3 refuses it.",
            Remedy("command", "terraform apply"),
            detail=detail,
            exit_code=ExitCode.CHECK_FAILED,
        )

    instance = (
        diag.aws.instance_role(diag.instance_id)
        if diag.instance_id
        else InstanceRole(error="no GCS instance id is recorded in SSM")
    )
    detail["instance_role"] = instance.arn
    if instance.error:
        detail["instance_role_error"] = instance.error

    assume = None
    if instance.arn:
        if not role.trusts(instance.arn):
            return CheckResult(
                CHECK_S3_CREDENTIAL,
                title,
                FAIL,
                f"`{role_name}` does not trust the GCS instance's role ({instance.arn}), so "
                "the listener cannot assume it and every transfer is forwarded unsigned. It "
                f"trusts {_principal_list(role.trusted_principals)}.",
                Remedy("command", "terraform apply"),
                detail=detail,
                exit_code=ExitCode.CHECK_FAILED,
            )
        assume = diag.aws.can_assume(instance.arn, role.arn)
        detail["assume"] = assume.as_dict()
        if assume.evaluated and not assume.allowed:
            return CheckResult(
                CHECK_S3_CREDENTIAL,
                title,
                FAIL,
                f"The GCS instance's role ({instance.arn}) is not allowed `sts:AssumeRole` "
                f"on `{role_name}`, so the listener cannot sign as it.",
                Remedy("command", "terraform apply"),
                detail=detail,
                exit_code=ExitCode.CHECK_FAILED,
            )

    prefix = _landing_prefix(diag, bucket)
    detail["prefix"] = prefix or None
    within = diag.aws.can_write_prefix(role.arn, bucket, prefix)
    outside = diag.aws.can_write_outside_prefix(role.arn, bucket)
    detail["permissions"] = within.as_dict()
    detail["confinement"] = outside.as_dict()

    if within.evaluated and not within.allowed:
        return CheckResult(
            CHECK_S3_CREDENTIAL,
            title,
            FAIL,
            f"`{role_name}` is not allowed to {', '.join(within.denied_actions)} under "
            f"{within.resource}, so transfers into this collection will fail.",
            Remedy("command", "terraform apply"),
            detail=detail,
            exit_code=ExitCode.CHECK_FAILED,
        )

    if outside.evaluated and outside.allowed_actions:
        return CheckResult(
            CHECK_S3_CREDENTIAL,
            title,
            FAIL,
            f"`{role_name}` is not confined to its prefix: it may also "
            f"{', '.join(outside.allowed_actions)} at {outside.resource}. Confinement is what "
            "makes the listener safe to run unauthenticated on loopback.",
            Remedy("command", "terraform apply"),
            detail=detail,
            exit_code=ExitCode.CHECK_FAILED,
        )

    # Warnings after every failure branch: a gap in what this operator could see
    # must never be reported in front of something that is actually broken.
    if not instance.arn:
        return CheckResult(
            CHECK_S3_CREDENTIAL,
            title,
            WARN,
            f"Could not tell which role the GCS instance runs as ({instance.error}), so "
            f"whether it can assume `{role_name}` is unverified.",
            Remedy(
                "human",
                "ask for `ec2:DescribeInstances` and `iam:GetInstanceProfile`, or check by hand",
            ),
            detail=detail,
        )

    unverified = [p for p in (assume, within, outside) if p is not None and not p.evaluated]
    if unverified:
        return CheckResult(
            CHECK_S3_CREDENTIAL,
            title,
            WARN,
            f"Could not simulate {len(unverified)} of the questions this check asks about "
            f"`{role_name}` (first refused on {unverified[0].resource}), so its reach is "
            "unverified.",
            Remedy("human", "ask for `iam:SimulatePrincipalPolicy`, or check by hand"),
            detail=detail,
        )

    return CheckResult(
        CHECK_S3_CREDENTIAL,
        title,
        PASS,
        f"`{role_name}` is assumable by the GCS instance, may write under {within.resource}, "
        "and nothing outside it",
        detail=detail,
    )


def check_stale_nodes(diag: Diagnostics) -> CheckResult:
    """Check 13: node records the endpoint still holds for a host that is gone.

    `aws_instance.ami` is ForceNew, so pinning a new AMI replaces the Globus host.
    Everything else survives that — the deployment key is in SSM, the boot unit
    re-registers, the Elastic IP re-associates, the UUIDs do not change — but the
    replaced host's node record stays on the endpoint, still naming an address that
    no longer exists.

    Nothing off-host can see those records: `globus-connect-server node list` is
    answered by the GCS Manager running on the node, and the node is stopped
    between batches. So this check does not observe anything itself. It reads the
    report the host wrote at registration time (`reconcile.node_report`, stored by
    `cloudpipe-gcs-boot`) and applies the rule to it here, where fixing the rule is
    a code change rather than an AMI rebuild.

    The report's `instance_id` is what makes that safe. A report written by a
    replaced host is `skipped`, not evidence: the dangerous failure for this check
    is not missing a leftover record but naming the LIVE node as the one to delete,
    which is exactly what an old snapshot read as current would do.
    Split in two because the two questions are different: `_believable_node_report`
    decides whether the report is evidence at all, and this function applies the
    rule to a report that is.
    """
    detail: dict[str, Any] = {}
    report, refusal = _believable_node_report(diag, detail)
    if refusal is not None:
        return refusal
    assert report is not None

    nodes: list[dict[str, Any]] = report["nodes"]
    address = detail["instance_ip"]
    generated = detail["generated_at"]
    current = detail["instance_id"]

    if not nodes:
        return _nodes_result(
            WARN,
            f"The endpoint reported no node records at {generated}, moments after instance "
            f"{current} registered one at {address}. Nothing is leftover, but a node the "
            "endpoint does not know about cannot serve a transfer either.",
            Remedy("command", f"pixi run globus doctor --env {diag.env.name} --start-instance"),
            detail=detail,
        )

    # An absent address list is deliberately NOT read as stale: `ip_addresses: null`
    # means this GCS release did not report the field, and treating "not told" as
    # "not this host" would name the live node for deletion.
    unreadable = [n for n in nodes if not isinstance(n.get("ip_addresses"), list)]
    stale = [
        n
        for n in nodes
        if isinstance(n.get("ip_addresses"), list)
        and address not in [str(a) for a in n["ip_addresses"]]
    ]
    detail["stale"] = [str(n.get("id") or "") for n in stale]

    if unreadable:
        return _nodes_result(
            WARN,
            f"{len(unreadable)} of {len(nodes)} node record(s) reported no address at "
            f"{generated}, so they cannot be told apart from this host's own record. "
            "Nothing is claimed about the rest either.",
            Remedy(
                "human",
                "run `sudo globus-connect-server node list` on the instance and compare the "
                "addresses by hand",
            ),
            detail=detail,
        )

    if stale:
        ids = ", ".join(detail["stale"]) or "unnamed record(s)"
        active = [n for n in stale if str(n.get("status") or "").lower() == "active"]
        remedy = Remedy(
            "human",
            f"connect to the instance (`aws ssm start-session --target {current}`) and run "
            "`sudo globus-connect-server node delete <id>` for each id listed; that removes "
            "the record only and does not touch the running node",
        )
        if active:
            return _nodes_result(
                FAIL,
                f"The endpoint holds {len(stale)} node record(s) not at {address}, and "
                f"{len(active)} of those is still `active`: {ids}. An active record for a "
                "host that no longer exists advertises an address nothing answers on, so "
                f"transfers can be routed nowhere. Reported by {current} at {generated}.",
                remedy,
                detail=detail,
                exit_code=ExitCode.CHECK_FAILED,
            )
        return _nodes_result(
            WARN,
            f"The endpoint holds {len(stale)} inactive node record(s) not at {address}: "
            f"{ids}. Harmless to transfers. Note this is NOT the expected residue of an "
            "AMI bump — a replaced host re-registers under the same deployment key and so "
            "reuses its record — so a leftover here came from somewhere else: a node built "
            f"by hand, or one registered with a different key. Reported by {current} at "
            f"{generated}.",
            remedy,
            detail=detail,
        )

    if detail["truncated"]:
        return _nodes_result(
            WARN,
            f"The first {len(nodes)} node record(s) are all at {address}, but the report was "
            "truncated to fit one SSM parameter, so there may be more that were not "
            f"examined. Reported by {current} at {generated}.",
            Remedy(
                "human",
                "run `sudo globus-connect-server node list` on the instance to see them all",
            ),
            detail=detail,
        )

    return _nodes_result(
        PASS,
        f"{len(nodes)} node record(s), all at {address} (reported by {current} at {generated})",
        detail=detail,
    )


def _nodes_result(
    severity: str,
    message: str,
    remedy: Remedy | None = None,
    *,
    detail: dict[str, Any],
    exit_code: ExitCode | None = None,
) -> CheckResult:
    """One `CheckResult` shape, since check 13 has eleven of them."""
    return CheckResult(
        CHECK_STALE_NODES,
        _TITLES[CHECK_STALE_NODES],
        severity,
        message,
        remedy,
        detail=detail,
        exit_code=exit_code,
    )


@dataclass(frozen=True)
class _ReportRefusals:
    """What a host report's believability check should SAY when it refuses.

    The decisions are shared — a parameter with no value, something that is not a
    JSON object, an unknown current instance, a replaced writer, in that order and
    at those severities — because two copies of a freshness rule is how one of them
    ends up more trusting than the other. The sentences are not shared: an operator
    reading "no node report yet" needs a different next step from one reading "no
    plan recorded yet", and a message generic enough to serve both would serve
    neither.
    """

    missing: str
    malformed: str
    unknown_instance: str
    replaced: Callable[[str, str, str], str]
    rewrite: Remedy


def _believable_host_report(
    diag: Diagnostics,
    param: str,
    refusals: _ReportRefusals,
    result: Callable[..., CheckResult],
    detail: dict[str, Any],
) -> tuple[dict[str, Any] | None, CheckResult | None]:
    """A report the GCS host wrote to SSM, if it is evidence about the host running now.

    Returns `(report, None)` when the report can be believed at all, and
    `(None, result)` otherwise — the result being what `doctor` should print
    instead. `detail` is filled in either way, so a refusal still carries what was
    read.

    Both callers (checks 7 and 13) read a JSON document a stopped host left behind,
    and for both the dangerous answer is not a missed finding but a confident wrong
    one drawn from a stale snapshot. So everything here is a refusal to conclude,
    and the severities encode which kind of doubt it is: `skipped` when nobody could
    have answered, `warn` when something unexpected wrote the parameter.
    """
    state = diag.aws.parameter_state(param)
    detail |= {"parameter": param, "state": state.state}

    if state.state != SET:
        return None, result(SKIPPED, refusals.missing, detail=detail)

    try:
        report = json.loads(state.value or "")
    except json.JSONDecodeError as exc:
        report = None
        detail["error"] = str(exc)
    if not isinstance(report, dict):
        return None, result(WARN, refusals.malformed, refusals.rewrite, detail=detail)

    generated = str(report.get("generated_at") or "an unrecorded time")
    written_by = str(report.get("instance_id") or "")
    current = diag.parameter(diag.env.instance_id_param)
    detail |= {"generated_at": generated, "written_by": written_by, "instance_id": current}

    if not current:
        return None, result(SKIPPED, refusals.unknown_instance, detail=detail)

    if written_by != current:
        return None, result(
            SKIPPED, refusals.replaced(written_by, generated, current), detail=detail
        )

    return report, None


def _node_refusals(param: str) -> _ReportRefusals:
    return _ReportRefusals(
        missing=(
            f"Not run: no node report has been written to {param}. The instance writes "
            "one each time it registers the node, so this check starts reporting after "
            "the instance next starts."
        ),
        malformed=(
            f"The node report at {param} is not a JSON object, so the endpoint's node "
            "records could not be examined. Something other than the boot unit has "
            "written to this parameter."
        ),
        unknown_instance=(
            "Not run: this deployment's instance id is not recorded in SSM (see the "
            "parameter check), so there is no way to tell whether this report describes "
            "the host that is running now."
        ),
        replaced=lambda written_by, generated, current: (
            f"Not run: the report was written by instance {written_by or 'an unnamed host'} "
            f"at {generated}, and this deployment's instance is now {current} — so it "
            "describes node records as a replaced host saw them. A fresh report appears "
            "the next time the instance registers."
        ),
        rewrite=Remedy("human", f"delete {param}; the instance rewrites it when it next registers"),
    )


def _believable_node_report(
    diag: Diagnostics, detail: dict[str, Any]
) -> tuple[dict[str, Any] | None, CheckResult | None]:
    """The node report if it is evidence about the current host, or why it is not.

    Returns `(report, None)` when the report can be believed and lists records in
    the expected shape, and `(None, result)` otherwise — the result being what
    `doctor` should print instead. `detail` is filled in either way, so a refusal
    still carries what was read.

    Everything here is a refusal to conclude, and that asymmetry is the point: the
    worst outcome for this check is not failing to spot a leftover record but
    pointing at the live one, which is what believing a stale or malformed report
    would do. The first four of those refusals are `_believable_host_report`,
    shared with check 7; what remains below is what only node records need.
    """
    param = diag.env.node_report_param
    refusals = _node_refusals(param)
    rewrite = refusals.rewrite
    report, refusal = _believable_host_report(diag, param, refusals, _nodes_result, detail)
    if refusal is not None:
        return None, refusal
    assert report is not None

    generated = detail["generated_at"]
    current = detail["instance_id"]

    nodes = report.get("nodes")
    if nodes is None:
        # `nodes: null` is the host saying nobody could ask, never "there are none".
        return None, _nodes_result(
            SKIPPED,
            f"Not run: instance {current} could not read the endpoint's node records at "
            f"{generated}: {report.get('unavailable') or 'no reason was recorded'}.",
            detail=detail,
        )

    if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
        return None, _nodes_result(
            WARN,
            f"The node report written at {generated} does not list node records in the "
            "expected shape, so nothing can be concluded about them.",
            rewrite,
            detail=detail,
        )

    address = str(report.get("instance_ip") or "")
    detail |= {"instance_ip": address, "nodes": nodes, "truncated": bool(report.get("truncated"))}

    if not address:
        return None, _nodes_result(
            WARN,
            f"The node report written at {generated} does not say which address the "
            "instance registered with, so its own record cannot be told from a leftover.",
            rewrite,
            detail=detail,
        )

    return report | {"nodes": nodes}, None


def check_s3_listener(diag: Diagnostics) -> CheckResult:
    """Check 14: is this gateway's signing listener actually serving?

    The other half of the 403 check 12 asks about. Once a gateway is cut over,
    every transfer into it is signed by an Envoy process on the GCS host: Globus
    signs with a dummy key, the listener discards that and re-signs as a
    prefix-scoped role (D3). A listener that is not running does not announce
    itself — the request goes nowhere, or goes on unsigned, and S3 answers the same
    plain 403 an unassumable role produces. So without this line every listener
    failure is read as a credential failure, and the credential is the part that is
    fine.

    Nothing off-host can observe it. The listener binds `127.0.0.1` and nothing
    else, which is precisely what makes an unauthenticated signing proxy safe to
    run (D4), and `doctor` runs on a workstation. So this check observes nothing
    itself: it reads the report the host wrote when the listeners were last
    installed (`reconcile.listener_report`, published by the SSM document in
    `s3_listeners.tf`) and applies the rule here, where fixing the rule is a code
    change rather than an AMI rebuild.

    WHAT A PASS DOES AND DOES NOT SAY. The socket accepted a connection at the
    moment the installer ran. The host is stopped between batches and this report
    does not move while it is, so a listener that died afterwards still reads as
    serving — which is why every message carries the timestamp it was observed at,
    and why `restarts` is a WARN. A unit systemd has already restarted is the one
    piece of evidence here about time rather than an instant.

    A gateway that declares no listener is SKIPPED, matching check 12: it still
    signs with a static key, so whether an Envoy is listening for it says nothing
    about whether transfers work.
    """
    detail: dict[str, Any] = {"gateway": diag.env.gateway_name}

    gateway = diag.gateway_declaration()
    if gateway is None:
        # Not FAIL, even when the miss is the defect `declared_gateway_names`
        # describes: check 12 reports that loudly two lines above, and the whole
        # point of an ordered checklist is to name a cause once.
        detail["declared_gateways"] = diag.declared_gateway_names()
        return _listener_result(
            SKIPPED,
            f"Not run: no storage gateway named {diag.env.gateway_name} is declared, so "
            "there is no listener to look for. See the gateway credential check above for "
            "why that is, and whether it matters.",
            detail=detail,
        )

    listener = gateway.get("s3_listener")
    detail["s3_listener"] = listener if isinstance(listener, dict) else None
    if not isinstance(listener, dict):
        return _listener_result(
            SKIPPED,
            f"Not run: the {diag.env.gateway_name} gateway is not cut over to a signing "
            "listener yet, so it still signs with a static access key and no listener "
            "serves it. Its cutover is `retire-globus-s3-access-keys` 8.3.",
            detail=detail,
        )

    record, refusal = _believable_listener_report(diag, detail)
    if refusal is not None:
        return refusal
    assert record is not None

    generated = detail["generated_at"]
    current = detail["instance_id"]
    port = record.get("port")
    serving = bool(record.get("serving"))
    restarts = record.get("restarts")
    error = str(record.get("error") or "")

    # The port Globus was registered against, from the declaration rather than the
    # report: a listener bound to a port nothing was registered against is serving
    # transfers that never arrive, and only the declaration knows which is which.
    declared_port = _declared_listener_port(listener)
    detail["declared_port"] = declared_port

    if not serving:
        remedy = Remedy("command", "terraform apply")
        if not record.get("config_present"):
            return _listener_result(
                FAIL,
                f"Nothing is listening on 127.0.0.1:{port} for {diag.env.gateway_name}, and "
                "the host holds no rendered config for it, so the unit's "
                "`ConditionPathExists` stops it before it starts. Every transfer into this "
                f"collection is answered with a 403 that looks like a broken credential. "
                f"Observed by {current} at {generated}.",
                remedy,
                detail=detail,
                exit_code=ExitCode.CHECK_FAILED,
            )
        unit_file_state = str(record.get("unit_file_state") or "")
        if unit_file_state and unit_file_state != "enabled":
            return _listener_result(
                FAIL,
                f"Nothing is listening on 127.0.0.1:{port} for {diag.env.gateway_name}: its "
                f"unit is `{unit_file_state}`, so it does not start with the host. Transfers "
                f"into this collection are answered with a 403. Observed by {current} at "
                f"{generated}.",
                remedy,
                detail=detail,
                exit_code=ExitCode.CHECK_FAILED,
            )
        states = (
            f"{record.get('active_state') or 'an unreported state'}"
            f"/{record.get('sub_state') or 'unreported'}"
        )
        because = f" systemd could not be asked why ({error})." if error else ""
        return _listener_result(
            FAIL,
            f"Nothing is listening on 127.0.0.1:{port} for {diag.env.gateway_name}. Its "
            f"config is in place and its unit is enabled, but the unit is {states}"
            f"{' after ' + str(restarts) + ' restart(s)' if restarts else ''}.{because} "
            f"Transfers into this collection are answered with a 403 that looks like a "
            f"broken credential. Observed by {current} at {generated}.",
            Remedy(
                "human",
                f"connect to the instance (`aws ssm start-session --target {current}`) and "
                f"run `sudo journalctl -u cloudpipe-s3-listener@{diag.env.gateway_name} -n 50` "
                "for Envoy's own error",
            ),
            detail=detail,
            exit_code=ExitCode.CHECK_FAILED,
        )

    # Serving. Everything below is a caveat on a listener that answered, so none of
    # it may be reported in front of a listener that did not.
    if declared_port is not None and port != declared_port:
        return _listener_result(
            WARN,
            f"A listener answered on 127.0.0.1:{port}, but Globus registered the "
            f"{diag.env.gateway_name} gateway against port {declared_port} — so the process "
            "that is running is not the one transfers reach. The likeliest cause is a report "
            f"written before the port changed. Observed by {current} at {generated}.",
            Remedy("command", "terraform apply"),
            detail=detail,
        )

    if restarts:
        return _listener_result(
            WARN,
            f"The {diag.env.gateway_name} listener was serving on 127.0.0.1:{port} at "
            f"{generated}, but systemd had already restarted it {restarts} time(s) — so it "
            "has been failing, and a `Restart=on-failure` loop is what is keeping it up. "
            "Transfers work between restarts and fail during them.",
            Remedy(
                "human",
                f"connect to the instance (`aws ssm start-session --target {current}`) and "
                f"run `sudo journalctl -u cloudpipe-s3-listener@{diag.env.gateway_name} -n 50` "
                "to see what it is dying of",
            ),
            detail=detail,
        )

    if error:
        return _listener_result(
            WARN,
            f"A listener answered on 127.0.0.1:{port} for {diag.env.gateway_name} at "
            f"{generated}, so transfers could be signed — but systemd could not be asked "
            f"about the unit ({error}), so whether it has been restarting is unknown.",
            detail=detail,
        )

    return _listener_result(
        PASS,
        f"serving on 127.0.0.1:{port} with no restarts (observed by {current} at {generated})",
        detail=detail,
    )


def _listener_result(
    severity: str,
    message: str,
    remedy: Remedy | None = None,
    *,
    detail: dict[str, Any],
    exit_code: ExitCode | None = None,
) -> CheckResult:
    """One `CheckResult` shape, since check 14 has a dozen of them."""
    return CheckResult(
        CHECK_S3_LISTENER,
        _TITLES[CHECK_S3_LISTENER],
        severity,
        message,
        remedy,
        detail=detail,
        exit_code=exit_code,
    )


def _declared_listener_port(listener: dict[str, Any]) -> int | None:
    """The port Globus was registered against, from the declared endpoint URL.

    `None` when the endpoint is absent or unparseable, and the caller then makes no
    claim about the port — the schema already requires an `endpoint`
    (`configdoc`), so a missing one is that check's finding and not this one's.
    """
    endpoint = str(listener.get("endpoint") or "").strip()
    if not endpoint:
        return None
    try:
        return urlsplit(endpoint).port
    except ValueError:
        return None


def _listener_refusals(param: str, gateway_name: str) -> _ReportRefusals:
    return _ReportRefusals(
        missing=(
            f"Not run: no listener report has been written to {param}. The instance writes "
            "one each time the listener install document runs, so this check starts "
            "reporting after the next `terraform apply` that touches the listeners, or the "
            "next time the instance is replaced."
        ),
        malformed=(
            f"The listener report at {param} is not a JSON object, so whether the "
            f"{gateway_name} listener is serving could not be examined. Something other "
            "than the listener install document has written to this parameter."
        ),
        unknown_instance=(
            "Not run: this deployment's instance id is not recorded in SSM (see the "
            "parameter check), so there is no way to tell whether this report describes "
            "the host that is running now."
        ),
        replaced=lambda written_by, generated, current: (
            f"Not run: the report was written by instance {written_by or 'an unnamed host'} "
            f"at {generated}, and this deployment's instance is now {current} — so it "
            "describes listeners on a host that no longer exists. A replacement's listeners "
            "are installed by the same apply that replaces it, and a fresh report lands with "
            "them."
        ),
        rewrite=Remedy(
            "human",
            f"delete {param}; the instance rewrites it the next time the listener install "
            "document runs",
        ),
    )


def _believable_listener_report(
    diag: Diagnostics, detail: dict[str, Any]
) -> tuple[dict[str, Any] | None, CheckResult | None]:
    """This gateway's record from the listener report, or why there is no evidence.

    Returns `(record, None)` when the report can be believed and describes THIS
    environment's gateway, and `(None, result)` otherwise. `detail` is filled in
    either way, so a refusal still carries what was read.

    One report covers every declared listener, because the host cannot know which
    environment is asking — so the last step here is picking out one record, and a
    report that does not contain one is its own finding rather than a silent pass.
    The first four refusals are `_believable_host_report`, shared with checks 7 and
    13: two copies of a freshness rule is how one of them ends up more trusting than
    the other.
    """
    param = diag.env.listener_report_param
    refusals = _listener_refusals(param, diag.env.gateway_name)
    report, refusal = _believable_host_report(diag, param, refusals, _listener_result, detail)
    if refusal is not None:
        return None, refusal
    assert report is not None

    generated = detail["generated_at"]
    current = detail["instance_id"]

    listeners = report.get("listeners")
    if listeners is None:
        # `listeners: null` is the host saying nobody looked, never "none are
        # declared". A host that could not examine its own units must not read as a
        # host with no listener to run.
        return None, _listener_result(
            SKIPPED,
            f"Not run: instance {current} did not examine its listeners at {generated}: "
            f"{report.get('unavailable') or 'no reason was recorded'}.",
            detail=detail,
        )

    if not isinstance(listeners, list) or not all(isinstance(item, dict) for item in listeners):
        return None, _listener_result(
            WARN,
            f"The listener report written at {generated} does not describe listeners in the "
            "expected shape, so nothing can be concluded about them.",
            refusals.rewrite,
            detail=detail,
        )

    detail["reported_listeners"] = [str(item.get("gateway") or "") for item in listeners]
    detail["truncated"] = bool(report.get("truncated"))

    for item in listeners:
        if str(item.get("gateway") or "") == diag.env.gateway_name:
            detail["listener"] = item
            return item, None

    # Declared a listener in the configuration document, but the installer did not
    # install one. Not a FAIL, because the same shape arises from a report written
    # before the declaration, and the two cannot be told apart from here.
    reported = ", ".join(sorted(n for n in detail["reported_listeners"] if n)) or "none"
    truncated = (
        " The report was truncated to fit one SSM parameter, so it may have been dropped."
        if detail["truncated"]
        else ""
    )
    return None, _listener_result(
        WARN,
        f"The {diag.env.gateway_name} gateway declares a signing listener, but the report "
        f"written at {generated} describes no listener for it (it describes: {reported})."
        f"{truncated} Either the install document does not know about this gateway — in "
        "which case transfers into it are answered with a 403 — or the report predates the "
        "declaration.",
        Remedy("command", "terraform apply"),
        detail=detail,
    )


def _principal_list(principals: tuple[str, ...]) -> str:
    """A trust policy's principals as prose, including "nothing"."""
    return ", ".join(principals) if principals else "no AWS principal at all"


def _landing_prefix(diag: Diagnostics, bucket: str) -> str:
    """The S3 key prefix transfers into this gateway's collection land under.

    The collection's own prefix is not enough on its own, because the two
    collections are rooted differently: staging at its prefix, production at the
    BUCKET ROOT with transfers going to `/mmps_mproc` inside it. Its writer role is
    confined to that prefix, so asking about the collection root would report a
    healthy role as unable to write. Terraform publishes the collection-relative
    landing path per environment (the same parameter check 9 lists), and the key
    prefix is the collection's prefix plus that path.
    """
    base = _write_prefix(diag.document, diag.env.gateway_name, bucket)
    landing = (diag.parameter(diag.env.destination_listing_path_param) or "/").strip("/")
    return "/".join(part for part in (base, landing) if part)


def _write_prefix(document: Any, gateway_name: str, bucket: str) -> str:
    """The S3 key prefix this gateway's collection writes under.

    A collection's `base_path` is a Globus path that starts with the bucket
    component (`/my-bucket/scratch/x`), while IAM wants the key prefix inside the
    bucket (`scratch/x`). Stripping the bucket is what turns one into the other.
    """
    if not isinstance(document, dict):
        return ""
    for collection in document.get("collections") or []:
        if not isinstance(collection, dict) or collection.get("gateway") != gateway_name:
            continue
        base = str(collection.get("base_path") or "").strip("/")
        if bucket and (base == bucket or base.startswith(f"{bucket}/")):
            base = base[len(bucket) :].strip("/")
        return base
    return ""


# ---------------------------------------------------------------------------
# Orchestration
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
    results.append(check_configuration(diag))
    results.append(check_instance(diag))

    if diag.instance_running:
        results.append(check_gridftp(diag))
    else:
        results.append(
            _skipped(
                CHECK_GRIDFTP,
                f"GridFTP on port {GRIDFTP_PORT}",
                "Not run: the instance is not running. Re-run with --start-instance to "
                "start it and include this check.",
            )
        )

    token = diag.token()
    endpoint_id = diag.parameter(diag.env.endpoint_id_param)
    if not token:
        results.append(_skipped(CHECK_SUBSCRIPTION, "Endpoint subscription", _NO_LOGIN))
    elif not endpoint_id:
        results.append(
            _skipped(
                CHECK_SUBSCRIPTION,
                "Endpoint subscription",
                "Not run: no endpoint id is recorded in SSM.",
            )
        )
    else:
        results.append(check_subscription(diag))

    results.append(check_drift(diag))
    results.append(check_session(diag))

    destination_id = diag.parameter(diag.env.collection_id_param)
    if not token:
        results.append(_skipped(CHECK_DESTINATION, "Destination collection listing", _NO_LOGIN))
    elif not destination_id:
        results.append(
            _skipped(
                CHECK_DESTINATION,
                "Destination collection listing",
                "Not run: no collection id is recorded in SSM.",
            )
        )
    else:
        results.append(check_destination_listing(diag))

    source_id = diag.parameter(diag.env.source_collection_id_param)
    if not token:
        results.append(_skipped(CHECK_SOURCE, "Source collection listing", _NO_LOGIN))
    elif not source_id:
        results.append(
            _skipped(
                CHECK_SOURCE,
                "Source collection listing",
                "Not run: no source collection id is recorded in SSM.",
            )
        )
    else:
        results.append(check_source_listing(diag))

    if diag.cluster is not None and diag.cluster.reachable:
        results.append(check_kubernetes_secret(diag))
    else:
        results.append(
            _skipped(
                CHECK_KUBERNETES_SECRET,
                "Kubernetes credential Secret",
                "Not run: the Kubernetes API was unreachable. Connect Cloudflare WARP "
                "(`warp-cli debug access-reauth`) to include this check.",
            )
        )

    if diag.gateway_declaration() is not None:
        results.append(check_s3_gateway_credential(diag))
    else:
        results.append(_unmatched_gateway_result(diag))

    # No dependency gate: this check reads one parameter and reports every state it
    # can find there, including "nothing has written one yet".
    results.append(check_stale_nodes(diag))

    # Also ungated, and NOT behind `gateway_declaration()` the way check 12 is. It
    # needs the same declaration, but it reports an unmatched gateway as `skipped`
    # itself rather than repeating check 12's FAIL: the ordering exists so a cause is
    # named once, and the gate here would name it twice.
    results.append(check_s3_listener(diag))

    return results


def exit_code_for(results: list[CheckResult]) -> ExitCode:
    """The first failure decides.

    Checks run in dependency order, so the first FAIL is the root cause and its
    classification is the one a caller should act on. Later failures are usually
    the same problem seen from further downstream.
    """
    for result in results:
        if result.severity == FAIL:
            return result.exit_code or ExitCode.CHECK_FAILED
    return ExitCode.OK


def _skip_rest(from_id: str, reason: str) -> list[CheckResult]:
    start = CHECK_ORDER.index(from_id)
    return [_skipped(check_id, _TITLES[check_id], reason) for check_id in CHECK_ORDER[start:]]


def _skipped(check_id: str, title: str, reason: str) -> CheckResult:
    return CheckResult(check_id, title, SKIPPED, reason)


def _unmatched_gateway_result(diag: Diagnostics) -> CheckResult:
    """Check 12 when no declaration matched — and why that is two different answers.

    A document declaring no gateways is genuinely nothing to check. A document
    declaring gateways under *other* names is a defect: the name is derived from
    the answers document, everything downstream matches by display name, and a
    wrong one means the gateway actually serving transfers is the one neither
    this check nor the reconcile is looking at. Reporting that as "not run"
    hides it behind a line that reads like good news.
    """
    title = _TITLES[CHECK_S3_CREDENTIAL]
    declared = diag.declared_gateway_names()
    detail: dict[str, Any] = {
        "looked_for": diag.env.gateway_name,
        "declared_gateways": declared,
    }

    if not declared:
        return CheckResult(
            CHECK_S3_CREDENTIAL,
            title,
            SKIPPED,
            "Not run: the configuration document declares no storage gateways, so there is "
            "no gateway credential to look for.",
            detail=detail,
        )

    names = ", ".join(sorted(declared))
    return CheckResult(
        CHECK_S3_CREDENTIAL,
        title,
        FAIL,
        f"This deployment looks for a storage gateway named `{diag.env.gateway_name}`, but "
        f"the configuration document declares {names}. Everything here matches gateways by "
        "display name, so the mismatch means the gateway serving transfers is unchecked — "
        "and its credential, which this check exists to verify, is never looked at.",
        Remedy(
            "human",
            "set `gateway_name:` in the answers document to the gateway's real display name "
            f"(one of: {names}), then re-run `pixi run globus doctor`",
        ),
        detail=detail,
        exit_code=ExitCode.INVALID,
    )


def _listing_check(
    diag: Diagnostics, check_id: str, title: str, collection_id: str, path: str
) -> CheckResult:
    """One listing, retried only while a just-started GridFTP could still be restarting.

    Why a retry belongs here at all: check 5's readiness test is a TCP connect, and
    a listening socket accepts connections throughout a restart. A GCS instance
    restarts GridFTP several times in the seconds after boot while the node
    re-registers, so check 5 passes and this check — the very next one — gets a
    control channel that ends mid-login. Measured on 2026-09-24: five restarts in
    fifteen seconds, a FAIL here, and a PASS from the identical command once the
    instance had settled, with nothing changed in between.

    That false FAIL is worth code rather than a docs note because it is
    indistinguishable from the genuine one a production cutover produces, and the
    remedy text for the genuine one sends an operator after a credential.

    Two things keep the wait from hiding anything. The window is open only when
    `--start-instance` asked for the boot, matching `check_gridftp`'s idiom — with
    no boot the deadline is now, so exactly one attempt happens and settled
    behaviour is unchanged. And only `GRIDFTP_UNAVAILABLE` is waited on: every
    other failure is answered by changing something, so retrying it would delay
    the report without improving it.
    """
    deadline = diag.clock() + (GRIDFTP_WAIT_TIMEOUT if diag.start_instance else 0)
    announced = False
    while True:
        try:
            globus_client.collection_reachable(diag.transfer(), collection_id, path)
        except CliError as err:
            failure = err
        except Exception as exc:
            failure = _translate(exc, diag)
        else:
            return CheckResult(
                check_id,
                title,
                PASS,
                f"listed {path} on {collection_id}",
                detail={"collection_id": collection_id, "path": path},
            )

        if not _worth_waiting_for(failure) or diag.clock() >= deadline:
            return _from_cli_error(check_id, title, failure)
        if not announced:
            # Once, not per attempt: the point is to explain a silence, and a line
            # every ten seconds would instead bury the checklist that follows.
            announced = True
            diag.say(
                f"{title}: the collection's GridFTP server closed the connection, which is "
                "expected while a just-started instance re-registers. Retrying for up to "
                f"{GRIDFTP_WAIT_TIMEOUT / 60:.0f} minutes."
            )
        diag.pause(POLL_INTERVAL)


def _worth_waiting_for(err: CliError) -> bool:
    from . import errors

    return err.code == errors.GRIDFTP_UNAVAILABLE


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


def _translate(exc: Exception, diag: Diagnostics) -> CliError:
    from . import errors

    return errors.globus_error(exc, env=diag.env.name)


def _wait_for_running(diag: Diagnostics, instance_id: str) -> str:
    deadline = diag.clock() + INSTANCE_START_TIMEOUT
    state = diag.aws.instance_state(instance_id)
    while state != "running" and diag.clock() < deadline:
        diag.pause(POLL_INTERVAL)
        state = diag.aws.instance_state(instance_id)
    return state


def _get(obj: Any, key: str) -> Any:
    """Globus SDK responses behave like mappings but are not dicts."""
    try:
        return obj[key]
    except (KeyError, TypeError):
        return getattr(obj, key, None)


def _fingerprint(value: str) -> str | None:
    """A short hash, so two tokens can be compared in a report without printing either."""
    if not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:12]
