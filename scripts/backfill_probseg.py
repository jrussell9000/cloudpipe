#!/usr/bin/env python3
"""
backfill_probseg.py — plan, submit and verify the probseg backfill.

Every `label-{GM,WM,CSF}_probseg` published before the orig_nu fix was fitted to
brainmask.mgz's WM-normalised intensities, and about half name the wrong tissue
(images/afni/tissue_masks.py). The probseg-backfill WorkflowTemplate regenerates
them from inputs already in S3 (images/afni/probseg_backfill.py). This script is
its only driver:

  plan    list every session whose probseg is not FIXED, skip the subjects Argo
          is running (re-plan them later), and with --write store the list at
          config/probseg-backfill/{label}/sessions.json
  submit  print, or with --write run, the `argo submit` for that list
  verify  read S3 back: every planned session must now be FIXED, or REJECTED
          (the new fit failed the dseg check too, so the probseg was removed and
          a row says so). Exit 1 otherwise.

FIXED means a TissueQC row newer than every one of the session's probseg
objects: probseg_backfill.session_state, the one rule the pod, the plan and the
verify all use. There is no time cutoff, because none can work: a func workflow
submitted before the fix keeps its image and writes old-method maps after any
pin. Its maps carry no row, so they read as not fixed, and the next plan picks
them up. Run plan again after the func reprocess finishes, with a new label.

Dry run by default; every --write is logged to data/reprocess/probseg_backfill_{label}.jsonl.

  pixi run -e ops python scripts/backfill_probseg.py plan --label probseg-1 \\
      --data-bucket "$CLOUDPIPE_BUCKET" --metrics-bucket "$CLOUDPIPE_METRICS_BUCKET"
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / "images" / "afni"))

from probseg_backfill import MARKER, session_state, tissue_rows  # noqa: E402
from reprocess_func import (  # noqa: E402
    ARGO_NAMESPACE,
    Audit,
    get_json,
    iso,
    now_utc,
    progress,
    read_subject_csv,
    require_same_account,
    running_workflows,
    workflow_subject,
)

FUNC_PREFIX = "derivatives/func/"
TEMPLATE = "probseg-backfill"
# The template's spec.parallelism. Every shard starts at once and holds a pod
# (2 cpu / 4G) to the end, so the shard count IS the concurrency. Shards beyond
# this would queue, and queued shards are what stalled probseg-1 when a
# semaphore held them (see the template's header).
MAX_SHARDS = 100
SESSION_DIR = re.compile(r"^derivatives/func/(sub-[^/]+)/(ses-[^/]+)/(.+)$")
PROBSEG_SUFFIXES = tuple(
    f"_space-MNI152NLin2009cAsym_res-1_label-{t}_probseg.nii.gz" for t in ("GM", "WM", "CSF")
)


def sessions_key(label: str) -> str:
    return f"config/probseg-backfill/{label}/sessions.json"


def subjects_with_func(s3, bucket: str) -> list[str]:
    subjects = []
    pages = s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=FUNC_PREFIX, Delimiter="/"
    )
    for page in pages:
        subjects += [p["Prefix"][len(FUNC_PREFIX) : -1] for p in page.get("CommonPrefixes", [])]
    return sorted(subjects)


def subject_objects(s3, bucket: str, subject: str) -> list[dict]:
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{FUNC_PREFIX}{subject}/"
    ):
        out += page.get("Contents", [])
    return out


def session_stamps(objects: list[dict]) -> dict[tuple[str, str], dict[str, object]]:
    """(subject, session) -> {file name: LastModified} for one subject's listing."""
    out: dict[tuple[str, str], dict[str, object]] = {}
    for o in objects:
        if m := SESSION_DIR.match(o["Key"]):
            out.setdefault((m[1], m[2]), {})[m[3]] = o["LastModified"]
    return out


def state_of(subj: str, ses: str, stamps: dict, row_at, listed: bool | None = None) -> str:
    """session_state from a listing. `listed` defaults to "the GM probseg exists"."""
    names = [f"{subj}_{ses}{s}" for s in PROBSEG_SUFFIXES]
    probseg_at = [stamps.get(n) for n in names]
    if listed is None:
        listed = probseg_at[0] is not None
    return session_state(listed, probseg_at, row_at, stamps.get(MARKER))


def plan(s3, args) -> int:
    subjects = (
        [s for path in args.subjects for s in read_subject_csv(path)]
        if args.subjects
        else subjects_with_func(s3, args.data_bucket)
    )
    running = running_workflows()
    if running is None:
        raise SystemExit("refusing: cannot read Argo's running workflows")
    busy = {workflow_subject(wf) for wf in running} - {None}
    rows = tissue_rows(s3, args.metrics_bucket)

    def one(subject):
        found = []
        for (subj, ses), stamps in session_stamps(
            subject_objects(s3, args.data_bucket, subject)
        ).items():
            if subj == subject and state_of(subj, ses, stamps, rows.get((subj, ses))) == "stale":
                found.append((subj, ses))
        return found

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        found = list(progress(pool.map(one, subjects), "subjects", len(subjects), "subject"))
    stale = sorted(s for sessions in found for s in sessions)
    held = [s for s in stale if s[0] in busy]
    planned = [list(s) for s in stale if s[0] not in busy]
    print(f"subjects scanned          {len(subjects)}")
    print(f"sessions not fixed        {len(stale)}")
    print(f"  held (subject running)  {len(held)} across {len({s for s, _ in held})} subjects")
    print(f"  planned                 {len(planned)}")
    doc = {"label": args.label, "planned_at": iso(now_utc()), "sessions": planned}
    key = sessions_key(args.label)
    if not args.write:
        print(f"dry run: --write stores the list at s3://{args.data_bucket}/{key}")
        return 0
    if get_json(s3, args.data_bucket, key) is not None:
        raise SystemExit(f"refusing: s3://{args.data_bucket}/{key} exists; use a new --label")
    s3.put_object(Bucket=args.data_bucket, Key=key, Body=json.dumps(doc).encode())
    audit(args).write("planned", key=key, sessions=len(planned), held=len(held))
    print(f"wrote s3://{args.data_bucket}/{key}")
    return 0


def submit(s3, args) -> int:
    key = sessions_key(args.label)
    doc = get_json(s3, args.data_bucket, key)
    if doc is None:
        raise SystemExit(f"no plan at s3://{args.data_bucket}/{key}; run plan --write first")
    n = len(doc["sessions"])
    if args.shards > MAX_SHARDS:
        raise SystemExit(
            f"refusing --shards {args.shards}: the shard count is the concurrency, "
            f"and the template runs at most {MAX_SHARDS} at once"
        )
    shards = min(args.shards, n)
    if shards < 1:
        raise SystemExit("the plan has no sessions")
    cmd = [
        "argo", "-n", ARGO_NAMESPACE, "submit", "--from", f"workflowtemplate/{TEMPLATE}",
        "--generate-name", f"{TEMPLATE}-{args.label}-",
        "-p", f"sessions-key={key}", "-p", f"shards={shards}",
    ]  # fmt: skip
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


# session_state -> what verify reports for a planned session.
VERDICT = {"fixed": "OK", "rejected": "REJECTED", "stale": "PENDING", "no-probseg": "PENDING"}


def verify(s3, args) -> int:
    key = sessions_key(args.label)
    doc = get_json(s3, args.data_bucket, key)
    if doc is None:
        raise SystemExit(f"no plan at s3://{args.data_bucket}/{key}")
    rows = tissue_rows(s3, args.metrics_bucket)
    by_subject: dict[str, list[str]] = {}
    for subj, ses in doc["sessions"]:
        by_subject.setdefault(subj, []).append(ses)

    def one(subject):
        stamps = session_stamps(subject_objects(s3, args.data_bucket, subject))
        results = []
        for ses in by_subject[subject]:
            marker = get_json(s3, args.data_bucket, f"{FUNC_PREFIX}{subject}/{ses}/{MARKER}")
            names = {f"{subject}_{ses}{s}" for s in PROBSEG_SUFFIXES}
            listed = bool(names & set((marker or {}).get("session_files") or []))
            state = state_of(
                subject, ses, stamps.get((subject, ses), {}), rows.get((subject, ses)), listed
            )
            results.append(((subject, ses), VERDICT[state]))
        return results

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        found = list(
            progress(pool.map(one, sorted(by_subject)), "subjects", len(by_subject), "subject")
        )
    status = dict(r for results in found for r in results)
    counts = Counter(status.values())
    for name in ("OK", "REJECTED", "PENDING"):
        print(f"{name:9} {counts.get(name, 0)}")
    for unit, state in sorted(status.items()):
        if state == "PENDING":
            print(f"  PENDING {unit[0]} {unit[1]}")
    # REJECTED is a verdict, not a failure: the new fit disagreed with the dseg,
    # so the session ships without probseg rather than with a wrong one.
    return 1 if counts.get("PENDING", 0) else 0


def audit(args) -> Audit:
    return Audit(REPO / "data" / "reprocess" / f"probseg_backfill_{args.label}.jsonl")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "submit", "verify"):
        c = sub.add_parser(name)
        c.add_argument("--label", required=True)
        c.add_argument("--data-bucket", required=True)
        c.add_argument("--metrics-bucket", required=True)
        c.add_argument("--workers", type=int, default=32)
        c.add_argument("--region", help="default: the AWS profile's")
        if name != "verify":
            c.add_argument("--write", action="store_true")
        if name == "plan":
            c.add_argument(
                "--subjects", nargs="+", help="CSV(s) with subject_id; default: every subject"
            )
        if name == "submit":
            c.add_argument(
                "--shards", type=int, default=60, help="pods, all at once (the concurrency)"
            )
    args = p.parse_args(argv)
    if getattr(args, "write", False) or args.cmd == "plan":
        require_same_account()
    s3 = boto3.client("s3", region_name=args.region)
    return {"plan": plan, "submit": submit, "verify": verify}[args.cmd](s3, args)


if __name__ == "__main__":
    sys.exit(main())
