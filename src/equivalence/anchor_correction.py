"""Design D20: the tier-1 anchors re-read with run b on an independent anatomy and warp.

The committed tier-1 values were read off cloudpipe run pairs sharing one
anatomy and one T1w→MNI warp (`margin_anchors`). D20 corrects the construct:
the same 200 pairs, with run b reprocessed by the `fmriprep-bench-anchor`
workflow through a cross-sectional FastSurfer anatomy and a fresh warp, and no
fMRIPrep output anywhere. For each pair this computes `endpoints.tier1` twice
under the current rule (template `res-02` mask minus voxels without signal):

- same-warp: run a against run b as published;
- corrected: run a against run b from `derivatives/fmriprep_bench/anchors/`.

It then applies D20's rule mechanically. Each value keeps the percentile rank it
held in the committed pooled same-warp distribution, re-read on the corrected
pooled distribution (`numpy.percentile`, linear). r values are rounded to two
decimals, as the committed ones were. Nothing here chooses a number.

    PYTHONPATH=src python -m equivalence.anchor_correction --bucket <b> \\
        --pairs d20-pairs.csv --template-mask <TF res-02 brain mask> --json d20.json

The pair list and the JSON name subjects: keep both out of the repository.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path

import numpy as np

from .calibrate import MNI_BOLD, MNI_MASK, _download_and_extract
from .endpoints import tier1
from .run_endpoints import verify_template_mask

#: The frozen pair list (task 3.7): the committed anchors' 200 pairs.
PAIRS_SHA256 = "4726fa25b54fd61bebf5cc31023cb7f6fb35bf9ace220076299ae847d50bb88c"
ANCHOR_ROOT = "derivatives/fmriprep_bench/anchors"

#: entry -> (the corrected distribution it is read on, the rank it held).
RANKS: dict[str, tuple[str, float]] = {
    "mean_image_correlation.threshold": ("mean_image_correlation", 6.0),
    "mean_image_low_share.below": ("mean_image_correlation", 1.5),
    "tsnr_map_correlation.threshold": ("tsnr_map_correlation", 22.0),
}


def _record(prefix: str) -> dict:
    subject, session, task, run = prefix.split("_")
    return {"subject": subject, "session": session, "task": task, "run": run}


def read_pairs(path: Path) -> list[tuple[str, str, str]]:
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if digest != PAIRS_SHA256:
        raise SystemExit(f"{path}: SHA-256 {digest} is not the frozen D20 pair list")
    with open(path, newline="") as fh:
        return [(r["task"], r["run_a"], r["run_b"]) for r in csv.DictReader(fh)]


def _anchor_dir(s3, bucket: str, prefix: str, dest: Path) -> Path:
    rec = _record(prefix)
    root = f"{ANCHOR_ROOT}/{rec['subject']}/{rec['session']}/func/"
    for name in (MNI_BOLD, MNI_MASK):
        name = name.format(prefix=prefix)
        s3.download_file(bucket, root + name, str(dest / name))
    return dest


def _tier1(dir_a: Path, a: str, dir_b: Path, b: str, template_mask, nss: int) -> dict:
    t = tier1(
        dir_a / MNI_BOLD.format(prefix=a),
        dir_a / MNI_MASK.format(prefix=a),
        dir_b / MNI_BOLD.format(prefix=b),
        dir_b / MNI_MASK.format(prefix=b),
        template_mask=template_mask,
        n_nss_frames=nss,
    )
    return {
        "mean_image_correlation": t.mean_image.correlation,
        "tsnr_map_correlation": t.tsnr_map.correlation,
        "n_no_signal": t.n_no_signal,
    }


def measure_pair(s3, bucket: str, task: str, run_a: str, run_b: str, nss: int, template_mask):
    """Both comparisons for one pair, or a skip with its reason."""
    out = {"task": task, "run_a": run_a, "run_b": run_b}
    with tempfile.TemporaryDirectory(prefix="d20-") as tmp:
        try:
            dirs = {k: Path(tmp) / k for k in ("a", "b", "b_anchor")}
            for d in dirs.values():
                d.mkdir()
            dir_a = _download_and_extract(s3, bucket, _record(run_a), dirs["a"])
            dir_b = _download_and_extract(s3, bucket, _record(run_b), dirs["b"])
            dir_b2 = _anchor_dir(s3, bucket, run_b, dirs["b_anchor"])
            out["same_warp"] = _tier1(dir_a, run_a, dir_b, run_b, template_mask, nss)
            out["corrected"] = _tier1(dir_a, run_a, dir_b2, run_b, template_mask, nss)
        except Exception as exc:  # a missing or unreadable output is a skip, reported
            return {**out, "skipped_reason": f"{type(exc).__name__}: {exc}"}
    return {**out, "skipped_reason": ""}


def corrected_values(pairs: list[dict]) -> dict:
    """D20's rule applied to the measured pairs. Pure: no choice is made here."""
    measured = [p for p in pairs if not p["skipped_reason"]]
    if not measured:
        raise ValueError("no measured pairs")

    def pooled(kind: str, field: str) -> np.ndarray:
        return np.array([p[kind][field] for p in measured], dtype=np.float64)

    values = {}
    for entry, (field, rank) in RANKS.items():
        values[entry] = round(float(np.percentile(pooled("corrected", field), rank)), 2)

    by_task: dict[str, dict[str, list[float]]] = {}
    for p in measured:
        for field in ("mean_image_correlation", "tsnr_map_correlation"):
            by_task.setdefault(p["task"], {}).setdefault(field, []).append(p["corrected"][field])
    for field in ("mean_image_correlation", "tsnr_map_correlation"):
        lowest = min(float(np.median(v[field])) for v in by_task.values())
        values[f"sample_size.assumed.{field}"] = round(lowest, 3)
    below = values["mean_image_low_share.below"]
    share = float(np.mean(pooled("corrected", "mean_image_correlation") < below))
    values["sample_size.assumed.mean_image_low_share"] = round(share, 3)

    summary = {}
    for kind in ("same_warp", "corrected"):
        for field in ("mean_image_correlation", "tsnr_map_correlation"):
            x = pooled(kind, field)
            summary[f"{kind}.{field}"] = {
                f"p{q:g}": float(np.percentile(x, q)) for q in (1.5, 5, 6, 22, 25, 50)
            }
    return {"n_measured": len(measured), "n_skipped": len(pairs) - len(measured),
            "values": values, "summary": summary}  # fmt: skip


_S3 = None


def _measure_job(bucket: str, region: str | None, template_mask: str, job: tuple) -> dict:
    """One pair in a worker process, with that process's own S3 client."""
    global _S3
    if _S3 is None:
        import boto3

        _S3 = boto3.client("s3", region_name=region)
    task, a, b, nss = job
    return measure_pair(_S3, bucket, task, a, b, nss, template_mask)


def _key(result: dict) -> tuple[str, str, str]:
    return result["task"], result["run_a"], result["run_b"]


def load_checkpoint(path: Path) -> dict[tuple[str, str, str], dict]:
    """Measured pairs from earlier runs, newest per pair. A skipped pair is measured again."""
    done: dict[tuple[str, str, str], dict] = {}
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if line.strip():
                result = json.loads(line)
                done[_key(result)] = result
    return {k: v for k, v in done.items() if not v["skipped_reason"]}


def run_pairs(jobs: list[tuple], measure, checkpoint: Path, workers: int) -> list[dict]:
    """Every pair's result in the frozen list's order, resuming from `checkpoint`.

    Each result is appended to the checkpoint the moment it exists, so an
    interrupted run loses at most the pairs in flight.
    """
    done = load_checkpoint(checkpoint)
    todo = [j for j in jobs if j[:3] not in done]
    print(f"{len(done)} pairs already measured, {len(todo)} to go", file=sys.stderr, flush=True)
    with open(checkpoint, "a") as out:

        def record(result: dict) -> None:
            done[_key(result)] = result
            out.write(json.dumps(result) + "\n")
            out.flush()
            print(f"[{len(done)}/{len(jobs)}] {result['task']}", file=sys.stderr, flush=True)

        if workers <= 1:
            for job in todo:
                record(measure(job))
        else:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                for result in pool.map(measure, todo):
                    record(result)
    return [done[j[:3]] for j in jobs if j[:3] in done] + [
        {"task": j[0], "run_a": j[1], "run_b": j[2], "skipped_reason": "not measured"}
        for j in jobs
        if j[:3] not in done
    ]


def main(argv: list[str] | None = None) -> int:
    import boto3

    from .frame import read_nss

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bucket", required=True)
    p.add_argument("--region")
    p.add_argument("--pairs", type=Path, required=True)
    p.add_argument("--template-mask", type=Path, required=True)
    p.add_argument("--json", type=Path, required=True)
    p.add_argument(
        "--checkpoint",
        type=Path,
        help="JSONL of measured pairs, appended as each finishes and read back on a "
        "rerun (default: the --json path with .pairs.jsonl)",
    )
    p.add_argument("--workers", type=int, default=3, help="pairs measured at once (~6 GB each)")
    args = p.parse_args(argv)

    verify_template_mask(args.template_mask)
    pairs = read_pairs(args.pairs)
    nss = read_nss(boto3.client("s3", region_name=args.region), args.bucket)
    jobs = [
        (task, a, b, nss[(_record(a)["subject"], _record(a)["session"])]) for task, a, b in pairs
    ]
    checkpoint = args.checkpoint or args.json.with_suffix(".pairs.jsonl")
    measure = partial(_measure_job, args.bucket, args.region, str(args.template_mask))
    results = run_pairs(jobs, measure, checkpoint, args.workers)
    report = {"pairs_sha256": PAIRS_SHA256, "ranks": RANKS, "pairs": results}
    report.update(corrected_values(results))
    args.json.write_text(json.dumps(report, indent=1))
    print(json.dumps({k: report[k] for k in ("n_measured", "n_skipped", "values")}, indent=1))
    print(f"report sha256 {hashlib.sha256(args.json.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
