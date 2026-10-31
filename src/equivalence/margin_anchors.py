"""Anchors for the functional margins, measured on cloudpipe alone.

The draft margins in `margins.yaml` were written on 2026-10-09 from remembered
ranges, before #859 moved every FuncQC summary to the steady-state frames. Two
of those ranges no longer describe the data: gcor's "0.12-0.26" came from
records that spanned the dummy-scan transient (steady-state gcor is closer to
0.01-0.06), and tSNR roughly doubled on the runs it affected. This measures what
the margins should be read against instead.

Two parts, and neither touches the reference arm, so neither can carry a result
into a margin (the spec requires the margins to precede the comparison):

1. `record_anchors`, from current FuncQC records alone, with no downloads. For
   each tier-2 metric: its distribution, its SD between runs of one session, its
   SD between sessions, and ICC(1). A margin can then be stated as a fraction of
   a measured spread.
2. `run_pair_anchor`, for a seeded random sample of sessions: two runs of one
   task compared by `endpoints.tier1`, the same code the two arms will be
   compared with. How closely cloudpipe agrees with ITSELF across two
   acquisitions of the same brain, minutes apart, is a natural scale for the
   tier-1 thresholds: a between-pipeline agreement no worse than that is
   difficult to call a meaningful difference. It is a scale, not a margin; how a
   threshold relates to it is still the operator's and a reviewer's decision.

Not anchored here: the grayordinate tail. It is a temporal correlation, and two
runs share no timeline, so there is no within-pipeline analogue to measure.

Run in-region, like calibrate. Part 2 holds two runs' BOLD in memory at once
(~2 x 1.7 GB, plus the tSNR maps), so budget ~8 GB:

    PYTHONPATH=src python -m equivalence.margin_anchors --bucket <bucket> \
        --session ses-00A --pairs 20 --json anchors.json

`--pairs 0` runs part 1 only.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import nibabel as nib
import numpy as np

from .calibrate import MNI_BOLD, MNI_MASK, _download_and_extract, _prefix, current_records
from .endpoints import TIER2_METRICS, canonical, tier1
from .equivalence import icc_one_way

PERCENTILES = (5, 25, 50, 75, 95)


def _intersection(mask_a, mask_b) -> nib.Nifti1Image:
    a, b = canonical(mask_a), canonical(mask_b)
    both = (np.asanyarray(a.dataobj) > 0) & (np.asanyarray(b.dataobj) > 0)
    return nib.Nifti1Image(both.astype(np.uint8), a.affine)


def summarise(values) -> dict:
    """n, mean, SD and percentiles over the finite values."""
    v = np.asarray([x for x in values if x is not None], dtype=np.float64)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return {"n": 0}
    out = {
        "n": int(v.size),
        "mean": float(v.mean()),
        "sd": float(v.std(ddof=1)) if v.size > 1 else 0.0,
    }
    out.update({f"p{p}": float(np.percentile(v, p)) for p in PERCENTILES})
    return out


def _session(record: dict) -> str:
    return f"{record['subject']}_{record['session']}"


def spread(values, clusters) -> dict:
    """Between-run SD within a session, SD of session means, and ICC(1).

    The within-session SD is pooled over sessions with at least two runs:
    sqrt(sum of squared deviations from each session's mean / (N - k)). Two runs
    differ by sqrt(2) times it on average, which is the figure to set a paired
    between-pipeline margin against.
    """
    groups: dict[str, list[float]] = defaultdict(list)
    for value, cluster in zip(values, clusters, strict=True):
        if value is not None and np.isfinite(value):
            groups[cluster].append(float(value))
    multi = [np.asarray(g) for g in groups.values() if len(g) > 1]
    within = None
    if multi:
        ss = sum(float(((g - g.mean()) ** 2).sum()) for g in multi)
        dof = sum(g.size for g in multi) - len(multi)
        within = float(np.sqrt(ss / dof)) if dof > 0 else None
    means = [float(np.mean(g)) for g in groups.values()]
    flat = [v for g in groups.values() for v in g]
    labels = [c for c, g in groups.items() for _ in g]
    try:
        icc = icc_one_way(flat, labels)
    except ValueError:
        icc = None
    return {
        "sessions": len(groups),
        "within_session_sd": within,
        "between_run_difference_sd": within * np.sqrt(2) if within is not None else None,
        "between_session_sd": float(np.std(means, ddof=1)) if len(means) > 1 else None,
        "icc1": icc,
    }


def record_anchors(records: list[dict], metrics=TIER2_METRICS) -> dict:
    """Per task, per metric: distribution and spread, from records alone."""
    by_task: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_task[str(r["task"])].append(r)
    out = {}
    for task, rows in sorted(by_task.items()):
        clusters = [_session(r) for r in rows]
        out[task] = {}
        for metric in metrics:
            values = [_number(r.get(metric)) for r in rows]
            out[task][metric] = {**summarise(values), **spread(values, clusters)}
    return out


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pick_pairs(records: list[dict], task: str, n: int, seed: int) -> list[tuple[dict, dict]]:
    """`n` sessions drawn at random (seeded), each with its first two runs of `task`.

    The first two by run label rather than any two: ABCD acquires rest in
    back-to-back pairs, so runs 01 and 02 are the closest in time.
    """
    sessions: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if str(r["task"]) == task:
            sessions[_session(r)].append(r)
    eligible = sorted(k for k, rows in sessions.items() if len(rows) >= 2)
    chosen = random.Random(seed).sample(eligible, min(n, len(eligible)))
    pairs = []
    for key in sorted(chosen):
        a, b = sorted(sessions[key], key=lambda r: str(r["run"]))[:2]
        pairs.append((a, b))
    return pairs


def run_pair_anchor(s3, bucket: str, a: dict, b: dict) -> dict:
    """Tier 1 between two runs of one session, as the arms will be compared."""
    out = {"run_a": _prefix(a), "run_b": _prefix(b)}
    nss_a, nss_b = int(a.get("n_nss_frames") or 0), int(b.get("n_nss_frames") or 0)
    if nss_a != nss_b:
        return {**out, "skipped_reason": f"n_nss_frames differ ({nss_a} vs {nss_b})"}
    with tempfile.TemporaryDirectory(prefix="equivalence-anchors-") as workdir:
        try:
            (Path(workdir) / "a").mkdir()
            (Path(workdir) / "b").mkdir()
            dir_a = _download_and_extract(s3, bucket, a, Path(workdir) / "a")
            dir_b = _download_and_extract(s3, bucket, b, Path(workdir) / "b")
            mask_a = dir_a / MNI_MASK.format(prefix=_prefix(a))
            mask_b = dir_b / MNI_MASK.format(prefix=_prefix(b))
            result = tier1(
                dir_a / MNI_BOLD.format(prefix=_prefix(a)),
                mask_a,
                dir_b / MNI_BOLD.format(prefix=_prefix(b)),
                mask_b,
                tsnr_median_a=_number(a.get("tsnr_median")),
                tsnr_median_b=_number(b.get("tsnr_median")),
                # The anchors were measured over the two runs' mask intersection,
                # and are recomputed that way so the committed figures reproduce.
                template_mask=_intersection(mask_a, mask_b),
                n_nss_frames=nss_a,
            )
        except Exception as exc:  # a missing or unreadable output is a skip, reported
            return {**out, "skipped_reason": f"{type(exc).__name__}: {exc}"}
    return {
        **out,
        "skipped_reason": "",
        "mean_image_correlation": result.mean_image.correlation,
        "tsnr_map_correlation": result.tsnr_map.correlation,
        "brain_mask_dice": result.mask_dice,
        "tsnr_median_difference": result.tsnr_median_difference,
        "n_voxels": result.mean_image.n_voxels,
    }


PAIR_FIELDS = (
    "mean_image_correlation",
    "brain_mask_dice",
    "tsnr_map_correlation",
    "tsnr_median_difference",
)


def anchors(
    bucket: str,
    pairs: int,
    task: str = "task-rest",
    seed: int = 0,
    region: str | None = None,
    **filters,
) -> dict:
    import boto3

    from metrics.athena import CloudpipeMetrics

    filters = {k: v for k, v in filters.items() if v is not None}
    records = current_records(CloudpipeMetrics(bucket=bucket, region=region), **filters)
    report = {
        "filters": filters,
        "records": len(records),
        "record_anchors": record_anchors(records),
        "pair_task": task,
        "pair_seed": seed,
        "pairs": [],
    }
    chosen = pick_pairs(records, task, pairs, seed) if pairs else []
    s3 = boto3.client("s3", region_name=region) if chosen else None
    for i, (a, b) in enumerate(chosen, 1):
        print(f"[{i}/{len(chosen)}] {_prefix(a)} vs run {b['run']}", file=sys.stderr, flush=True)
        report["pairs"].append(run_pair_anchor(s3, bucket, a, b))
    measured = [p for p in report["pairs"] if not p["skipped_reason"]]
    report["pair_summary"] = {f: summarise(p[f] for p in measured) for f in PAIR_FIELDS}
    return report


def _fmt(value) -> str:
    return "-" if value is None else f"{value:.4g}"


def render(report: dict) -> str:
    lines = [f"{report['records']} current FuncQC records, filters {report['filters']}", ""]
    for task, metrics in report["record_anchors"].items():
        lines.append(f"task {task}")
        lines.append(
            f"  {'metric':<20}{'n':>7}{'p5':>10}{'median':>10}{'p95':>10}"
            f"{'run-diff SD':>13}{'session SD':>12}{'ICC(1)':>8}"
        )
        for metric, s in metrics.items():
            if not s.get("n"):
                continue
            lines.append(
                f"  {metric:<20}{s['n']:>7}{_fmt(s['p5']):>10}{_fmt(s['p50']):>10}"
                f"{_fmt(s['p95']):>10}{_fmt(s['between_run_difference_sd']):>13}"
                f"{_fmt(s['between_session_sd']):>12}{_fmt(s['icc1']):>8}"
            )
        lines.append("")
    if report["pairs"]:
        skipped = [p for p in report["pairs"] if p["skipped_reason"]]
        lines.append(
            f"run pairs, task {report['pair_task']} (seed {report['pair_seed']}): "
            f"{len(report['pairs']) - len(skipped)} measured, {len(skipped)} skipped"
        )
        for field, s in report["pair_summary"].items():
            if s.get("n"):
                lines.append(
                    f"  {field:<26} p5 {_fmt(s['p5'])}  median {_fmt(s['p50'])}  "
                    f"p95 {_fmt(s['p95'])}  (n={s['n']})"
                )
        for p in skipped:
            lines.append(f"  SKIP {p['run_a']} / {p['run_b']}: {p['skipped_reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--session", default=None, help="e.g. ses-00A, the study's sampling rule")
    parser.add_argument("--pairs", type=int, default=20, help="sessions for part 2; 0 skips it")
    parser.add_argument(
        "--task", default="task-rest", help="task whose run pairs part 2 compares, as recorded"
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--region", default=None)
    parser.add_argument("--dt-from", default=None)
    parser.add_argument("--dt-to", default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    report = anchors(
        bucket=args.bucket,
        pairs=args.pairs,
        task=args.task,
        seed=args.seed,
        region=args.region,
        session=args.session,
        dt_from=args.dt_from,
        dt_to=args.dt_to,
    )
    print(render(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str))
    if not report["records"]:
        print("no current records matched; nothing is anchored")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
