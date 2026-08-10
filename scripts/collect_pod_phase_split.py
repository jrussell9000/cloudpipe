#!/usr/bin/env python3
"""Record the init/compute split of every cloudpipe pod in a batch.

WHY THIS EXISTS
---------------
`cpu_efficiency` in the metrics corpus comes straight from Kubecost's pod-level
`cpuEfficiency` (src/metrics/kubecost_scraper.py) — usage over request, averaged
across the pod's whole allocation window. Argo stages input artifacts in the
pod's *init* container, and a pod's scheduling footprint
(`max(sum(app containers), max(init containers))`) is reserved for its entire
lifetime, init included. Argo's init/wait containers request almost nothing, so
the reservation is `main`'s — sized for compute, held idle through staging.

So `cpu_efficiency` blends phases that behave completely differently, and reading
it as "the compute phase only uses this fraction of its cores" is unsafe without
knowing the split. How much it actually distorts depends on how long the step
computes, because staging cost is roughly constant per step. Measured on the
2026-08-03 10-subject batch:

    long-parcellation      29.9 s staging / 1922 s compute    1.5%
    template-parcellation  27.0 s staging /  974 s compute    2.7%
    bold-to-t1w            34.9 s staging /  240 s compute   12.7%
    hydrate-fastsurfer     52.6 s staging /    2 s compute   96.3%

WHY THIS READS THE TIMESTAMPS INSTEAD OF SUBTRACTING
----------------------------------------------------
A pod has THREE phases, not two: staging, then the **main-container image pull**,
then compute. Deriving staging as `pod duration - compute` silently includes the
pull, which is 1-22 s on a warm node but **84-152 s on a cold one** for the
multi-GB images here.

That is not hypothetical — it is why this script exists in its current form. The
original write-up of issue #123 reported `bold-to-t1w` as ~50% staging, measured
on a pinned single-pod probe. A pinned probe sets `do-not-disrupt` and gets a
fresh node every run, so it paid a cold image pull every time; the real figure on
a warm batch is 12.7%. Reading `initContainerStatuses[].state.terminated` and
`containerStatuses[main]` separately, as `_extract` does, cannot make that error.

Corollary: measure phase fractions on a **warm, real batch**. A pinned probe is
the right instrument for A/B-ing one variable, because it holds the host
constant, and the wrong instrument for anything expressed as a fraction of pod
lifetime.

WHAT TO DO WITH THE OUTPUT
--------------------------
`idle_core_seconds` (cpu_request x init_seconds) is the directly billable waste
and is the number to rank steps by. But note the ranking only identifies where
staging is worth attacking — it does NOT tell you a request is correctly sized.
For that, compare `compute_seconds` against what the step achieves with fewer
cores (an A/B on a pinned host is exactly right for that question).

USAGE
-----
Start it BEFORE submitting the batch and leave it running:

    scripts/collect_pod_phase_split.py -o /tmp/phase-split.jsonl

    # then, in another shell
    argo -n argo-workflows submit --from workflowtemplate/cloudpipe -p ...

Ctrl-C when the batch finishes; a summary by step is printed to stderr on exit
(also available any time via `--summarize` on an existing file).

WHY A WATCH AND NOT A POLL
--------------------------
`cloudpipe`'s spec sets `podGC: strategy: OnPodSuccess`, so a successful pod is
deleted within seconds of finishing. A polling loop races that deletion and
loses the terminal container state on exactly the pods that completed normally —
i.e. all the interesting ones. `kubectl get pods -w --output-watch-events`
delivers every intermediate state before the DELETE, so the last observed state
is always complete. Nothing here mutates the cluster; it is a read-only watch.
"""

from __future__ import annotations

import argparse
import collections
import json
import signal
import subprocess
import sys
import time
from datetime import datetime
from typing import Any

NAMESPACE = "argo-workflows"

# Argo names the user container of a script/container template `main`. The
# artifact-staging init container is `init`, but it is matched by position
# (every init container counts) rather than by name — see _extract. The `wait`
# sidecar uploads outputs and is deliberately NOT counted as compute.
MAIN = "main"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    # Kubernetes emits RFC3339 with a literal Z, which fromisoformat rejects on
    # Python < 3.11. The images run 3.10, and this script may be run from one.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 1)


def _cpu_to_cores(quantity: str | None) -> float | None:
    """Kubernetes CPU quantities are either cores ('4') or millicores ('100m')."""
    if not quantity:
        return None
    return float(quantity[:-1]) / 1000.0 if quantity.endswith("m") else float(quantity)


def _terminated_window(status: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    """Start/finish of a container that has run to completion."""
    term = (status.get("state") or {}).get("terminated") or {}
    if term:
        return _parse_ts(term.get("startedAt")), _parse_ts(term.get("finishedAt"))
    # Still running: we know when it started but not when it ends. Callers treat
    # a None finish as "not yet measurable" rather than zero.
    running = (status.get("state") or {}).get("running") or {}
    return _parse_ts(running.get("startedAt")), None


def _extract(pod: dict[str, Any]) -> dict[str, Any] | None:
    """One record per pod, or None if the pod has not run far enough to be useful."""
    meta = pod.get("metadata") or {}
    labels = meta.get("labels") or {}
    spec = pod.get("spec") or {}
    status = pod.get("status") or {}

    # Only cloudpipe workflow pods. `workflows.argoproj.io/workflow` is set by
    # the controller on every workflow pod; `cloudpipe.io/step` is our own label
    # and is the readable name, but not every template sets it.
    workflow = labels.get("workflows.argoproj.io/workflow")
    if not workflow:
        return None

    main_spec = next((c for c in spec.get("containers") or [] if c.get("name") == MAIN), None)
    if main_spec is None:
        return None

    init_statuses = status.get("initContainerStatuses") or []
    main_status = next(
        (c for c in status.get("containerStatuses") or [] if c.get("name") == MAIN),
        None,
    )
    if main_status is None:
        return None

    pod_start = _parse_ts(status.get("startTime"))

    # Init phase spans ALL init containers, not just Argo's `init` — a template
    # may add its own (the fastsurfer GPU steps do), and every one of them runs
    # while the pod already holds its full reservation.
    init_starts, init_ends = [], []
    for st in init_statuses:
        s, e = _terminated_window(st)
        if s:
            init_starts.append(s)
        if e:
            init_ends.append(e)
    init_start = min(init_starts) if init_starts else None
    init_end = max(init_ends) if len(init_ends) == len(init_statuses) and init_ends else None

    main_start, main_end = _terminated_window(main_status)

    requests = (main_spec.get("resources") or {}).get("requests") or {}
    cpu_request = _cpu_to_cores(requests.get("cpu"))

    # Prefer pod.startTime as the start of the billed window: the reservation
    # begins when the pod is bound, slightly before the first init container's
    # own startedAt (image pull for the init container itself sits in that gap).
    init_seconds = _seconds(pod_start or init_start, init_end)
    main_seconds = _seconds(main_start, main_end)

    record = {
        "pod": meta.get("name"),
        "workflow": workflow,
        "step": labels.get("cloudpipe.io/step")
        or labels.get("workflows.argoproj.io/node-name")
        or "(unlabelled)",
        # Node name only. Resolving it to an instance type would need a second
        # API call per pod against a node that Karpenter may already have
        # consolidated away; `kubectl get node <n> -L node.kubernetes.io/instance-type`
        # after the fact is good enough, and staging time is dominated by S3
        # throughput rather than instance type anyway.
        "node": spec.get("nodeName"),
        "cpu_request_cores": cpu_request,
        "gpu_request": (
            ((main_spec.get("resources") or {}).get("limits") or {}).get("nvidia.com/gpu")
            or requests.get("nvidia.com/gpu")
        ),
        "memory_request": requests.get("memory"),
        "init_container_count": len(init_statuses),
        "init_seconds": init_seconds,
        "compute_seconds": main_seconds,
        # Absolute boundaries, not just durations. Two questions need them and
        # cannot be answered from durations alone:
        #   - the init-end -> main-start gap (the main-container image pull),
        #     which is the third phase the docstring above warns about;
        #   - which pods shared a node at the same instant, for the co-packing
        #     question in issue #132 -- that needs overlapping intervals per
        #     `node`, which only absolute times can reconstruct.
        "pod_start": _iso(pod_start),
        "init_end": _iso(init_end),
        "main_start": _iso(main_start),
        "main_end": _iso(main_end),
        "phase": status.get("phase"),
        "observed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    # Time between the last init container terminating and `main` starting: the
    # image pull, plus scheduling slack. 1-22 s warm, 84-152 s cold.
    pull_gap = _seconds(init_end, main_start)
    if pull_gap is not None:
        record["pull_gap_seconds"] = pull_gap

    # A GPU pod holds `nvidia.com/gpu` from bind to exit, so everything before
    # `main` starts is GPU capacity paid for and not used. With time-slicing at
    # 2 pods/GPU that is half a device. This is the #123 residual.
    if record["gpu_request"] and init_seconds is not None and pull_gap is not None:
        record["gpu_idle_seconds"] = round(init_seconds + pull_gap, 1)

    if init_seconds is not None and main_seconds is not None:
        total = init_seconds + main_seconds
        record["measured_seconds"] = round(total, 1)
        record["init_fraction"] = round(init_seconds / total, 3) if total else None
        if cpu_request is not None:
            # The cores held but not used while artifacts stage. This is the
            # billable waste, and the right thing to rank steps by.
            record["idle_core_seconds"] = round(cpu_request * init_seconds, 1)
    return record


def summarize(records: list[dict[str, Any]], out=sys.stderr) -> None:
    """Per-step aggregate. Only complete records contribute."""
    by_step: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in records:
        if r.get("init_fraction") is not None:
            by_step[r["step"]].append(r)

    if not by_step:
        print("no complete pod records collected", file=out)
        return

    rows = []
    for step, rs in by_step.items():
        n = len(rs)
        init = sum(r["init_seconds"] for r in rs) / n
        comp = sum(r["compute_seconds"] for r in rs) / n
        idle = sum(r.get("idle_core_seconds") or 0.0 for r in rs)
        cpu = rs[0].get("cpu_request_cores")
        rows.append((idle, step, n, cpu, init, comp, init / (init + comp), idle))

    rows.sort(reverse=True)
    print(
        f"\n{'step':<44}{'n':>4}{'cpu':>6}{'init s':>9}{'comp s':>9}"
        f"{'init%':>8}{'idle core-s':>13}",
        file=out,
    )
    print("-" * 93, file=out)
    for _, step, n, cpu, init, comp, frac, idle in rows:
        cpu_s = f"{cpu:g}" if cpu is not None else "-"
        print(
            f"{step[:44]:<44}{n:>4}{cpu_s:>6}{init:>9.1f}{comp:>9.1f}"
            f"{frac * 100:>7.1f}%{idle:>13.1f}",
            file=out,
        )
    print(
        "\nHigh init% with a saturated compute phase means the request is sized "
        "right and\nthe waste is in staging — trimming cpu there throttles real "
        "work. Rank remediation\nby `idle core-s`, which is what the reservation "
        "actually costs.",
        file=out,
    )


def watch(output_path: str) -> list[dict[str, Any]]:
    """Stream pod events, keeping the most complete record seen per pod."""
    # --output-watch-events wraps each object as {"type": ..., "object": ...}.
    # Without it, a DELETE is indistinguishable from an update and the final
    # state can be lost.
    cmd = [
        "kubectl",
        "get",
        "pods",
        "-n",
        NAMESPACE,
        "-w",
        "--output-watch-events",
        "-o",
        "json",
    ]
    best: dict[str, dict[str, Any]] = {}
    decoder = json.JSONDecoder()

    def _flush() -> None:
        with open(output_path, "w") as fh:
            for rec in best.values():
                fh.write(json.dumps(rec) + "\n")

    # The API server ends a watch on its own schedule (resourceVersion expiry,
    # request timeout, apiserver rollover) and `kubectl get -w` exits 0 when it
    # does. Without this loop the collector stopped ~28 minutes into a 2.5-hour
    # batch and printed a perfectly normal-looking summary of the fraction it
    # had seen — silent partial telemetry, which is worse than a crash. Re-watch
    # until interrupted; `best` is keyed by pod name and carries across, and the
    # relaunched watch replays a LIST of everything still alive, so only pods
    # that both started AND were reaped inside the gap are lost.
    stopping = False

    def _on_signal(_sig, _frm):
        nonlocal stopping
        stopping = True
        if proc_ref["p"] is not None:
            proc_ref["p"].terminate()

    proc_ref: dict[str, subprocess.Popen | None] = {"p": None}
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    watch_generation = 0
    backoff = 0.0
    try:
        while not stopping:
            watch_generation += 1
            if watch_generation > 1:
                print(
                    f"[collector] watch stream ended; reconnecting in {backoff:.0f}s "
                    f"(generation {watch_generation}, {len(best)} pods held)",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(backoff)
                if stopping:
                    break
            started = time.monotonic()
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
            proc_ref["p"] = proc
            _consume(proc, best, decoder, _flush)
            proc.terminate()
            proc.wait()
            # A watch that ends immediately means kubectl cannot reach the API
            # server at all — VPN dropped, SSO expired, context gone. Without a
            # backoff that is a hot relaunch loop; with one, the collector simply
            # waits out the outage and picks the batch back up. A watch that ran
            # for a while ended normally, so reconnect at once.
            if time.monotonic() - started < 5:
                backoff = min(backoff * 2, 60.0) if backoff else 5.0
            else:
                # Measured on the 2026-08-03 batch: 543 reconnects in ~1.75 h,
                # i.e. a watch lasting ~11 s. An idle namespace holds a watch for
                # well over a minute, so this is pod CHURN — podGC: OnPodSuccess
                # deletes pods constantly during a batch, and the resourceVersion
                # the watch is anchored to expires. Reconnecting is therefore the
                # normal case during a run, not an error path.
                #
                # Each reconnect replays a full LIST of the namespace, so a zero
                # floor turns a busy batch into a steady stream of LISTs against
                # the API server. One second bounds that without meaningfully
                # widening the window in which a pod could be both created and
                # reaped unseen (the fastest observed pod lives ~2 s).
                backoff = 1.0
    except KeyboardInterrupt:
        pass
    finally:
        if proc_ref["p"] is not None:
            proc_ref["p"].terminate()
        _flush()

    return list(best.values())


def _consume(
    proc: subprocess.Popen,
    best: dict[str, dict[str, Any]],
    decoder: json.JSONDecoder,
    _flush,
) -> None:
    """Drain one watch stream into `best`. Returns when the stream ends."""
    buf = ""
    try:
        assert proc.stdout is not None
        for chunk in proc.stdout:
            buf += chunk
            # kubectl pretty-prints, so objects span many lines and arrive
            # back-to-back with no separator. raw_decode peels them off one at a
            # time and leaves any partial object in the buffer.
            while buf:
                buf = buf.lstrip()
                if not buf:
                    break
                try:
                    event, end = decoder.raw_decode(buf)
                except ValueError:
                    break  # incomplete object; wait for more input
                buf = buf[end:]

                pod = event.get("object") or {}
                rec = _extract(pod)
                if rec is None:
                    continue
                name = rec["pod"]
                prev = best.get(name)
                # A later event is preferred only if it is at least as complete:
                # DELETED events sometimes carry a thinner object than the final
                # MODIFIED, and overwriting a complete record with it would lose
                # the terminal timestamps this whole script exists to capture.
                if (
                    prev is None
                    or (rec.get("compute_seconds") is not None)
                    or (prev.get("compute_seconds") is None)
                ):
                    best[name] = rec
                if rec.get("compute_seconds") is not None:
                    print(
                        f"[collector] {rec['step']} init={rec['init_seconds']}s "
                        f"compute={rec['compute_seconds']}s "
                        f"cpu={rec['cpu_request_cores']}",
                        file=sys.stderr,
                        flush=True,
                    )
                    _flush()
    except KeyboardInterrupt:
        pass
    finally:
        _flush()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "-o",
        "--output",
        default="pod-phase-split.jsonl",
        help="JSONL destination (rewritten as records complete)",
    )
    ap.add_argument(
        "--summarize",
        metavar="FILE",
        help="print the per-step summary for an existing JSONL file and exit",
    )
    args = ap.parse_args()

    if args.summarize:
        with open(args.summarize) as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        summarize(records, out=sys.stdout)
        return 0

    print(
        f"[collector] watching {NAMESPACE} — start your batch now, Ctrl-C when done",
        file=sys.stderr,
    )
    records = watch(args.output)
    print(f"\n[collector] wrote {len(records)} pod records to {args.output}", file=sys.stderr)
    summarize(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
