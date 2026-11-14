#!/usr/bin/env python3
"""
backfill_conn_dedup.py — plan, submit and verify the removal of nback_conn's shared copies.

First-level trees written before fmri-first-level-proc 4.0.0+cp.12 keep the files
nback_act and nback_conn share twice: the PSC runs, their means, the run-intersection mask
and the resampled atlas (~0.6 GB a session). cp.12 keeps them once, in nback_act/. The
conn-dedup WorkflowTemplate (images/fmri-first-level-proc/conn_dedup.py) brings older
trees to that layout, deleting each copy only once it is shown to be nback_act's data.
This script is its only driver (operator, 2026-11-13):

  plan    list every session whose nback_conn/ still holds a shared file that nback_act/
          also holds, and whose marker records no `deduplicated`; skip subjects Argo is
          running; with --write store the list at config/first-level/conn-dedup/{label}/
          sessions.json. --sample N takes a seeded random N, for a pilot.
  submit  print, or with --write run, the `argo submit`. Refuses while any
          fmri-first-level-proc workflow is active (a marker is briefly absent).
  verify  read S3 back: each planned session must be DONE (marker records conn_dedup,
          every removed copy is gone, its nback_act counterpart is there, and file_count
          matches the listing) or SET-ASIDE with a reason. Exit 1 otherwise.

Do not run the first-level queue manager while this runs.

  pixi run -e ops python scripts/backfill_conn_dedup.py plan --label conn-pilot \\
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

from backfill_staged_runs import (  # noqa: E402
    FIRST_LEVEL_TEMPLATE,
    MAX_SHARDS,
    active_subjects,
    sessions_of,
    subjects,
)
from conn_dedup import ACT, CONN, SHARED, sessions_key, state_key  # noqa: E402
from reprocess_func import (  # noqa: E402
    ARGO_NAMESPACE,
    Audit,
    get_json,
    iso,
    now_utc,
    progress,
    require_same_account,
)
from staged_backfill import MARKER, PREFIX, listing  # noqa: E402

TEMPLATE = "conn-dedup"


def shared_copies(objs: dict[str, int], subj: str, ses: str) -> list[str]:
    pre = f"{subj}_{ses}_nback"
    return sorted(
        r.split("/", 1)[1]
        for r in objs
        if r.startswith(f"{CONN}/")
        and (m := SHARED.match(r.split("/", 1)[1]))
        and m["pre"] == pre
        and f"{ACT}/{r.split('/', 1)[1]}" in objs
    )


def wants_dedup(s3, bucket: str, subj: str, ses: str) -> bool:
    marker = get_json(s3, bucket, f"{PREFIX}/{subj}/{ses}/{MARKER}")
    if (
        marker is None
        or "conn_dedup" in marker
        or "deduplicated" in (marker.get("analyses", {}).get(CONN) or {})
    ):
        return False
    return bool(shared_copies(listing(s3, bucket, f"{PREFIX}/{subj}/{ses}"), subj, ses))


def plan(s3, args) -> int:
    busy, _ = active_subjects()
    subs = subjects(s3, args.data_bucket)

    def one(subj):
        return [
            (subj, ses)
            for ses in sessions_of(s3, args.data_bucket, subj)
            if wants_dedup(s3, args.data_bucket, subj, ses)
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
    print(f"sessions with copies      {len(found)}")
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
            " (state saved: rerun the shard to resume)" if "same" in state else ""
        )
    record = marker.get("conn_dedup")
    if record is None:
        if state.get("outcome") == "set-aside":
            return "SET-ASIDE", state.get("reason", "")
        return "PENDING", ""
    objs = listing(s3, bucket, prefix)
    for r in record["removed"]:
        if r["path"] in objs:
            return "BROKEN", f"{r['path']} still there"
        if f"{ACT}/{r['path'].split('/', 1)[1]}" not in objs:
            return "BROKEN", f"nback_act's {r['path'].split('/', 1)[1]} is missing"
    if marker.get("file_count") != sum(1 for r in objs if r not in (MARKER, "_links.json")):
        return "BROKEN", "file_count disagrees with the listing"
    return "DONE", f"kept {len(record['kept'])}" if record["kept"] else ""


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
        if v != "DONE" or detail:
            print(f"  {v} {subj} {ses} {detail}")
    return 1 if counts.get("PENDING", 0) or counts.get("BROKEN", 0) else 0


def audit(args) -> Audit:
    return Audit(REPO / "data" / "reprocess" / f"conn_dedup_{args.label}.jsonl")


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
            c.add_argument("--seed", type=int, default=20261113)
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
