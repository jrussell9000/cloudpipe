"""Calibrate the recompute harness against cloudpipe's own in-pod records.

The gate in design D2: for runs cloudpipe already processed, the record this
harness recomputes from the outputs must equal the record `images/afni/preproc.py`
wrote in-pod. Until it does, a measured arm difference cannot be separated from a
harness difference, and nothing downstream of it means anything.

This is the real-data half and it does not run in CI: it needs the metrics store
and the per-run outputs out of S3, and CI's test job carries no AWS
credentials. Run it in-region, against real runs, before the pilot:

    PYTHONPATH=src python -m equivalence.calibrate --bucket <bucket> --limit 20

The in-process half — that the recompute envelope feeds the shared measurement
the same frame count, TR, mask and confound names the in-pod path does — is
`tests/equivalence/test_recompute.py`, and that one does run in CI.

Exit status is the gate: 0 only if every run compared agreed on all nine fields.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .confound_map import CLOUDPIPE
from .recompute import (
    CALIBRATED_FIELDS,
    FUNC_QC_SCHEMA_VERSION,
    FieldMismatch,
    RunInputs,
    calibration_mismatches,
)
from .recompute import recompute_func_qc as _recompute

#: The legacy per-run archive, rooted at `{task}_{run}/` (the old driver's
#: `arcname`). A session republished since the exploded layout (ADR 017) holds
#: the three files below as separate objects in the session prefix instead.
FUNC_TARBALL = "{prefix}_space-MNI152NLin2009cAsym_bold.tar.gz"
MNI_BOLD = "{prefix}_space-MNI152NLin2009cAsym_bold.nii.gz"
MNI_MASK = "{prefix}_space-MNI152NLin2009cAsym_brainmask.nii.gz"
CONFOUNDS = "{prefix}_desc-confounds_timeseries.tsv"


@dataclass(frozen=True)
class RunResult:
    """One run's calibration outcome."""

    prefix: str
    mismatches: tuple[FieldMismatch, ...]
    skipped_reason: str = ""

    @property
    def agreed(self) -> bool:
        return not self.mismatches and not self.skipped_reason


def _prefix(record: dict) -> str:
    return "_".join(
        (str(record["subject"]), str(record["session"]), str(record["task"]), str(record["run"]))
    )


def _download_and_extract(s3, bucket: str, record: dict, dest: Path) -> Path:
    """Fetch the three files the harness reads for one run into a directory under `dest`.

    The exploded objects when the session has them; otherwise the legacy tarball,
    extracted (it is rooted at `{task}_{run}/`, so the members land one level down).
    """
    from botocore.exceptions import ClientError

    prefix = _prefix(record)
    session_prefix = f"derivatives/func/{record['subject']}/{record['session']}/"
    run_dir = dest / "exploded"
    run_dir.mkdir()
    try:
        for name in (MNI_BOLD, MNI_MASK, CONFOUNDS):
            name = name.format(prefix=prefix)
            s3.download_file(bucket, session_prefix + name, str(run_dir / name))
        return run_dir
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchKey"):
            raise

    key = session_prefix + FUNC_TARBALL.format(prefix=prefix)
    archive = dest / "run.tar.gz"
    s3.download_file(bucket, key, str(archive))
    with tarfile.open(archive) as tar:
        # filter="data" refuses absolute paths and traversal members. These are
        # our own archives, but the extraction runs wherever this is invoked.
        tar.extractall(dest, filter="data")
    archive.unlink()

    wanted = MNI_BOLD.format(prefix=prefix)
    for candidate in dest.rglob(wanted):
        return candidate.parent
    raise FileNotFoundError(f"{key}: no {wanted} inside the archive")


def calibrate_run(s3, bucket: str, record: dict) -> RunResult:
    """Recompute one run from its outputs and compare against its in-pod record."""
    prefix = _prefix(record)
    with tempfile.TemporaryDirectory(prefix="equivalence-calibrate-") as workdir:
        try:
            run_dir = _download_and_extract(s3, bucket, record, Path(workdir))
        except Exception as exc:  # a missing or unreadable output is not a mismatch
            return RunResult(prefix, (), f"{type(exc).__name__}: {exc}")

        inputs = RunInputs(
            mni_bold=run_dir / MNI_BOLD.format(prefix=prefix),
            brain_mask=run_dir / MNI_MASK.format(prefix=prefix),
            confounds_tsv=run_dir / CONFOUNDS.format(prefix=prefix),
            subject=str(record["subject"]),
            session=str(record["session"]),
            task=str(record["task"]),
            run=str(record["run"]),
            vocabulary=CLOUDPIPE,
            n_nss_frames=int(record.get("n_nss_frames", 0) or 0),
        )
        recomputed = _recompute(inputs, arm=str(record.get("pipeline", "cloudpipe_minproc")))

    return RunResult(prefix, tuple(calibration_mismatches(record, recomputed)))


def calibrate(bucket: str, limit: int, region: str | None = None, **filters) -> list[RunResult]:
    """Calibrate against up to `limit` cloudpipe runs from the metrics store.

    `filters` are passed to `CloudpipeMetrics.func_qc`, so a window can be scoped
    with `dt_from` / `dt_to` — worth doing, since those are the partition keys
    Athena prunes on.

    Only records at the schema version this harness recomputes are compared. An
    older record was measured by older code — before 1.3 (#859) its IQMs spanned
    the non-steady-state frames the recompute now drops — and a reprocessed run
    keeps its old record in the store beside the new one, while its outputs on S3
    are the new ones. Comparing across that boundary would fail a correct harness.
    """
    import boto3

    from metrics.athena import CloudpipeMetrics

    store = CloudpipeMetrics(bucket=bucket, region=region)
    usable = [r for r in current_records(store, **filters) if iqms_computed(r)][:limit]

    s3 = boto3.client("s3", region_name=region)
    results = []
    for i, record in enumerate(usable, 1):
        # One line per run, as it starts: a run is a ~350 MB download plus a
        # recompute, so twenty of them are silent for a quarter of an hour
        # without this. stderr, so the report on stdout stays parseable.
        print(f"[{i}/{len(usable)}] {_prefix(record)}", file=sys.stderr, flush=True)
        results.append(calibrate_run(s3, bucket, record))
    return results


def current_records(store, **filters) -> list[dict]:
    """The newest record per run, at the FuncQC schema version this harness measures.

    Older versions were measured by older code (see `calibrate`), and a run
    reprocessed twice at the same version has two records, of which only the
    later describes the outputs now on S3. `func_qc` orders by completed_at DESC,
    so the first record seen for a run is its newest.
    """
    filters.setdefault("schema_version", FUNC_QC_SCHEMA_VERSION)
    records = store.func_qc(**filters)
    if hasattr(records, "to_dict"):
        records = records.to_dict("records")
    newest, seen = [], set()
    for r in records:
        if r.get("schema_version") != FUNC_QC_SCHEMA_VERSION or _prefix(r) in seen:
            continue
        seen.add(_prefix(r))
        newest.append(r)
    return newest


def iqms_computed(record: dict) -> bool:
    """Were the in-pod IQMs actually computed for this run?

    0.0 on gcor/aor/aqi is the schema default meaning "not computed" (#119), not a
    value, so such a run would compare a real number against a placeholder.
    """
    return all(float(record.get(f, 0.0) or 0.0) != 0.0 for f in ("gcor", "aor", "aqi"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True, help="the cloudpipe bucket holding metrics/")
    parser.add_argument("--limit", type=int, default=20, help="runs to compare (default 20)")
    parser.add_argument("--region", default=None)
    parser.add_argument("--dt-from", default=None, help="inclusive dt= partition lower bound")
    parser.add_argument("--dt-to", default=None, help="inclusive dt= partition upper bound")
    parser.add_argument("--json", type=Path, default=None, help="write the report here as well")
    args = parser.parse_args(argv)

    results = calibrate(
        bucket=args.bucket,
        limit=args.limit,
        region=args.region,
        dt_from=args.dt_from,
        dt_to=args.dt_to,
    )

    compared = [r for r in results if not r.skipped_reason]
    failed = [r for r in compared if r.mismatches]

    for result in results:
        if result.skipped_reason:
            print(f"SKIP  {result.prefix}: {result.skipped_reason}")
        elif result.mismatches:
            print(f"FAIL  {result.prefix}")
            for mismatch in result.mismatches:
                print(f"        {mismatch}")
        else:
            print(f"OK    {result.prefix}")

    print(
        f"\n{len(compared) - len(failed)}/{len(compared)} runs agreed on "
        f"{len(CALIBRATED_FIELDS)} fields; {len(results) - len(compared)} skipped"
    )

    if args.json:
        args.json.write_text(
            json.dumps(
                [
                    {
                        "prefix": r.prefix,
                        "skipped_reason": r.skipped_reason,
                        "mismatches": [
                            {"field": m.field, "in_pod": m.in_pod, "recomputed": m.recomputed}
                            for m in r.mismatches
                        ],
                    }
                    for r in results
                ],
                indent=2,
                default=str,
            )
        )

    if not compared:
        print("no runs compared — the gate is unproven, which is not a pass")
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
