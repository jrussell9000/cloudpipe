"""
backfill_boldref.py — bring published `desc-boldref` objects to the steady-state
convention (#858) by copying each run's registration reference over them.

The afni image used to build `desc-boldref` from the NSS frames, whose contrast
depends on the scanner software. It now builds the steady-state temporal mean,
which is the image bold_to_t1w.py already publishes per run as
`derivatives/registration/{subj}/{ses}/bold_to_t1w_{task}_{run}/{prefix}_desc-bold2t1w_ref.nii.gz`.
The native BOLD of a finished subject is gone, so its reference cannot be rebuilt;
the registration copy is the same image, on the same grid, and is copied
server-side over the key the session marker already lists. The marker names files
only, so it needs no edit.

Run it only after the image with the fix is pinned, and only on subjects whose
func workflows have all finished: a workflow submitted before the pin keeps the old
image and would publish the old convention after this ran.

Per run, one outcome:

  copy                        the boldref differs from the registration ref (planned, or done with --write)
  already                     same ETag as the registration ref; nothing to do
  no-registration-ref         the run has no registration ref to copy
  registration-ref-too-old    the registration ref predates the steady-state reference (2026-06-27)
  registration-newer-than-func the ref was rewritten after the session's marker, so it may
                              not be the registration this func output was built from
  boldref-missing             the marker lists a boldref that is not in the bucket

Sessions with no func marker (pre-2026-10-14 tarball layout) are counted, not
touched; the func reprocess republishes them. Any run outcome other than copy and
already makes the exit status 1, so a run that needs a look is never silent.

Dry run unless --write. Writes a JSONL audit naming every copy, with the version it
replaced (the data bucket is versioned, so a copy can be undone).

Usage:
  pixi run -e ops python scripts/backfill_boldref.py --subjects <csv> [--subjects <csv> ...] \\
      --data-bucket "$CLOUDPIPE_BUCKET" [--write]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reprocess_func import (  # noqa: E402
    DEFAULT_AUDIT_DIR,
    Audit,
    get_json,
    now_utc,
    parse_instant,
    read_subject_csv,
    require_same_account,
    running_workflows,
    workflow_subject,
)

FUNC_PREFIX = "derivatives/func"
REG_PREFIX = "derivatives/registration"
MARKER = "_complete.json"
MARKER_KIND = "func-session"  # images/shared/func_derivatives.py KIND
BOLDREF_SUFFIX = "_desc-boldref.nii.gz"
REG_REF_SUFFIX = "_desc-bold2t1w_ref.nii.gz"

# bold_to_t1w.py has written the steady-state temporal mean since 0cda2914
# (2026-06-26). An older ref is some other image, so it is refused, not copied.
STEADY_STATE_REF_SINCE = datetime(2026, 6, 27, tzinfo=UTC)

OK_OUTCOMES = {"copy", "already"}


def list_with_etags(s3, bucket: str, prefix: str) -> dict[str, dict]:
    """Every object under a prefix, keyed by Key, as {LastModified, ETag}."""
    out: dict[str, dict] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            out[o["Key"]] = {"LastModified": parse_instant(o["LastModified"]), "ETag": o["ETag"]}
    return out


def plan_subject(s3, bucket: str, subject: str) -> list[dict]:
    """One record per run (or per markerless session), with its outcome."""
    func = list_with_etags(s3, bucket, f"{FUNC_PREFIX}/{subject}/")
    reg = list_with_etags(s3, bucket, f"{REG_PREFIX}/{subject}/")
    sessions = sorted({k.split("/")[3] for k in func if k.count("/") >= 4})
    records: list[dict] = []
    for ses in sessions:
        marker_key = f"{FUNC_PREFIX}/{subject}/{ses}/{MARKER}"
        marker = get_json(s3, bucket, marker_key) if marker_key in func else None
        if not marker or marker.get("kind") != MARKER_KIND:
            records.append({"subject": subject, "session": ses, "outcome": "legacy-session"})
            continue
        written_at = parse_instant(marker.get("written_at"))
        for rid, files in sorted((marker.get("runs") or {}).items()):
            boldref = next((f for f in files if f.endswith(BOLDREF_SUFFIX)), None)
            if boldref is None:
                continue
            prefix = boldref[: -len(BOLDREF_SUFFIX)]
            dst = f"{FUNC_PREFIX}/{subject}/{ses}/{boldref}"
            src = f"{REG_PREFIX}/{subject}/{ses}/bold_to_t1w_{rid}/{prefix}{REG_REF_SUFFIX}"
            rec = {"subject": subject, "session": ses, "run": rid, "src": src, "dst": dst}
            if dst not in func:
                rec["outcome"] = "boldref-missing"
            elif src not in reg:
                rec["outcome"] = "no-registration-ref"
            elif reg[src]["LastModified"] < STEADY_STATE_REF_SINCE:
                rec["outcome"] = "registration-ref-too-old"
            elif written_at is None or reg[src]["LastModified"] > written_at:
                rec["outcome"] = "registration-newer-than-func"
            elif reg[src]["ETag"] == func[dst]["ETag"]:
                rec["outcome"] = "already"
            else:
                rec["outcome"] = "copy"
                rec["old_etag"] = func[dst]["ETag"]
            records.append(rec)
    return records


def copy_run(s3, bucket: str, rec: dict) -> dict:
    """Copy the registration ref over the boldref; returns what to audit."""
    previous = s3.head_object(Bucket=bucket, Key=rec["dst"])
    resp = s3.copy_object(
        Bucket=bucket,
        Key=rec["dst"],
        CopySource={"Bucket": bucket, "Key": rec["src"]},
        MetadataDirective="COPY",
    )
    return {
        "src": rec["src"],
        "dst": rec["dst"],
        "old_etag": previous.get("ETag"),
        "replaced_version": previous.get("VersionId"),
        "new_version": resp.get("VersionId"),
    }


def refuse_running(subjects: list[str]) -> None:
    """Refuse while any of these subjects has a running workflow, or Argo is unreadable."""
    running = running_workflows()
    if running is None:
        raise SystemExit("refusing: could not list running Argo workflows")
    busy = sorted({workflow_subject(wf) for wf in running} & set(subjects))
    if busy:
        raise SystemExit(
            f"refusing: {len(busy)} subject(s) have a running workflow, e.g. {busy[0]}. "
            "Backfill a slice only after its workflows have finished."
        )


def main(argv: list[str] | None = None, s3=None, guards: bool = True) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--subjects",
        required=True,
        action="append",
        help="CSV with a `subject_id` header; repeatable.",
    )
    # Required, never defaulted — the same stance as reprocess_func.py.
    p.add_argument("--data-bucket", required=True)
    p.add_argument("--region")
    p.add_argument("--workers", type=int, default=32)
    p.add_argument("--audit-dir", default=str(DEFAULT_AUDIT_DIR))
    p.add_argument("--write", action="store_true", help="Copy (default: dry run).")
    args = p.parse_args(argv)
    if not args.data_bucket.strip():
        raise SystemExit("--data-bucket must not be empty (run in pixi shell -e ops)")

    subjects = sorted({s for path in args.subjects for s in read_subject_csv(path)})
    # A dry run only reads the bucket the AWS credentials name. The guards protect
    # the copy: kubectl must name the same account so that Argo's view of running
    # workflows is this bucket's.
    if guards and args.write:
        require_same_account()
        refuse_running(subjects)
    s3 = s3 or boto3.client("s3", region_name=args.region)

    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    audit = Audit(Path(args.audit_dir) / f"backfill_boldref_{stamp}.jsonl")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        records = [
            r
            for rs in pool.map(lambda s: plan_subject(s3, args.data_bucket, s), subjects)
            for r in rs
        ]

    counts = Counter(r["outcome"] for r in records)
    for rec in records:
        if rec["outcome"] not in OK_OUTCOMES:
            audit.write("needs-a-look", **rec)
    to_copy = [r for r in records if r["outcome"] == "copy"]
    if args.write:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for done in pool.map(lambda r: copy_run(s3, args.data_bucket, r), to_copy):
                audit.write("copied", **done)
    else:
        for rec in to_copy:
            audit.write("planned", src=rec["src"], dst=rec["dst"], old_etag=rec["old_etag"])

    tag = "write" if args.write else "dry run"
    print(f"[{tag}] {len(subjects)} subject(s), {len(records)} record(s)")
    for outcome, n in sorted(counts.items()):
        print(f"  {outcome}: {n}")
    print(f"Audit: {audit.path}")
    flagged = sum(n for o, n in counts.items() if o not in OK_OUTCOMES | {"legacy-session"})
    if flagged:
        print(
            f"\n{flagged} run(s) were not copied and need a look (audit: needs-a-look).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
