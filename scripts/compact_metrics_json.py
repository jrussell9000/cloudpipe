"""
Rewrite all cloudpipe metrics JSON files in S3 as compact (single-line) JSON.

Athena's TextInputFormat + JsonSerDe reads line-by-line; pretty-printed
multi-line JSON causes HIVE_CURSOR_ERROR. This script downloads each file,
re-serializes without indentation, and puts it back.

Usage:
  python scripts/compact_metrics_json.py --bucket <YOUR_S3_BUCKET> --dry-run
  python scripts/compact_metrics_json.py --bucket <YOUR_S3_BUCKET>
"""

import argparse
import json
import sys

import boto3

PREFIXES = [
    "metrics/func-preproc/",
    "metrics/registration/",
    "metrics/workflow-runs/",
    "metrics/anat-qc/",
]


def compact_prefix(s3, bucket: str, prefix: str, dry_run: bool) -> tuple[int, int]:
    updated = skipped = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".json"):
                continue
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            try:
                data = json.loads(body)
            except json.JSONDecodeError as e:
                print(f"  SKIP (invalid JSON): {key}: {e}", file=sys.stderr)
                skipped += 1
                continue
            compact = json.dumps(data).encode()
            if compact == body.rstrip():
                skipped += 1
                continue
            if dry_run:
                print(f"  [dry-run] would compact: {key} ({len(body)} → {len(compact)} bytes)")
            else:
                s3.put_object(Bucket=bucket, Key=key, Body=compact, ContentType="application/json")
                print(f"  compacted: {key} ({len(body)} → {len(compact)} bytes)")
            updated += 1
    return updated, skipped


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bucket", required=True)
    p.add_argument("--region", default="<YOUR_AWS_REGION>")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    s3 = boto3.client("s3", region_name=args.region)
    total_updated = total_skipped = 0
    for prefix in PREFIXES:
        print(f"\n{prefix}")
        updated, skipped = compact_prefix(s3, args.bucket, prefix, args.dry_run)
        print(f"  → {updated} updated, {skipped} unchanged/skipped")
        total_updated += updated
        total_skipped += skipped
    print(f"\nTotal: {total_updated} updated, {total_skipped} unchanged/skipped")
    if args.dry_run:
        print("(dry-run — no changes written)")


if __name__ == "__main__":
    main()
