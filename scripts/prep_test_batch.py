"""
prep_test_batch.py — Flush S3 derivatives and subject-keyed metrics for a
cloudpipe test batch run, then check cluster and Globus instance health.

Uses boto3 directly (one process, one pooled S3 client) instead of spawning
the aws CLI per object. The previous shell version spawned ~10 `aws`
processes per subject (~0.9s startup each) and rescanned every shared metrics
prefix once per subject. This version pays boto3 startup once, lists each
metrics prefix a single time, and bulk-deletes 1000 keys per delete_objects
call — collapsing both the per-process overhead and the redundant scanning.

Usage:
  pixi run python scripts/prep_test_batch.py <subjects_csv> [--dry-run]
  pixi run python scripts/prep_test_batch.py tools/cloudpipe_test_sample.csv --dry-run
  pixi run python scripts/prep_test_batch.py tools/cloudpipe_test_sample.csv
"""

import argparse
import csv
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import boto3

# Subject-keyed derivative prefixes — one subject maps to exactly one prefix.
#
# This list must cover EVERY derivative prefix the pipeline writes, because the
# inventory step skips any step whose output already exists. A prefix omitted
# here leaves stale outputs in place, so the step it belongs to is silently
# marked `skipped` / `failure_category: dependency` on a run the operator
# believes is a full reprocess. That is how surface-resample sat out the
# 2026-07-31 batch entirely: func_surf/ was missing from this list and held
# surfaces from a batch eight days earlier.
DERIV_PREFIX_TEMPLATES = [
    "derivatives/fastsurfer/{subj}/",
    "derivatives/registration/{subj}/",
    "derivatives/func/{subj}/",
    "derivatives/func_surf/{subj}/",
    "derivatives/subregions/{subj}/",
    "derivatives/subregions_mni/{subj}/",
]

# Shared metrics prefixes — subject id is embedded in object keys, so each
# prefix is listed once and filtered locally against the subject set.
#
# Only workflow-keyed prefixes are flushed. Their keys embed the Argo workflow
# name, which is unique per run, so records ACCUMULATE across batches rather
# than overwriting — and validate_test_batch.py reads step_outcomes with no
# recency window, so a stale failure from a prior batch would fail validation
# spuriously.
#
# The per-scan QC prefixes (func-preproc, registration, anat-qc, fsqc-qc) are
# deliberately NOT flushed. Their keys are {subject}_{session}_{task}_{run}-shaped
# with no workflow name, so a rerun overwrites each record in place; deleting them
# first gains nothing and destroys the only copy of the motion/tSNR/registration QC
# that the pipeline will ever produce for that run. (cloudpipe-metrics IS versioned
# — that was the point of the bucket split — so such a delete is recoverable from
# delete markers, but only by manual enumeration, so this stays opt-in.) See
# QC_PREFIXES / --flush-qc below for the fresh-start case, and
# docs/observability.md.
#
# fsqc-qc is named above explicitly so its absence reads as a decision rather
# than the oversight that kept func_surf/ out of DERIV_PREFIX_TEMPLATES. It
# belongs in this category and not in METRIC_PREFIXES: recomputation IS the fsqc
# step's idempotency mechanism (there is deliberately no metrics-exist gate on
# it — see the openspec change add-fsqc-anatomical-qc), so a rerun on the same
# dt overwrites the record in place and one on a later dt is covered by the
# completed_at scoping described above, exactly as for anat-qc. Its QC images
# under fsqc/{subj}/{ses}/ and the summary page at fsqc/{subj}/fsqc-results.html
# are dt-less and always overwrite, so they need no flush either.
#
# Known gap, resolved by the metrics-bucket split: a run processed by an earlier
# batch but not the current one leaves a stale QC record behind. Scope analyses
# by completed_at until the run-of-record prefix exists.
#
# metrics/costs/ is deliberately NOT in this list: cost object keys are
# `{date}_{workflow_name}_cost_allocation.json` (CostAllocation.s3_key) and
# contain no subject id, so a subject substring match never fires. It is flushed
# by flush_costs() below, which resolves workflow name -> subject explicitly.
METRIC_PREFIXES = [
    "metrics/step-outcomes/",
    "metrics/workflow-runs/",
    "metrics/subject-manifests/",
]

# Per-scan QC prefixes, flushed ONLY under --flush-qc. Keys are
# `{subject}_{session}_...`, so they are subject-scoped by substring match
# exactly like METRIC_PREFIXES; they are held separately because deleting them
# is a different decision, not a different mechanism.
#
# The reasoning quoted above ("the bucket is unversioned, so those deletes were
# unrecoverable") no longer holds: cloudpipe-metrics has versioning ENABLED, so
# a delete here places a delete marker and is recoverable. That is what makes an
# opt-in flag defensible where a silent default would not be. Recoverable is not
# free, though — restoring means enumerating delete markers — so the default
# still spares these, and only a full fresh-start batch passes --flush-qc.
QC_PREFIXES = [
    "metrics/func-preproc/",
    "metrics/registration/",
    "metrics/anat-qc/",
    "metrics/fsqc-qc/",
    "metrics/surface-sample/",
]

# Workflow-keyed metric prefixes, flushed ONLY under --flush-qc. Like
# metrics/costs/, these keys carry the Argo workflow name and NO subject id, so
# a subject substring match never fires and they need the workflow -> subject
# index (see flush_workflow_keyed).
#
# The two differ in orphan recoverability. A pod-costs object carries a
# `subject` field, so an orphan whose run summary is gone can still be resolved
# by reading it. A workflow-starts object is `{"first_step_started_at": ...}` and
# nothing else — there is no subject anywhere in the key or the body, so an
# orphaned one is unresolvable by construction and is reported, not guessed at.
WORKFLOW_KEYED_PREFIXES = {
    "metrics/pod-costs/": "_pod_costs.json",
    "metrics/workflow-starts/": ".json",
}

# Compacted Parquet aggregates, flushed wholesale under --flush-qc. Each file
# packs many subjects' records into one object, so there is no subject-scoped
# delete available: it is all or nothing. Deleting all of it is safe because the
# compactor rebuilds it from the raw per-record prefixes, which is also why it
# must be flushed at all — a stale aggregate would otherwise keep serving the
# `*_compacted` Athena tables after the raw records behind it were deleted.
COMPACTED_PREFIX = "metrics/compacted/"

COST_PREFIX = "metrics/costs/"
COST_SUFFIX = "_cost_allocation.json"
# Leading date prefix on cost keys: "YYYY-MM-DD_". Anchored, so it only matches a
# real date at the stem start, never a hyphenated workflow name.
COST_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_")
WORKFLOW_RUN_PREFIX = "metrics/workflow-runs/"
WORKFLOW_RUN_SUFFIX = "_run_summary.json"
# Leading Hive partition segment on every metrics key since the date-partitioning
# change (issue #65): "dt=YYYY-MM-DD/". Anchored to the start of the stem so it
# only strips a real partition folder, never a hyphenated filename component.
DT_PARTITION_RE = re.compile(r"^dt=\d{4}-\d{2}-\d{2}/")


def _strip_dt_partition(stem: str) -> str:
    """Drop a leading `dt=YYYY-MM-DD/` segment, if present, from a key stem."""
    return DT_PARTITION_RE.sub("", stem, count=1)


def read_subjects(path: str) -> list[str]:
    """Read subject IDs from the first column of a CSV, skipping the header."""
    subjects: list[str] = []
    with open(path, newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # skip header
        for row in reader:
            if not row:
                continue
            subj = row[0].strip().strip('"')
            if subj:
                subjects.append(subj)
    return subjects


def list_keys(s3, bucket: str, prefix: str) -> list[str]:
    """Return every object key under a prefix (auto-paginated)."""
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
    return keys


def delete_keys(s3, bucket: str, keys: list[str], dry_run: bool) -> int:
    """Bulk-delete keys (1000 per delete_objects call). Returns count."""
    if not keys or dry_run:
        return len(keys)
    for i in range(0, len(keys), 1000):
        chunk = keys[i : i + 1000]
        s3.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
        )
    return len(keys)


def flush_derivatives(s3, bucket, subjects, dry_run, workers) -> int:
    """Delete every subject-keyed derivative prefix per subject."""

    def collect(subj: str) -> tuple[str, list[str]]:
        keys: list[str] = []
        for tmpl in DERIV_PREFIX_TEMPLATES:
            keys += list_keys(s3, bucket, tmpl.format(subj=subj))
        return subj, keys

    total = 0
    tag = "dry-run" if dry_run else "done"
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for subj, keys in pool.map(collect, subjects):
            n = delete_keys(s3, bucket, keys, dry_run)
            total += n
            print(f"  [{tag}] {subj}: {n} derivative objects")
    return total


def flush_metrics(s3, bucket, subjects, dry_run, workers, prefixes=None) -> int:
    """List each shared metrics prefix once; delete objects matching any subject.

    `prefixes` defaults to METRIC_PREFIXES; --flush-qc passes QC_PREFIXES through
    the same matcher, since those keys are subject-scoped by the same rule.
    """
    prefixes = METRIC_PREFIXES if prefixes is None else prefixes
    pattern = re.compile("|".join(re.escape(s) for s in subjects))

    def scan(prefix: str) -> tuple[str, list[str]]:
        matched = [k for k in list_keys(s3, bucket, prefix) if pattern.search(k)]
        return prefix, matched

    total = 0
    tag = "dry-run" if dry_run else "done"
    with ThreadPoolExecutor(max_workers=min(workers, len(prefixes))) as pool:
        results = list(pool.map(scan, prefixes))
    for prefix, keys in results:
        n = delete_keys(s3, bucket, keys, dry_run)
        total += n
        print(f"  [{tag}] {prefix}: {n} objects")
    return total


def workflow_name_from_cost_key(key: str) -> str | None:
    """`metrics/costs/dt={date}/{date}_{wf}_cost_allocation.json` -> `{wf}` (None if malformed).

    Handles both the current dated key and the legacy undated
    `{wf}_cost_allocation.json`, which still exists in buckets written before the
    date prefix was added. Argo workflow names are DNS-1123 labels and cannot
    contain `_`, so a leading `YYYY-MM-DD_` prefix is unambiguous to strip.
    """
    if not key.startswith(COST_PREFIX) or not key.endswith(COST_SUFFIX):
        return None
    stem = _strip_dt_partition(key[len(COST_PREFIX) : -len(COST_SUFFIX)])
    if COST_DATE_RE.match(stem):
        stem = stem[11:]  # drop "YYYY-MM-DD_"
    return stem or None


def workflow_subject_from_run_key(key: str) -> tuple[str, str] | None:
    """`metrics/workflow-runs/dt={date}/{wf}__{subj}_run_summary.json` -> (wf, subj).

    Argo workflow names are DNS-1123 labels, so they can never contain `__`;
    splitting on the last `__` is unambiguous.
    """
    if not key.startswith(WORKFLOW_RUN_PREFIX) or not key.endswith(WORKFLOW_RUN_SUFFIX):
        return None
    stem = _strip_dt_partition(key[len(WORKFLOW_RUN_PREFIX) : -len(WORKFLOW_RUN_SUFFIX)])
    wf, sep, subj = stem.rpartition("__")
    if not sep or not wf or not subj:
        return None
    return wf, subj


def read_cost_subject(s3, bucket: str, key: str) -> str | None:
    """GET a metrics object and return its `subject` field (None if unreadable).

    Handles both record shapes on this bucket: a cost allocation is a JSON
    object, while a pod-costs object is a JSON *array* of per-pod records. A
    bare `.get("subject")` raises AttributeError on the latter, which the
    except-clause would swallow into a None — silently sparing every pod-costs
    orphan. Unwrap the array to its first element instead.
    """
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        doc = json.loads(body)
        if isinstance(doc, list):
            doc = doc[0] if doc else {}
        subj = doc.get("subject") if isinstance(doc, dict) else None
    except Exception:
        return None
    return subj if isinstance(subj, str) and subj else None


def build_workflow_subject_index(s3, bucket: str) -> dict[str, str]:
    """Build `workflow_name -> subject` from `metrics/workflow-runs/` key names.

    LIST only, no object reads. Shared by every workflow-keyed flush, and all of
    them must run BEFORE flush_metrics(), which deletes the objects this reads.
    """
    index: dict[str, str] = {}
    for key in list_keys(s3, bucket, WORKFLOW_RUN_PREFIX):
        parsed = workflow_subject_from_run_key(key)
        if parsed:
            index[parsed[0]] = parsed[1]
    return index


def workflow_name_from_key(key: str, prefix: str, suffix: str) -> str | None:
    """`{prefix}dt={date}/[{date}_]{wf}{suffix}` -> `{wf}` (None if malformed).

    Generalises workflow_name_from_cost_key over the workflow-keyed prefixes:
    pod-costs keys carry the `{date}_` stem prefix, workflow-starts keys do not.
    """
    if not key.startswith(prefix) or not key.endswith(suffix):
        return None
    stem = _strip_dt_partition(key[len(prefix) : -len(suffix)])
    if COST_DATE_RE.match(stem):
        stem = stem[11:]  # drop "YYYY-MM-DD_"
    return stem or None


def flush_workflow_keyed(s3, bucket, subjects, dry_run, workers, wf_to_subject) -> int:
    """Delete workflow-keyed metric records belonging to the batch subjects.

    Same two-pass resolution as flush_costs (index first, then read the `subject`
    field out of leftovers), because these keys carry a workflow name and no
    subject. Objects that resolve to no subject at all are left in place and
    counted — a workflow-starts orphan has no subject anywhere to read, so
    deleting it would mean guessing.
    """
    subject_set = set(subjects)
    total = 0
    tag = "dry-run" if dry_run else "done"

    for prefix, suffix in WORKFLOW_KEYED_PREFIXES.items():
        matched: list[str] = []
        unresolved: list[str] = []
        for key in list_keys(s3, bucket, prefix):
            wf = workflow_name_from_key(key, prefix, suffix)
            if wf is None:
                continue
            subj = wf_to_subject.get(wf)
            if subj is None:
                unresolved.append(key)
            elif subj in subject_set:
                matched.append(key)

        n_from_index = len(matched)
        if unresolved:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                subs = list(pool.map(lambda k: read_cost_subject(s3, bucket, k), unresolved))
            matched += [k for k, s in zip(unresolved, subs, strict=True) if s in subject_set]
            n_unresolvable = sum(1 for s in subs if s is None)
        else:
            n_unresolvable = 0

        n = delete_keys(s3, bucket, matched, dry_run)
        total += n
        print(
            f"  [{tag}] {prefix}: {n} objects "
            f"({n_from_index} via workflow-runs index, "
            f"{n - n_from_index} via object read of {len(unresolved)} orphans)"
        )
        if n_unresolvable:
            print(
                f"    [note] {n_unresolvable} orphan(s) carry no subject and were left "
                f"in place (no way to attribute them to a batch)"
            )
        if dry_run:
            for key in matched:
                print(f"    [dry-run] would delete {key}")
    return total


def flush_compacted(s3, bucket, dry_run) -> int:
    """Delete ALL compacted Parquet aggregates.

    Not subject-scoped, and cannot be: one Parquet file packs many subjects'
    records, so there is no per-subject delete. Safe wholesale because the
    compactor rebuilds these from the raw per-record prefixes on its next run;
    leaving them would keep the `*_compacted` Athena tables serving rows whose
    underlying raw records this flush just deleted.
    """
    keys = list_keys(s3, bucket, COMPACTED_PREFIX)
    n = delete_keys(s3, bucket, keys, dry_run)
    tag = "dry-run" if dry_run else "done"
    print(f"  [{tag}] {COMPACTED_PREFIX}: {n} objects (all subjects — aggregates are not scopable)")
    return n


def flush_costs(s3, bucket, subjects, dry_run, workers) -> int:
    """Delete cost records belonging to the batch subjects.

    Cost keys are workflow-keyed, not subject-keyed, so the subject has to be
    resolved for every cost object. Two passes, cheapest first:

      1. List `metrics/workflow-runs/` once (LIST only, no GETs) and build a
         workflow_name -> subject map from the key names. This resolves every
         cost record whose sibling run-summary still exists — the normal case.
      2. For cost objects left unresolved (orphans whose run summary was
         already flushed by an earlier batch), GET the object and read its
         `subject` field. Authoritative, and only paid for on the leftovers.

    Must run BEFORE flush_metrics(), which deletes the `metrics/workflow-runs/`
    objects that pass 1 depends on.
    """
    subject_set = set(subjects)

    wf_to_subject = build_workflow_subject_index(s3, bucket)

    matched: list[str] = []
    unresolved: list[str] = []
    for key in list_keys(s3, bucket, COST_PREFIX):
        wf = workflow_name_from_cost_key(key)
        if wf is None:
            continue
        subj = wf_to_subject.get(wf)
        if subj is None:
            unresolved.append(key)
        elif subj in subject_set:
            matched.append(key)

    n_from_index = len(matched)
    if unresolved:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            subs = pool.map(lambda k: read_cost_subject(s3, bucket, k), unresolved)
        # pool.map yields exactly one result per input, so the lengths always match.
        matched += [k for k, s in zip(unresolved, subs, strict=True) if s in subject_set]

    tag = "dry-run" if dry_run else "done"
    n = delete_keys(s3, bucket, matched, dry_run)
    print(
        f"  [{tag}] {COST_PREFIX}: {n} objects "
        f"({n_from_index} via workflow-runs index, "
        f"{n - n_from_index} via object read of {len(unresolved)} orphans)"
    )
    if dry_run:
        for key in matched:
            print(f"    [dry-run] would delete {key}")
    return n


def check_cluster() -> None:
    print("\n=== Cluster health ===")
    for cmd in (
        ["kubectl", "get", "nodes", "-L", "karpenter.sh/nodepool"],
        ["kubectl", "get", "applications", "-n", "argocd"],
    ):
        try:
            subprocess.run(cmd, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            print(f"[WARN] command failed (check VPN/cluster access): {' '.join(cmd)}")


def check_globus(region: str) -> None:
    print("\n=== Globus instance ===")
    try:
        iid = boto3.client("ssm", region_name=region).get_parameter(
            Name="/cloudpipe/globus/instance-id"
        )["Parameter"]["Value"]
    except Exception:
        print("[WARN] Globus instance ID not found in SSM (/cloudpipe/globus/instance-id)")
        return
    try:
        state = boto3.client("ec2", region_name=region).describe_instances(InstanceIds=[iid])[
            "Reservations"
        ][0]["Instances"][0]["State"]["Name"]
    except Exception:
        state = "unknown"
    print(f"Instance {iid}: {state}")
    if state != "running":
        print("[WARN] Globus instance is not running — it will be started by the first workflow.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("subjects_csv", help="Path to CSV with a subject_id column header.")
    p.add_argument(
        "--bucket", default="<YOUR_S3_BUCKET>", help="Data bucket — derivatives are flushed here."
    )
    # Required, never defaulted: the metrics bucket is the run of record, and a
    # defaulted value is how an operator deletes the wrong thing by reflex. The
    # bucket is versioned, so these deletes are recoverable, but naming it is
    # still a deliberate act.
    p.add_argument(
        "--metrics-bucket",
        required=True,
        help="Metrics bucket — workflow-keyed metric records are flushed here.",
    )
    p.add_argument("--region", default="<YOUR_AWS_REGION>")
    p.add_argument("--workers", type=int, default=20, help="Max concurrent list/delete threads.")
    p.add_argument("--dry-run", action="store_true", help="Print counts without deleting.")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    # Off by default. The per-scan QC prefixes are the pipeline's only record of
    # output quality, and for a one-subject rerun deleting them is pure loss: the
    # keys carry no workflow name, so the rerun overwrites each record in place.
    # A full fresh-start batch is the case where they SHOULD go, because
    # dashboards read the raw QC tables and stale rows from subjects processed by
    # an earlier batch otherwise survive and mix into every panel.
    p.add_argument(
        "--flush-qc",
        action="store_true",
        help=(
            "Also flush per-scan QC (func-preproc, registration, anat-qc, fsqc-qc, "
            "surface-sample), workflow-keyed pod-costs/workflow-starts, and ALL "
            "compacted Parquet. Use for a fresh-start batch; omit for a subject rerun."
        ),
    )
    args = p.parse_args()

    subjects = read_subjects(args.subjects_csv)
    if not subjects:
        print(f"Error: no subjects found in {args.subjects_csv}", file=sys.stderr)
        sys.exit(1)

    print("=== cloudpipe test batch prep ===")
    print(f"Subjects CSV : {args.subjects_csv}")
    print(f"Subject count: {len(subjects)}")
    print(f"Bucket       : s3://{args.bucket}")
    if args.dry_run:
        print("Mode         : DRY RUN (no changes will be made)")

    if args.flush_qc:
        print("Mode         : --flush-qc (per-scan QC + ALL compacted aggregates will be deleted)")

    if not args.dry_run and not args.yes:
        extra = (
            "\n  WARNING: --flush-qc also deletes per-scan QC for these subjects and"
            "\n  ALL compacted Parquet aggregates (every subject, not just this batch)."
            if args.flush_qc
            else ""
        )
        confirm = input(
            f"\nFlush derivatives and metrics for {len(subjects)} subjects "
            f"in s3://{args.bucket}?{extra} [y/N] "
        )
        if confirm.strip().lower() != "y":
            print("Aborted.")
            sys.exit(0)

    s3 = boto3.client("s3", region_name=args.region)

    key = "config/test_batch_subjects.csv"
    print(f"\nUploading subjects file to s3://{args.bucket}/{key} ...")
    if args.dry_run:
        print(f"  [dry-run] would upload {args.subjects_csv}")
    else:
        s3.upload_file(args.subjects_csv, args.bucket, key)
        print("  Done.")

    print("\nFlushing per-subject derivatives ...")
    d = flush_derivatives(s3, args.bucket, subjects, args.dry_run, args.workers)
    # Metrics now live in their own bucket. Costs first: resolving workflow name
    # -> subject reads metrics/workflow-runs/, which flush_metrics is about to
    # delete.
    print("\nFlushing workflow-keyed cost records ...")
    c = flush_costs(s3, args.metrics_bucket, subjects, args.dry_run, args.workers)

    # Also before flush_metrics, and for the same reason: these resolve subjects
    # through the workflow-runs index that flush_metrics deletes.
    wk = 0
    if args.flush_qc:
        print("\nFlushing workflow-keyed pod-costs and workflow-starts ...")
        wk = flush_workflow_keyed(
            s3,
            args.metrics_bucket,
            subjects,
            args.dry_run,
            args.workers,
            build_workflow_subject_index(s3, args.metrics_bucket),
        )

    print("\nFlushing shared metrics ...")
    m = flush_metrics(s3, args.metrics_bucket, subjects, args.dry_run, args.workers)

    q = 0
    if args.flush_qc:
        print("\nFlushing per-scan QC (--flush-qc) ...")
        q = flush_metrics(
            s3, args.metrics_bucket, subjects, args.dry_run, args.workers, prefixes=QC_PREFIXES
        )
        print("\nFlushing compacted aggregates (--flush-qc) ...")
        q += flush_compacted(s3, args.metrics_bucket, args.dry_run)

    verb = "would delete" if args.dry_run else "deleted"
    total_metrics = m + c + wk + q
    print(
        f"\nFlush complete: {verb} {d} derivative + {total_metrics} metric objects "
        f"(incl. {c} cost records) across {len(subjects)} subjects."
    )
    if args.flush_qc:
        print(f"  --flush-qc also removed {wk} workflow-keyed + {q} QC/compacted objects.")
    else:
        print("  Per-scan QC and compacted aggregates were PRESERVED (pass --flush-qc to remove).")

    check_cluster()
    check_globus(args.region)

    print("\n=== Prep complete. Ready to submit. ===")


if __name__ == "__main__":
    main()
