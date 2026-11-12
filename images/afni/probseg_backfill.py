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
  no-probseg   the marker lists no probseg (its tissue maps failed); left alone,
               unless --fill-missing true and it lists no tissue map at all:
  filled       every tissue map was made and published, then the marker, then the
               row (fill_session)
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

# The fixed-or-not rule lives in images/shared so first-level applies the same one.
from probseg_state import (  # noqa: F401 -- re-exported for scripts/backfill_probseg.py
    ROW_PREFIX,
    SPACE,
    TISSUES,
    probseg_names,
    session_state,
    tissue_rows,
)

MARKER = "_complete.json"
TEMPLATE_KEY = "config/MNI152NLin2009cAsym_res-01_T1w_brain.nii"
TISSUE_MASKS = Path(__file__).resolve().parent / "tissue_masks.py"
TEMPFAIL = 75


def log(event: dict) -> None:
    print(f"[backfill] {json.dumps(event)}", flush=True)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        tissue_maps = [
            f for f in marker.get("session_files") or [] if f.startswith(f"{subj}_{ses}_{SPACE}_")
        ]
        if args.fill_missing and not tissue_maps:
            return fill_session(s3, args, subj, ses, marker, template)
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


def fill_session(s3, args, subj: str, ses: str, marker: dict, template: Path) -> dict:
    """Publish every tissue map of a session that has none (`--fill-missing`).

    Func preproc's export of the maps is non-fatal, so a session can be published with
    its BOLD and no maps; its marker then lists no `session_files`. This writes them
    the way publish does: the maps first, then the marker listing them, then the
    TissueQC row. A probseg the fit rejects is simply not among the maps, as in func
    preproc. Outcomes: `filled`, or the shared `missing-input`, `failed` and `raced`.
    """
    prefix = f"derivatives/func/{subj}/{ses}"
    with tempfile.TemporaryDirectory(dir=args.scratch) as tmp:
        work = Path(tmp)
        try:
            inputs = fetch_inputs(s3, args.data_bucket, subj, ses, work)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey"):
                raise
            return {"outcome": "missing-input", "error": str(exc)[:200]}
        rc, out, qc = run_tissue_masks(inputs, template, subj, ses, work, args.image_tag)
        if rc != 0 or not qc.is_file():
            return {"outcome": "failed", "rc": rc}
        maps = sorted(p.name for p in out.glob(f"{subj}_{ses}_{SPACE}_*") if p.is_file())
        if not maps:
            return {"outcome": "failed", "rc": rc, "error": "tissue_masks.py wrote no maps"}
        record = json.loads(qc.read_text())

        again = read_json(s3, args.data_bucket, f"{prefix}/{MARKER}")
        if (again or {}).get("written_at") != marker.get("written_at"):
            return {"outcome": "raced"}

        for name in maps:
            with (out / name).open("rb") as fh:
                s3.put_object(Bucket=args.data_bucket, Key=f"{prefix}/{name}", Body=fh)
        session_files = sorted(set(marker.get("session_files") or []) | set(maps))
        runs = marker.get("runs") or {}
        count = sum(len(f) for f in runs.values()) + len(session_files)
        put_marker(
            s3,
            args.data_bucket,
            prefix,
            {**marker, "session_files": session_files, "file_count": count},
        )
        # LAST: the row is what says the session's maps are right (session_state).
        dt = record["completed_at"][:10]
        s3.put_object(
            Bucket=args.metrics_bucket,
            Key=f"{ROW_PREFIX}dt={dt}/{subj}_{ses}_tissue_qc.json",
            Body=qc.read_bytes(),
        )
    kinds = sorted(n.removeprefix(f"{subj}_{ses}_{SPACE}_") for n in maps)
    return {"outcome": "filled", "maps": kinds, "probseg_written": record.get("probseg_written")}


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
    p.add_argument(
        "--fill-missing",
        choices=("true", "false"),
        default="false",
        help="also publish every tissue map of a listed session whose marker lists none",
    )
    args = p.parse_args(argv)
    args.fill_missing = args.fill_missing == "true"
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
