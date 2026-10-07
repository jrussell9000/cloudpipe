"""Reach a web UI in port-forward mode: one fixed local port per UI.

A deployment with no domain publishes no UI (design D2 of
openspec/changes/optional-domain-and-cognito-auth). Each one is reached with
`kubectl port-forward` over WARP instead, and on a FIXED local port, because the
identity provider matches redirect URIs exactly: a port chosen per session
would be a URI nobody registered. The ports here are the ones
`local.ui_base_urls` in terraform/modules/stack/locals.tf registers, and
tests/test_setup_wizard_ui.py holds the two equal.

Every decision is here and every side effect is injected, so the command module
stays thin and the whole flow is testable without a cluster. Nothing here calls
AWS or changes anything: a port-forward is a read of a Service, carried inside
the API server's TLS connection.
"""

from __future__ import annotations

import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import IO, Protocol

from .exits import CliError, ExitCode, Remedy


@dataclass(frozen=True)
class Ui:
    name: str
    namespace: str
    service: str
    service_port: int
    local_port: int
    title: str

    @property
    def url(self) -> str:
        return f"http://localhost:{self.local_port}"


#: Keyed by the name the deployer types. Prefect goes through oauth2-proxy, not
#: prefect-server: forwarding to the server itself would skip sign-in.
UIS: dict[str, Ui] = {
    ui.name: ui
    for ui in (
        Ui("argocd", "argocd", "argocd-server", 80, 8080, "ArgoCD"),
        Ui("argo", "argo-workflows", "argo-workflows-server", 2746, 2746, "Argo Workflows"),
        Ui("prefect", "prefect", "prefect-oauth2-proxy", 4180, 4200, "Prefect"),
        Ui("grafana", "grafana", "grafana", 80, 3000, "Grafana"),
        Ui("kubecost", "kubecost", "kubecost-frontend", 9090, 9090, "Kubecost"),
    )
}

#: How long to wait for each step before saying so, rather than hanging.
PROBE_TIMEOUT_S = 10
FORWARD_TIMEOUT_S = 20

_REMOTE_ACCESS = (
    "Connect Cloudflare WARP (`warp-cli status` should say Connected; if it does and this "
    "still fails, `warp-cli debug access-reauth`), and check that kubectl points at this "
    "cluster (`kubectl config current-context`)."
)


class Process(Protocol):
    stdout: IO[str] | None
    stderr: IO[str] | None

    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...


Run = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]
Popen = Callable[[Sequence[str]], Process]


def _run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def _popen(argv: Sequence[str]) -> Process:
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def lookup(name: str) -> Ui:
    try:
        return UIS[name]
    except KeyError:
        raise CliError(
            "ui.unknown",
            f"no UI named {name!r}",
            exit_code=ExitCode.INVALID,
            remedy=Remedy("human", f"Use one of: {', '.join(UIS)}."),
            detail={"known": list(UIS)},
        ) from None


def require_kubectl(which: Callable[[str], str | None] = shutil.which) -> str:
    path = which("kubectl")
    if path is None:
        raise CliError(
            "ui.no_kubectl",
            "kubectl is not on PATH",
            exit_code=ExitCode.BLOCKED,
            remedy=Remedy("human", "Install kubectl, then run `aws eks update-kubeconfig`."),
        )
    return path


def probe(kubectl: str, run: Run = _run) -> None:
    """Fail fast, and name WARP, when the API server cannot be reached.

    `/readyz` is readable by every authenticated user, so a failure here is
    reachability or credentials, not permissions on any one UI. Bounded twice —
    kubectl's own request timeout and the subprocess's — because a WARP session
    that has lapsed holds the TCP connection open rather than refusing it.
    """
    argv = [kubectl, "get", "--raw", "/readyz", f"--request-timeout={PROBE_TIMEOUT_S}s"]
    try:
        result = run(argv, PROBE_TIMEOUT_S + 5)
    except subprocess.TimeoutExpired:
        raise _unreachable(f"no answer within {PROBE_TIMEOUT_S + 5}s") from None
    if result.returncode != 0:
        raise _unreachable((result.stderr or result.stdout).strip())


def _unreachable(raw: str) -> CliError:
    return CliError(
        "ui.cluster_unreachable",
        "the cluster's API server cannot be reached; the remote-access (WARP) session is the "
        "likely cause",
        exit_code=ExitCode.BLOCKED,
        remedy=Remedy("human", _REMOTE_ACCESS),
        raw=raw or None,
    )


def require_port(ui: Ui, port_free: Callable[[int], bool] = _port_free) -> None:
    if not port_free(ui.local_port):
        raise CliError(
            "ui.port_in_use",
            f"localhost:{ui.local_port} is already in use, and {ui.title} can only be reached on "
            "that port: its sign-in redirect is registered there",
            exit_code=ExitCode.BLOCKED,
            remedy=Remedy(
                "human",
                f"If another `cloudpipe ui {ui.name}` is running, use it at {ui.url}. Otherwise "
                f"stop whatever is listening on {ui.local_port}.",
            ),
            detail={"port": ui.local_port},
        )


_FORWARDING = re.compile(r"^Forwarding from 127\.0\.0\.1:(\d+)")


def _drain(stream: IO[str], sink: Callable[[str], None]) -> None:
    """Read one of kubectl's streams to the end, handing each line over.

    Runs for the life of the forward, not only until it is listening: kubectl
    writes a "Handling connection" line per request on stdout and connection
    errors on stderr, and a pipe nobody reads fills and then blocks kubectl
    mid-session. The empty string marks the end.
    """
    for line in stream:
        sink(line)
    sink("")


@dataclass
class Forward:
    """A running port-forward, and the tail of what kubectl said on stderr."""

    process: Process
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=20))

    def stderr_text(self) -> str:
        return "".join(self.stderr_tail).strip()


def start(
    ui: Ui,
    kubectl: str,
    popen: Popen = _popen,
    timeout: float = FORWARD_TIMEOUT_S,
) -> Forward:
    """Start the port-forward and return once kubectl says it is listening.

    Bound to 127.0.0.1 only: the forward is a door into the cluster, and nobody
    else on the operator's network should be able to walk through it.
    """
    argv = [
        kubectl,
        "port-forward",
        "--namespace",
        ui.namespace,
        "--address",
        "127.0.0.1",
        f"service/{ui.service}",
        f"{ui.local_port}:{ui.service_port}",
    ]
    process = popen(argv)
    assert process.stdout is not None and process.stderr is not None
    forward = Forward(process)
    lines: queue.Queue[str] = queue.Queue()
    stderr_done = threading.Event()

    def keep_stderr(line: str) -> None:
        if line:
            forward.stderr_tail.append(line)
        else:
            stderr_done.set()

    threading.Thread(target=_drain, args=(process.stdout, lines.put), daemon=True).start()
    threading.Thread(target=_drain, args=(process.stderr, keep_stderr), daemon=True).start()

    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            break
        if _FORWARDING.match(line):
            return forward
        if line == "":  # end of stream: kubectl exited without listening
            break
    if process.poll() is None:
        process.terminate()
    process.wait()
    stderr_done.wait(timeout=2)
    stderr = forward.stderr_text()
    raise CliError(
        "ui.forward_failed",
        f"kubectl could not forward {ui.namespace}/{ui.service}",
        exit_code=ExitCode.CHECK_FAILED,
        remedy=Remedy(
            "command",
            f"kubectl -n {ui.namespace} get service {ui.service}  "
            "(a missing Service means this UI is not deployed, or has a different name here)",
        ),
        raw=stderr or None,
    )


def open_browser(url: str, opener: Callable[[str], bool] = webbrowser.open) -> bool:
    """True if a browser was asked to open. False is not an error: print the URL."""
    try:
        return bool(opener(url))
    except webbrowser.Error:
        return False
