#!/usr/bin/env python
"""GPU duty cycle and VRAM headroom, from archived pod logs.

Answers the question that gates the time-slice count (issue #102): a GPU pod holds
its slice for 100% of its runtime, but computes on the card for only part of that.
The idle fraction is what extra slices can be packed into.

Reads `s3://{bucket}/logs/{workflow}/{pod}/main.log`, which outlives workflow
deletion and is the only GPU history that survives — Argo pod logs are not
forwarded to CloudWatch.

Two parsing traps, both handled here:

1. The `nvidia-smi --query-gpu` CSV rows are written into the SAME stream as
   FastSurfer's tqdm progress bars, and land mid-line rather than at column 0.
   Matching `^timestamp` finds nothing; the regex below matches anywhere.
2. `memory.used` is DEVICE-wide, not per-process. With time-slicing the sample
   already includes every co-tenant, so it is not a per-pod footprint. Divide by
   the number of concurrent tenants, or cross-check against the documented
   working set in gitops/apps/nvidia-device-plugin/values.yaml.

Usage:
    pixi run python scripts/gpu_duty_cycle.py --workflows cloudpipe-c5hwv,cloudpipe-c8rhn

Workflow names come from `argo list -n argo-workflows`, or from the pod-cost records
for a batch — the logs outlive the workflows, so `argo list` may no longer show them.
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import boto3

# Steps that request nvidia.com/gpu. Anything else has no nvidia-smi output.
GPU_STEPS = (
    "fastsurfer-template-build",
    "fastsurfer-long-segmentation",
    "t1w-to-mni",
)

# timestamp, name, memory.total [MiB], utilization.gpu [%], utilization.memory [%],
# memory.used [MiB] — matched anywhere in the line, see trap 1 above.
ROW = re.compile(
    r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d+),\s*([^,]+?),\s*(\d+) MiB,\s*"
    r"(\d+) %,\s*(\d+) %,\s*(\d+) MiB"
)


def step_of(key: str) -> str | None:
    for st in GPU_STEPS:
        if st in key:
            return st
    return None


def list_gpu_logs(s3, bucket: str, workflows: list[str]) -> list[tuple[str, str]]:
    out = []
    for wf in workflows:
        token = None
        while True:
            kw = {"Bucket": bucket, "Prefix": f"logs/{wf}/"}
            if token:
                kw["ContinuationToken"] = token
            resp = s3.list_objects_v2(**kw)
            for o in resp.get("Contents", []):
                st = step_of(o["Key"])
                if st and o["Key"].endswith("main.log"):
                    out.append((st, o["Key"]))
            if not resp.get("IsTruncated"):
                break
            token = resp["NextContinuationToken"]
    return out


def analyse(s3, bucket: str, item: tuple[str, str]) -> tuple[str, dict] | None:
    st, key = item
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8", "replace")
    rows = ROW.findall(body)
    if not rows:
        return None
    util = [int(r[3]) for r in rows]
    mem = [int(r[5]) for r in rows]
    busy = [u for u in util if u > 0]
    return st, {
        "pod": key.split("/")[-2],
        "samples": len(rows),
        "duty": len(busy) / len(rows),
        "mean_util_busy": (sum(busy) / len(busy)) if busy else 0.0,
        "peak_mib": max(mem),
        "total_mib": int(rows[0][2]),
        "gpu": rows[0][1].strip(),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bucket", default="<YOUR_S3_BUCKET>", help="Data bucket holding logs/.")
    p.add_argument("--region", default="<YOUR_AWS_REGION>")
    p.add_argument(
        "--workflows",
        required=True,
        help="Comma-separated Argo workflow names (e.g. from `argo list`).",
    )
    p.add_argument("--workers", type=int, default=16)
    args = p.parse_args()

    s3 = boto3.client("s3", region_name=args.region)
    workflows = [w.strip() for w in args.workflows.split(",") if w.strip()]
    items = list_gpu_logs(s3, args.bucket, workflows)
    print(f"{len(items)} GPU pod log(s) across {len(workflows)} workflow(s)\n")

    agg: dict[str, list[dict]] = defaultdict(list)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for res in pool.map(lambda i: analyse(s3, args.bucket, i), items):
            if res:
                agg[res[0]].append(res[1])

    hdr = (
        f"{'step':<32} {'pods':>4} {'duty%':>7} {'utilBusy%':>10} {'peakMiB':>9} {'peak%VRAM':>10}"
    )
    print(hdr)
    print("-" * len(hdr))
    for st, g in agg.items():
        n = len(g)
        duty = sum(x["duty"] for x in g) / n
        ub = sum(x["mean_util_busy"] for x in g) / n
        peak = max(x["peak_mib"] for x in g)
        total = g[0]["total_mib"]
        print(
            f"{st:<32} {n:>4} {duty * 100:>6.1f}% {ub:>9.1f}% {peak:>9} {peak / total * 100:>9.1f}%"
        )
        print(
            f"    GPU={g[0]['gpu']}  total={total} MiB  samples/pod={sum(x['samples'] for x in g) // n}"
        )

    print(
        "\nNOTE: memory.used is device-wide. Under time-slicing the peak above already\n"
        "includes every co-tenant on that card — divide by the tenant count before\n"
        "treating it as a per-pod working set."
    )


if __name__ == "__main__":
    main()
