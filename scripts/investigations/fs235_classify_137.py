"""#235 §1a — classify every 137 on the long-parcellation step as spot-kill vs true OOM.

Per-attempt data lives in Argo `status.nodes` for ~24h only, so the durable source is
the `argo-nodes-snapshot` CronWorkflow output under `snapshots/argo-nodes/`. Reads every
snapshot, dedupes workflows (a 4-hourly snapshot sees the same workflow repeatedly; the
last sighting is the most complete), and reports the 137s with the message that
discriminates cause.

`137` alone does not discriminate: a spot reclaim and an OOMKill are both SIGKILL. Only
the node message separates them, and that distinction is already load-bearing in the
retry expression -- so it is reused here rather than re-derived.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
from collections import defaultdict

import boto3

METRICS_BUCKET = "cloudpipe-metrics"
SNAPSHOT_PREFIX = "snapshots/argo-nodes/"
STEP_MATCH = "fastsurfer-long-parcellation"

# A spot reclaim carries one of these in the node message. Anything else on a 137 is a
# candidate true OOM. Keep the alternation explicit: a looser match on "terminated"
# re-admits real OOMKills, which is the whole distinction being drawn.
SPOT_MARKERS = (
    "imminent node shutdown",
    "pod deleted",
    "spot interruption",
)
OOM_MARKERS = ("oomkill", "out of memory")


def classify(pod: dict) -> str:
    msg = (pod.get("message") or "").lower()
    if any(m in msg for m in SPOT_MARKERS):
        return "spot"
    if any(m in msg for m in OOM_MARKERS):
        return "oom"
    # exit 137 with a bare `main: Error (exit code 137)` and no reclaim language is the
    # OOMKill shape -- the kernel killed the container and Argo saw only the code.
    if msg.strip() == "" or re.search(r"main: error \(exit code 137\)", msg):
        return "oom-suspected"
    return "unclear"


def load_snapshots(s3, since: str | None) -> dict[str, dict]:
    """Return {workflow_name: latest record} across every snapshot object."""
    latest: dict[str, dict] = {}
    paginator = s3.get_paginator("list_objects_v2")
    n_objects = 0
    for page in paginator.paginate(Bucket=METRICS_BUCKET, Prefix=SNAPSHOT_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".jsonl.gz"):
                continue
            if since and not _key_dt(key) >= since:
                continue
            n_objects += 1
            body = s3.get_object(Bucket=METRICS_BUCKET, Key=key)["Body"].read()
            with gzip.open(io.BytesIO(body), "rt") as fh:
                for line in fh:
                    rec = json.loads(line)
                    wf = rec.get("workflow")
                    if not wf:
                        continue
                    prev = latest.get(wf)
                    # keep the newest sighting: it has the most attempts recorded
                    if prev is None or rec["snapshot_at"] > prev["snapshot_at"]:
                        latest[wf] = rec
    print(f"read {n_objects} snapshot objects -> {len(latest)} distinct workflows")
    return latest


def _key_dt(key: str) -> str:
    m = re.search(r"dt=(\d{4}-\d{2}-\d{2})", key)
    return m.group(1) if m else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", help="only snapshots with dt= on/after this YYYY-MM-DD")
    args = ap.parse_args(argv)

    s3 = boto3.client("s3")
    workflows = load_snapshots(s3, args.since)

    hits: list[dict] = []
    attempts_by_step = defaultdict(int)
    for wf, rec in workflows.items():
        for pod in rec.get("pods") or []:
            template = pod.get("template") or ""
            if STEP_MATCH not in template:
                continue
            attempts_by_step[template] += 1
            if str(pod.get("exit_code")) == "137":
                hits.append(
                    {
                        "workflow": wf,
                        "subject": rec.get("subject"),
                        "created_at": rec.get("created_at"),
                        "node_id": pod.get("node_id"),
                        "display_name": pod.get("display_name"),
                        "phase": pod.get("phase"),
                        "host_node": pod.get("host_node"),
                        "message": pod.get("message"),
                        "started_at": pod.get("started_at"),
                        "finished_at": pod.get("finished_at"),
                        "verdict": classify(pod),
                    }
                )

    total = sum(attempts_by_step.values())
    print(f"\nattempts on {STEP_MATCH}*: {total}")
    for step, n in sorted(attempts_by_step.items()):
        print(f"  {step}: {n}")

    print(f"\n137s found: {len(hits)}")
    by_verdict = defaultdict(list)
    for h in hits:
        by_verdict[h["verdict"]].append(h)
    for verdict, group in sorted(by_verdict.items()):
        print(f"  {verdict}: {len(group)}")

    for h in sorted(hits, key=lambda x: (x["created_at"] or "", x["workflow"])):
        print(
            f"\n--- {h['workflow']} / {h['subject']} [{h['verdict']}]"
            f"\n    created  {h['created_at']}"
            f"\n    pod      {h['node_id']}"
            f"\n    node     {h['host_node']}"
            f"\n    window   {h['started_at']} -> {h['finished_at']}"
            f"\n    message  {h['message']!r}"
        )

    out = "/tmp/fs235_137_hits.json"
    with open(out, "w") as fh:
        json.dump({"attempts": dict(attempts_by_step), "hits": hits}, fh, indent=1)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
