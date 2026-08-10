#!/usr/bin/env python3
"""
List subjects under s3://<YOUR_S3_BUCKET>/derivatives/fmriprep/ and write a CSV of
subject IDs (sub-NDARXXX format) for use with the first-level Prefect flow.

Usage
-----
    # Write to S3 (default):
    python generate_first_level_subjects.py

    # Write locally:
    python generate_first_level_subjects.py --output /tmp/first-level-subjects.csv

    # Restrict to specific sessions by checking for their existence on S3:
    python generate_first_level_subjects.py --require-session 00
"""

import argparse
import csv
import io
import sys

import boto3

BUCKET = "<YOUR_S3_BUCKET>"
PREFIX = "derivatives/fmriprep/"
DEFAULT_OUTPUT = "s3://<YOUR_S3_BUCKET>/first-level-subjects.csv"


def list_subjects(s3, bucket: str, prefix: str) -> list[str]:
    paginator = s3.get_paginator("list_objects_v2")
    subjects = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for p in page.get("CommonPrefixes", []):
            name = p["Prefix"].rstrip("/").split("/")[-1]
            if name.startswith("sub-"):
                subjects.append(name)
    subjects.sort()
    return subjects


def has_session(s3, bucket: str, prefix: str, subj: str, session: str) -> bool:
    resp = s3.list_objects_v2(
        Bucket=bucket,
        Prefix=f"{prefix}{subj}/ses-{session}A/",
        MaxKeys=1,
    )
    return resp.get("KeyCount", 0) > 0


def write_csv(subjects: list[str], output: str, s3) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["subject_id"])
    for subj in subjects:
        writer.writerow([subj])
    data = buf.getvalue().encode()

    if output.startswith("s3://"):
        _, _, rest = output.partition("s3://")
        out_bucket, _, key = rest.partition("/")
        s3.put_object(Bucket=out_bucket, Key=key, Body=data)
        print(f"Wrote {len(subjects)} subjects → s3://{out_bucket}/{key}")
    else:
        with open(output, "wb") as f:
            f.write(data)
        print(f"Wrote {len(subjects)} subjects → {output}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--bucket", default=BUCKET, help="S3 bucket containing fmriprep derivatives"
    )
    parser.add_argument("--prefix", default=PREFIX, help="S3 key prefix for fmriprep output")
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT, help="Destination path (local or s3://bucket/key)"
    )
    parser.add_argument(
        "--require-session",
        metavar="SESSION",
        help="Only include subjects that have a ses-{SESSION}A/ directory (e.g. '00')",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Cap the output at N subjects (applied after all filtering)",
    )
    args = parser.parse_args()

    s3 = boto3.client("s3")

    print(f"Listing subjects under s3://{args.bucket}/{args.prefix} …", file=sys.stderr)
    subjects = list_subjects(s3, args.bucket, args.prefix)
    print(f"  Found {len(subjects)} subject directories", file=sys.stderr)

    if args.require_session:
        before = len(subjects)
        subjects = [
            s
            for s in subjects
            if has_session(s3, args.bucket, args.prefix, s, args.require_session)
        ]
        print(
            f"  Filtered to {len(subjects)} subjects with ses-{args.require_session}A/ "
            f"({before - len(subjects)} skipped)",
            file=sys.stderr,
        )

    if args.limit is not None:
        subjects = subjects[: args.limit]
        print(f"  Limited to {len(subjects)} subjects", file=sys.stderr)

    write_csv(subjects, args.output, s3)


if __name__ == "__main__":
    main()
