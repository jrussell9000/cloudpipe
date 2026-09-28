"""Reaching the cluster, and behaving sensibly when it cannot be reached.

The EKS API and the web UIs are private and reached over Cloudflare WARP. Globus
setup itself runs through AWS APIs, so WARP gates only the parts that touch
Kubernetes: syncing the token Secret after a login, checking for running
workflows, and the Kubernetes check in `doctor`.

Those parts therefore degrade rather than fail the command outright — but they
never report a Kubernetes-derived fact as healthy when they could not look.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import errors
from .exits import CliError, ExitCode, Remedy
from .shell import Runner, subprocess_runner

DEFAULT_ESO_MANIFEST = "argo/workflows/cloudpipe_minproc/globus-credentials-external-secret.yaml"
ARGO_NAMESPACE = "argo-workflows"
REFRESH_TOKEN_KEY = "refresh-token"

# kubectl's own wording for an object that is not there: `Error from server
# (NotFound): externalsecrets.external-secrets.io "x" not found`. Matched on the
# message because `kubectl annotate` exits 1 for every failure alike.
_MISSING_OBJECT = re.compile(r"\bNotFound\b|\bnot found\b", re.IGNORECASE)


@dataclass(frozen=True)
class ClusterAccess:
    reachable: bool
    error: CliError | None = None

    def require(self) -> None:
        """Raise the translated error. For a step that cannot proceed blind."""
        if not self.reachable and self.error is not None:
            raise self.error


def probe(runner: Runner = subprocess_runner, *, timeout: float = 10.0) -> ClusterAccess:
    """One cheap reachability check, shared by everything that needs the cluster."""
    result = runner(
        ["kubectl", "version", "-o", "json", "--request-timeout=8s"],
        timeout=timeout,
    )
    if not result.found:
        return ClusterAccess(
            reachable=False,
            error=CliError(
                "cluster.kubectl_missing",
                "The `kubectl` command is not on PATH, so nothing about the cluster can be checked.",
                exit_code=ExitCode.BLOCKED,
                remedy=Remedy("human", "install kubectl and run `aws eks update-kubeconfig`"),
                gate="cluster_access",
            ),
        )
    if result.ok and _has_server_version(result.stdout):
        return ClusterAccess(reachable=True)
    return ClusterAccess(reachable=False, error=errors.cluster_error(result.output))


def eso_refresh_interval(manifest: Path | None = None) -> str:
    """How long a login can take to reach the pipeline without cluster access.

    Read from the ExternalSecret rather than hard-coded, so `login`'s message
    cannot drift from what the cluster will actually do.
    """
    path = manifest or _repo_root() / DEFAULT_ESO_MANIFEST
    try:
        spec = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return "its refresh interval"
    interval = (spec.get("spec") or {}).get("refreshInterval")
    return str(interval) if interval else "its refresh interval"


@dataclass(frozen=True)
class SecretRead:
    """What the cluster holds for the token, or why it could not be read."""

    token: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def read_secret_token(
    runner: Runner = subprocess_runner,
    *,
    name: str,
    namespace: str = ARGO_NAMESPACE,
) -> SecretRead:
    """The `refresh-token` value inside a Kubernetes Secret.

    Shared by `doctor`'s Secret check and `login`'s wait: both need the same
    answer, and two readers of one Secret would eventually disagree about what
    "missing" means.
    """
    result = runner(
        ["kubectl", "get", "secret", name, "-n", namespace, "-o", "json", "--request-timeout=15s"]
    )
    if not result.ok:
        return SecretRead(error=result.output[:300] or f"kubectl exited {result.returncode}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return SecretRead(error="kubectl returned output that is not JSON")
    encoded = (payload.get("data") or {}).get(REFRESH_TOKEN_KEY)
    if not encoded:
        return SecretRead(token=None)
    try:
        return SecretRead(token=base64.b64decode(encoded).decode().strip())
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return SecretRead(error=f"the Secret's {REFRESH_TOKEN_KEY} is not readable base64 text")


def force_external_secret_sync(
    runner: Runner = subprocess_runner,
    *,
    name: str,
    namespace: str = ARGO_NAMESPACE,
    now: float | None = None,
) -> CliError | None:
    """Ask External Secrets to re-read Secrets Manager now, instead of on its interval.

    An annotation, never `kubectl delete secret`. Deleting the Secret is what the
    old procedure did, and it leaves every pod that mounts it without a
    credential for as long as the re-create takes — including pods mid-transfer.
    The annotation changes the ExternalSecret's spec hash, which is what makes
    the operator reconcile it.
    """
    stamp = int(now if now is not None else time.time())
    result = runner(
        [
            "kubectl",
            "annotate",
            "externalsecret",
            name,
            "-n",
            namespace,
            f"force-sync={stamp}",
            "--overwrite",
            "--request-timeout=15s",
        ]
    )
    if result.ok:
        return None
    # "Could not trigger a re-sync" plus "the cluster will pick it up on its own
    # schedule" is true of a transient failure and FALSE when there is no
    # ExternalSecret at all: nothing is watching Secrets Manager, so there is no
    # schedule to wait for. Staging is in exactly that state — only production has
    # a manifest (DEFAULT_ESO_MANIFEST) — so the environment that gets this message
    # most often is the one it misleads. Separated rather than softened: a reader
    # who has to act now should not be told to wait.
    if _MISSING_OBJECT.search(result.output or ""):
        return CliError(
            "cluster.no_external_secret",
            f"There is no `{name}` ExternalSecret in {namespace}, so nothing in the cluster "
            "is watching Secrets Manager for this credential and the new token will NOT be "
            "picked up on any schedule. The token itself is stored safely, but the "
            f"Kubernetes Secret `{name}` has to be created or refreshed by hand until this "
            "environment has an ExternalSecret of its own.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy(
                "human",
                f"Create or refresh `{name}` in {namespace} by hand from the token in "
                "Secrets Manager, or give this environment its own ExternalSecret, "
                f"modelled on {DEFAULT_ESO_MANIFEST}.",
            ),
            raw=result.output[:300],
        )
    return CliError(
        "cluster.force_sync_failed",
        f"Could not trigger a re-sync of the `{name}` credential in the cluster. The new "
        "token is safely stored in Secrets Manager; the cluster will pick it up on its "
        "own schedule.",
        exit_code=ExitCode.CHECK_FAILED,
        remedy=Remedy(
            "command",
            f"kubectl annotate externalsecret {name} -n {namespace} "
            f"force-sync=$(date +%s) --overwrite",
        ),
        raw=result.output[:300],
    )


def wait_for_secret_token(
    runner: Runner = subprocess_runner,
    *,
    name: str,
    expected: str,
    namespace: str = ARGO_NAMESPACE,
    timeout: float = 120.0,
    interval: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll until the cluster Secret carries `expected`, or give up.

    Giving up is not an error: the value is already in Secrets Manager and
    External Secrets will converge on its own interval. The caller reports the
    wait as unfinished rather than the login as failed — telling someone their
    login did not work, when it did, sends them round the loop again.
    """
    deadline = clock() + timeout
    while True:
        read = read_secret_token(runner, name=name, namespace=namespace)
        if read.ok and read.token == expected:
            return True
        if clock() >= deadline:
            return False
        sleep(interval)


def _has_server_version(stdout: str) -> bool:
    """`kubectl version` exits 0 with only client info when the API is unreachable."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return bool(re.search(r"Server Version", stdout))
    return bool(payload.get("serverVersion"))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]
