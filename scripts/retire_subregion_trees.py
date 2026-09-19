#!/usr/bin/env python3
"""Delete every `derivatives/subregions/{subj}/{region}/` tree for a RETIRED region — irreversibly.

Two regions have been retired, both on 2026-09-16, along with the TensorFlow
segmentation pod that produced them:

  hypothalamic  FreeSurfer's hypothalamic subunits, superseded by FastSurfer's
                HypVINN (written per session by the anatomical phase).
                Executed 2026-09-16: 47,204 objects across 11,801 subjects.
  sclimbic      FreeSurfer's ScLimbic. Its accumbens is in aseg and its fornix
                and mammillary bodies are in HypVINN; basal forebrain and septal
                nuclei, the only unique structures, are not needed.

The legacy trees are not just unused, they are wrong: a repeated-`--s` bug in
the segmentation pod meant each one covers only its subject's LAST session,
while its `_complete.json` claims a whole tree. Nothing reads them any more (the
subregion gate and fsqc both stopped), so deleting them removes the one way left
to use bad data by accident.

`<YOUR_S3_BUCKET>` has versioning DISABLED, so a delete cannot be undone. Hence:

  - dry run is the DEFAULT; deleting takes an explicit `--execute`
  - a preflight refuses `--execute` while any Running workflow still carries a
    segmentation template that runs the region's tool in `status.storedTemplates`.
    Argo freezes templates at workflow start, so such a workflow will PUBLISH a
    fresh tree after this script finishes. Wait for those workflows to finish.
  - markers are deleted before files, and the prefix is re-listed afterwards.

Usage:
  pixi run python scripts/retire_subregion_trees.py --region sclimbic            # dry run
  pixi run python scripts/retire_subregion_trees.py --region sclimbic --execute
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

BUCKET = "<YOUR_S3_BUCKET>"
REGION = "<YOUR_AWS_REGION>"
SUBREGIONS_ROOT = "derivatives/subregions/"
NAMESPACE = "argo-workflows"

# The stored-template key of the (now removed) TensorFlow segmentation step, and
# the tool whose presence in it means a workflow can still publish that region.
DL_TEMPLATE_KEY = "namespaced/subregion-seg/segment-subregions-dl-template"
RETIRED_TOOLS = {
    "hypothalamic": "mri_segment_hypothalamic_subunits",
    "sclimbic": "mri_sclimbic_seg",
}


def stale_running_workflows(workflows: dict, tool: str) -> list[str]:
    """Names of Running workflows whose frozen DL template still runs `tool`.

    Takes the parsed output of `kubectl get workflows -o json` so it can be tested
    without a cluster. A workflow with no stored DL template is NOT stale: Argo
    snapshots every referenced template into storedTemplates when the workflow
    STARTS (verified 2026-09-15 on a workflow still at its first step), so absence
    means the workflow never runs that step — not that it has yet to resolve it.
    """
    stale = []
    for wf in workflows.get("items", []):
        if wf.get("status", {}).get("phase") != "Running":
            continue
        tmpl = wf.get("status", {}).get("storedTemplates", {}).get(DL_TEMPLATE_KEY)
        if tmpl is None:
            continue
        body = " ".join(tmpl.get("container", {}).get("args", []))
        if tool in body:
            stale.append(wf["metadata"]["name"])
    return stale


def preflight(region: str, strict: bool) -> None:
    """Refuse (strict) or warn (dry run) if a Running workflow can recreate a tree.

    A dry run deletes nothing, so it only warns — that lets the scope be sized
    while a batch is still in flight. `--execute` refuses.
    """
    out = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "get", "workflows", "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    stale = stale_running_workflows(json.loads(out), RETIRED_TOOLS[region])
    if not stale:
        print(f"preflight: no Running workflow can recreate a {region} tree")
        return
    msg = (
        f"{len(stale)} Running workflow(s) may still publish {region} trees "
        f"(e.g. {', '.join(stale[:5])})"
    )
    if strict:
        sys.exit(f"REFUSING: {msg}. Wait for them to finish.")
    print(f"preflight WARNING: {msg} — --execute will refuse until they finish")


def list_subjects(s3) -> list[str]:
    subjects = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=BUCKET, Prefix=SUBREGIONS_ROOT, Delimiter="/"
    ):
        subjects += [p["Prefix"].split("/")[2] for p in page.get("CommonPrefixes", [])]
    return subjects


def list_keys(s3, prefix: str) -> list[str]:
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        keys += [o["Key"] for o in page.get("Contents", [])]
    return keys


def delete_keys(s3, keys: list[str]) -> int:
    """Bulk-delete keys, 1000 per call, exiting on any per-key error."""
    for i in range(0, len(keys), 1000):
        chunk = keys[i : i + 1000]
        resp = s3.delete_objects(
            Bucket=BUCKET,
            Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
        )
        if resp.get("Errors"):
            sys.exit(f"delete_objects reported errors: {resp['Errors'][:3]}")
    return len(keys)


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--region", required=True, choices=sorted(RETIRED_TOOLS))
    p.add_argument("--execute", action="store_true", help="actually delete (default: dry run)")
    p.add_argument("--workers", type=int, default=32)
    args = p.parse_args()

    preflight(args.region, strict=args.execute)

    s3 = boto3.client("s3", region_name=REGION, config=Config(max_pool_connections=args.workers))
    subjects = list_subjects(s3)
    print(f"{len(subjects)} subjects under s3://{BUCKET}/{SUBREGIONS_ROOT}")

    def collect(subj: str) -> list[str]:
        return list_keys(s3, f"{SUBREGIONS_ROOT}{subj}/{args.region}/")

    with ThreadPoolExecutor(args.workers) as pool:
        per_subject = list(pool.map(collect, subjects))
    keys = [k for ks in per_subject for k in ks]
    with_tree = sum(1 for ks in per_subject if ks)
    markers = sum(1 for k in keys if k.endswith("/_complete.json"))
    print(
        f"{with_tree} subjects carry a {args.region}/ tree: {len(keys)} objects, {markers} markers"
    )

    if not args.execute:
        print("dry run — nothing deleted. Re-run with --execute to delete (irreversible).")
        return

    # Markers first: if this dies partway, no remaining tree still claims to be
    # complete, so nothing can mistake a half-deleted tree for a whole one.
    marker_keys = [k for k in keys if k.endswith("/_complete.json")]
    other_keys = [k for k in keys if not k.endswith("/_complete.json")]
    deleted = delete_keys(s3, marker_keys) + delete_keys(s3, other_keys)
    print(f"deleted {deleted} objects")

    with ThreadPoolExecutor(args.workers) as pool:
        remaining = sum(len(ks) for ks in pool.map(collect, subjects))
    if remaining:
        sys.exit(f"verification FAILED: {remaining} objects remain")
    print(f"verified: no {args.region} objects remain")


if __name__ == "__main__":
    main()
