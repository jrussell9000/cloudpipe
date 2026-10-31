"""
probseg_backfill.py — regenerate one shard's published probseg maps in place.

Every `label-{GM,WM,CSF}_probseg` written before the orig_nu fix was fitted to
brainmask.mgz's WM-normalised intensities, and about half of them name the wrong
tissue (see tissue_masks.py). Their inputs are all still in S3, so this redoes
only the tissue maps, never the BOLD:

  derivatives/fastsurfer/{subj}/{ses}/mri/{aseg.auto,brainmask,orig_nu}.mgz
  derivatives/registration/{subj}/{ses}/t1w_to_mni/{prefix}_desc-t1w2mni_{affine.mat,warp.nii.gz}
  config/MNI152NLin2009cAsym_res-01_T1w_brain.nii

The session list is a JSON document in the data bucket, written by
scripts/backfill_probseg.py plan:

  {"label": ..., "planned_at": "...", "sessions": [["sub-X", "ses-00A"], ...]}

A session's probseg is FIXED when it has a TissueQC row newer than every one of
its probseg objects (session_state). Only the fixed code writes rows, and both
writers put the row after the objects: the func pod's row is an Argo output
artifact, uploaded after its in-pod publish, and this module writes it last. A
workflow still running an image from before the fix (running workflows keep
their images) republishes old-method maps without a row, which makes the objects
newer than the row again, so the next plan picks the session up. No time cutoff
can say that: those workflows write after any pin.

This pod takes every `--shards`-th session from `--shard` on. Per session, one
outcome (printed as a `[backfill] {json}` line and appended to the shard's
outcome file under config/probseg-backfill/{label}/outcomes/):

  rewritten    the new fit passed and replaced the three probseg objects
  rejected     the new fit disagreed with the dseg too; the probseg was removed
               from the marker, then deleted (marker first, so it never lists a
               missing object)
  already      session_state says fixed or rejected (a func run on the fixed
               image, or an earlier attempt of this shard, did it)
  no-marker    no func-session `_complete.json`; nothing published to fix
  no-probseg   the marker lists no probseg (its tissue maps failed); left alone
  raced        the marker changed while this session ran (a func workflow
               republished it); nothing written, re-plan it
  missing-input  an input object is not in S3; nothing written
  failed       tissue_masks.py exited non-zero; nothing written

Each rewritten or rejected session gets a TissueQC row (metrics/tissue-masks/
dt=.../, source "probseg-backfill"), written LAST: a pod that dies before it
leaves the session stale, so it is redone. The dseg and masks are not
re-uploaded: they never had the defect, and the fit reproduces them.

Idempotent, so Argo may retry a shard: `already` makes a retry skip what is done.
An S3 error exits 75 (EX_TEMPFAIL, in the template's retry expression); a
session tissue_masks.py fails on is recorded and the shard moves on.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

SPACE = "space-MNI152NLin2009cAsym_res-1"
TISSUES = ("GM", "WM", "CSF")
MARKER = "_complete.json"
TEMPLATE_KEY = "config/MNI152NLin2009cAsym_res-01_T1w_brain.nii"
TISSUE_MASKS = Path(__file__).resolve().parent / "tissue_masks.py"
TEMPFAIL = 75


def log(event: dict) -> None:
    print(f"[backfill] {json.dumps(event)}", flush=True)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


ROW_PREFIX = "metrics/tissue-masks/"


def session_state(
    listed: bool, probseg_at: list[datetime | None], row_at: datetime | None, marker_at
) -> str:
    """`fixed`, `rejected`, `stale` or `no-probseg` for one session.

    `listed`: the marker names the probseg. `probseg_at`: each probseg object's
    LastModified, None if absent. `row_at`: the session's newest TissueQC row,
    None if none. `marker_at`: the marker's LastModified.
    """
    present = [t for t in probseg_at if t is not None]
    if listed:
        if row_at is not None and len(present) == len(probseg_at) and row_at >= max(present):
            return "fixed"
        return "stale"
    if present:
        return "stale"  # a rejection that died between its marker and its delete
    if row_at is not None and marker_at is not None and row_at >= marker_at:
        return "rejected"
    return "no-probseg"


def tissue_rows(s3, bucket: str) -> dict[tuple[str, str], datetime]:
    """(subject, session) -> its newest TissueQC row's LastModified."""
    out: dict[tuple[str, str], datetime] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=ROW_PREFIX):
        for o in page.get("Contents", []):
            name = o["Key"].rsplit("/", 1)[1]
            if not name.endswith("_tissue_qc.json"):
                continue
            subj, _, rest = name.partition("_")
            unit = (subj, rest.removesuffix("_tissue_qc.json"))
            if unit not in out or o["LastModified"] > out[unit]:
                out[unit] = o["LastModified"]
    return out


def probseg_names(subj: str, ses: str) -> list[str]:
    return [f"{subj}_{ses}_{SPACE}_label-{t}_probseg.nii.gz" for t in TISSUES]


def shard(sessions: list, index: int, count: int) -> list:
    if not 0 <= index < count:
        raise SystemExit(f"--shard {index} is outside 0..{count - 1}")
    return sessions[index::count]


def read_json(s3, bucket: str, key: str) -> dict | None:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    doc = json.loads(body)
    return doc if isinstance(doc, dict) else None


def last_modified(s3, bucket: str, prefix: str) -> dict[str, datetime]:
    """Name -> LastModified for every object directly in a session prefix."""
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for o in page.get("Contents", []):
            out[o["Key"].rsplit("/", 1)[1]] = o["LastModified"]
    return out


def without_probseg(marker: dict, names: list[str]) -> dict:
    """The marker with these session files dropped and its count recomputed."""
    session_files = [f for f in marker.get("session_files") or [] if f not in names]
    runs = marker.get("runs") or {}
    return {
        **marker,
        "session_files": sorted(session_files),
        "file_count": sum(len(f) for f in runs.values()) + len(session_files),
    }


def put_marker(s3, bucket: str, prefix: str, marker: dict) -> None:
    # Same serialisation as func_derivatives.publish_session.
    s3.put_object(
        Bucket=bucket,
        Key=f"{prefix}/{MARKER}",
        Body=json.dumps(marker, indent=2, sort_keys=True).encode(),
    )


def fetch_inputs(s3, bucket: str, subj: str, ses: str, work: Path) -> dict[str, Path]:
    fs = f"derivatives/fastsurfer/{subj}/{ses}/mri"
    reg = f"derivatives/registration/{subj}/{ses}/t1w_to_mni/{subj}_{ses}_desc-t1w2mni"
    keys = {
        "aseg": (f"{fs}/aseg.auto.mgz", "aseg.auto.mgz"),
        "brainmask": (f"{fs}/brainmask.mgz", "brainmask.mgz"),
        "orig_nu": (f"{fs}/orig_nu.mgz", "orig_nu.mgz"),
        "affine": (f"{reg}_affine.mat", "affine.mat"),
        "warp": (f"{reg}_warp.nii.gz", "warp.nii.gz"),
    }
    paths = {}
    for name, (key, filename) in keys.items():
        paths[name] = work / filename
        s3.download_file(bucket, key, str(paths[name]))
    return paths


def run_tissue_masks(inputs: dict, template: Path, subj: str, ses: str, work: Path, image_tag: str):
    out, qc = work / "out", work / "qc"
    cmd = [
        sys.executable, str(TISSUE_MASKS),
        "--aseg", str(inputs["aseg"]),
        "--template", str(template),
        "--t1w2mni-affine", str(inputs["affine"]),
        "--t1w2mni-warp", str(inputs["warp"]),
        "--brainmask", str(inputs["brainmask"]),
        "--orig-nu", str(inputs["orig_nu"]),
        "--outdir", str(out),
        "--subj", subj,
        "--session", ses,
        "--qc-out", str(qc),
        "--source", "probseg-backfill",
        "--image-tag", image_tag,
    ]  # fmt: skip
    rc = subprocess.run(cmd).returncode
    return rc, out, qc / f"{subj}_{ses}_tissue_qc.json"


def backfill_session(s3, args, subj: str, ses: str, rows: dict, template: Path) -> dict:
    prefix = f"derivatives/func/{subj}/{ses}"
    names = probseg_names(subj, ses)
    marker = read_json(s3, args.data_bucket, f"{prefix}/{MARKER}")
    if not marker or marker.get("kind") != "func-session":
        return {"outcome": "no-marker"}
    listed = bool(set(names) & set(marker.get("session_files") or []))
    stamps = last_modified(s3, args.data_bucket, prefix)
    state = session_state(
        listed, [stamps.get(n) for n in names], rows.get((subj, ses)), stamps.get(MARKER)
    )
    if state in ("fixed", "rejected"):
        return {"outcome": "already"}
    if state == "no-probseg":
        return {"outcome": "no-probseg"}

    with tempfile.TemporaryDirectory(dir=args.scratch) as tmp:
        work = Path(tmp)
        try:
            inputs = fetch_inputs(s3, args.data_bucket, subj, ses, work)
        except ClientError as exc:
            # A missing input is this session's problem and retries cannot fix
            # it; any other S3 error propagates to the shard's exit 75.
            if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey"):
                raise
            return {"outcome": "missing-input", "error": str(exc)[:200]}
        rc, out, qc = run_tissue_masks(inputs, template, subj, ses, work, args.image_tag)
        if rc != 0 or not qc.is_file():
            return {"outcome": "failed", "rc": rc}
        record = json.loads(qc.read_text())

        # A func workflow that republished this session meanwhile owns it now.
        again = read_json(s3, args.data_bucket, f"{prefix}/{MARKER}")
        if (again or {}).get("written_at") != marker.get("written_at"):
            return {"outcome": "raced"}

        if record["probseg_written"]:
            for name in names:
                with (out / name).open("rb") as fh:
                    s3.put_object(Bucket=args.data_bucket, Key=f"{prefix}/{name}", Body=fh)
            if not listed:  # finishing a rejection that died half-way, the other way
                session_files = sorted(set(marker.get("session_files") or []) | set(names))
                runs = marker.get("runs") or {}
                count = sum(len(f) for f in runs.values()) + len(session_files)
                put_marker(
                    s3,
                    args.data_bucket,
                    prefix,
                    {**marker, "session_files": session_files, "file_count": count},
                )
            outcome = "rewritten"
        else:
            put_marker(s3, args.data_bucket, prefix, without_probseg(marker, names))
            s3.delete_objects(
                Bucket=args.data_bucket,
                Delete={"Objects": [{"Key": f"{prefix}/{n}"} for n in names]},
            )
            outcome = "rejected"
        # LAST: the row is what says the session is done (session_state).
        dt = record["completed_at"][:10]
        s3.put_object(
            Bucket=args.metrics_bucket,
            Key=f"{ROW_PREFIX}dt={dt}/{subj}_{ses}_tissue_qc.json",
            Body=qc.read_bytes(),
        )
    agreement = {t: record.get(f"agreement_{t.lower()}") for t in TISSUES}
    return {"outcome": outcome, "agreement": agreement}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--sessions-key", required=True, help="the session list, a key in --data-bucket")
    p.add_argument("--shard", type=int, required=True)
    p.add_argument("--shards", type=int, required=True)
    p.add_argument("--data-bucket", required=True)
    p.add_argument("--metrics-bucket", required=True)
    p.add_argument("--region", default=os.environ.get("AWS_REGION"))
    p.add_argument("--scratch", type=Path, default=Path(tempfile.gettempdir()))
    p.add_argument("--image-tag", default=os.environ.get("CLOUDPIPE_IMAGE", "").rpartition(":")[2])
    args = p.parse_args(argv)
    s3 = boto3.client("s3", region_name=args.region)
    try:
        doc = read_json(s3, args.data_bucket, args.sessions_key)
        if not doc:
            raise SystemExit(f"no session list at s3://{args.data_bucket}/{args.sessions_key}")
        mine = shard(doc["sessions"], args.shard, args.shards)
        rows = tissue_rows(s3, args.metrics_bucket)
        outcomes_key = (
            f"config/probseg-backfill/{doc['label']}/outcomes/shard-{args.shard:04d}.jsonl"
        )
        template = args.scratch / "MNI152NLin2009cAsym_res-01_T1w_brain.nii"
        s3.download_file(args.data_bucket, TEMPLATE_KEY, str(template))
        log({"shard": args.shard, "of": args.shards, "sessions": len(mine), "rows": len(rows)})
        # A retried shard keeps the earlier attempts' lines.
        try:
            earlier = s3.get_object(Bucket=args.data_bucket, Key=outcomes_key)["Body"].read()
            lines = earlier.decode().splitlines()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("NoSuchKey", "404"):
                raise
            lines = []
        counts: dict[str, int] = {}
        for subj, ses in mine:
            result = {"subject": subj, "session": ses, "at": now_iso()}
            result |= backfill_session(s3, args, subj, ses, rows, template)
            log(result)
            lines.append(json.dumps(result))
            counts[result["outcome"]] = counts.get(result["outcome"], 0) + 1
            # Re-put after every session, so a reclaimed pod leaves what it did.
            s3.put_object(
                Bucket=args.data_bucket, Key=outcomes_key, Body=("\n".join(lines) + "\n").encode()
            )
        log({"shard": args.shard, "done": counts})
    except (BotoCoreError, ClientError) as exc:
        print(f"[backfill] S3 error, exiting {TEMPFAIL} for a retry: {exc}", flush=True)
        return TEMPFAIL
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
