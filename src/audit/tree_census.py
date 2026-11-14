"""Census every stored FastSurfer tree: is it complete, what built it, on which device.

openspec add-reprocess-audit, tasks 2.3 and 2.4. One row per tree, session trees
and each subject's long-template alike:

    subject, tree ("ses-00A" ... or "long-template"), complete,
    fastsurfer   first token of scripts/build.log line 1, e.g. "2.5.4+cdfccea"
    freesurfer   scripts/build-stamp.txt, e.g. "freesurfer-linux-...-7.4.1-..."
    device       "cuda" / "cpu" from scripts/deep-seg.log's "Using device:" line
                 (session trees; the template has no segmentation), or None

The device is read from the tree itself, not inferred: the workflow label that
also records it is gone with the workflow, and the pod-cost instance type is a
hostname join that #1060 showed can name the wrong node. `_complete.json` is the
only existence test for a tree (docs/pipelines.md, ADR 017), so an incomplete tree's
logs are not read at all.

Read-only. The 2026-10-26 census of 45,246 trees survives only as a summary;
this keeps the per-tree table (data/reprocess/fastsurfer-tree-census-*.jsonl).

    pixi run -e ops python -m audit.tree_census --bucket "$CLOUDPIPE_BUCKET" \\
        --out data/reprocess/fastsurfer-tree-census-2026-11-08.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = "derivatives/fastsurfer/"
COMPLETE_MARKER = "_complete.json"
_DEVICE = re.compile(r"Using device:\s*(\S+)")


def parse_release(build_log: bytes | None) -> str | None:
    """`2.5.4+cdfccea (stable)` on line 1 -> `2.5.4+cdfccea`."""
    if not build_log:
        return None
    first = build_log.decode(errors="replace").splitlines()[:1]
    return first[0].split()[0] if first and first[0].split() else None


def parse_stamp(stamp: bytes | None) -> str | None:
    if not stamp:
        return None
    text = stamp.decode(errors="replace").strip()
    return text or None


def parse_device(deep_seg_log: bytes | None) -> str | None:
    """The FIRST `Using device:` line: the main segmentation network's device.

    Later lines (CerebNet, HypVINN) run in the same container and normally say
    the same; a mix is returned as "mixed" so it is not mistaken for either.
    """
    if not deep_seg_log:
        return None
    devices = _DEVICE.findall(deep_seg_log.decode(errors="replace"))
    if not devices:
        return None
    first = devices[0]
    return first if all(d == first for d in devices) else "mixed"


def _get(s3, bucket: str, key: str) -> bytes | None:
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return None


def _prefixes(s3, bucket: str, prefix: str) -> list[str]:
    out: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix, Delimiter="/"
    ):
        out.extend(p["Prefix"] for p in page.get("CommonPrefixes", []))
    return out


def census_tree(s3, bucket: str, tree_prefix: str) -> dict[str, Any]:
    """One row for `derivatives/fastsurfer/{subj}/{tree}/`."""
    subject, tree = tree_prefix[len(ROOT) :].rstrip("/").split("/", 1)
    listing = s3.list_objects_v2(Bucket=bucket, Prefix=tree_prefix + COMPLETE_MARKER, MaxKeys=1)
    complete = any(o["Key"] == tree_prefix + COMPLETE_MARKER for o in listing.get("Contents", []))
    row: dict[str, Any] = {
        "subject": subject,
        "tree": tree,
        "complete": complete,
        "fastsurfer": None,
        "freesurfer": None,
        "device": None,
    }
    if complete:
        scripts = tree_prefix + "scripts/"
        row["fastsurfer"] = parse_release(_get(s3, bucket, scripts + "build.log"))
        row["freesurfer"] = parse_stamp(_get(s3, bucket, scripts + "build-stamp.txt"))
        if tree != "long-template":
            row["device"] = parse_device(_get(s3, bucket, scripts + "deep-seg.log"))
    return row


def run(s3, bucket: str, out: Path, workers: int = 48) -> Counter:
    subjects = _prefixes(s3, bucket, ROOT)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        trees = [t for ts in pool.map(lambda p: _prefixes(s3, bucket, p), subjects) for t in ts]
        tally: Counter = Counter()
        with out.open("w") as fh:
            for row in pool.map(lambda t: census_tree(s3, bucket, t), trees):
                fh.write(json.dumps(row) + "\n")
                tally["trees"] += 1
                tally["complete" if row["complete"] else "incomplete"] += 1
                tally[f"fastsurfer={row['fastsurfer']}"] += 1
                if row["tree"] != "long-template" and row["complete"]:
                    tally[f"device={row['device']}"] += 1
    tally["subjects"] = len(subjects)
    return tally


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bucket", required=True)
    p.add_argument("--region")
    p.add_argument("--workers", type=int, default=48)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)

    import boto3
    from botocore.config import Config

    s3 = boto3.client(
        "s3", region_name=args.region, config=Config(max_pool_connections=args.workers + 4)
    )
    tally = run(s3, args.bucket, args.out, args.workers)
    for k, v in sorted(tally.items()):
        print(f"{k}\t{v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
