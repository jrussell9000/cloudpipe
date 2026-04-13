#!/usr/bin/env python3
"""
Aggregate FreeSurfer stats files from a BIDS-format directory tree.

Expected layout:
    <bids_dir>/
        sub-<label>/
            ses-<label>/
                aseg.stats
                lh.aparc.stats
                rh.aparc.stats

Runs:
    asegstats2table  -> <output_dir>/aseg_stats.tsv
    aparcstats2table -> <output_dir>/lh_aparc_<measure>.tsv
    aparcstats2table -> <output_dir>/rh_aparc_<measure>.tsv

Files are processed in batches to stay within OS argument-length limits,
then concatenated into a single output table per stats type.
"""

import argparse
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

log = logging.getLogger(__name__)


def find_stats_files(bids_dir: Path, filename: str) -> list[Path]:
    """Return sorted list of matching stats files under sub-*/ses-*/ directories."""
    return sorted(bids_dir.glob(f"sub-*/ses-*/{filename}"))


def run(cmd: list[str], env: dict | None = None) -> None:
    log.debug("CMD: %s", " ".join(cmd))

    merged_env = os.environ | env if env else None
    result = subprocess.run(cmd, capture_output=True, text=True, env=merged_env)
    if result.returncode != 0:
        log.error("STDOUT: %s", result.stdout)
        log.error("STDERR: %s", result.stderr)
        raise RuntimeError(f"Command failed (exit {result.returncode}): {' '.join(cmd)}")
    if result.stdout:
        log.debug("STDOUT: %s", result.stdout)


def concatenate_tables(batch_files: list[Path], output: Path) -> None:
    """Concatenate TSV batch outputs, keeping the header from the first file only."""
    with output.open("w") as out_fh:
        for i, batch_file in enumerate(batch_files):
            lines = batch_file.read_text().splitlines(keepends=True)
            if i == 0:
                out_fh.writelines(lines)
            else:
                out_fh.writelines(lines[1:])  # skip repeated header


def run_batched(
    files: list[Path],
    cmd_factory,
    output: Path,
    batch_size: int
) -> None:
    """Run cmd_factory(batch, tmp_out) for each batch, then concatenate results."""
    batches = [files[i:i + batch_size] for i in range(0, len(files), batch_size)]
    n_batches = len(batches)
    log.info("Processing %d files in %d batch(es) of up to %d", len(files), n_batches, batch_size)

    batch_files: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="fs_stats_batches_") as tmpdir:
        with logging_redirect_tqdm():
            with tqdm(total=len(files), desc=output.name, unit="file") as pbar:
                for idx, batch in enumerate(batches):
                    tmp_out = Path(tmpdir) / f"batch_{idx:04d}.tsv"
                    cmd, env = cmd_factory(batch, tmp_out)
                    run(cmd, env)
                    batch_files.append(tmp_out)
                    pbar.update(len(batch))

        concatenate_tables(batch_files, output)


def aggregate_aseg(
    files: list[Path],
    output_dir: Path,
    delimiter: str,
    batch_size: int,
    dry_run: bool,
) -> None:
    if not files:
        log.warning("No aseg.stats files found — skipping aseg table.")
        return

    out = output_dir / "aseg_stats.tsv"

    def cmd_factory(batch: list[Path], tmp_out: Path) -> tuple[list[str], None]:
        return [
            "asegstats2table",
            "--inputs", *[str(f) for f in batch],
            "--delimiter", delimiter,
            "--tablefile", str(tmp_out),
        ], None

    run_batched(files, cmd_factory, out, batch_size, dry_run)


def aggregate_aparc(
    files: list[Path],
    hemi: str,
    measure: str,
    output_dir: Path,
    delimiter: str,
    batch_size: int
) -> None:
    if not files:
        log.warning("No %s.aparc.stats files found — skipping.", hemi)
        return

    out = output_dir / f"{hemi}_aparc_{measure}.tsv"
    parc = "aparc"

    def cmd_factory(batch: list[Path], tmp_out: Path) -> tuple[list[str], dict]:
        # aparcstats2table looks for subject_id/session/stats/lh.aparc.stats (e.g.,)
        # within SUBJECTS_DIR directory. We need to build that
        # structure with symlinks inside the existing temp directory and pass the
        # path via the SUBJECTS_DIR environment variable.
        subjects_dir = tmp_out.parent / f"subjects_{tmp_out.stem}"
        subject_ids = []
        for f in batch:
            parts = f.parts
            sub = next((p for p in parts if p.startswith("sub-")), None)
            ses = next((p for p in parts if p.startswith("ses-")), None)
            subject_id = f"{sub}_{ses}" if (sub and ses) else f.parent.name
            subject_ids.append(subject_id)
        return [
            "aparcstats2table",
            "--subjects", *subject_ids,
            "--parc", parc,
            "--hemi", hemi,
            "--meas", measure,
            "--delimiter", delimiter,
            "--tablefile", str(tmp_out),
        ], {"SUBJECTS_DIR": str(subjects_dir)}

    run_batched(files, cmd_factory, out, batch_size)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate FreeSurfer stats files from a BIDS directory tree."
    )
    parser.add_argument("bids_dir", type=Path, help="Root BIDS directory")
    parser.add_argument("output_dir", type=Path, help="Directory for output tables")
    parser.add_argument(
        "--measures",
        nargs="+",
        default=["thickness", "area", "volume"],
        metavar="MEASURE",
        help="aparc measures to extract (default: thickness area volume)",
    )
    parser.add_argument(
        "--delimiter",
        default="tab",
        choices=["tab", "space", "comma", "semicolon"],
        help="Column delimiter for output tables (default: tab)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        metavar="N",
        help="Files per FreeSurfer invocation — keep low enough to avoid ARG_MAX (default: 500)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    bids_dir: Path = args.bids_dir.resolve()
    output_dir: Path = args.output_dir.resolve()

    if not bids_dir.is_dir():
        log.error("bids_dir does not exist: %s", bids_dir)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    aseg_files = find_stats_files(bids_dir, "aseg.stats")
    lh_files = find_stats_files(bids_dir, "lh.aparc.stats")
    rh_files = find_stats_files(bids_dir, "rh.aparc.stats")

    log.info(
        "Found: %d aseg, %d lh.aparc, %d rh.aparc",
        len(aseg_files), len(lh_files), len(rh_files),
    )

    aggregate_aseg(aseg_files, output_dir, args.delimiter, args.batch_size)

    for measure in args.measures:
        aggregate_aparc(lh_files, "lh", measure, output_dir, args.delimiter, args.batch_size)
        aggregate_aparc(rh_files, "rh", measure, output_dir, args.delimiter, args.batch_size)


if __name__ == "__main__":
    main()
