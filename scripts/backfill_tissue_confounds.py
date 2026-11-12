#!/usr/bin/env python3
"""
backfill_tissue_confounds.py — plan, submit, verify and finish the csf/white_matter backfill.

Published confounds TSVs predate the `csf` and `white_matter` columns
(add-tissue-confound-columns). The tissue-confounds-backfill WorkflowTemplate computes
them in-cluster from each run's published MNI BOLD (images/afni/
tissue_confounds_backfill.py), writes the TSV, and patches the run's FuncQC record
to schema 1.5 in place. This script is its only driver, and it never reads a BOLD.

  plan     every func session whose marker lists a run with a TSV, BOLD and brain
           mask. Each run's FuncQC record key comes from one index of
           metrics/func-preproc/. A run with no record, or more than one, is counted
           and left out, because the backfill must patch exactly one. Subjects Argo
           is running are held (re-plan them later). --write stores the list at
           config/tissue-backfill/{label}/sessions.json.
  submit   print, or with --write run, the `argo submit` in one mode: apply,
           verify (no BOLD reads), recompute (--sample N sessions, a separate list:
           the full computation, compared byte for byte, writing nothing) or
           restore.
  verify   read the verify-mode outcome lines: every planned run must have both
           columns and a 1.5 record consistent with them. Then, if a recompute
           ran, every sampled run must be `equal`. Exit 1 otherwise.
  finish   recompact every func_preproc dt= partition holding a patched record (the
           nightly compactor revisits only the last few days).

Dry run by default. Every --write is logged to data/reprocess/tissue_backfill_{label}.jsonl.

  pixi run -e ops python scripts/backfill_tissue_confounds.py plan --label tissue-1 \\
      --data-bucket "$CLOUDPIPE_BUCKET" --metrics-bucket "$CLOUDPIPE_METRICS_BUCKET" \\
      --subjects tools/some-subjects.csv
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / "src" / "metrics"))
sys.path.insert(0, str(REPO / "images" / "afni"))
sys.path.insert(0, str(REPO / "images" / "shared"))

from compactor import compact_prefix_dt  # noqa: E402
from reprocess_func import (  # noqa: E402
    ARGO_NAMESPACE,
    DT_IN_KEY_RE,
    Audit,
    get_json,
    iso,
    list_keys_parallel,
    now_utc,
    progress,
    read_subject_csv,
    require_same_account,
    running_workflows,
    workflow_subject,
)
from tissue_confounds_backfill import (  # noqa: E402
    MARKER,
    MODES,
    label_root,
    runs_of,
    sessions_key,
)

LABEL_PREFIX = "tissue-"
FUNC_PREFIX = "derivatives/func/"
QC_PREFIX = "metrics/func-preproc/"
TEMPLATE = "tissue-confounds-backfill"
#: The template's spec.parallelism; the shard count is the concurrency.
MAX_SHARDS = 100
SESSION_KEY = re.compile(r"^derivatives/func/(sub-[^/]+)/(ses-[^/]+)/(.+)$")


def subjects_with_func(s3, bucket: str) -> list[str]:
    subjects = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=FUNC_PREFIX, Delimiter="/"
    ):
        subjects += [p["Prefix"][len(FUNC_PREFIX) : -1] for p in page.get("CommonPrefixes", [])]
    return sorted(subjects)


def record_index(s3, bucket: str, workers: int) -> dict[str, list[str]]:
    """Record file name ({subj}_{ses}_{task}_{run}_qc.json) -> its keys, every dt."""
    out: dict[str, list[str]] = {}
    for key in list_keys_parallel(s3, bucket, [QC_PREFIX], workers, "records")[QC_PREFIX]:
        out.setdefault(key.rsplit("/", 1)[1], []).append(key)
    return out


def survey_subject(s3, bucket: str, subject: str, records: dict) -> tuple[list[dict], Counter]:
    """(the subject's planned sessions, counts)."""
    counts: Counter = Counter()
    sessions: dict[str, set[str]] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{FUNC_PREFIX}{subject}/"
    ):
        for o in page.get("Contents", []):
            if (m := SESSION_KEY.match(o["Key"])) and m[1] == subject:
                sessions.setdefault(m[2], set()).add(m[3])
    planned = []
    for ses, names in sorted(sessions.items()):
        if MARKER not in names:
            counts["sessions-without-marker"] += 1
            continue
        marker = get_json(s3, bucket, f"{FUNC_PREFIX}{subject}/{ses}/{MARKER}") or {}
        if marker.get("kind") != "func-session":
            counts["sessions-with-a-non-func-marker"] += 1
            continue
        keys = {}
        for run_id in runs_of(marker, subject, ses):
            found = records.get(f"{subject}_{ses}_{run_id}_qc.json", [])
            if len(found) != 1:
                counts["runs-no-record" if not found else "runs-duplicate-records"] += 1
                continue
            keys[run_id] = found[0]
        counts["sessions"] += 1
        counts["runs-planned"] += len(keys)
        if not set(marker.get("session_files") or []) & {
            f"{subject}_{ses}_space-MNI152NLin2009cAsym_res-1_label-{t}_mask.nii.gz"
            for t in ("WM", "CSF")
        }:
            counts["sessions-without-maps"] += 1
        if keys:
            planned.append({"subject": subject, "session": ses, "records": keys})
    return planned, counts


def plan(s3, args) -> int:
    subjects = (
        [
            s if s.startswith("sub-") else f"sub-{s}"
            for path in args.subjects
            for s in read_subject_csv(path)
        ]
        if args.subjects
        else subjects_with_func(s3, args.data_bucket)
    )
    running = running_workflows()
    if running is None:
        raise SystemExit("refusing: cannot read Argo's running workflows")
    busy = {workflow_subject(wf) for wf in running} - {None}
    records = record_index(s3, args.metrics_bucket, args.workers)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        found = list(
            progress(
                pool.map(lambda s: survey_subject(s3, args.data_bucket, s, records), subjects),
                "subjects", len(subjects), "subject",
            )
        )  # fmt: skip
    counts: Counter = Counter()
    planned, held = [], []
    for subject, (sessions, c) in zip(subjects, found, strict=True):
        counts += c
        (held if subject in busy else planned).extend(sessions)
    print(f"subjects scanned                {len(subjects)}")
    for name, n in sorted(counts.items()):
        print(f"  {name:30} {n}")
    print(f"held (subject running)          {len(held)} sessions")
    print(f"planned                         {len(planned)} sessions, "
          f"{sum(len(e['records']) for e in planned)} runs")  # fmt: skip
    return store(s3, args, args.label, planned, {"held": len(held), "counts": dict(counts)})


def store(s3, args, label: str, sessions: list[dict], extra: dict) -> int:
    key = sessions_key(label)
    if not args.write:
        print(f"dry run: --write stores the list at s3://{args.data_bucket}/{key}")
        return 0
    if get_json(s3, args.data_bucket, key) is not None:
        raise SystemExit(f"refusing: s3://{args.data_bucket}/{key} exists; use a new --label")
    if not sessions:
        raise SystemExit("nothing to plan")
    doc = {"label": label, "planned_at": iso(now_utc()), "sessions": sessions, **extra}
    s3.put_object(Bucket=args.data_bucket, Key=key, Body=json.dumps(doc).encode())
    audit(args).write("planned", key=key, sessions=len(sessions), **extra)
    print(f"wrote s3://{args.data_bucket}/{key}")
    return 0


def sample_label(label: str) -> str:
    return f"{label}-recompute"


def submit(s3, args) -> int:
    doc = get_json(s3, args.data_bucket, sessions_key(args.label))
    if doc is None:
        raise SystemExit(f"no plan for {args.label}; run plan --write first")
    if args.shards > MAX_SHARDS:
        raise SystemExit(
            f"refusing --shards {args.shards}: the shard count is the concurrency, "
            f"and the template runs at most {MAX_SHARDS} at once"
        )
    label = args.label
    if args.mode == "recompute":
        # A separate, seeded list of whole sessions, so recompute reads a sample's BOLD only.
        label = sample_label(args.label)
        if get_json(s3, args.data_bucket, sessions_key(label)) is None:
            pick = random.Random(args.seed).sample(
                doc["sessions"], min(args.sample, len(doc["sessions"]))
            )
            rc = store(s3, args, label, pick, {"sample_of": args.label, "seed": args.seed})
            if rc or not args.write:
                return rc
        doc = get_json(s3, args.data_bucket, sessions_key(label))
    n = len(doc["sessions"])
    shards = min(args.shards, n)
    cmd = [
        "argo", "-n", ARGO_NAMESPACE, "submit", "--from", f"workflowtemplate/{TEMPLATE}",
        "--generate-name", f"{TEMPLATE}-{label}-{args.mode}-",
        "-p", f"sessions-key={sessions_key(label)}", "-p", f"shards={shards}",
        "-p", f"mode={args.mode}",
    ]  # fmt: skip
    print(f"{n} sessions in {shards} shards (~{-(-n // shards)} each), mode {args.mode}")
    print(" ".join(cmd))
    if not args.write:
        print("dry run: --write submits it")
        return 0
    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(proc.stdout or proc.stderr)
    audit(args).write(
        "submitted", label=label, mode=args.mode, shards=shards, rc=proc.returncode,
        out=proc.stdout[-500:],
    )  # fmt: skip
    return proc.returncode


def outcome_lines(s3, bucket: str, label: str, mode: str) -> list[dict]:
    prefix = f"{label_root(label)}/{mode}/"
    lines = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            body = s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read().decode()
            lines += [json.loads(line) for line in body.splitlines() if line.strip()]
    return sorted(lines, key=lambda line: line.get("at", ""))


def latest(lines: list[dict]) -> dict[tuple, dict]:
    out: dict[tuple, dict] = {}
    for line in lines:
        out[(line["subject"], line["session"], line.get("run", "-"))] = line
    return out


def verdict(line: dict | None) -> str:
    if line is None:
        return "UNVERIFIED"
    if line.get("status") == "no-marker":
        return "PENDING"
    if line.get("tsv") == "present" and line.get("record") == "current":
        return "OK"
    if line.get("tsv") in ("absent",) or line.get("record") == "stale":
        return "PENDING"
    return "BAD"


def verify(s3, args) -> int:
    doc = get_json(s3, args.data_bucket, sessions_key(args.label))
    if doc is None:
        raise SystemExit(f"no plan for {args.label}")
    applied = Counter(
        f"{line.get('tsv')}/{line.get('record')}"
        for line in outcome_lines(s3, args.data_bucket, args.label, "apply")
        if "tsv" in line
    )
    statuses = Counter(
        line.get("tissue_confounds")
        for line in outcome_lines(s3, args.data_bucket, args.label, "apply")
        if line.get("tsv") in ("rewritten", "already")
    )
    seen = latest(outcome_lines(s3, args.data_bucket, args.label, "verify"))
    status: dict[tuple, str] = {}
    for entry in doc["sessions"]:
        for run_id in entry["records"]:
            unit = (entry["subject"], entry["session"], run_id)
            status[unit] = verdict(seen.get(unit) or seen.get((*unit[:2], "-")))
    print(
        "apply outcomes: " + (", ".join(f"{k} {v}" for k, v in sorted(applied.items())) or "none")
    )
    print(
        "tissue_confounds: "
        + (", ".join(f"{k} {v}" for k, v in sorted(statuses.items(), key=str)) or "none")
    )
    counts = Counter(status.values())
    for name in ("OK", "PENDING", "UNVERIFIED", "BAD"):
        print(f"{name:11} {counts.get(name, 0)}")
    shown = 0
    for unit, v in sorted(status.items()):
        if v != "OK" and shown < 50:
            line = seen.get(unit, {})
            print(f"  {v} {' '.join(unit)} tsv={line.get('tsv')} record={line.get('record')}")
            shown += 1
    ok = set(counts) <= {"OK"}
    sampled = outcome_lines(s3, args.data_bucket, sample_label(args.label), "recompute")
    if sampled:
        rc = Counter(line.get("tsv") for line in latest(sampled).values() if "tsv" in line)
        print("recompute: " + ", ".join(f"{k} {v}" for k, v in sorted(rc.items())))
        ok = ok and set(rc) <= {"equal"}
    return 0 if ok else 1


def finish(s3, args) -> int:
    doc = get_json(s3, args.data_bucket, sessions_key(args.label))
    if doc is None:
        raise SystemExit(f"no plan for {args.label}")
    keys = {
        (e["subject"], e["session"], run_id): key
        for e in doc["sessions"]
        for run_id, key in e["records"].items()
    }
    dts = {
        DT_IN_KEY_RE.search(keys[unit])[1]
        for unit, line in latest(outcome_lines(s3, args.data_bucket, args.label, "apply")).items()
        if line.get("record") == "patched" and unit in keys
    }
    print(f"func_preproc dt= partitions holding patched records: {len(dts)}")
    if not args.write:
        print("dry run: --write recompacts those partitions")
        return 0
    region = args.region or s3.meta.region_name
    for dt in sorted(dts):
        result = compact_prefix_dt(s3, args.metrics_bucket, region, "func_preproc", dt)
        audit(args).write("compacted", dt=dt, versions=result)
        print(f"  compacted dt={dt}: {result}")
    return 0


def audit(args) -> Audit:
    return Audit(REPO / "data" / "reprocess" / f"tissue_backfill_{args.label}.jsonl")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "submit", "verify", "finish"):
        c = sub.add_parser(name)
        c.add_argument("--label", required=True, help=f"starts with {LABEL_PREFIX!r}")
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
            c.add_argument("--mode", choices=MODES, required=True)
            c.add_argument(
                "--shards", type=int, default=50, help="pods, all at once (the concurrency)"
            )
            c.add_argument("--sample", type=int, default=20, help="recompute: sessions to sample")
            c.add_argument(
                "--seed", type=int, default=20261108, help="recompute: the sample's seed"
            )
    args = p.parse_args(argv)
    if not args.label.startswith(LABEL_PREFIX):
        raise SystemExit(f"--label must start with {LABEL_PREFIX!r}")
    if getattr(args, "write", False) or args.cmd == "plan":
        require_same_account()
    s3 = boto3.client("s3", region_name=args.region)
    return {"plan": plan, "submit": submit, "verify": verify, "finish": finish}[args.cmd](s3, args)


if __name__ == "__main__":
    sys.exit(main())
