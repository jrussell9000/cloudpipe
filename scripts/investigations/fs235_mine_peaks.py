"""#235 §1b -- mine cgroup memory.peak per k from archived long-parcellation pod logs.

The sampler landed in 85a64fb (2026-08-12T02:23Z), one hour before the 08-12 batch was
submitted, and it is a template-only change -- so it was live for both the 08-12 and
08-14 batches and their archived logs already carry the numbers. Pod logs outlive the
workflows (`s3://<YOUR_S3_BUCKET>/logs/{workflow}/{pod}/main.log`), so this needs no live cluster.

Two lines per pod matter:

    cpu request=8 K=4 --threads=2 (threads_hemi=1)
    [mem] K=4 surface-stage peak=7.42G

`memory.peak` is a kernel-maintained high-water mark, not a sample, so a sub-30s
transient cannot hide from it. The `[mem] ... anon=/file=` samples give the shape and,
critically, the split -- `anon` is unreclaimable demand while `file` is page cache the
kernel drops under pressure. pod_memory_working_set folds the two together, which is
why an average-based metric (ram_efficiency) could not answer this question.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import statistics
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

METRICS_BUCKET = "cloudpipe-metrics"
DATA_BUCKET = "<YOUR_S3_BUCKET>"
SNAPSHOT_PREFIX = "snapshots/argo-nodes/"
STEP_MATCH = "fastsurfer-long-parcellation"

RE_CPU = re.compile(r"cpu request=(\d+) K=(\d+) --threads=(\d+)")
RE_PEAK = re.compile(r"\[mem\] K=(\d+) surface-stage peak=([\d.]+)G")
RE_SAMPLE = re.compile(r"\[mem\] (\d\d:\d\d:\d\d) anon=([\d.]+)G file=([\d.]+)G")
# the signature of all k sessions in the memory-heavy stage at once
RE_BIAS = re.compile(r"Building bias image|building Voronoi diagram|Applying bias correction")


def collect_pods(s3) -> list[dict]:
    """Every long-parcellation pod attempt in the snapshot corpus, deduped."""
    latest: dict[str, dict] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=METRICS_BUCKET, Prefix=SNAPSHOT_PREFIX):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".jsonl.gz"):
                continue
            body = s3.get_object(Bucket=METRICS_BUCKET, Key=obj["Key"])["Body"].read()
            with gzip.open(io.BytesIO(body), "rt") as fh:
                for line in fh:
                    rec = json.loads(line)
                    wf = rec.get("workflow")
                    if not wf:
                        continue
                    prev = latest.get(wf)
                    if prev is None or rec["snapshot_at"] > prev["snapshot_at"]:
                        latest[wf] = rec

    pods = []
    for wf, rec in latest.items():
        for pod in rec.get("pods") or []:
            if STEP_MATCH in (pod.get("template") or ""):
                pods.append(
                    {
                        "workflow": wf,
                        "subject": rec.get("subject"),
                        "created_at": rec.get("created_at"),
                        "pod": pod.get("node_id"),
                        "phase": pod.get("phase"),
                        "exit_code": pod.get("exit_code"),
                        "message": pod.get("message"),
                        "host_node": pod.get("host_node"),
                    }
                )
    return pods


def parcellation_log_keys(s3, workflow: str) -> list[str]:
    """List archived parcellation logs for a workflow.

    The archived pod directory is `{workflow}-{template}-{hash}`, which is NOT the
    snapshot's Argo `node_id` -- the hash cannot be reconstructed, so the keys have to
    be listed rather than built. One workflow can hold several: a retried step archives
    one log per attempt.
    """
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=DATA_BUCKET, Prefix=f"logs/{workflow}/"):
        for obj in page.get("Contents", []):
            if STEP_MATCH in obj["Key"] and obj["Key"].endswith("/main.log"):
                keys.append(obj["Key"])
    return keys


def fetch_log(s3, entry: dict) -> dict:
    key = entry["log_key"]
    try:
        text = s3.get_object(Bucket=DATA_BUCKET, Key=key)["Body"].read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - absent log is data, not an error
        return {**entry, "log": None, "error": type(exc).__name__}

    out = {**entry, "log": key, "error": None, "log_bytes": len(text)}
    if m := RE_CPU.search(text):
        out["cpu_request"] = int(m.group(1))
        out["k"] = int(m.group(2))
        out["threads"] = int(m.group(3))
    if m := RE_PEAK.search(text):
        out["k_peak_line"] = int(m.group(1))
        out["peak_g"] = float(m.group(2))
    samples = RE_SAMPLE.findall(text)
    if samples:
        out["n_samples"] = len(samples)
        out["max_anon_g"] = max(float(a) for _, a, _ in samples)
        out["max_file_g"] = max(float(f) for _, _, f in samples)
    out["bias_hits"] = len(RE_BIAS.findall(text))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args(argv)

    cfg = Config(max_pool_connections=args.workers * 2, retries={"max_attempts": 5})
    s3 = boto3.client("s3", config=cfg)

    pods = collect_pods(s3)
    print(f"long-parcellation pod attempts in snapshot corpus: {len(pods)}")

    # one workflow can hold several archived attempts; expand to one task per log
    workflows = sorted({p["workflow"] for p in pods})
    by_wf = {p["workflow"]: p for p in pods}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        keysets = list(pool.map(lambda w: (w, parcellation_log_keys(s3, w)), workflows))
    tasks = [
        {**by_wf[w], "log_key": key, "attempts_archived": len(keys)}
        for w, keys in keysets
        for key in keys
    ]
    print(f"archived parcellation logs found: {len(tasks)} over {len(workflows)} workflows")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda e: fetch_log(s3, e), tasks))

    have_log = [r for r in results if r["log"] and not r["error"]]
    have_peak = [r for r in have_log if "peak_g" in r]
    print(f"archived logs fetched: {len(have_log)}/{len(pods)}")
    print(f"logs carrying a memory.peak line: {len(have_peak)}")

    by_k: dict[int, list[dict]] = defaultdict(list)
    for r in have_peak:
        if "k" in r:
            by_k[r["k"]].append(r)

    print(
        f"\n{'k':>2} {'n':>4} {'req':>5} {'peak min':>9} {'median':>8} {'max':>7} "
        f"{'max anon':>9} {'peak/req':>9}"
    )
    print("-" * 66)
    for k in sorted(by_k):
        group = by_k[k]
        peaks = sorted(r["peak_g"] for r in group)
        req = 18 if k > 3 else 13
        anons = [r["max_anon_g"] for r in group if "max_anon_g" in r]
        print(
            f"{k:>2} {len(group):>4} {req:>4}G {peaks[0]:>8.2f}G "
            f"{statistics.median(peaks):>7.2f}G {peaks[-1]:>6.2f}G "
            f"{(max(anons) if anons else float('nan')):>8.2f}G "
            f"{peaks[-1] / req:>8.2f}"
        )

    out = "/tmp/fs235_peaks.json"
    with open(out, "w") as fh:
        json.dump(results, fh, indent=1)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
