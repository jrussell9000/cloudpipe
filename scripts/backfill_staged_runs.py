#!/usr/bin/env python3
"""
backfill_staged_runs.py — plan, submit and verify the staged-run backfill.

First-level trees written before fmri-first-level-proc 4.0.0+cp.11 hold the n-back
concatenations ({pre}_concat_scan and {pre}_psc, in nback_act and nback_conn, ~2.5 GB a
session) and no staged runs. Since cp.11 a session keeps each run's series instead (per
Taylor Keding). The staged-backfill WorkflowTemplate cuts {pre}_concat_scan back into
its staged runs, checks every voxel, publishes them under staged/ and deletes the
concatenations (images/fmri-first-level-proc/staged_backfill.py). This script is its
only driver (operator, 2026-11-11):

  plan    list every session whose marker has no `staged_backfill` and that holds an
          n-back {pre}_concat_scan, skip the subjects Argo is running, and with
          --write store the list at config/first-level/staged-backfill/{label}/
          sessions.json. --sample N takes a seeded random N, for a pilot.
  submit  print, or with --write run, the `argo submit`. Refuses while any
          fmri-first-level-proc workflow is active: a session's marker is briefly
          absent mid-backfill, and a first-level run would read it as missing.
  verify  read S3 back: each planned session must be DONE (marker has
          staged_backfill, its staged runs are there at their recorded sizes, and no
          concatenation is left) or SET-ASIDE (left untouched, with a reason). Exit 1
          otherwise.

While a backfill runs, do not run the first-level queue manager.

Dry run by default; every --write is logged to data/reprocess/staged_backfill_{label}.jsonl.

  pixi run -e ops python scripts/backfill_staged_runs.py plan --label staged-pilot \\
      --data-bucket "$CLOUDPIPE_BUCKET" --sample 10
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / "images" / "fmri-first-level-proc"))

from reprocess_func import (  # noqa: E402
    ARGO_NAMESPACE,
    Audit,
    get_json,
    iso,
    now_utc,
    progress,
    require_same_account,
    running_workflows,
    workflow_subject,
)
from staged_backfill import MARKER, NBACK, PREFIX, listing, sessions_key, state_key  # noqa: E402

TEMPLATE = "staged-backfill"
FIRST_LEVEL_TEMPLATE = "fmri-first-level-proc"
# The template's spec.parallelism. Every shard starts at once and holds a pod (1 cpu /
# 4Gi) to the end, so the shard count IS the concurrency.
MAX_SHARDS = 400


def subjects(s3, bucket: str) -> list[str]:
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{PREFIX}/", Delimiter="/"
    ):
        out += [p["Prefix"][len(PREFIX) + 1 : -1] for p in page.get("CommonPrefixes", [])]
    return sorted(out)


def sessions_of(s3, bucket: str, subj: str) -> list[str]:
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"{PREFIX}/{subj}/", Delimiter="/")
    return sorted(p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", []))


def has_concat(objs: dict[str, int], subj: str, ses: str) -> bool:
    return any(f"{a}/{subj}_{ses}_nback_concat_scan.nii.gz" in objs for a in NBACK)


def wants_backfill(s3, bucket: str, subj: str, ses: str) -> bool:
    marker = get_json(s3, bucket, f"{PREFIX}/{subj}/{ses}/{MARKER}")
    if marker is None or "staged_backfill" in marker:
        return False
    return has_concat(listing(s3, bucket, f"{PREFIX}/{subj}/{ses}"), subj, ses)


def active_subjects() -> tuple[set[str], int]:
    """(subjects with an active workflow, active fmri-first-level-proc workflows)."""
    running = running_workflows()
    if running is None:
        raise SystemExit("refusing: cannot read Argo's running workflows")
    first_level = sum(
        1
        for wf in running
        if ((wf.get("metadata") or {}).get("labels") or {}).get(
            "workflows.argoproj.io/workflow-template"
        )
        == FIRST_LEVEL_TEMPLATE
    )
    return {workflow_subject(wf) for wf in running} - {None}, first_level


def plan(s3, args) -> int:
    busy, _ = active_subjects()
    subs = subjects(s3, args.data_bucket)

    def one(subj):
        return [
            (subj, ses)
            for ses in sessions_of(s3, args.data_bucket, subj)
            if wants_backfill(s3, args.data_bucket, subj, ses)
        ]

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        found = sorted(
            s for ss in progress(pool.map(one, subs), "subjects", len(subs), "subject") for s in ss
        )
    held = [s for s in found if s[0] in busy or s[0].removeprefix("sub-") in busy]
    planned = [s for s in found if s not in held]
    if args.sample:
        planned = sorted(random.Random(args.seed).sample(planned, min(args.sample, len(planned))))
    print(f"subjects scanned          {len(subs)}")
    print(f"sessions to backfill      {len(found)}")
    print(f"  held (subject running)  {len(held)}")
    print(f"  planned                 {len(planned)}")
    key = sessions_key(args.label)
    if not args.write:
        print(f"dry run: --write stores the list at s3://{args.data_bucket}/{key}")
        return 0
    if get_json(s3, args.data_bucket, key) is not None:
        raise SystemExit(f"refusing: s3://{args.data_bucket}/{key} exists; use a new --label")
    doc = {
        "label": args.label,
        "planned_at": iso(now_utc()),
        "sessions": [list(s) for s in planned],
    }
    s3.put_object(Bucket=args.data_bucket, Key=key, Body=json.dumps(doc).encode())
    audit(args).write("planned", key=key, sessions=len(planned), held=len(held), sample=args.sample)
    print(f"wrote s3://{args.data_bucket}/{key}")
    return 0


def submit(s3, args) -> int:
    key = sessions_key(args.label)
    doc = get_json(s3, args.data_bucket, key)
    if doc is None:
        raise SystemExit(f"no plan at s3://{args.data_bucket}/{key}; run plan --write first")
    if args.shards > MAX_SHARDS:
        raise SystemExit(
            f"refusing --shards {args.shards}: the template runs at most {MAX_SHARDS} at once"
        )
    shards = min(args.shards, len(doc["sessions"]))
    if shards < 1:
        raise SystemExit("the plan has no sessions")
    _, first_level = active_subjects()
    if first_level:
        raise SystemExit(f"refusing: {first_level} {FIRST_LEVEL_TEMPLATE} workflow(s) are active")
    cmd = [
        "argo", "-n", ARGO_NAMESPACE, "submit", "--from", f"workflowtemplate/{TEMPLATE}",
        "--generate-name", f"{TEMPLATE}-{args.label}-",
        "-p", f"label={args.label}", "-p", f"shards={shards}",
    ]  # fmt: skip
    n = len(doc["sessions"])
    print(f"{n} sessions in {shards} shards (~{-(-n // shards)} each)")
    print(" ".join(cmd))
    if not args.write:
        print("dry run: --write submits it")
        return 0
    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(proc.stdout or proc.stderr)
    audit(args).write(
        "submitted", key=key, shards=shards, rc=proc.returncode, out=proc.stdout[-500:]
    )
    return proc.returncode


def verdict(s3, bucket: str, label: str, subj: str, ses: str) -> tuple[str, str]:
    """(DONE | SET-ASIDE | PENDING | BROKEN, detail) for one planned session."""
    prefix = f"{PREFIX}/{subj}/{ses}"
    marker = get_json(s3, bucket, f"{prefix}/{MARKER}")
    state = get_json(s3, bucket, state_key(label, subj, ses)) or {}
    if marker is None:
        return "BROKEN", "no marker" + (
            " (state saved: rerun the shard to resume)" if "plan" in state else ""
        )
    if "staged_backfill" not in marker:
        if state.get("outcome") == "set-aside":
            return "SET-ASIDE", state.get("reason", "")
        return "PENDING", ""
    objs = listing(s3, bucket, prefix)
    for added in marker["staged_backfill"]["added"]:
        if objs.get(added["path"]) != added["bytes"]:
            return "BROKEN", f"{added['path']} missing or resized"
    if has_concat(objs, subj, ses) or any(
        r["path"] in objs for r in marker["staged_backfill"]["removed"]
    ):
        return "BROKEN", "a concatenation is still there"
    if marker.get("file_count") != sum(1 for r in objs if r not in (MARKER, "_links.json")):
        return "BROKEN", "file_count disagrees with the listing"
    return "DONE", ""


def verify(s3, args) -> int:
    key = sessions_key(args.label)
    doc = get_json(s3, args.data_bucket, key)
    if doc is None:
        raise SystemExit(f"no plan at s3://{args.data_bucket}/{key}")
    sessions = [tuple(s) for s in doc["sessions"]]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(
            progress(
                pool.map(lambda s: (s, verdict(s3, args.data_bucket, args.label, *s)), sessions),
                "sessions",
                len(sessions),
                "session",
            )
        )
    counts = Counter(v for _, (v, _) in results)
    for name in ("DONE", "SET-ASIDE", "PENDING", "BROKEN"):
        print(f"{name:10} {counts.get(name, 0)}")
    for (subj, ses), (v, detail) in results:
        if v != "DONE":
            print(f"  {v} {subj} {ses} {detail}")
    return 1 if counts.get("PENDING", 0) or counts.get("BROKEN", 0) else 0


def audit(args) -> Audit:
    return Audit(REPO / "data" / "reprocess" / f"staged_backfill_{args.label}.jsonl")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "submit", "verify"):
        c = sub.add_parser(name)
        c.add_argument("--label", required=True)
        c.add_argument("--data-bucket", required=True)
        c.add_argument("--workers", type=int, default=32)
        c.add_argument("--region", help="default: the AWS profile's")
        if name != "verify":
            c.add_argument("--write", action="store_true")
        if name == "plan":
            c.add_argument(
                "--sample", type=int, help="a seeded random sample of N sessions, for a pilot"
            )
            c.add_argument("--seed", type=int, default=20261111)
        if name == "submit":
            c.add_argument(
                "--shards", type=int, default=300, help="pods, all at once (the concurrency)"
            )
    args = p.parse_args(argv)
    if getattr(args, "write", False) or args.cmd == "plan":
        require_same_account()
    s3 = boto3.client("s3", region_name=args.region)
    return {"plan": plan, "submit": submit, "verify": verify}[args.cmd](s3, args)


if __name__ == "__main__":
    sys.exit(main())
