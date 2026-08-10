#!/usr/bin/env python3
"""Answer issue #132's co-packing question from `collect_pod_phase_split.py` output.

WHAT THIS ANSWERS
------------------
`long-parcellation` at k=4 requests `cpu: 8`, which only a `cpu-heavy-nodepool`
4xlarge can admit (8000m exceeds a 2xlarge's ~7225m schedulable outright, per
the daemonset-overhead arithmetic in issue #132). That pod holds a whole
4xlarge for ~54 minutes; the open question is whether the other ~7.2 cores on
that node are doing real work at the same time, or stranded.

`collect_pod_phase_split.py` already records the fields needed to answer this
(`node`, `cpu_request_cores`, `pod_start`, `main_end`) -- it was extended for
exactly this purpose in PR #139 -- but nothing consumed them for the co-packing
question until now. This script is that consumer: given a phase-split JSONL,
for each k=4 `long-parcellation` pod it reconstructs every other cloudpipe pod
that shared its node while it ran, and reports occupied capacity as a fraction
of the node's schedulable cores.

WHAT "OCCUPIED" MEANS HERE
---------------------------
Not "was there a second pod present at some instant" -- that overstates
packing, since a 1-second overlap at the pod's tail counts the same as full
overlap. Instead this computes the TIME-WEIGHTED average concurrent cpu
request across the target pod's own window, via a sweep over cpu request
deltas (+cpu at pod_start, -cpu at main_end). Dividing by node capacity gives
occupied_fraction: 1.0 would mean the node was fully requested for the whole
window (impossible to exceed on a real node, since the scheduler would not
have admitted more than capacity).

CAVEATS -- READ BEFORE TRUSTING A NUMBER OUT OF THIS
------------------------------------------------------
- Only pods the collector actually observed count as co-tenants. A watch gap
  (see collect_pod_phase_split.py's reconnect logic) silently drops pods and
  biases occupied_fraction low, the same coverage problem noted in the
  2026-08-03 10-subject attempt at this issue. Check the collector's own
  reconnect log before trusting a low number.
- A pod with no `main_end` (still running, or observed only via a thin DELETE
  event) is excluded from the sweep entirely rather than assumed either
  present or absent -- occupied_fraction is therefore a lower bound whenever
  incomplete records exist on a node, never an exact figure.
- Node capacity (15205 millicores schedulable) is the 4xlarge figure derived
  in issue #132 (allocatable minus guardduty/cloudwatch-agent/kube-proxy/
  fluent-bit/aws-node/ebs-csi/kubecost-network-costs daemonset overhead).
  Every node in this analysis IS a 4xlarge by construction: cpu-heavy-nodepool
  is the only pool admitting the 8-core long-parcellation request, and no
  smaller instance in that pool's families can fit it.
- node-hours-per-subject uses the observed pod span on a node (earliest
  pod_start to latest main_end among pods the collector saw there) as a proxy
  for node lifetime. This is a lower bound, not the true Karpenter node
  lifetime -- the node may have existed slightly before/after the observed
  pods.

USAGE
-----
    scripts/analyze_pod_copacking.py pod-phase-split.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from typing import Any

# millicores schedulable on a cpu-heavy-nodepool 4xlarge after daemonset
# overhead -- see issue #132 and the module docstring above.
NODE_CAPACITY_CORES_4XL = 15205 / 1000.0

LONG_PARCELLATION_STEP = "long-parcellation"
K4_CPU_CORES = 8.0
K3_CPU_CORES = 6.0


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_records(path: str) -> list[dict[str, Any]]:
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def occupied_fraction(
    window_start: datetime,
    window_end: datetime,
    node_pods: list[dict[str, Any]],
    capacity_cores: float,
) -> tuple[float, int] | None:
    """Time-weighted mean concurrent cpu request over [window_start, window_end],
    as a fraction of capacity_cores. Returns (fraction, n_cotenants) or None if
    the window has zero duration or no usable pods.

    Pods missing pod_start, main_end, or cpu_request_cores are skipped -- see
    the module docstring's caveat on why that makes this a lower bound, not an
    exact figure, whenever such pods exist on the node.
    """
    span = (window_end - window_start).total_seconds()
    if span <= 0:
        return None

    events: list[tuple[float, float]] = []
    cotenants = set()
    for pod in node_pods:
        start = _parse_ts(pod.get("pod_start"))
        end = _parse_ts(pod.get("main_end"))
        cpu = pod.get("cpu_request_cores")
        if start is None or end is None or cpu is None or end <= start:
            continue
        # Clip to the target's window -- only the overlapping portion of a
        # co-tenant's occupancy counts toward this target's occupied_fraction.
        clipped_start = max(start, window_start)
        clipped_end = min(end, window_end)
        if clipped_end <= clipped_start:
            continue
        events.append(((clipped_start - window_start).total_seconds(), cpu))
        events.append(((clipped_end - window_start).total_seconds(), -cpu))
        cotenants.add(pod["pod"])

    if not events:
        return None

    events.sort()
    concurrent = 0.0
    prev_t = 0.0
    weighted_sum = 0.0
    for t, delta in events:
        weighted_sum += concurrent * (t - prev_t)
        concurrent += delta
        prev_t = t
    weighted_sum += concurrent * (span - prev_t)

    mean_concurrent = weighted_sum / span
    return mean_concurrent / capacity_cores, len(cotenants)


def node_span_hours(node: str, records: list[dict[str, Any]]) -> float | None:
    """Earliest pod_start to latest main_end among observed pods on `node`."""
    starts, ends = [], []
    for r in records:
        if r.get("node") != node:
            continue
        s = _parse_ts(r.get("pod_start"))
        e = _parse_ts(r.get("main_end"))
        if s:
            starts.append(s)
        if e:
            ends.append(e)
    if not starts or not ends:
        return None
    return (max(ends) - min(starts)).total_seconds() / 3600.0


def analyze(records: list[dict[str, Any]], out=sys.stdout) -> None:
    by_node: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        node = r.get("node")
        if node:
            by_node.setdefault(node, []).append(r)

    k4_targets = [
        r
        for r in records
        if r.get("step") == LONG_PARCELLATION_STEP
        and r.get("cpu_request_cores") == K4_CPU_CORES
        and r.get("node")
        and r.get("pod_start")
        and r.get("main_end")
    ]
    k3_targets = [
        r
        for r in records
        if r.get("step") == LONG_PARCELLATION_STEP
        and r.get("cpu_request_cores") == K3_CPU_CORES
        and r.get("node")
        and r.get("pod_start")
        and r.get("main_end")
    ]

    if not k4_targets:
        print(
            "no complete k=4 long-parcellation pod records found -- nothing to analyze",
            file=out,
        )
        return

    fractions = []
    print(f"{'pod':<40}{'node':<24}{'occupied%':>10}{'co-tenants':>12}", file=out)
    print("-" * 86, file=out)
    for r in k4_targets:
        start = _parse_ts(r["pod_start"])
        end = _parse_ts(r["main_end"])
        result = occupied_fraction(start, end, by_node.get(r["node"], []), NODE_CAPACITY_CORES_4XL)
        if result is None:
            continue
        frac, n_cotenants = result
        fractions.append(frac)
        print(
            f"{r['pod'][:40]:<40}{r['node'][:24]:<24}{frac * 100:>9.1f}%{n_cotenants:>12}",
            file=out,
        )

    if not fractions:
        print("\nno k=4 pod had a usable node window -- see caveats", file=out)
        return

    print(
        f"\n{len(fractions)} k=4 pods measured "
        f"(of {len(k4_targets)} with complete pod_start/main_end)",
        file=out,
    )
    print(
        f"occupied_fraction: mean={statistics.mean(fractions) * 100:.1f}% "
        f"median={statistics.median(fractions) * 100:.1f}% "
        f"min={min(fractions) * 100:.1f}% max={max(fractions) * 100:.1f}%",
        file=out,
    )

    # A crude bimodality signal: split at the midpoint between the theoretical
    # "alone on the node" floor (1/capacity, one pod's own 8 cores) and full
    # occupancy (1.0). A batch that is "full during the run, empty at the
    # tail" clusters near both ends rather than the middle.
    low = sum(1 for f in fractions if f < 0.4)
    mid = sum(1 for f in fractions if 0.4 <= f < 0.75)
    high = sum(1 for f in fractions if f >= 0.75)
    print(
        f"distribution: <40% occupied: {low}  40-75%: {mid}  >=75%: {high}",
        file=out,
    )

    k4_nodes = {r["node"] for r in k4_targets}
    k3_nodes = {r["node"] for r in k3_targets}
    k4_node_hours = [h for n in k4_nodes if (h := node_span_hours(n, records)) is not None]
    k3_node_hours = [h for n in k3_nodes if (h := node_span_hours(n, records)) is not None]

    if k4_node_hours and k4_targets:
        per_k4 = sum(k4_node_hours) / len(k4_targets)
        print(f"\nnode-hours per k=4 subject (observed-span proxy): {per_k4:.2f}", file=out)
    if k3_node_hours and k3_targets:
        per_k3 = sum(k3_node_hours) / len(k3_targets)
        print(f"node-hours per k=3 subject (observed-span proxy): {per_k3:.2f}", file=out)
        if k4_node_hours and k4_targets:
            ratio = per_k4 / per_k3
            print(
                f"ratio k4/k3: {ratio:.2f}  (near 4/3={4 / 3:.2f} => packing fine; "
                f"near 2 => ~half the node idled)",
                file=out,
            )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("input", help="phase-split JSONL from collect_pod_phase_split.py")
    args = ap.parse_args()
    analyze(load_records(args.input))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
