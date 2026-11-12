#!/usr/bin/env python3
"""
backfill_dvars.py — plan, submit and verify the dvars_mean1000 backfill (#1016).

Confounds TSVs published before `dvars_mean1000` existed lack it. The column is
computed from the TSV's own `dvars` and `global_signal` (images/shared/
confounds_dvars.py), so the dvars-backfill WorkflowTemplate adds it in place,
in-cluster (src/dvars_backfill.py). This script is its only driver, and it never
reads a TSV: only S3 listings and the func `_complete.json` markers.

  plan    list every func session whose marker lists a confounds TSV, hold the
          subjects Argo is running (re-plan them later), count the legacy
          per-run tarballs the backfill does not touch, and with --write store
          the list at config/dvars-backfill/{label}/sessions.json
  submit  print, or with --write run, the `argo submit` for that list, in one of
          the pod's modes: apply (the backfill), verify (re-read every planned
          TSV and recompute the column; writes nothing), restore (put the
          originals back)
  verify  read the verify-mode outcome lines: every planned run must be
          `current`. Exit 1 otherwise. Run `submit --mode verify --write` first.

A TSV written by the #1027 image carries `dvars_median1000` instead; the pod
replaces it (`replaced-median`), and verify counts a run that still has it as
PENDING (`retired`), like one with no column at all.

The plan cannot tell a done TSV from an undone one without reading it, and
reading it is the pod's job, so every session is planned and the pods answer
`already` for the done ones. That also covers a func workflow still on an image
from before the column: it republishes a TSV without the column after the
backfill passed, verify reports that run as PENDING (`absent`), and a plan
under a new label picks it up. No time cutoff can do this, because such a
workflow writes after any pin.

Legacy tarballs: a run published before 2026-10-14 sits in one
`{prefix}_space-MNI152NLin2009cAsym_bold.tar.gz` with no marker entry. Those runs
are counted and reported, never planned: the confounds-only re-run skipped them
too (it plans from markers), so their CompCor is pre-1.4 and the right repair is
a func reprocess, which writes the column natively. See the PR for #1016.

Dry run by default; every --write is logged to data/reprocess/dvars_backfill_{label}.jsonl.

  pixi run -e ops python scripts/backfill_dvars.py plan --label dvars-1 \\
      --data-bucket "$CLOUDPIPE_BUCKET"
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
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "images" / "shared"))

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

from dvars_backfill import (  # noqa: E402
    MARKER,
    MODES,
    label_root,
    sessions_key,
    tsv_names,
)

LABEL_PREFIX = "dvars-"
FUNC_PREFIX = "derivatives/func/"
TEMPLATE = "dvars-backfill"
# The template's spec.parallelism. Every shard starts at once and holds a pod to
# the end, so the shard count IS the concurrency (probseg-backfill's lesson:
# queued shards behind a lock stalled; see that template's header).
MAX_SHARDS = 100
SESSION_KEY = re.compile(r"^derivatives/func/(sub-[^/]+)/(ses-[^/]+)/(.+)$")
LEGACY = re.compile(r"^(sub-[^_]+)_(ses-[^_]+)_(task-[^_]+_run-[^_]+)_space-MNI152NLin2009cAsym_bold\.tar\.gz$")  # fmt: skip


def subjects_with_func(s3, bucket: str) -> list[str]:
    subjects = []
    pages = s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=FUNC_PREFIX, Delimiter="/"
    )
    for page in pages:
        subjects += [p["Prefix"][len(FUNC_PREFIX) : -1] for p in page.get("CommonPrefixes", [])]
    return sorted(subjects)


def session_files(s3, bucket: str, subject: str) -> dict[str, set[str]]:
    """session -> the file names directly in its prefix, from one listing."""
    out: dict[str, set[str]] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{FUNC_PREFIX}{subject}/"
    ):
        for o in page.get("Contents", []):
            if (m := SESSION_KEY.match(o["Key"])) and m[1] == subject:
                out.setdefault(m[2], set()).add(m[3])
    return out


def survey_subject(s3, bucket: str, subject: str) -> tuple[list[str], Counter]:
    """(the sessions to plan, the layout counts) for one subject."""
    counts: Counter = Counter()
    planned = []
    for ses, names in sorted(session_files(s3, bucket, subject).items()):
        marker = (
            get_json(s3, bucket, f"{FUNC_PREFIX}{subject}/{ses}/{MARKER}")
            if MARKER in names
            else None
        )
        marked = marker if (marker or {}).get("kind") == "func-session" else None
        listed = set((marked or {}).get("runs") or {})
        legacy = {m[3] for n in names if (m := LEGACY.match(n))} - listed
        if marked:
            tsvs = tsv_names(marked, subject, ses)
            counts["exploded-sessions"] += 1
            counts["exploded-runs-with-tsv"] += len(tsvs)
            counts["exploded-runs-without-tsv"] += len(listed) - len(tsvs)
            if tsvs:
                planned.append(ses)
        elif MARKER in names:
            counts["sessions-with-a-non-func-marker"] += 1
        if legacy:
            counts["legacy-runs"] += len(legacy)
            counts["legacy-sessions-mixed" if marked else "legacy-sessions-no-marker"] += 1
        if not marked and not legacy:
            counts["sessions-with-neither"] += 1
    return planned, counts


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

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        found = list(
            progress(
                pool.map(lambda s: survey_subject(s3, args.data_bucket, s), subjects),
                "subjects",
                len(subjects),
                "subject",
            )
        )
    counts: Counter = Counter()
    planned, held = [], []
    for subject, (sessions, c) in zip(subjects, found, strict=True):
        counts += c
        (held if subject in busy else planned).extend([subject, ses] for ses in sessions)
    print(f"subjects scanned                {len(subjects)}")
    for name, n in sorted(counts.items()):
        print(f"  {name:30} {n}")
    print(f"held (subject running)          {len(held)} sessions")
    print(f"planned                         {len(planned)} sessions")
    doc = {
        "label": args.label,
        "planned_at": iso(now_utc()),
        "sessions": planned,
        "held": len(held),
        "counts": dict(counts),
    }
    key = sessions_key(args.label)
    if not args.write:
        print(f"dry run: --write stores the list at s3://{args.data_bucket}/{key}")
        return 0
    if get_json(s3, args.data_bucket, key) is not None:
        raise SystemExit(f"refusing: s3://{args.data_bucket}/{key} exists; use a new --label")
    if not planned:
        raise SystemExit("nothing to plan")
    s3.put_object(Bucket=args.data_bucket, Key=key, Body=json.dumps(doc).encode())
    audit(args).write(
        "planned", key=key, sessions=len(planned), held=len(held), counts=dict(counts)
    )
    print(f"wrote s3://{args.data_bucket}/{key}")
    return 0


def submit(s3, args) -> int:
    key = sessions_key(args.label)
    doc = get_json(s3, args.data_bucket, key)
    if doc is None:
        raise SystemExit(f"no plan at s3://{args.data_bucket}/{key}; run plan --write first")
    if args.shards > MAX_SHARDS:
        raise SystemExit(
            f"refusing --shards {args.shards}: the shard count is the concurrency, "
            f"and the template runs at most {MAX_SHARDS} at once"
        )
    n = len(doc["sessions"])
    shards = min(args.shards, n)
    if shards < 1:
        raise SystemExit("the plan has no sessions")
    cmd = [
        "argo", "-n", ARGO_NAMESPACE, "submit", "--from", f"workflowtemplate/{TEMPLATE}",
        "--generate-name", f"{TEMPLATE}-{args.label}-{args.mode}-",
        "-p", f"sessions-key={key}", "-p", f"shards={shards}", "-p", f"mode={args.mode}",
    ]  # fmt: skip
    print(f"{n} sessions in {shards} shards (~{-(-n // shards)} each), mode {args.mode}")
    print(" ".join(cmd))
    if not args.write:
        print("dry run: --write submits it")
        return 0
    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(proc.stdout or proc.stderr)
    audit(args).write(
        "submitted", key=key, mode=args.mode, shards=shards, rc=proc.returncode,
        out=proc.stdout[-500:],
    )  # fmt: skip
    return proc.returncode


def outcome_lines(s3, bucket: str, label: str, mode: str) -> list[dict]:
    """Every outcome line every shard of `mode` wrote, in the order they were written."""
    prefix = f"{label_root(label)}/{mode}/"
    lines = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            body = s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read().decode()
            lines += [json.loads(line) for line in body.splitlines() if line.strip()]
    return sorted(lines, key=lambda line: line.get("at", ""))


# A verify-mode status -> verify's verdict. PENDING wants a new plan (the TSV
# was republished without the column, or the session is markerless mid-publish);
# BAD wants a look.
VERDICT = {
    "current": "OK",
    "absent": "PENDING",
    "retired": "PENDING",
    "no-marker": "PENDING",
    "raced": "PENDING",
    "mismatch": "BAD",
    "bad-tsv": "BAD",
    "missing": "BAD",
}


def verify(s3, args) -> int:
    key = sessions_key(args.label)
    doc = get_json(s3, args.data_bucket, key)
    if doc is None:
        raise SystemExit(f"no plan at s3://{args.data_bucket}/{key}")
    applied = Counter(
        line["status"]
        for line in outcome_lines(s3, args.data_bucket, args.label, "apply")
        if "status" in line
    )
    latest: dict[tuple, dict] = {}
    for line in outcome_lines(s3, args.data_bucket, args.label, "verify"):
        latest[(line["subject"], line["session"], line.get("run", "-"))] = line
    by_session: dict[tuple, list] = {}
    for (subj, ses, run), line in latest.items():
        by_session.setdefault((subj, ses), []).append((run, line))
    status: dict[tuple, str] = {}
    for subj, ses in doc["sessions"]:
        lines = by_session.get((subj, ses))
        if not lines:
            status[(subj, ses, "-")] = "UNVERIFIED"
            continue
        # The newest line says whether the session had a marker at the last look.
        _, newest = max(lines, key=lambda r: r[1].get("at", ""))
        if newest["status"] == "no-marker":
            status[(subj, ses, "-")] = VERDICT["no-marker"]
            continue
        for run, line in lines:
            if run != "-":
                status[(subj, ses, run)] = VERDICT.get(line["status"], "BAD")
    print(
        "apply outcomes: " + (", ".join(f"{k} {v}" for k, v in sorted(applied.items())) or "none")
    )
    counts = Counter(status.values())
    for name in ("OK", "PENDING", "UNVERIFIED", "BAD"):
        print(f"{name:11} {counts.get(name, 0)}")
    for unit, verdict in sorted(status.items()):
        if verdict != "OK":
            print(f"  {verdict} {' '.join(unit)} {latest.get(unit, {}).get('status', '')}")
    return 0 if set(counts) <= {"OK"} else 1


def audit(args) -> Audit:
    return Audit(REPO / "data" / "reprocess" / f"dvars_backfill_{args.label}.jsonl")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "submit", "verify"):
        c = sub.add_parser(name)
        c.add_argument("--label", required=True, help=f"starts with {LABEL_PREFIX!r}")
        c.add_argument("--data-bucket", required=True)
        c.add_argument("--workers", type=int, default=32)
        c.add_argument("--region", help="default: the AWS profile's")
        if name != "verify":
            c.add_argument("--write", action="store_true")
        if name == "plan":
            c.add_argument(
                "--subjects", nargs="+", help="CSV(s) with subject_id; default: every subject"
            )
        if name == "submit":
            c.add_argument("--mode", choices=MODES, required=True)
            c.add_argument(
                "--shards", type=int, default=50, help="pods, all at once (the concurrency)"
            )
    args = p.parse_args(argv)
    if not args.label.startswith(LABEL_PREFIX):
        raise SystemExit(f"--label must start with {LABEL_PREFIX!r}")
    if getattr(args, "write", False) or args.cmd == "plan":
        require_same_account()
    s3 = boto3.client("s3", region_name=args.region)
    return {"plan": plan, "submit": submit, "verify": verify}[args.cmd](s3, args)


if __name__ == "__main__":
    sys.exit(main())
