"""Build the anatomical-QC reject list (src/metrics/anat_qc_rejects.py, #1205).

Reads every fsqc_qc record from the metrics bucket and writes the sessions whose
con_snr is below the floor to s3://<data bucket>/config/anat_qc_rejected.csv.
Compacted parquet is used for the days the compactor has reached, and raw JSON for
the rest. A session's newest record wins, so a session rebuilt and re-QC'd is
judged on its new anatomy.

Prints counts only: the list itself is keyed by subject ID (DUA).

Usage:
  pixi run -e ops python scripts/build_anat_qc_rejects.py --data-bucket "$CLOUDPIPE_BUCKET" --metrics-bucket "$CLOUDPIPE_METRICS_BUCKET"          # dry run
  pixi run -e ops python scripts/build_anat_qc_rejects.py --data-bucket "$CLOUDPIPE_BUCKET" --metrics-bucket "$CLOUDPIPE_METRICS_BUCKET" --write

Rebuild the list after any anatomy rebuild.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from metrics import anat_qc_rejects  # noqa: E402
from metrics.compactor import RAW_PREFIXES, read_raw_records_counting_skips  # noqa: E402

TABLE = "fsqc_qc"
COMPACTED = f"metrics/compacted/{TABLE}/"
RAW = RAW_PREFIXES[TABLE]
COLUMNS = ["subject", "session", "con_snr_lh", "con_snr_rh"]


def _dt(key: str) -> str:
    return next(p.removeprefix("dt=") for p in key.split("/") if p.startswith("dt="))


def _keys(s3, bucket: str, prefix: str) -> list[str]:
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out += [o["Key"] for o in page.get("Contents", [])]
    return out


def read_records(s3, bucket: str) -> list[dict]:
    """Every fsqc_qc record, each with its `dt`."""
    import pyarrow.parquet as pq

    records = []
    files = [k for k in _keys(s3, bucket, COMPACTED) if k.endswith(".parquet")]
    for key in files:
        table = pq.read_table(io.BytesIO(s3.get_object(Bucket=bucket, Key=key)["Body"].read()))
        cols = [c for c in COLUMNS if c in table.column_names]
        records += [{**r, "dt": _dt(key)} for r in table.select(cols).to_pylist()]
    compacted_days = {_dt(k) for k in files}
    raw_days = {
        p["Prefix"][len(RAW) :].strip("/").removeprefix("dt=")
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=RAW, Delimiter="/"
        )
        for p in page.get("CommonPrefixes", [])
    }
    for day in sorted(raw_days - compacted_days):
        raw, skipped = read_raw_records_counting_skips(
            s3, bucket, _keys(s3, bucket, f"{RAW}dt={day}/")
        )
        if skipped:
            # A skipped key may be a reject: the list would quietly lose it.
            raise SystemExit(f"{skipped} unreadable fsqc record(s) on {day}; not writing a list")
        records += [{**{c: r.get(c) for c in COLUMNS}, "dt": day} for r in raw]
    print(
        f"fsqc_qc: {len(files)} compacted file(s) over {len(compacted_days)} day(s), "
        f"{len(raw_days - compacted_days)} uncompacted day(s), {len(records)} record(s)"
    )
    return records


def newest(records: list[dict]) -> list[dict]:
    """One record per (subject, session): the newest `dt`. Within a day the later one
    read wins, which is the order the compactor wrote them in."""
    latest = {}
    for r in sorted(records, key=lambda r: r["dt"]):
        latest[(r["subject"], r["session"])] = r
    return list(latest.values())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-bucket", required=True)
    ap.add_argument("--metrics-bucket", required=True)
    ap.add_argument(
        "--region", help="AWS region (default: AWS_REGION, or the active AWS profile's region)."
    )
    ap.add_argument("--write", action="store_true", help="upload the list (default: dry run)")
    args = ap.parse_args(argv)

    s3 = boto3.client("s3", region_name=args.region)
    sessions = newest(read_records(s3, args.metrics_bucket))
    rows = anat_qc_rejects.rejects(sessions)
    subjects = len({r["subject"] for r in rows})
    print(
        f"{len(sessions)} session(s) with fsqc; {len(rows)} rejected "
        f"(min con_snr < {anat_qc_rejects.CON_SNR_FLOOR}) across {subjects} subject(s)"
    )
    if not args.write:
        print(f"dry run: s3://{args.data_bucket}/{anat_qc_rejects.KEY} not written")
        return 0
    s3.put_object(
        Bucket=args.data_bucket,
        Key=anat_qc_rejects.KEY,
        Body=anat_qc_rejects.to_csv(rows).encode(),
        ContentType="text/csv",
    )
    print(f"wrote s3://{args.data_bucket}/{anat_qc_rejects.KEY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
