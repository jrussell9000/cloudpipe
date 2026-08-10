#!/usr/bin/env python
"""pull_qc.py — pull all T1w->MNI QC JSONs for a batch from S3 into a CSV.

Reads   metrics/registration/*_t1w_to_mni_reg_qc.json   and emits one tidy row
per session so failures can be triaged by driving metric (see design spec
docs/superpowers/specs/2026-07-02-t1w-mni-registration-failures-investigation-design.md).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys

log = logging.getLogger(__name__)

# Verbatim from images/shared/registration_qc.py _T1W_MNI_THRESHOLDS.
_FAIL = {
    "dice": (0.82, "below"),
    "jac_det_frac_negative": (0.01, "above"),
    "centroid_displacement_mm": (15.0, "above"),
}


def driving_metrics(qc: dict) -> str:
    """Comma-joined metrics whose value crosses its fail threshold."""
    hit = []
    for key, (thr, direction) in _FAIL.items():
        if key not in qc:
            continue
        val = qc[key]
        if (direction == "below" and val < thr) or (direction == "above" and val > thr):
            hit.append(key)
    return ",".join(hit)


_FIELDS = [
    "subject",
    "session",
    "verdict",
    "dice",
    "jac_det_frac_negative",
    "jac_det_min",
    "centroid_displacement_mm",
    "driving_metrics",
    "completed_at",
]


def rows_from_qc_objects(objects: list[dict]) -> list[dict]:
    rows = []
    for qc in objects:
        row = {k: qc.get(k, "") for k in _FIELDS}
        row["driving_metrics"] = driving_metrics(qc)
        rows.append(row)
    return rows


def _fetch(bucket: str, region: str) -> list[dict]:
    import boto3

    s3 = boto3.client("s3", region_name=region)
    paginator = s3.get_paginator("list_objects_v2")
    objs = []
    for page in paginator.paginate(Bucket=bucket, Prefix="metrics/registration/"):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("_t1w_to_mni_reg_qc.json"):
                body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
                objs.append(json.loads(body))
    return objs


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )
    p = argparse.ArgumentParser()
    p.add_argument("--bucket", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--region", default="<YOUR_AWS_REGION>")
    args = p.parse_args()

    rows = rows_from_qc_objects(_fetch(args.bucket, args.region))
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_FIELDS)
        w.writeheader()
        w.writerows(rows)

    counts = {"pass": 0, "warn": 0, "fail": 0}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    log.info(f"Wrote {len(rows)} rows to {args.out}  counts={counts}")


if __name__ == "__main__":
    main()
