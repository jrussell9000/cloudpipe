"""The descriptive set from the cluster's per-session measurement (fmriprep-bench-describe).

The second half of methods 5.4, beside `describe` (which reads the session
table). No margin and no verdict. Two steps:

    python -m equivalence.describe_measures fetch --bucket <b> --draw <draw.csv> \\
        --from 1 --to 102 --out descriptive_all.jsonl
    python -m equivalence.describe_measures summarise --input descriptive_all.jsonl [--json out.json]

`fetch` concatenates every described session's `descriptive.jsonl`, gated on its
`_describe_complete.json`, into one local file (it names subjects: keep it
outside the repository). `summarise` reduces it to:

- **cortical grayordinates**: each session's median and 5th-percentile
  per-grayordinate temporal r, and its temporal-mean agreement, each averaged
  over the session's runs on Fisher z;
- **aCompCor**: per tissue, each session's mean cosine of the principal angles,
  over every retained component and over the leading five, and each pipeline's
  component count;
- **anatomy**: each global measure's two distributions, ICC(2,1) and Bland–Altman
  (cortical grey-matter volume, eTIV, mean thickness and white-surface area
  averaged over hemispheres, the 14 subcortical volumes); for the DKT regions,
  the distribution of their 62 ICC(2,1)s per measure; and the segmentation
  Dice, with how many sessions the grid guard refused;
- **warp folding**: each pipeline's folded-voxel counts and share of sessions
  with any fold, and the median Jacobian determinant.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .describe import Agreement, Distribution, agreement
from .equivalence import icc_2_1

DESCRIBE_ROOT = "derivatives/fmriprep_bench/describe"
DESCRIBE_MARKER = "_describe_complete.json"
_R_CLIP = 1.0 - 1e-7


def fetch(s3, bucket: str, draw: Path, first: int, last: int, out: Path) -> tuple[int, list[int]]:
    """(sessions fetched, positions with no published measurement)."""
    with open(draw, newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if first <= int(r["order"]) <= last]
    fetched, absent = 0, []
    with open(out, "w") as dest:
        for r in rows:
            root = f"{DESCRIBE_ROOT}/{r['subject']}/{r['session']}/"
            listed = s3.list_objects_v2(Bucket=bucket, Prefix=root + DESCRIBE_MARKER, MaxKeys=1)
            if not any(o["Key"] == root + DESCRIBE_MARKER for o in listed.get("Contents", [])):
                absent.append(int(r["order"]))
                continue
            body = s3.get_object(Bucket=bucket, Key=root + "descriptive.jsonl")["Body"].read()
            dest.write(body.decode().rstrip("\n") + "\n")
            fetched += 1
    return fetched, absent


def _key(r: dict) -> str:
    return f"{r['subject']}_{r['session']}"


def _fisher_mean(values: list[float]) -> float:
    v = np.arctanh(np.clip(np.asarray(values, dtype=float), -_R_CLIP, _R_CLIP))
    return float(np.tanh(v.mean()))


@dataclass(frozen=True)
class FoldingSummary:
    sessions: int
    sessions_with_any_fold: int
    folded_voxels: Distribution | None
    jacobian_median: Distribution


@dataclass(frozen=True)
class MeasuresSummary:
    n_sessions: int
    grayordinates: dict[str, Distribution]
    compcor: dict[str, dict[str, Distribution]]
    anatomy: list[Agreement]
    subcortical: list[Agreement]
    dkt_icc: dict[str, Distribution]
    segmentation: dict[str, Distribution]
    segmentation_refused: int
    folding: dict[str, FoldingSummary]
    notes: list[str] = field(default_factory=list)


def read_records(path: Path) -> dict[str, list[dict]]:
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for line in Path(path).read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            by_kind[r["kind"]].append(r)
    return dict(by_kind)


def _per_session(records: list[dict], value, fisher: bool = True) -> list[float]:
    """Each session's mean over its runs of `value(record)`: on Fisher z for a correlation."""
    by = defaultdict(list)
    for r in records:
        v = value(r)
        if v is not None and math.isfinite(v):
            by[_key(r)].append(v)
    reduce = _fisher_mean if fisher else (lambda v: float(np.mean(v)))
    return [reduce(by[k]) for k in sorted(by)]


def _mean_cosine(angles_deg: list[float]) -> float | None:
    return float(np.mean(np.cos(np.radians(angles_deg)))) if angles_deg else None


def _mean(*xs):
    vals = [x for x in xs if x is not None]
    return float(np.mean(vals)) if len(vals) == len(xs) else None


def _total(*xs):
    vals = [x for x in xs if x is not None]
    return float(np.sum(vals)) if len(vals) == len(xs) else None


GLOBAL_MEASURES = {
    "cortical grey-matter volume (mm³)": lambda a: a["cortex_volume"],
    "eTIV (mm³)": lambda a: a["etiv"],
    "mean cortical thickness (mm), hemisphere mean": lambda a: _mean(
        a["mean_thickness"]["lh"], a["mean_thickness"]["rh"]
    ),
    "white-surface area (mm²), both hemispheres": lambda a: _total(
        a["white_surface_area"]["lh"], a["white_surface_area"]["rh"]
    ),
}
ANATOMY_NOTE = (
    "FastSurfer longitudinal against FreeSurfer 7.3.2 cross-sectional: engine, "
    "processing design and FreeSurfer version differ at once, so this describes what "
    "a user of each pipeline gets and isolates none of them."
)


def _paired(records: list[dict], get) -> tuple[list[float], list[float]]:
    a, b = [], []
    for r in records:
        x, y = get(r["cloudpipe"]), get(r["fmriprep"])
        if x is not None and y is not None:
            a.append(float(x))
            b.append(float(y))
    return a, b


def summarise(path: Path) -> MeasuresSummary:
    rec = read_records(path)
    gray = [r for r in rec.get("grayordinates", []) if "absent" not in r]
    absent = [r for r in rec.get("grayordinates", []) if "absent" in r]
    anatomy = sorted(rec.get("anatomy", []), key=_key)
    sessions = {_key(r) for kind in rec.values() for r in kind}
    notes = []
    if absent:
        notes.append(f"{len(absent)} run(s) lacked a pipeline's dense time series")

    grayordinates = {
        "median per-grayordinate temporal r": Distribution.of(
            _per_session(gray, lambda r: r["median_temporal_correlation"])
        ),
        "5th-percentile per-grayordinate temporal r": Distribution.of(
            _per_session(gray, lambda r: r["tail_temporal_correlation"])
        ),
        "temporal-mean agreement r": Distribution.of(
            _per_session(gray, lambda r: r["mean_agreement"])
        ),
    }

    compcor: dict[str, dict[str, Distribution]] = {}
    for tissue in ("wm", "csf"):
        rows = [
            (r, t) for r in rec.get("compcor", []) for t in r["tissues"] if t["tissue"] == tissue
        ]
        compcor[tissue] = {
            "mean cosine, all retained": Distribution.of(
                _per_session([r | {"_v": _mean_cosine(t["all_retained"]["angles_deg"])} for r, t in rows], lambda r: r["_v"], fisher=False)
            ),
            "mean cosine, leading five": Distribution.of(
                _per_session([r | {"_v": _mean_cosine(t["leading"]["angles_deg"])} for r, t in rows], lambda r: r["_v"], fisher=False)
            ),
            "components, cloudpipe": Distribution.of([t["all_retained"]["n_cloudpipe"] for _, t in rows]),
            "components, fMRIPrep": Distribution.of([t["all_retained"]["n_fmriprep"] for _, t in rows]),
        }  # fmt: skip

    global_measures = [
        agreement(name, *_paired(anatomy, get), ANATOMY_NOTE)
        for name, get in GLOBAL_MEASURES.items()
    ]
    names = list(anatomy[0]["cloudpipe"]["subcortical_volume"]) if anatomy else []
    subcortical = []
    for n in names:
        a, b = _paired(anatomy, lambda x, n=n: x["subcortical_volume"].get(n))
        if len(a) < 3:
            notes.append(
                f"{n}: {len(a)} session(s) with a volume from both pipelines; not summarised"
            )
            continue
        subcortical.append(agreement(f"{n} volume (mm³)", a, b))
    dkt_icc = {}
    regions = sorted(anatomy[0]["cloudpipe"]["dkt"]) if anatomy else []
    for measure in ("ThickAvg", "SurfArea", "GrayVol"):
        iccs = []
        for region in regions:
            a, b = _paired(anatomy, lambda x, g=region, m=measure: (x["dkt"].get(g) or {}).get(m))
            if len(a) >= 3:
                iccs.append(icc_2_1(a, b, f"{region}.{measure}").icc)
        if len(iccs) >= 2:
            dkt_icc[measure] = Distribution.of(iccs)
        else:
            notes.append(f"DKT {measure}: {len(iccs)} region(s) with an ICC; not summarised")

    seg = [r["segmentation"] for r in anatomy]
    scored = [s for s in seg if s["scored"]]
    segmentation = {
        "mean Dice, 76 labels": Distribution.of([s["mean"] for s in scored]),
        "mean Dice, 14 subcortical": Distribution.of([s["mean_subcortical"] for s in scored]),
        "mean Dice, 62 DKT": Distribution.of([s["mean_dkt"] for s in scored]),
        "grid agreement r": Distribution.of([s["grid_agreement"] for s in seg]),
    }

    folding = {}
    fold = rec.get("folding", [])
    for arm in ("cloudpipe", "fmriprep"):
        counts = [r[arm]["folded"] for r in fold]
        folding[arm] = FoldingSummary(
            sessions=len(fold),
            sessions_with_any_fold=int(sum(c > 0 for c in counts)),
            folded_voxels=Distribution.of(counts) if len(counts) >= 2 else None,
            jacobian_median=Distribution.of([r[arm]["jacobian_median"] for r in fold]),
        )
    return MeasuresSummary(
        n_sessions=len(sessions),
        grayordinates=grayordinates,
        compcor=compcor,
        anatomy=global_measures,
        subcortical=subcortical,
        dkt_icc=dkt_icc,
        segmentation=segmentation,
        segmentation_refused=len(seg) - len(scored),
        folding=folding,
        notes=notes,
    )


def _d(d: Distribution, digits: int = 4) -> str:
    f = f"{{:.{digits}g}}"
    return (
        f"median {f.format(d.median)} [IQR {f.format(d.p25)}–{f.format(d.p75)}; "
        f"5th–95th {f.format(d.p5)}–{f.format(d.p95)}] (n={d.n})"
    )


def render(s: MeasuresSummary) -> str:
    lines = [
        f"Descriptive measures, {s.n_sessions} sessions. No margins; nothing here is a verdict."
    ]
    lines += [f"note: {n}" for n in s.notes]
    lines.append("Cortical grayordinates (59,412 shared fsLR vertices; session means over runs):")
    lines += [f"    {k:44s} {_d(v)}" for k, v in s.grayordinates.items()]
    for tissue, rows in s.compcor.items():
        lines.append(f"aCompCor, {tissue.upper()} (session means over runs):")
        lines += [f"    {k:44s} {_d(v)}" for k, v in rows.items()]
    lines.append("Anatomy, global (cloudpipe | fMRIPrep):")
    for a in s.anatomy + s.subcortical:
        ba = a.bland_altman
        lines.append(
            f"    {a.quantity:46s} {a.cloudpipe.median:.5g} | {a.fmriprep.median:.5g} (medians); "
            f"ICC {a.icc.icc:.3f} [{a.icc.ci_low:.3f}, {a.icc.ci_high:.3f}]; bias {ba.bias:+.4g}"
        )
    lines.append(f"    note: {ANATOMY_NOTE}")
    lines.append("DKT regions, ICC(2,1) across the 62 regions:")
    lines += [f"    {k:44s} {_d(v, 3)}" for k, v in s.dkt_icc.items()]
    lines.append(
        f"Segmentation Dice, fMRIPrep's mapped onto cloudpipe's grid ({s.segmentation_refused} "
        "session(s) refused by the grid guard):"
    )
    lines += [f"    {k:44s} {_d(v, 3)}" for k, v in s.segmentation.items()]
    lines.append("Warp folding (Jacobian determinant ≤ 0):")
    for arm, f in s.folding.items():
        folded = _d(f.folded_voxels, 3) if f.folded_voxels else "-"
        lines.append(
            f"    {arm:10s} sessions with any fold {f.sessions_with_any_fold} of {f.sessions}; "
            f"folded voxels {folded}; median det J {f.jacobian_median.median:.3f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--bucket", required=True)
    f.add_argument("--region")
    f.add_argument("--draw", type=Path, required=True)
    f.add_argument("--from", dest="first", type=int, default=1)
    f.add_argument("--to", dest="last", type=int, required=True)
    f.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("summarise")
    s.add_argument("--input", type=Path, required=True)
    s.add_argument("--json", type=Path)
    args = p.parse_args(argv)
    if args.command == "fetch":
        import boto3

        n, absent = fetch(
            boto3.client("s3", region_name=args.region),
            args.bucket, args.draw, args.first, args.last, args.out,
        )  # fmt: skip
        print(f"{n} sessions -> {args.out}; no measurement at positions {absent}")
        return 0
    summary = summarise(args.input)
    print(render(summary))
    if args.json:
        args.json.write_text(json.dumps(asdict(summary), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
