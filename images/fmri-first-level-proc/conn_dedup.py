"""
conn_dedup.py — remove nback_conn's copies of what it shares with nback_act from a
first-level tree written before fmri-first-level-proc 4.0.0+cp.12 (operator, 2026-11-13).

Since cp.12 a session keeps the files nback_act and nback_conn share once, in nback_act/
(staging deletes nback_conn's hard links and records them as `deduplicated`). Older trees
hold both copies: the PSC runs, their means, the run-intersection mask and the resampled
atlas, ~0.6 GB a session. This brings them to cp.12's layout, and deletes a copy only once
it is shown to be the same data as nback_act's. Run by the conn-dedup WorkflowTemplate,
planned and verified by scripts/backfill_conn_dedup.py.

Per session, in this order (ADR 017: `_complete.json` is the only existence test):
  1. read the marker; one whose nback_conn already records `deduplicated` is `already`
  2. candidates: nback_conn's ledger `intermediate` outputs with a shared name that
     nback_act/ also holds. The listing must agree with the marker
     (staged_backfill.require_listing_matches), and no concatenation may remain
  3. check each candidate against nback_act's file: equal size and ETag is the same
     bytes (cp.11 uploaded both from one hard-linked file); otherwise both are
     decompressed and their voxels, shape and grid compared. A file that differs stays
  4. save the marker and the verified list to the session's state key
  5. delete the marker, delete the verified copies, write the new marker LAST, with
     counts from the new listing and nback_conn's `deduplicated`, as cp.12 writes it
An interrupted session resumes from its state. Exit 0 when the shard is done, 75 on an
S3 error (retried), anything else is a bug and fails the pod loudly.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import boto3
import nibabel as nib
import numpy as np
from botocore.exceptions import BotoCoreError, ClientError
from staged_backfill import (
    MARKER,
    PREFIX,
    TEMPFAIL,
    SetAside,
    delete_keys,
    get_json,
    listing,
    log,
    now,
    put_json,
    require_listing_matches,
    tree_counts,
)

ACT, CONN = "nback_act", "nback_conn"
# The outputs task_act and task_conn compute identically (shared stages since cp.11):
# each run's PSC and mean (no run number when one run was supplied), the
# run-intersection mask, and the atlas resampled to the BOLD grid.
SHARED = re.compile(
    r"^(?P<pre>sub-[^_]+_ses-[^_]+_nback)_(?:(?:run\d+_)?(?:psc|mean)|run_intersect_mask|.+_resampled)\.nii\.gz$"
)
CHECK_SLAB = 32  # frames compared at a time


def state_prefix(label: str) -> str:
    return f"config/first-level/conn-dedup/{label}"


def sessions_key(label: str) -> str:
    return f"{state_prefix(label)}/sessions.json"


def state_key(label: str, subj: str, ses: str) -> str:
    return f"{state_prefix(label)}/state/{subj}_{ses}.json"


def etags(s3, bucket: str, prefix: str) -> dict[str, str]:
    out, token = {}, None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": f"{prefix}/"}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            out[obj["Key"][len(prefix) + 1 :]] = obj.get("ETag", "").strip('"')
        if not resp.get("IsTruncated"):
            return out
        token = resp["NextContinuationToken"]


def intermediates(s3, bucket: str, prefix: str, pre: str) -> set[str]:
    """nback_conn's outputs its ledger last classed `intermediate`."""
    ledger = get_json_lines(s3, bucket, f"{prefix}/{CONN}/{pre}_ledger.jsonl")
    classes: dict[str, str] = {}
    for record in ledger:
        for out in record.get("outputs") or []:
            classes[out["path"]] = out["class"]
    return {p for p, c in classes.items() if c == "intermediate"}


def get_json_lines(s3, bucket: str, key: str) -> list[dict]:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return []
        raise
    out = []
    for line in body.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def make_plan(s3, bucket: str, prefix: str, marker: dict, subj: str, ses: str) -> list[str] | None:
    """The shared names to check, or None when nback_conn holds none (nothing to do)."""
    objs = listing(s3, bucket, prefix)
    pre = f"{subj}_{ses}_nback"
    names = sorted(
        r.split("/", 1)[1]
        for r in objs
        if r.startswith(f"{CONN}/") and (m := SHARED.match(r.split("/", 1)[1])) and m["pre"] == pre
    )
    names = [n for n in names if f"{ACT}/{n}" in objs]
    if not names:
        return None
    require_listing_matches(marker, objs)
    if any(r.endswith(f"{pre}_concat_scan.nii.gz") for r in objs):
        raise SetAside("concatenation-still-there")  # not staged-backfilled: {pre}_psc may be one
    inter = intermediates(s3, bucket, prefix, pre)
    if not inter:
        raise SetAside("no-nback-conn-ledger")
    return [n for n in names if n in inter]


def same_voxels(a: Path, b: Path) -> bool:
    ia, ib = nib.load(str(a)), nib.load(str(b))
    if ia.shape != ib.shape or not np.allclose(ia.affine, ib.affine, atol=1e-5):
        return False
    da, db = np.asanyarray(ia.dataobj), np.asanyarray(ib.dataobj)
    if da.dtype != db.dtype:
        return False
    if da.ndim < 4:
        return bool(np.array_equal(da, db))
    return all(
        np.array_equal(da[..., k : k + CHECK_SLAB], db[..., k : k + CHECK_SLAB])
        for k in range(0, da.shape[3], CHECK_SLAB)
    )


def gunzip(s3, bucket: str, key: str, dest: Path) -> Path:
    gz = dest.with_suffix(dest.suffix + ".gz")
    s3.download_file(bucket, key, str(gz))
    with gzip.open(gz, "rb") as src, dest.open("wb") as dst:  # read below as a memory map
        shutil.copyfileobj(src, dst, 1 << 20)
    gz.unlink()
    return dest


def verify(s3, bucket: str, prefix: str, names: list[str], scratch: Path) -> tuple[list, list]:
    """([[name, bytes, how]] proven the same as nback_act's, [[name, reason]] kept)."""
    sizes, tags = listing(s3, bucket, prefix), etags(s3, bucket, prefix)
    same, kept = [], []
    for name in names:
        conn, act = f"{CONN}/{name}", f"{ACT}/{name}"
        if sizes[conn] == sizes[act] and tags[conn] and tags[conn] == tags[act]:
            same.append([name, sizes[conn], "etag"])
            continue
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            a = gunzip(s3, bucket, f"{prefix}/{conn}", Path(tmp) / "conn.nii")
            b = gunzip(s3, bucket, f"{prefix}/{act}", Path(tmp) / "act.nii")
            if same_voxels(a, b):
                same.append([name, sizes[conn], "voxels"])
            else:
                kept.append([name, "differs-from-nback_act"])
    return same, kept


def finish(s3, bucket: str, prefix: str, label: str, skey: str, state: dict) -> str:
    """Delete the verified copies, then write the marker last."""
    objs = listing(s3, bucket, prefix)
    delete_keys(
        s3, bucket, [f"{prefix}/{CONN}/{n}" for n, _, _ in state["same"] if f"{CONN}/{n}" in objs]
    )
    old = state["original_marker"]
    marker = json.loads(json.dumps(old))
    files, total, dirs = tree_counts(listing(s3, bucket, prefix))
    if state["same"]:
        conn = marker.setdefault("analyses", {}).setdefault(CONN, {})
        conn["deduplicated"] = {
            ACT: sorted(
                {*conn.get("deduplicated", {}).get(ACT, []), *(n for n, _, _ in state["same"])}
            )
        }
    marker["conn_dedup"] = {
        "label": label,
        "at": now(),
        "removed": [
            {"path": f"{CONN}/{n}", "bytes": b, "checked_by": how} for n, b, how in state["same"]
        ],
        "kept": [{"path": f"{CONN}/{n}", "reason": r} for n, r in state["kept"]],
        "original_marker": skey,
        "original_written_at": old.get("written_at"),
        "original_file_count": old.get("file_count"),
        "original_total_bytes": old.get("total_bytes"),
    }
    marker.update(file_count=files, total_bytes=total, dir_count=dirs, written_at=now())
    put_json(s3, bucket, f"{prefix}/{MARKER}", marker)
    put_json(s3, bucket, skey, {**state, "outcome": "done", "finished_at": now()})
    return "done"


def process(s3, bucket: str, label: str, subj: str, ses: str, scratch: Path) -> str:
    prefix = f"{PREFIX}/{subj}/{ses}"
    skey = state_key(label, subj, ses)
    marker = get_json(s3, bucket, f"{prefix}/{MARKER}")
    if marker is None:
        state = get_json(s3, bucket, skey)
        if not state or "same" not in state:
            return "no-marker"  # not this job's doing: leave it alone
        return finish(s3, bucket, prefix, label, skey, state)  # resumed after the marker went
    if "deduplicated" in (marker.get("analyses", {}).get(CONN) or {}) or "conn_dedup" in marker:
        return "already"
    try:
        names = make_plan(s3, bucket, prefix, marker, subj, ses)
    except SetAside as exc:
        put_json(s3, bucket, skey, {"outcome": "set-aside", "reason": str(exc), "at": now()})
        log("set-aside", subj=subj, ses=ses, reason=str(exc))
        return "set-aside"
    if names is None:
        return "nothing-shared"
    same, kept = verify(s3, bucket, prefix, names, scratch)
    if not same:
        put_json(
            s3,
            bucket,
            skey,
            {"outcome": "set-aside", "reason": "no-copy-matches", "kept": kept, "at": now()},
        )
        log("set-aside", subj=subj, ses=ses, reason="no-copy-matches", kept=kept)
        return "set-aside"
    state = {"original_marker": marker, "same": same, "kept": kept, "saved_at": now()}
    put_json(s3, bucket, skey, state)
    s3.delete_object(Bucket=bucket, Key=f"{prefix}/{MARKER}")
    return finish(s3, bucket, prefix, label, skey, state)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--label", required=True)
    ap.add_argument("--shard", type=int, required=True)
    ap.add_argument("--shards", type=int, required=True)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--region")
    ap.add_argument("--scratch", type=Path, default=Path("/scratch"))
    return ap


def main(argv: list[str] | None = None, s3=None) -> int:
    args = build_parser().parse_args(argv)
    s3 = s3 or boto3.client("s3", region_name=args.region)
    counts: Counter[str] = Counter()
    try:
        doc = get_json(s3, args.bucket, sessions_key(args.label))
        if doc is None:
            log("no-plan", key=sessions_key(args.label))
            return 1
        mine = doc["sessions"][args.shard :: args.shards]
        log("shard-start", shard=args.shard, shards=args.shards, sessions=len(mine))
        for subj, ses in mine:
            t0 = time.monotonic()
            outcome = process(s3, args.bucket, args.label, subj, ses, args.scratch)
            counts[outcome] += 1
            log(
                "session",
                subj=subj,
                ses=ses,
                outcome=outcome,
                seconds=round(time.monotonic() - t0, 1),
            )
    except (ClientError, BotoCoreError) as exc:
        log("s3-error", error=f"{type(exc).__name__}: {exc}", done=dict(counts))
        return TEMPFAIL
    log("shard-done", **counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
