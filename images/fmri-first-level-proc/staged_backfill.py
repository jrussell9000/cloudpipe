"""
staged_backfill.py — give a first-level tree written before fork 4.0.0+cp.11 its staged
runs, and drop its n-back concatenations (operator, 2026-11-11).

Since cp.11 a session keeps each run's series, not the concatenations made from them
(per Taylor Keding): `staged/{subj}_{ses}_{rid}_bold.nii.gz` holds each staged run, and
`{pre}_psc` and `{pre}_concat_scan` are deleted. Older trees hold both concatenations in
both n-back analyses (~2.5 GB a session) and no staged runs. `{pre}_concat_scan` IS the
staged n-back runs end to end (fmri_first_level_proc's prep.task_prep 3dTcats the runs it
modelled, in label order), so this cuts it back into those runs, checks every voxel, and
only then rewrites the tree. Run by the staged-backfill WorkflowTemplate, planned and
verified by scripts/backfill_staged_runs.py.

Per session, in this order (ADR 017: `_complete.json` is the only existence test):
  1. read the marker; one with `staged_backfill` is `already`
  2. plan: the concatenation (nback_act's, else nback_conn's) holds the runs whose
     `{pre}_run{N}_psc.nii.gz` sits beside it, in label order, each the marker's
     `n_frames` long. The listing must agree with the marker's file count, bytes and
     dirs, or the session is set aside untouched
  3. save the marker and the plan to the session's state key
  4. 3dTcat each run out of the concatenation; check it voxel for voxel against the
     concatenation, frame count, grid and TR. A mismatch sets the session aside untouched
  5. delete the marker, upload staged/, record the uploads in the state, delete the
     concatenations, and write the new marker LAST, with counts from the new listing
An interrupted session resumes from its state: the marker is gone but the plan and the
original marker are saved, and once the uploads are recorded only steps after them run.

Exit 0 when the shard's list is done (outcomes are logged and kept in each state), 75 on
an S3 error (EX_TEMPFAIL, retried by the template), anything else is a bug and fails the
pod loudly.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import boto3
import nibabel as nib
import numpy as np
from botocore.exceptions import BotoCoreError, ClientError

PREFIX = "derivatives/first_levels"
MARKER = "_complete.json"
LINKS = "_links.json"
STAGED_DIR = "staged"
NBACK = ("nback_act", "nback_conn")
TEMPFAIL = 75
RUN_ID = re.compile(r"^task-nback_run-(\d+)$")
# Compared in frames this size, so a check never holds a second copy of a whole run.
CHECK_FRAMES = 64


class SetAside(Exception):
    """The session cannot be backfilled as planned; its tree is left untouched."""


def state_prefix(label: str) -> str:
    return f"config/first-level/staged-backfill/{label}"


def sessions_key(label: str) -> str:
    return f"{state_prefix(label)}/sessions.json"


def state_key(label: str, subj: str, ses: str) -> str:
    return f"{state_prefix(label)}/state/{subj}_{ses}.json"


def log(event: str, **fields) -> None:
    print(f"[staged-backfill] {json.dumps({'event': event, **fields}, default=str)}", flush=True)


def now() -> str:
    return datetime.now(UTC).isoformat()


def run_tcat(cmd: list[str]) -> None:
    """Run 3dTcat, raising on failure. Module-level so tests can replace it."""
    subprocess.run(cmd, check=True, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


def get_json(s3, bucket: str, key: str) -> dict | None:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return None
        raise


def put_json(s3, bucket: str, key: str, doc: dict) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(doc, indent=2, sort_keys=True).encode())


def listing(s3, bucket: str, prefix: str) -> dict[str, int]:
    """{tree-relative path: bytes} for every object under the tree."""
    out: dict[str, int] = {}
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": f"{prefix}/"}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            out[obj["Key"][len(prefix) + 1 :]] = obj["Size"]
        if not resp.get("IsTruncated"):
            return out
        token = resp["NextContinuationToken"]


def delete_keys(s3, bucket: str, keys: list[str]) -> None:
    if not keys:
        return
    resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys]})
    if resp.get("Errors"):
        raise ClientError(
            {"Error": {"Code": "DeleteFailed", "Message": str(resp["Errors"])}}, "DeleteObjects"
        )


def tree_counts(objs: dict[str, int]) -> tuple[int, int, int]:
    """(files, bytes, dirs) as fs_derivatives.publish counts them: every object but the
    sidecars, and every directory that holds one."""
    files = {r: n for r, n in objs.items() if r not in (MARKER, LINKS)}
    dirs = {"/".join(r.split("/")[:i]) for r in files for i in range(1, r.count("/") + 1)}
    return len(files), sum(files.values()), len(dirs)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def require_listing_matches(marker: dict, objs: dict[str, int]) -> None:
    """Raise SetAside unless the tree in the bucket is the one its marker describes.

    Files and bytes must agree exactly. A marker may count MORE directories than the
    listing shows: publish counts directories on disk, and an empty one (left by a
    failed analysis, seen on 8 cp.7 trees) has no object in S3. Fewer would mean a
    file the marker does not know, so that is still refused. conn_dedup.py uses it too.
    """
    if marker.get("link_count"):
        raise SetAside("tree-has-links")
    counts = tree_counts(objs)
    recorded = (marker.get("file_count"), marker.get("total_bytes"), marker.get("dir_count"))
    if counts[:2] != recorded[:2] or not isinstance(recorded[2], int) or recorded[2] < counts[2]:
        raise SetAside(f"listing-disagrees-with-marker: {counts} != {recorded}")


def make_plan(marker: dict, objs: dict[str, int], subj: str, ses: str) -> dict | None:
    """What this session's backfill does, or None when it has no concatenation."""
    pre = f"{subj}_{ses}_nback"
    source = next((a for a in NBACK if f"{a}/{pre}_concat_scan.nii.gz" in objs), None)
    if source is None:
        return None
    require_listing_matches(marker, objs)
    if any(r.startswith(f"{STAGED_DIR}/") for r in objs):
        raise SetAside("already-has-staged")

    psc = re.compile(rf"^{source}/{re.escape(pre)}_run(\d+)_psc\.nii\.gz$")
    labels = sorted(int(m[1]) for r in objs if (m := psc.match(r)))
    by_label = {}
    for rid, rec in (marker.get("runs") or {}).items():
        if (m := RUN_ID.match(rid)) and "excluded" not in rec:
            by_label[int(m[1])] = (rid, int(rec["n_frames"]))
    if not labels:
        raise SetAside("no-psc-runs-beside-the-concatenation")
    if missing := [lbl for lbl in labels if lbl not in by_label]:
        raise SetAside(f"psc-runs-without-a-staged-run: {missing}")

    removed = [
        [f"{a}/{pre}_{name}", objs[f"{a}/{pre}_{name}"]]
        for a in NBACK
        for name in ("concat_scan.nii.gz", "psc.nii.gz")
        if f"{a}/{pre}_{name}" in objs
    ]
    return {
        "source": f"{source}/{pre}_concat_scan.nii.gz",
        "runs": [list(by_label[lbl]) for lbl in labels],
        "removed": removed,
    }


# ---------------------------------------------------------------------------
# Build and check the staged runs
# ---------------------------------------------------------------------------


def staged_name(subj: str, ses: str, rid: str) -> str:
    return f"{STAGED_DIR}/{subj}_{ses}_{rid}_bold.nii.gz"


def build_runs(
    s3, bucket: str, prefix: str, plan: dict, subj: str, ses: str, work: Path
) -> list[tuple[str, Path]]:
    """Cut the concatenation into its runs and check each; [(staged rel path, file)].
    Raises SetAside on any disagreement, before anything in the bucket has changed."""
    gz, nii = work / "concat.nii.gz", work / "concat.nii"
    s3.download_file(bucket, f"{prefix}/{plan['source']}", str(gz))
    with gzip.open(gz, "rb") as src, nii.open("wb") as dst:  # read below as a memory map
        shutil.copyfileobj(src, dst, 1 << 20)
    gz.unlink()

    ref = nib.load(str(nii))
    total = sum(n for _, n in plan["runs"])
    if len(ref.shape) != 4 or ref.shape[3] != total:
        raise SetAside(
            f"concatenation-has-{ref.shape[3] if len(ref.shape) == 4 else 0}-frames-not-{total}"
        )
    ref_data = np.asanyarray(ref.dataobj)
    out, start = [], 0
    for rid, n in plan["runs"]:
        rel = staged_name(subj, ses, rid)
        path = work / Path(rel).name
        run_tcat(["3dTcat", "-prefix", str(path), f"{nii}[{start}..{start + n - 1}]"])
        img = nib.load(str(path))
        if img.shape != (*ref.shape[:3], n):
            raise SetAside(f"{rid}-shape-{img.shape}")
        if not np.allclose(img.affine, ref.affine, atol=1e-4):
            raise SetAside(f"{rid}-affine-differs")
        if not np.isclose(img.header.get_zooms()[3], ref.header.get_zooms()[3], atol=1e-4):
            raise SetAside(f"{rid}-tr-differs")
        data = np.asanyarray(img.dataobj)
        if data.dtype != ref_data.dtype:
            raise SetAside(f"{rid}-dtype-{data.dtype}")
        for k in range(0, n, CHECK_FRAMES):
            if not np.array_equal(
                data[..., k : k + CHECK_FRAMES],
                ref_data[..., start + k : start + min(k + CHECK_FRAMES, n)],
            ):
                raise SetAside(f"{rid}-voxels-differ-at-frame-{k}")
        del data, img
        out.append((rel, path))
        start += n
    del ref_data, ref
    return out


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def new_marker(
    old: dict, plan: dict, state: dict, objs: dict[str, int], label: str, skey: str
) -> dict:
    files, total, dirs = tree_counts(objs)
    marker = json.loads(json.dumps(old))
    for (rid, _), (rel, _) in zip(plan["runs"], state["uploaded"], strict=True):
        marker["runs"][rid] = {**marker["runs"][rid], "staged_bold": rel}
    for path, _ in plan["removed"]:
        analysis, name = path.split("/", 1)
        if analysis in marker.get("analyses", {}):
            entry = marker["analyses"][analysis]
            entry["removed"] = sorted({*entry.get("removed", []), name})
    marker["staged_backfill"] = {
        "label": label,
        "at": now(),
        "source": plan["source"],
        "removed": [{"path": p, "bytes": n} for p, n in plan["removed"]],
        "added": [{"path": p, "bytes": n} for p, n in state["uploaded"]],
        "original_marker": skey,
        "original_written_at": old.get("written_at"),
        "original_file_count": old.get("file_count"),
        "original_total_bytes": old.get("total_bytes"),
        "original_dir_count": old.get("dir_count"),
    }
    marker.update(file_count=files, total_bytes=total, dir_count=dirs, written_at=now())
    return marker


def finish(s3, bucket: str, prefix: str, label: str, skey: str, state: dict) -> str:
    """After the uploads: delete the concatenations, then write the marker last."""
    objs = listing(s3, bucket, prefix)
    for rel, n in state["uploaded"]:
        if objs.get(rel) != n:
            raise RuntimeError(
                f"{prefix}/{rel}: uploaded {n} bytes, the bucket holds {objs.get(rel)}"
            )
    plan = state["plan"]
    delete_keys(s3, bucket, [f"{prefix}/{p}" for p, _ in plan["removed"] if p in objs])
    marker = new_marker(
        state["original_marker"], plan, state, listing(s3, bucket, prefix), label, skey
    )
    put_json(s3, bucket, f"{prefix}/{MARKER}", marker)
    put_json(s3, bucket, skey, {**state, "outcome": "done", "finished_at": now()})
    return "done"


def process(s3, bucket: str, label: str, subj: str, ses: str, scratch: Path) -> str:
    prefix = f"{PREFIX}/{subj}/{ses}"
    skey = state_key(label, subj, ses)
    marker = get_json(s3, bucket, f"{prefix}/{MARKER}")

    if marker is not None:
        if "staged_backfill" in marker:
            return "already"
        try:
            plan = make_plan(marker, listing(s3, bucket, prefix), subj, ses)
        except SetAside as exc:
            put_json(s3, bucket, skey, {"outcome": "set-aside", "reason": str(exc), "at": now()})
            log("set-aside", subj=subj, ses=ses, reason=str(exc))
            return "set-aside"
        if plan is None:
            return "no-concatenation"
        state = {"original_marker": marker, "plan": plan, "saved_at": now()}
        put_json(s3, bucket, skey, state)
    else:
        state = get_json(s3, bucket, skey)
        if not state or "plan" not in state:
            return "no-marker"  # not this backfill's doing: leave it alone
        if state.get("uploaded"):
            return finish(s3, bucket, prefix, label, skey, state)
        plan = state["plan"]

    with tempfile.TemporaryDirectory(dir=scratch) as tmp:
        try:
            built = build_runs(s3, bucket, prefix, plan, subj, ses, Path(tmp))
        except SetAside as exc:
            if marker is None:  # resumed after the marker went: put it back as it was
                put_json(s3, bucket, f"{prefix}/{MARKER}", state["original_marker"])
            put_json(
                s3, bucket, skey, {**state, "outcome": "set-aside", "reason": str(exc), "at": now()}
            )
            log("set-aside", subj=subj, ses=ses, reason=str(exc))
            return "set-aside"
        if marker is not None:
            s3.delete_object(Bucket=bucket, Key=f"{prefix}/{MARKER}")
        uploaded = []
        for rel, path in built:
            s3.upload_file(str(path), bucket, f"{prefix}/{rel}")
            uploaded.append([rel, path.stat().st_size])
    state = {**state, "uploaded": uploaded}
    put_json(s3, bucket, skey, state)
    return finish(s3, bucket, prefix, label, skey, state)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


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
