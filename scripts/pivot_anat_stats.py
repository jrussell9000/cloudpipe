"""
pivot_anat_stats.py — Split the long-format anatomical stats (from
aggregate_anat_stats.py) into one WIDE table per original stats file: one row
per subject x session, one column per `{structure}__{measure}`.

Reads only the local shards; nothing here touches S3.

  pixi run python scripts/pivot_anat_stats.py --shards data/anat-stats --out data/anat-stats/wide

Output:
  wide/aseg.stats.parquet, wide/lh.aparc.DKTatlas.mapped.stats.parquet, ...
  wide/thalamus__ThalamicNuclei.long.volumes.txt.parquet, ...   (subregions)
  wide/long-template/*.parquet                                  (one row per subject)
  wide/columns.csv                                              (unit + coverage per column)

A session whose file is missing has no row in that file's table, so a NULL cell
always means "structure not reported", never "file missing". Join a table to
qc.parquet, or to another table, on (subject, session).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from anat_stats.wide import SHARD_GLOB, build_wide  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--shards",
        required=True,
        type=Path,
        help="Directory holding the shard-*.parquet files from aggregate_anat_stats.py.",
    )
    p.add_argument("--out", required=True, type=Path, help="Directory for the wide tables.")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not any(args.shards.glob(SHARD_GLOB)):
        raise SystemExit(f"No {SHARD_GLOB} files in {args.shards}")
    build_wide(args.shards, args.out)


if __name__ == "__main__":
    main()
