#!/usr/bin/env python3
"""
rerun_confounds.py — plan, submit, verify and finish the confounds-only re-run.

Every confounds TSV published under FuncQC 1.3 has the old CompCor and cosine
columns; 1.4 (2026-10-27) computes them as nipype does for fMRIPrep. The MNI BOLD,
dtseries, tissue maps and IQMs are unchanged, so only the TSVs are redone, by the
confounds-rerun WorkflowTemplate: it re-ingests a subject's func inputs and runs
images/afni/confounds_rerun.py, which rewrites each planned run's TSV in place and
then patches its FuncQC record to 1.4. This script is its only driver:

  plan    find every published run whose FuncQC record is 1.3, hold the subjects
          Argo is running (re-plan them later), and with --write store one plan
          per subject at config/confounds-rerun/{label}/subjects/{subj}.json, the
          subject list beside it, and plan.json (written last). --compare plans
          the runs that are ALREADY 1.4 instead: their pods recompute the TSV and
          compare it with the published one, writing nothing (the smoke proof that
          the re-run reproduces a full run byte for byte).
  submit  print, or with --write create, the cloudpipe-queue-manager flow run
          (workflow_template=confounds-rerun, ingress https) for the subject list.
  verify  read S3 back. A planned run is OK when its record is 1.4 and its TSV is
          the object the pod wrote (same ETag), or, failing that, when the TSV's
          content passes audit_func_outputs.check_confounds (nipype cosine count,
          zero on the NSS rows). Content, never timestamps: a func workflow on
          the 1.4 image may have republished the run since. Exit 1 otherwise.
  finish  recompact every metrics dt= partition holding a patched record (the
          nightly compactor revisits only the last few days), and delete staged
          input a failed workflow left under staging/confounds-rerun/.

A run whose record is older than 1.3 (or has no record, or two) is reported, not
planned: its BOLD predates the func reprocess, which is a different repair.

Dry run by default; every --write is logged to data/reprocess/confounds_rerun_{label}.jsonl.

  pixi run -e ops python scripts/rerun_confounds.py plan --label confounds-1 \\
      --data-bucket "$CLOUDPIPE_BUCKET" --metrics-bucket "$CLOUDPIPE_METRICS_BUCKET"
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
import pandas as pd
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "metrics"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / "images" / "afni"))

from audit_func_outputs import check_confounds  # noqa: E402
from compactor import compact_prefix_dt  # noqa: E402
from confounds_rerun import FAILING, MARKER, outcomes_key, plan_key  # noqa: E402
from reprocess_func import (  # noqa: E402
    ABSENT_CODES,
    DT_IN_KEY_RE,
    Audit,
    check_prefect_target,
    create_flow_run,
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

LABEL_PREFIX = "confounds-"
FUNC_PREFIX = "derivatives/func/"
QC_PREFIX = "metrics/func-preproc/"
NSS_KEY = "config/nss_volumes.csv"
#: Equals the confounds-rerun template's https-transfer dest-base-path.
STAGING_BASE = "staging/confounds-rerun"
#: The FuncQC version the re-run writes, and the one it repairs.
DONE = "1.4"
REPAIRABLE = "1.3"
TSV_SUFFIX = "_desc-confounds_timeseries.tsv"


def label_root(label: str) -> str:
    return f"config/confounds-rerun/{label}"


def summary_key(label: str) -> str:
    return f"{label_root(label)}/plan.json"


def subjects_key(label: str) -> str:
    return f"{label_root(label)}/subjects.csv"


def subjects_with_func(s3, bucket: str) -> list[str]:
    subjects = []
    pages = s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=FUNC_PREFIX, Delimiter="/"
    )
    for page in pages:
        subjects += [p["Prefix"][len(FUNC_PREFIX) : -1] for p in page.get("CommonPrefixes", [])]
    return sorted(subjects)


def subject_listing(s3, bucket: str, subject: str) -> dict[str, str]:
    """Key -> ETag for every object under the subject's func prefix."""
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{FUNC_PREFIX}{subject}/"
    ):
        out.update({o["Key"]: o["ETag"] for o in page.get("Contents", [])})
    return out


def record_index(s3, bucket: str, workers: int) -> dict[str, list[str]]:
    """Record file name ({subj}_{ses}_{task}_{run}_qc.json) -> its keys, every dt."""
    out: dict[str, list[str]] = {}
    for key in list_keys_parallel(s3, bucket, [QC_PREFIX], workers, "records")[QC_PREFIX]:
        out.setdefault(key.rsplit("/", 1)[1], []).append(key)
    return out


def nss_table(s3, bucket: str) -> dict[tuple[str, str], int]:
    body = s3.get_object(Bucket=bucket, Key=NSS_KEY)["Body"].read().decode()
    return {
        (row["subject_id"], row["session"]): int(row["nss_frames"])
        for row in csv.DictReader(io.StringIO(body))
        if row.get("nss_frames")
    }


def plan_subject(s3, args, subject: str, records: dict, nss: dict) -> tuple[dict, Counter]:
    """(this subject's plan sessions, a Counter of every run's classification)."""
    counts: Counter = Counter()
    sessions: dict = {}
    listing = subject_listing(s3, args.data_bucket, subject)
    markers = sorted(k for k in listing if k.endswith(f"/{MARKER}"))
    legacy = {k.split("/")[3] for k in listing if k.endswith("_bold.tar.gz")}
    for key in markers:
        ses = key.split("/")[3]
        legacy.discard(ses)
        marker = get_json(s3, args.data_bucket, key)
        if not marker or marker.get("kind") != "func-session":
            counts["session-without-func-marker"] += 1
            continue
        runs = {}
        for run_id, files in sorted((marker.get("runs") or {}).items()):
            name = f"{subject}_{ses}_{run_id}"
            if f"{name}{TSV_SUFFIX}" not in files:
                counts["no-tsv"] += 1
                continue
            keys = records.get(f"{name}_qc.json", [])
            if len(keys) != 1:
                counts["no-record" if not keys else "duplicate-records"] += 1
                continue
            record = get_json(s3, args.metrics_bucket, keys[0]) or {}
            version = record.get("schema_version")
            want = DONE if args.compare else REPAIRABLE
            if version != want:
                counts["done" if version == DONE else f"schema-{version}"] += 1
                continue
            if (subject, ses) not in nss:
                counts["no-nss"] += 1
                continue
            if record.get("n_nss_frames") != nss[(subject, ses)]:
                counts["nss-mismatch"] += 1
                continue
            runs[run_id] = {"record_key": keys[0]}
            counts["planned"] += 1
        if runs:
            sessions[ses] = {
                "nss_frames": nss[(subject, ses)],
                "marker_written_at": marker.get("written_at"),
                "runs": runs,
            }
    counts["legacy-tarball-session"] += len(legacy)
    return sessions, counts


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
    records = record_index(s3, args.metrics_bucket, args.workers)
    nss = nss_table(s3, args.data_bucket)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        found = list(
            progress(
                pool.map(lambda s: plan_subject(s3, args, s, records, nss), subjects),
                "subjects",
                len(subjects),
                "subject",
            )
        )
    counts: Counter = Counter()
    plans, held = {}, []
    for subject, (sessions, c) in zip(subjects, found, strict=True):
        counts += c
        if sessions and subject in busy:
            held.append(subject)
        elif sessions:
            plans[subject] = sessions
    n_runs = sum(len(s["runs"]) for ses in plans.values() for s in ses.values())
    print(f"subjects scanned          {len(subjects)}")
    for name, n in sorted(counts.items()):
        print(f"  runs/sessions {name:24} {n}")
    print(f"held (subject running)    {len(held)} subjects")
    print(f"planned                   {n_runs} runs across {len(plans)} subjects")
    mode = "compare" if args.compare else "rewrite"
    if not args.write:
        print(
            f"dry run ({mode}): --write stores the plan under s3://{args.data_bucket}/{label_root(args.label)}/"
        )
        return 0
    if get_json(s3, args.data_bucket, summary_key(args.label)) is not None:
        raise SystemExit(f"refusing: {summary_key(args.label)} exists; use a new --label")
    if not plans:
        raise SystemExit("nothing to plan")
    planned_at = iso(now_utc())

    def put(subject):
        doc = {
            "label": args.label,
            "planned_at": planned_at,
            "subject": subject,
            "mode": mode,
            "sessions": plans[subject],
        }
        s3.put_object(
            Bucket=args.data_bucket,
            Key=plan_key(args.label, subject),
            Body=json.dumps(doc).encode(),
        )

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(progress(pool.map(put, sorted(plans)), "plans", len(plans), "subject"))
    csv_body = "subject_id\n" + "".join(f"{s}\n" for s in sorted(plans))
    s3.put_object(Bucket=args.data_bucket, Key=subjects_key(args.label), Body=csv_body.encode())
    summary = {
        "label": args.label,
        "planned_at": planned_at,
        "mode": mode,
        "subjects": len(plans),
        "runs": n_runs,
        "held": held,
        "counts": dict(counts),
    }
    # LAST: plan.json is what says the plan is complete.
    s3.put_object(
        Bucket=args.data_bucket, Key=summary_key(args.label), Body=json.dumps(summary).encode()
    )
    audit(args).write("planned", subjects=len(plans), runs=n_runs, held=len(held), mode=mode)
    print(f"wrote s3://{args.data_bucket}/{label_root(args.label)}/")
    return 0


def submit(s3, args) -> int:
    summary = load_summary(s3, args)
    uri = f"s3://{args.data_bucket}/{subjects_key(args.label)}"
    parameters = {
        "subjects_file": uri,
        "batch_label": args.label,
        "workflow_template": "confounds-rerun",
        "ingress_mode": "https",
        # Both are fixed in the template; sent so the flow's own log says so.
        "globus_scan_types": '["rest","nback"]',
        "globus_dest_base_path": f"/{STAGING_BASE}",
    }
    print(f"{summary['subjects']} subjects, {summary['runs']} runs ({summary['mode']})")
    print(json.dumps(parameters, indent=2))
    if not args.write:
        print("dry run: --write creates the flow run")
        return 0
    check_prefect_target()
    flow_run = create_flow_run(parameters, idempotency_key=f"{args.label}:submit")
    audit(args).write("submitted", flow_run_id=str(flow_run.id), flow_run_name=flow_run.name)
    print(f"Submitted flow run {flow_run.name} ({flow_run.id}) for {uri}")
    return 0


def latest_outcomes(s3, bucket: str, label: str, subject: str) -> dict[tuple[str, str], dict]:
    """(session, run) -> the last outcome line the subject's pods wrote for it."""
    try:
        body = s3.get_object(Bucket=bucket, Key=outcomes_key(label, subject))["Body"].read()
    except ClientError as exc:
        # No pod has written for this subject yet. Anything else (an expired
        # token) must not read as "no outcomes", which verify reports as PENDING.
        if exc.response.get("Error", {}).get("Code") in ABSENT_CODES:
            return {}
        raise
    out = {}
    for line in body.decode().splitlines():
        if line.strip():
            o = json.loads(line)
            if "run" in o:
                out[(o["session"], o["run"])] = o
    return out


def content_ok(s3, bucket: str, key: str, tr: float, nss: int) -> tuple[bool, list[str]]:
    """audit_func_outputs.check_confounds on the published TSV: (ok, failed check names)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "confounds.tsv"
        s3.download_file(bucket, key, str(path))
        conf = pd.read_csv(path, sep="\t", na_values="n/a")
    failed = [c.name for c in check_confounds(conf, len(conf), tr, nss) if not c.ok]
    return not failed, failed


# A compare outcome -> verify's status. EQUIV passes: same table, every column
# within confounds_rerun.COMPARE_TOLERANCE once sign flips are allowed.
COMPARE_STATUS = {"identical": "OK", "equivalent": "EQUIV", "differs": "DIFFERS", None: "PENDING"}


def describe(line: dict) -> str:
    """One compare outcome's difference and host, for verify's listing."""
    diff = line.get("difference")
    host = line.get("host") or {}
    where = f"{host.get('openblas_core', '?')} / {host.get('cpu', '?')}"
    if not isinstance(diff, dict):
        return f"{diff} on {where}"
    if not diff.get("same_shape"):
        return f"shape differs {diff} on {where}"
    return (
        f"max_rel_diff {diff['max_rel_diff']:.2e} ({diff['worst_column']}), "
        f"{diff['columns_differing']} cols differ, {diff['n_sign_flipped']} sign-flipped, "
        f"nan_mismatch {diff['nan_mismatch']} on {where}"
    )


def verify_subject(s3, args, subject: str, mode: str, sample: set) -> list[tuple]:
    doc = get_json(s3, args.data_bucket, plan_key(args.label, subject)) or {"sessions": {}}
    outcomes = latest_outcomes(s3, args.data_bucket, args.label, subject)
    listing = subject_listing(s3, args.data_bucket, subject)
    results = []
    for ses, spec in sorted(doc["sessions"].items()):
        for run_id, run_spec in sorted(spec["runs"].items()):
            unit = (subject, ses, run_id)
            outcome = outcomes.get((ses, run_id), {}).get("outcome")
            if mode == "compare":
                status = COMPARE_STATUS.get(outcome, "RETRY")
                line = outcomes.get((ses, run_id), {})
                detail = describe(line) if status in ("EQUIV", "DIFFERS") else outcome
                results.append((unit, status, detail))
                continue
            record = get_json(s3, args.metrics_bucket, run_spec["record_key"]) or {}
            if record.get("schema_version") != DONE:
                status = "PENDING" if outcome is None else "RETRY"
                results.append((unit, status, outcome or "no outcome yet"))
                continue
            tsv = f"{FUNC_PREFIX}{subject}/{ses}/{subject}_{ses}_{run_id}{TSV_SUFFIX}"
            ours = outcomes.get((ses, run_id), {}).get("tsv_etag")
            if listing.get(tsv) is None:
                results.append((unit, "BAD", "TSV missing"))
                continue
            if ours == listing[tsv] and unit not in sample:
                results.append((unit, "OK", outcome))
                continue
            ok, failed = content_ok(
                s3, args.data_bucket, tsv, float(record["tr_seconds"]), spec["nss_frames"]
            )
            results.append((unit, "OK" if ok else "BAD", "content" if ok else failed))
    if has_staging(s3, args.data_bucket, subject):
        results.append(((subject, "-", "-"), "STAGED", "input left under staging"))
    return results


def has_staging(s3, bucket: str, subject: str) -> bool:
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"{STAGING_BASE}/{subject}/", MaxKeys=1)
    return bool(resp.get("Contents"))


def verify(s3, args) -> int:
    summary = load_summary(s3, args)
    subjects = read_csv_key(s3, args.data_bucket, subjects_key(args.label))
    # A seeded sample of runs to content-check even when their ETag matches.
    rng = random.Random(args.seed)
    sample: set = set()
    if args.sample:
        candidates = []
        for subject in rng.sample(subjects, min(len(subjects), args.sample)):
            doc = get_json(s3, args.data_bucket, plan_key(args.label, subject)) or {"sessions": {}}
            for ses, spec in doc["sessions"].items():
                candidates += [(subject, ses, r) for r in spec["runs"]]
        sample = set(rng.sample(candidates, min(len(candidates), args.sample)))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        found = list(
            progress(
                pool.map(lambda s: verify_subject(s3, args, s, summary["mode"], sample), subjects),
                "subjects",
                len(subjects),
                "subject",
            )
        )
    results = [r for rs in found for r in rs]
    counts = Counter(status for _, status, _ in results)
    for name in ("OK", "EQUIV", "PENDING", "RETRY", "BAD", "DIFFERS", "STAGED"):
        print(f"{name:8} {counts.get(name, 0)}")
    retry_reasons = Counter(str(d) for _, s, d in results if s == "RETRY")
    for reason, n in retry_reasons.most_common():
        print(f"  RETRY {reason}: {n}{' (fails the pod)' if reason in FAILING else ''}")
    for unit, status, detail in results:
        if status not in ("OK", "RETRY"):
            print(f"  {status} {' '.join(unit)} {detail}")
    if sample:
        print(f"content-checked a sample of {len(sample)} runs (seed {args.seed})")
    return 0 if set(counts) <= {"OK", "EQUIV"} else 1


def finish(s3, args) -> int:
    load_summary(s3, args)
    subjects = read_csv_key(s3, args.data_bucket, subjects_key(args.label))
    dts: set[str] = set()
    for subject in subjects:
        doc = get_json(s3, args.data_bucket, plan_key(args.label, subject)) or {"sessions": {}}
        outcomes = latest_outcomes(s3, args.data_bucket, args.label, subject)
        for ses, spec in doc["sessions"].items():
            for run_id, run_spec in spec["runs"].items():
                if outcomes.get((ses, run_id), {}).get("outcome") == "rewritten":
                    dts.add(DT_IN_KEY_RE.search(run_spec["record_key"])[1])
    running = running_workflows()
    if running is None:
        raise SystemExit("refusing: cannot read Argo's running workflows")
    busy = {workflow_subject(wf) for wf in running} - {None}
    staged = [s for s in subjects if s not in busy and has_staging(s3, args.data_bucket, s)]
    print(f"func_preproc dt= partitions holding patched records: {len(dts)}")
    print(f"subjects with staged input left (none running): {len(staged)}")
    if not args.write:
        print("dry run: --write recompacts those partitions and deletes that input")
        return 0
    region = args.region or s3.meta.region_name
    for dt in sorted(dts):
        result = compact_prefix_dt(s3, args.metrics_bucket, region, "func_preproc", dt)
        audit(args).write("compacted", dt=dt, versions=result)
        print(f"  compacted dt={dt}: {result}")
    for subject in staged:
        keys = [
            o["Key"]
            for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=args.data_bucket, Prefix=f"{STAGING_BASE}/{subject}/"
            )
            for o in page.get("Contents", [])
        ]
        for i in range(0, len(keys), 1000):
            s3.delete_objects(
                Bucket=args.data_bucket,
                Delete={"Objects": [{"Key": k} for k in keys[i : i + 1000]]},
            )
        audit(args).write("deleted-staging", subject=subject, objects=len(keys))
    return 0


def load_summary(s3, args) -> dict:
    summary = get_json(s3, args.data_bucket, summary_key(args.label))
    if summary is None:
        raise SystemExit(f"no plan at s3://{args.data_bucket}/{summary_key(args.label)}")
    return summary


def read_csv_key(s3, bucket: str, key: str) -> list[str]:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    return [row["subject_id"] for row in csv.DictReader(io.StringIO(body))]


def audit(args) -> Audit:
    return Audit(REPO / "data" / "reprocess" / f"confounds_rerun_{args.label}.jsonl")


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
            c.add_argument(
                "--compare",
                action="store_true",
                help="plan the runs already at 1.4, to compare with, not rewrite",
            )
        if name == "verify":
            c.add_argument("--sample", type=int, default=0, help="also content-check N runs")
            c.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)
    if not args.label.startswith(LABEL_PREFIX):
        raise SystemExit(f"--label must start with {LABEL_PREFIX!r}")
    if getattr(args, "write", False) or args.cmd == "plan":
        require_same_account()
    s3 = boto3.client("s3", region_name=args.region)
    return {"plan": plan, "submit": submit, "verify": verify, "finish": finish}[args.cmd](s3, args)


if __name__ == "__main__":
    sys.exit(main())
