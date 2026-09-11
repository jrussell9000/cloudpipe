"""GPU spot-drought detection for the cloudpipe queue manager (#373).

`gpu-nodepool` is spot-only in a region where G-family capacity is thin. Over
2026-09-03 → 09-10, 97% of GPU NodeClaims died on `insufficient_capacity`, and
twice that week the fleet sat at 0–5 nodes with 80–350 GPU pods Pending for hours
(docs/investigations/2026-09-10-gpu-spot-acquisition-review.md). `cpu-heavy-nodepool`
launched 1,072 nodes in the same window without a single acquisition failure.

FastSurfer segmentation is the bulk of that GPU demand and runs on CPU with
`--device cpu`. This module decides, per submission, whether a new workflow carries
`fastsurfer-device=cpu` (segmentation on cpu-heavy-nodepool) or the default `cuda`.
The signal is the one thing a drought always produces and a healthy pool never
does: GPU-requesting pods that have been Pending for a long time.

Two design rules, both with a failure they prevent:

1. **Hysteresis.** Enter on `enter_pods` starved pods, leave on `exit_pods`. A
   drought entered on 10 pods that then hovers around the threshold must not flap
   a batch between devices on every poll — each flip changes which nodepool the
   next subjects' anatomy lands on.
2. **Hold on error.** A Kubernetes API blip returns the previous verdict rather than
   raising or resetting — the same posture as `cap_reader` in lib.argo. The last
   decision is still a valid decision, and a multi-day leg must not die on a read.

Read-only against the Kubernetes API (pods list in `argo-workflows`), authenticated
with the flow pod's projected service-account token, over `httpx` (a Prefect
dependency). No kubernetes client: the flow-runner image does not carry one and a
single GET does not justify adding it. The RBAC is `prefect-worker-argo-pods` in
terraform/modules/prefect/iam.tf.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

ARGO_NAMESPACE = "argo-workflows"
GPU_RESOURCE = "nvidia.com/gpu"

_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_SA_TOKEN = _SA_DIR / "token"
_SA_CA = _SA_DIR / "ca.crt"

# A GPU pod normally waits 3–5 minutes for Karpenter to provision a node, and a
# cold-start burst can hold 0 GPU nodes for ~10 minutes with nothing wrong. Only
# pods older than this count toward a drought. The 2026-09-10 drought read 82
# pending at 43–134 minutes; a healthy batch reads 0 at this age.
DEFAULT_MIN_PENDING_AGE_S = 15 * 60
# Enter/leave thresholds on the count of pods pending at least that long.
DEFAULT_ENTER_PODS = 10
DEFAULT_EXIT_PODS = 3

DEVICE_AUTO = "auto"
DEVICE_CUDA = "cuda"
DEVICE_CPU = "cpu"
DEVICES = (DEVICE_CUDA, DEVICE_CPU)
DEVICE_MODES = (DEVICE_AUTO, *DEVICES)

Sampler = Callable[[], list[float]]


def _gpu_limit(pod: dict) -> int:
    """Whole GPUs the pod asks for, summed over its containers' `nvidia.com/gpu` limits."""
    total = 0
    for container in (pod.get("spec") or {}).get("containers") or []:
        limits = (container.get("resources") or {}).get("limits") or {}
        raw = limits.get(GPU_RESOURCE)
        if raw is None:
            continue
        try:
            total += int(str(raw))
        except ValueError:
            total += 1  # an unparseable quantity is still a GPU ask, not a zero
    return total


def _parse_k8s_time(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def gpu_pending_ages(pod_list: dict, now: float) -> list[float]:
    """Seconds each GPU-requesting, non-terminating pod in a PodList has existed.

    The caller has already filtered on `status.phase=Pending`, so age since
    creation is age spent waiting.

    Pods whose `nvidia.com/gpu` limit is 0 do NOT count. That is exactly the shape
    of a segmentation pod already on the CPU path — the templates' podSpecPatch
    zeroes the limit rather than removing it — and counting those would make the
    fallback self-sustaining: every CPU pod Pending for a cpu-heavy node would read
    as more GPU starvation.
    """
    ages: list[float] = []
    for pod in pod_list.get("items") or []:
        meta = pod.get("metadata") or {}
        if meta.get("deletionTimestamp"):
            continue
        if _gpu_limit(pod) <= 0:
            continue
        ages.append(now - _parse_k8s_time(meta["creationTimestamp"]))
    return ages


def pending_gpu_pod_ages(namespace: str = ARGO_NAMESPACE, *, timeout: float = 30.0) -> list[float]:
    """Live sampler: ages of the GPU pods currently Pending in `namespace`."""
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    # Re-read per call: the projected token rotates, and a long-lived flow that
    # cached it at start-up would begin failing hours in.
    token = _SA_TOKEN.read_text().strip()
    response = httpx.get(
        f"https://{host}:{port}/api/v1/namespaces/{namespace}/pods",
        params={"fieldSelector": "status.phase=Pending"},
        headers={"Authorization": f"Bearer {token}"},
        verify=str(_SA_CA),
        timeout=timeout,
    )
    response.raise_for_status()
    return gpu_pending_ages(response.json(), now=time.time())


class DroughtDetector:
    """Hysteresis over the pending-GPU-pod signal; see the module docstring."""

    def __init__(
        self,
        sampler: Sampler = pending_gpu_pod_ages,
        *,
        enter_pods: int = DEFAULT_ENTER_PODS,
        exit_pods: int = DEFAULT_EXIT_PODS,
        min_pending_age_s: float = DEFAULT_MIN_PENDING_AGE_S,
        on_change: Callable[[bool, int, int], None] | None = None,
        on_error: Callable[[Exception, bool], None] | None = None,
    ) -> None:
        if exit_pods >= enter_pods:
            raise ValueError(
                f"exit_pods ({exit_pods}) must be below enter_pods ({enter_pods}); "
                "equal thresholds remove the hysteresis and the detector flaps"
            )
        self._sampler = sampler
        self._enter = enter_pods
        self._exit = exit_pods
        self._min_age = min_pending_age_s
        self._on_change = on_change
        self._on_error = on_error
        self.in_drought = False

    def observe(self) -> bool:
        """Sample once and return whether the pool is in a drought.

        On a sampler failure the previous verdict is returned unchanged (and
        `on_error` told), never raised: a decision made on the last good read is
        the safe direction, and the queue manager must not die on it.
        """
        try:
            ages = self._sampler()
        except Exception as exc:
            if self._on_error is not None:
                self._on_error(exc, self.in_drought)
            return self.in_drought

        starved = sum(1 for age in ages if age >= self._min_age)
        before = self.in_drought
        if not before and starved >= self._enter:
            self.in_drought = True
        elif before and starved <= self._exit:
            self.in_drought = False

        if self.in_drought != before and self._on_change is not None:
            self._on_change(self.in_drought, starved, len(ages))
        return self.in_drought


def parse_device_mode(raw: object) -> str:
    """Normalise the `cloudpipe-fastsurfer-device` Variable: auto | cuda | cpu.

    Raises on anything else so the caller's `cap_reader` holds the last good mode
    instead of submitting a workflow Argo would reject — or worse, one the
    templates would silently treat as cuda.
    """
    value = str(raw if raw is not None else DEVICE_AUTO).strip().lower()
    if value not in DEVICE_MODES:
        raise ValueError(
            f"cloudpipe-fastsurfer-device must be one of {'/'.join(DEVICE_MODES)}, got {raw!r}"
        )
    return value


def choose_device(mode: str, in_drought: bool) -> str:
    """The `fastsurfer-device` to submit with: an explicit mode wins, auto follows the detector."""
    if mode in DEVICES:
        return mode
    if mode != DEVICE_AUTO:
        raise ValueError(f"unknown device mode {mode!r}")
    return DEVICE_CPU if in_drought else DEVICE_CUDA
