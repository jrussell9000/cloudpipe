"""Compare one subject's derivatives across two roots (openspec add-reprocess-audit, task 3.1).

Spec `audit-comparison`. Reads only; writes only --out. A root is a data bucket
(optionally with a key prefix before `derivatives/`), so the same code compares
production with the audit (fresh vs stored) and the audit with its repeat pair
(the noise floor). Layout is the production layout (docs/architecture.md).

Every measure is recorded as a DISTANCE: 0 means identical, larger means
further apart. Correlations and Dice become 1 - value; differences are
absolute. That one orientation is what lets classify() (task 3.4) treat every
measure alike: a fresh-vs-stored distance is compared with the largest distance
the noise floor produced for that measure.

Per subject, for each session present in either root:

  anatomy          FastSurfer tree: segmentation Dice (mean, min) through
                   equivalence.descriptive.segmentation_dice, brain.mgz voxel
                   mean abs difference, aseg/DKT measure relative differences
  subregions       every *volumes.txt under each region's session: the largest
                   relative volume difference
  t1w_to_mni       warped-T1w correlation, and the QC record beside the warp
                   (lncc, mask Dice, folding): the largest absolute difference
  bold_to_t1w      per run: mean displacement (mm) between the two rigid
                   transforms over the brain mask's voxels
  func             per run: MNI BOLD voxel equality and mean abs difference;
                   tier-1 mean-image and tSNR-map correlation and own-mask Dice
                   (equivalence.endpoints.tier1, when --template-mask is given);
                   confounds by family (confounds_difference): motion, signals, dvars_fd,
                   design as max relative difference; each CompCor family as a
                   subspace distance and a component-count difference;
                   boldref correlation
  tissue           dseg Dice (mean, min over GM/WM/CSF), probseg mean abs difference
                   over voxels in either map
  grayordinates    per run: fsLR32k dtseries 5th-percentile temporal correlation

Extremes (a max over voxels, a min Jacobian) are not recorded: one voxel moves by
the full range for any change at all, so they could not tell a defect from noise.

A derivative in one root only is reported as missing_in_a / missing_in_b, never
as a numeric difference (spec: "A derivative exists in one root only").

    pixi run -e ops python -m audit.compare --a-bucket <production> --b-bucket <audit> \\
        --subjects data/reprocess/audit-sample-2026-11-08/sample.jsonl \\
        --template-mask tpl-MNI152NLin2009cAsym_res-02_desc-brain_mask.nii.gz --out compare.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

REPO = Path(__file__).resolve().parents[2]
TASKS = ("task-rest", "task-nback")
SUBREGIONS = ("thalamus", "brainstem", "hippoamyg")
TISSUE_LABELS = ("GM", "WM", "CSF")
# Not jac_det_min: the smallest Jacobian determinant is one voxel of the warp, so the
# difference of two runs' minima is noise about any change (0.96 on 2026-11-09).
QC_FIELDS = ("lncc", "mask_dice", "jac_det_frac_negative")
SPACE = "space-MNI152NLin2009cAsym"


# ---------------------------------------------------------------------------
# Roots and read-only stores
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Root:
    """A data bucket (or, in tests, a local directory) and the prefix before `derivatives/`."""

    bucket: str
    prefix: str = ""

    def key(self, rel: str) -> str:
        return f"{self.prefix}{rel}"


class Store(Protocol):
    def list(self, root: Root, rel_prefix: str) -> list[str]: ...
    def fetch(self, root: Root, rel: str, dest: Path) -> bool: ...


class S3Store:
    """Read-only S3. Lists and gets; nothing here can write."""

    def __init__(self, s3):
        self.s3 = s3

    def list(self, root: Root, rel_prefix: str) -> list[str]:
        out: list[str] = []
        full = root.key(rel_prefix)
        for page in self.s3.get_paginator("list_objects_v2").paginate(
            Bucket=root.bucket, Prefix=full
        ):
            out += [o["Key"][len(root.prefix) :] for o in page.get("Contents", [])]
        return out

    def fetch(self, root: Root, rel: str, dest: Path) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.s3.download_file(root.bucket, root.key(rel), str(dest))
        except Exception as exc:  # noqa: BLE001 — a 404 is "not in this root"
            if "404" in str(exc) or "Not Found" in str(exc) or "NoSuchKey" in str(exc):
                return False
            raise
        return True


class LocalStore:
    """A directory tree laid out like a bucket (tests, and synced copies)."""

    def list(self, root: Root, rel_prefix: str) -> list[str]:
        base = Path(root.bucket)
        full = base / root.key(rel_prefix)
        parent = full if full.is_dir() else full.parent
        if not parent.exists():
            return []
        keys = [p.relative_to(base).as_posix() for p in parent.rglob("*") if p.is_file()]
        return sorted(k[len(root.prefix) :] for k in keys if k.startswith(root.key(rel_prefix)))

    def fetch(self, root: Root, rel: str, dest: Path) -> bool:
        src = Path(root.bucket) / root.key(rel)
        if not src.is_file():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return True


# ---------------------------------------------------------------------------
# Distances (0 = identical)
# ---------------------------------------------------------------------------


def _load(path: Path):
    import nibabel as nib

    return nib.load(str(path))


def _array(path: Path) -> np.ndarray:
    return np.asarray(_load(path).dataobj)


def rel_diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    if a == b:
        return 0.0
    scale = max(abs(a), abs(b))
    return abs(a - b) / scale if scale else 0.0


def correlation_distance(a: np.ndarray, b: np.ndarray) -> float:
    """1 - Pearson r over voxels both images define; 0 for identical images."""
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    if a.shape != b.shape:
        raise ValueError(f"grids differ: {a.shape} vs {b.shape}")
    if np.array_equal(a, b):
        return 0.0
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    if a.std() == 0 or b.std() == 0:
        return 1.0
    return float(1.0 - np.corrcoef(a, b)[0, 1])


def voxel_difference(
    a: np.ndarray, b: np.ndarray, within: np.ndarray | None = None
) -> dict[str, float]:
    """Decompressed comparison: .nii.gz bytes depend on the compressor, voxels do not.

    `mean_abs_diff` is what the audit records: a max is one voxel's extreme, and any
    change at all moves a boundary voxel by the full range (a probseg from 0 to 1).
    `max_abs_diff` is kept for diagnosis. The mean is over `within` (a boolean array
    of a's shape, e.g. voxels nonzero in either image) when given, else every voxel.
    """
    if a.shape != b.shape:
        return {"same_grid": 0.0, "voxels_differ_frac": 1.0, "max_abs_diff": math.inf,
                "mean_abs_diff": math.inf}  # fmt: skip
    if np.array_equal(a, b):  # the expected case, and free of a full-size difference array
        return {
            "same_grid": 1.0,
            "voxels_differ_frac": 0.0,
            "max_abs_diff": 0.0,
            "mean_abs_diff": 0.0,
        }
    # A 2 mm MNI BOLD run is ~1.7 GB as float32: difference it a slab at a time.
    fa, fb = a.reshape(a.shape[0], -1), b.reshape(b.shape[0], -1)
    fw = within.reshape(within.shape[0], -1) if within is not None else None
    differ, worst, total, n = 0, 0.0, 0.0, 0
    for i in range(0, fa.shape[0], 8):
        d = np.abs(fa[i : i + 8].astype(np.float32) - fb[i : i + 8].astype(np.float32))
        differ += int(np.count_nonzero(d))
        worst = max(worst, float(d.max()) if d.size else 0.0)
        if fw is not None:
            d = d[fw[i : i + 8]]
        total += float(d.sum(dtype=np.float64))
        n += d.size
    return {"same_grid": 1.0, "voxels_differ_frac": differ / a.size, "max_abs_diff": worst,
            "mean_abs_diff": total / n if n else 0.0}  # fmt: skip


def label_dice_distances(a: np.ndarray, b: np.ndarray, labels) -> dict[int, float]:
    out = {}
    for lab in labels:
        x, y = a == lab, b == lab
        total = x.sum() + y.sum()
        out[int(lab)] = 0.0 if total == 0 else float(1.0 - 2.0 * np.logical_and(x, y).sum() / total)
    return out


def parse_lta_matrix(path: Path) -> np.ndarray:
    """The 4x4 from an LTA (after its `N 4 4` line) or a headerless plain 4x4.

    Both roots' transforms come from the same code, so the matrices are compared
    as written; their direction (images/freesurfer/bold_to_t1w.py:parse_lta_matrix:
    T1w RAS -> BOLD RAS) is the same on both sides and does not need resolving.
    """
    lines = [ln.split() for ln in path.read_text().splitlines()]
    for i, parts in enumerate(lines):
        if parts[:3] == ["1", "4", "4"] or parts[:2] == ["4", "4"]:
            rows = [list(map(float, r[:4])) for r in lines[i + 1 : i + 5]]
            return np.array(rows)
    numeric = [list(map(float, r)) for r in lines if len(r) == 4 and _all_float(r)]
    if len(numeric) >= 4:
        return np.array(numeric[:4])
    raise ValueError(f"{path}: no 4x4 matrix")


def _all_float(parts: list[str]) -> bool:
    try:
        [float(p) for p in parts]
    except ValueError:
        return False
    return True


# A confounds column's family. CompCor families are compared as subspaces (below);
# every other family column by column. A column that fits none is "other".
_COMPCOR = re.compile(r"^([a-z]_comp_cor(?:_[a-z]+)?)_\d+$")
_FAMILIES = (
    ("motion", re.compile(r"^(trans|rot)_[xyz]")),
    ("signals", re.compile(r"^(global_signal|csf|white_matter)")),
    ("dvars_fd", re.compile(r"^(dvars|std_dvars|framewise_displacement)")),
    ("design", re.compile(r"^(cosine|non_steady_state_outlier|motion_outlier|t_indx)")),
)


def confound_family(column: str) -> tuple[str, bool]:
    """(family, is_compcor) for a confounds TSV column."""
    m = _COMPCOR.match(column)
    if m:
        return m.group(1), True
    for name, pattern in _FAMILIES:
        if pattern.match(column):
            return name, False
    return "other", False


def subspace_distance(x: np.ndarray, y: np.ndarray) -> float:
    """1 - mean squared cosine of the principal angles between two column spaces.

    0 when one set's span contains the other's (k = min of the two counts), up to 1
    when they are orthogonal. CompCor components are PCA directions: two runs whose
    noise spaces agree can still order, sign or mix components with near-equal
    variance differently, so comparing them column by column measures that
    reordering (one component of 2026-11-09's first fresh subject correlated at
    -0.13 with its namesake), not a difference in what is regressed out.
    """

    def basis(m: np.ndarray) -> np.ndarray:
        m = m - m.mean(axis=0)
        m = m[:, np.linalg.norm(m, axis=0) > 0]
        return np.linalg.qr(m)[0] if m.shape[1] else m

    if x.shape == y.shape and np.array_equal(x, y):  # exactly 0, not SVD round-off
        return 0.0
    qx, qy = basis(x), basis(y)
    if not qx.shape[1] and not qy.shape[1]:
        return 0.0
    if not qx.shape[1] or not qy.shape[1]:
        return 1.0
    s = np.clip(np.linalg.svd(qx.T @ qy, compute_uv=False), 0.0, 1.0)
    return float(1.0 - np.mean(s**2))


def confounds_difference(a_path: Path, b_path: Path) -> list[tuple[str, float, dict]]:
    """(measure, distance, detail) per confounds family of one run.

    Not column by column over the whole TSV: the largest such difference is always
    a CompCor component (see subspace_distance), so it said nothing about motion,
    the signals or the design columns, which should match exactly. Non-CompCor
    families give max over columns of max|a - b| / max|a|. CompCor families give
    the subspace distance and the component-count difference: the count follows a
    variance threshold, so differing counts are a measurement, not a shape error.
    Shape differs only if the row counts or the non-CompCor columns differ.
    """
    import pandas as pd

    a = pd.read_csv(a_path, sep="\t", na_values="n/a")
    b = pd.read_csv(b_path, sep="\t", na_values="n/a")
    fam_a = {c: confound_family(c) for c in a.columns}
    fam_b = {c: confound_family(c) for c in b.columns}
    plain_a = [c for c, (_, cc) in fam_a.items() if not cc]
    plain_b = [c for c, (_, cc) in fam_b.items() if not cc]
    if len(a) != len(b) or plain_a != plain_b:
        return [("confounds_shape", 1.0, {"status": "shape_differs", "rows": [len(a), len(b)],
                 "only_a": sorted(set(plain_a) - set(plain_b))[:5],
                 "only_b": sorted(set(plain_b) - set(plain_a))[:5]})]  # fmt: skip

    out: list[tuple[str, float, dict]] = []
    worst: dict[str, tuple[float, str]] = {}
    nan_mismatch: dict[str, list[str]] = {}
    for col in plain_a:
        fam = fam_a[col][0]
        x, y = a[col].to_numpy(np.float64), b[col].to_numpy(np.float64)
        if (np.isnan(x) != np.isnan(y)).any():
            nan_mismatch.setdefault(fam, []).append(col)
            continue
        x, y = np.nan_to_num(x), np.nan_to_num(y)
        rel = float(np.abs(x - y).max() / (np.abs(x).max() or 1.0))
        if rel >= worst.get(fam, (-1.0, ""))[0]:
            worst[fam] = (rel, col)
    for fam in sorted(set(worst) | set(nan_mismatch)):
        if fam in nan_mismatch:
            out.append((f"confounds_{fam}_max_rel_diff", 1.0,
                        {"status": "nan_mismatch", "columns": nan_mismatch[fam][:5]}))  # fmt: skip
        else:
            out.append(
                (f"confounds_{fam}_max_rel_diff", worst[fam][0], {"worst_column": worst[fam][1]})
            )

    for fam in sorted(
        {f for f, cc in fam_a.values() if cc} | {f for f, cc in fam_b.values() if cc}
    ):
        ca = [c for c, (f, cc) in fam_a.items() if cc and f == fam]
        cb = [c for c, (f, cc) in fam_b.items() if cc and f == fam]
        x = a[ca].to_numpy(np.float64) if ca else np.zeros((len(a), 0))
        y = b[cb].to_numpy(np.float64) if cb else np.zeros((len(b), 0))
        finite = np.isfinite(x).all(axis=1) & np.isfinite(y).all(axis=1)
        out.append((f"confounds_{fam}_subspace_distance", subspace_distance(x[finite], y[finite]),
                    {"n_components": [len(ca), len(cb)]}))  # fmt: skip
        out.append((f"confounds_{fam}_n_components_abs_diff", float(abs(len(ca) - len(cb))), {}))
    return out


# ---------------------------------------------------------------------------
# Per-derivative comparisons
# ---------------------------------------------------------------------------


@dataclass
class Ctx:
    store: Store
    a: Root
    b: Root
    work: Path
    template_mask: Path | None

    def pair(self, rel: str) -> tuple[Path | None, Path | None]:
        pa, pb = self.work / "a" / rel, self.work / "b" / rel
        return (pa if self.store.fetch(self.a, rel, pa) else None,
                pb if self.store.fetch(self.b, rel, pb) else None)  # fmt: skip


def _row(subject, session, unit, measure, distance=None, status="ok", **detail) -> dict[str, Any]:
    return {"subject": subject, "session": session, "unit": unit, "measure": measure,
            "distance": distance, "status": status, **detail}  # fmt: skip


def _presence(subject, session, unit, measure, pa, pb) -> dict | None:
    if pa and pb:
        return None
    status = "missing_in_both" if not (pa or pb) else ("missing_in_a" if not pa else "missing_in_b")
    return _row(subject, session, unit, measure, status=status)


def compare_anatomy(ctx: Ctx, subject: str, session: str) -> Iterator[dict]:
    from equivalence.descriptive import read_stats_file, segmentation_dice

    tree = f"derivatives/fastsurfer/{subject}/{session}/"
    orig = ctx.pair(tree + "mri/orig.mgz")
    seg = ctx.pair(tree + "mri/aparc.DKTatlas+aseg.mapped.mgz")
    miss = _presence(subject, session, "anatomy", "segmentation_dice", *seg)
    if miss or not (orig[0] and orig[1]):
        yield miss or _row(subject, session, "anatomy", "segmentation_dice", status="missing_orig")
    else:
        d = segmentation_dice(_load(orig[0]), _load(orig[1]), _load(seg[0]), _load(seg[1]))
        if not d.scored:
            yield _row(subject, session, "anatomy", "segmentation_dice", status="refused_grid",
                       grid_agreement=d.grid_agreement)  # fmt: skip
        else:
            # A label absent from BOTH trees is agreement here, not a failure: the
            # benchmark scores it 0 (an engine that failed to produce a structure
            # is penalised), but two runs of one pipeline that both lack it agree.
            absent = set(d.missing)
            dist = [1.0 - v for k, v in d.per_label.items() if k not in absent]
            if not dist:
                yield _row(subject, session, "anatomy", "segmentation_dice", status="no_labels")
            else:
                yield _row(subject, session, "anatomy", "segmentation_dice_mean", float(np.mean(dist)),
                           labels_absent_in_both=len(absent))  # fmt: skip
                yield _row(subject, session, "anatomy", "segmentation_dice_worst", float(max(dist)))

    brain = ctx.pair(tree + "mri/brain.mgz")
    miss = _presence(subject, session, "anatomy", "brain_voxels", *brain)
    if miss:
        yield miss
    else:
        v = voxel_difference(_array(brain[0]), _array(brain[1]))
        yield _row(subject, session, "anatomy", "brain_voxels_differ_frac", v["voxels_differ_frac"])
        yield _row(subject, session, "anatomy", "brain_mean_abs_diff", v["mean_abs_diff"])

    aseg = ctx.pair(tree + "stats/aseg.stats")
    if aseg[0] and aseg[1]:
        sa, sb = read_stats_file(aseg[0]), read_stats_file(aseg[1])
        for m in ("eTIV", "CortexVol", "TotalGrayVol", "SubCortGrayVol"):
            d = rel_diff(sa.measures.get(m), sb.measures.get(m))
            if d is not None:
                yield _row(subject, session, "anatomy", f"aseg_{m}_rel_diff", d)
    for hemi in ("lh", "rh"):
        dkt = ctx.pair(tree + f"stats/{hemi}.aparc.DKTatlas.mapped.stats")
        if dkt[0] and dkt[1]:
            ra, rb = read_stats_file(dkt[0]).rows, read_stats_file(dkt[1]).rows
            diffs = [
                rel_diff(ra[k].get("ThickAvg"), rb[k].get("ThickAvg"))
                for k in ra.keys() & rb.keys()
            ]
            diffs = [x for x in diffs if x is not None]
            if diffs:
                yield _row(
                    subject, session, "anatomy", f"{hemi}_thickness_max_rel_diff", max(diffs)
                )


def _volumes(path: Path) -> dict[str, float]:
    out = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and _all_float(parts[-1:]):
            out[" ".join(parts[:-1])] = float(parts[-1])
    return out


def compare_subregions(ctx: Ctx, subject: str, session: str) -> Iterator[dict]:
    for region in SUBREGIONS:
        rel = f"derivatives/subregions/{subject}/{region}/{session}/mri/"
        names = sorted({k.removeprefix(rel) for root in (ctx.a, ctx.b)
                        for k in ctx.store.list(root, rel) if "olumes" in k and k.endswith(".txt")})  # fmt: skip
        if not names:
            continue
        worst, missing = 0.0, []
        for name in names:
            pa, pb = ctx.pair(rel + name)
            if not (pa and pb):
                missing.append(name)
                continue
            va, vb = _volumes(pa), _volumes(pb)
            for k in va.keys() | vb.keys():
                d = rel_diff(va.get(k), vb.get(k))
                worst = max(worst, 1.0 if d is None else d)
        if missing:
            yield _row(subject, session, f"subregions/{region}", "volumes", status="missing_files",
                       files=missing)  # fmt: skip
        yield _row(subject, session, f"subregions/{region}", "volumes_max_rel_diff", worst)


def compare_t1w_to_mni(ctx: Ctx, subject: str, session: str) -> Iterator[dict]:
    rel = (
        f"derivatives/registration/{subject}/{session}/t1w_to_mni/{subject}_{session}_desc-t1w2mni_"
    )
    warped = ctx.pair(rel + "warped.nii.gz")
    miss = _presence(subject, session, "t1w_to_mni", "warped_correlation", *warped)
    if miss:
        yield miss
        return
    yield _row(subject, session, "t1w_to_mni", "warped_correlation",
               correlation_distance(_array(warped[0]), _array(warped[1])))  # fmt: skip
    qc = ctx.pair(rel + "qc.json")
    if qc[0] and qc[1]:
        qa, qb = json.loads(qc[0].read_text()), json.loads(qc[1].read_text())
        for f in QC_FIELDS:
            if isinstance(qa.get(f), (int, float)) and isinstance(qb.get(f), (int, float)):
                yield _row(subject, session, "t1w_to_mni", f"qc_{f}_abs_diff", abs(qa[f] - qb[f]))
        if qa.get("verdict") != qb.get("verdict"):
            yield _row(subject, session, "t1w_to_mni", "qc_verdict", 1.0,
                       a=qa.get("verdict"), b=qb.get("verdict"))  # fmt: skip


def compare_bold_to_t1w(ctx: Ctx, subject: str, session: str, run: str) -> Iterator[dict]:
    from equivalence.registration import average_absolute_displacement

    base = f"derivatives/registration/{subject}/{session}/bold_to_t1w_{run}/{subject}_{session}_{run}_desc-bold2t1w"
    lta = ctx.pair(base + ".lta")
    miss = _presence(subject, session, f"bold_to_t1w/{run}", "displacement_mm", *lta)
    if miss:
        yield miss
        return
    mask_path = ctx.pair(base + "_brainmask.nii.gz")[0]
    if mask_path is None:
        yield _row(subject, session, f"bold_to_t1w/{run}", "displacement_mm", status="missing_mask")
        return
    img = _load(mask_path)
    ijk = np.argwhere(np.asarray(img.dataobj) > 0)[::8]
    pts = (img.affine @ np.c_[ijk, np.ones(len(ijk))].T).T[:, :3]
    d = average_absolute_displacement(parse_lta_matrix(lta[0]), parse_lta_matrix(lta[1]), pts)
    yield _row(subject, session, f"bold_to_t1w/{run}", "displacement_mm", float(d))


def _nss(confounds: Path) -> int:
    header = confounds.read_text().splitlines()[0].split("\t")
    return sum(1 for c in header if c.startswith("non_steady_state"))


def _run_paths(subject: str, session: str, run: str) -> str:
    return f"derivatives/func/{subject}/{session}/{subject}_{session}_{run}"


def compare_mni_bold(ctx: Ctx, subject: str, session: str, run: str) -> Iterator[dict]:
    p, unit = _run_paths(subject, session, run), f"func/{run}"
    bold = ctx.pair(f"{p}_{SPACE}_bold.nii.gz")
    miss = _presence(subject, session, unit, "mni_bold", *bold)
    if miss:
        yield miss
        return
    v = voxel_difference(_array(bold[0]), _array(bold[1]))
    yield _row(subject, session, unit, "mni_bold_voxels_differ_frac", v["voxels_differ_frac"])
    yield _row(subject, session, unit, "mni_bold_mean_abs_diff", v["mean_abs_diff"])


def compare_tier1(ctx: Ctx, subject: str, session: str, run: str) -> Iterator[dict]:
    """equivalence.endpoints.tier1 over the template mask. It takes paths or images
    (it reorients them itself), never bare arrays."""
    if ctx.template_mask is None:
        return
    from equivalence.endpoints import tier1

    p, unit = _run_paths(subject, session, run), f"func/{run}"
    bold, mask = ctx.pair(f"{p}_{SPACE}_bold.nii.gz"), ctx.pair(f"{p}_{SPACE}_brainmask.nii.gz")
    conf = ctx.pair(f"{p}_desc-confounds_timeseries.tsv")
    if not (bold[0] and bold[1] and mask[0] and mask[1] and conf[0]):
        return  # reported by the other func measures
    t = tier1(bold[0], mask[0], bold[1], mask[1],
              template_mask=ctx.template_mask, n_nss_frames=_nss(conf[0]))  # fmt: skip
    yield _row(subject, session, unit, "tier1_mean_image", 1.0 - t.mean_image.correlation)
    yield _row(subject, session, unit, "tier1_tsnr_map", 1.0 - t.tsnr_map.correlation)
    yield _row(subject, session, unit, "tier1_own_mask_dice", 1.0 - t.mask_dice)


def compare_confounds(ctx: Ctx, subject: str, session: str, run: str) -> Iterator[dict]:
    p, unit = _run_paths(subject, session, run), f"func/{run}"
    conf = ctx.pair(f"{p}_desc-confounds_timeseries.tsv")
    miss = _presence(subject, session, unit, "confounds", *conf)
    if miss:
        yield miss
        return
    for measure, distance, detail in confounds_difference(conf[0], conf[1]):
        status = detail.pop("status", "ok")
        yield _row(subject, session, unit, measure, distance, status=status, **detail)


def compare_boldref(ctx: Ctx, subject: str, session: str, run: str) -> Iterator[dict]:
    p, unit = _run_paths(subject, session, run), f"func/{run}"
    ref = ctx.pair(f"{p}_desc-boldref.nii.gz")
    miss = _presence(subject, session, unit, "boldref", *ref)
    if miss:
        yield miss
        return
    yield _row(subject, session, unit, "boldref_correlation",
               correlation_distance(_array(ref[0]), _array(ref[1])))  # fmt: skip


def compare_tissue(ctx: Ctx, subject: str, session: str) -> Iterator[dict]:
    p = f"derivatives/func/{subject}/{session}/{subject}_{session}_{SPACE}_res-1_"
    dseg = ctx.pair(p + "dseg.nii.gz")
    miss = _presence(subject, session, "tissue", "dseg", *dseg)
    if miss:
        yield miss
        return
    d = label_dice_distances(_array(dseg[0]), _array(dseg[1]), (1, 2, 3))
    yield _row(subject, session, "tissue", "dseg_dice_mean", float(np.mean(list(d.values()))))
    yield _row(subject, session, "tissue", "dseg_dice_worst", float(max(d.values())))
    for lab in TISSUE_LABELS:
        ps = ctx.pair(p + f"label-{lab}_probseg.nii.gz")
        if ps[0] and ps[1]:
            pa, pb = _array(ps[0]), _array(ps[1])
            # Over voxels in either map: the rest of the 1 mm volume is zero in both
            # and would only dilute the mean.
            within = (pa > 0) | (pb > 0) if pa.shape == pb.shape else None
            yield _row(subject, session, "tissue", f"probseg_{lab}_mean_abs_diff",
                       voxel_difference(pa, pb, within)["mean_abs_diff"])  # fmt: skip


#: fsLR 32k cortical grayordinates, first in every cloudpipe CIFTI (CORTEX_LEFT then
#: CORTEX_RIGHT); the subcortical block after them follows the subject's T1w->MNI
#: registration, so its per-structure counts can differ between two runs.
CORTICAL_GRAYORDINATES = 59412


def _structure_counts(img) -> dict[str, int]:
    # len(bm), not the slice: nibabel leaves the last structure's slice open (stop=None).
    return {str(name).replace("CIFTI_STRUCTURE_", ""): len(bm)
            for name, _, bm in img.header.get_axis(1).iter_structures()}  # fmt: skip


def cortical_tail_distance(a: np.ndarray, b: np.ndarray, n_nss_frames: int) -> tuple[float, int]:
    """(1 - 5th-percentile temporal r over the cortex, grayordinates left out).

    `a`, `b` are CIFTI arrays, time x grayordinates. Not
    equivalence.endpoints.cortical_grayordinates: its Pearson is 0 where either
    series is constant, so a grayordinate constant in BOTH runs (agreement) counts
    as r = 0. A run with more than 5% of its cortex constant (2026-11-12: 4,471 of
    59,412 in one session, in production and fresh alike) then has a 5th
    percentile of 0, distance 1, though its cortex differed by at most 0.045 and its
    median r was 0.99999999999. Here a grayordinate constant in both is left out and
    counted; one constant in only one run stays at r = 0, a real disagreement.
    """
    x = np.asarray(a[n_nss_frames:, :CORTICAL_GRAYORDINATES], dtype=np.float64)
    y = np.asarray(b[n_nss_frames:, :CORTICAL_GRAYORDINATES], dtype=np.float64)
    x = x - x.mean(axis=0)
    y = y - y.mean(axis=0)
    sx, sy = np.sqrt((x * x).sum(axis=0)), np.sqrt((y * y).sum(axis=0))
    both_constant = (sx == 0) & (sy == 0)
    r = np.zeros(x.shape[1])
    ok = (sx > 0) & (sy > 0)
    r[ok] = (x[:, ok] * y[:, ok]).sum(axis=0) / (sx[ok] * sy[ok])
    kept = r[~both_constant]
    tail = float(np.percentile(kept, 5)) if kept.size else 1.0
    return 1.0 - tail, int(both_constant.sum())


def compare_grayordinates(
    ctx: Ctx, subject: str, session: str, run: str, nss: int
) -> Iterator[dict]:
    unit = f"grayordinates/{run}"
    rel = f"derivatives/func_surf/{subject}/{session}/fsLR32k/{subject}_{session}_{run}_space-fsLR32k_bold.dtseries.nii"
    pa, pb = ctx.pair(rel)
    miss = _presence(subject, session, unit, "dtseries", pa, pb)
    if miss:
        yield miss
        return
    ia, ib = _load(pa), _load(pb)
    ca, cb = _structure_counts(ia), _structure_counts(ib)
    if ca != cb:
        # The subcortical block's voxel counts follow T1w->MNI, so a different
        # registration changes the layout; no grayordinate past the cortex is
        # comparable by position. Reported, not scored.
        differ = {
            k: [ca.get(k), cb.get(k)] for k in sorted(set(ca) | set(cb)) if ca.get(k) != cb.get(k)
        }
        yield _row(subject, session, unit, "dtseries_layout", status="layout_differs",
                   structures_differ=differ)  # fmt: skip
    a, b = np.asarray(ia.dataobj), np.asarray(ib.dataobj)
    if a.shape == b.shape and np.array_equal(a, b):
        yield _row(subject, session, unit, "dtseries_p5_temporal_r", 0.0)
        return
    if a.shape[0] != b.shape[0] or min(a.shape[1], b.shape[1]) < CORTICAL_GRAYORDINATES:
        yield _row(subject, session, unit, "dtseries_p5_temporal_r", status="shape_differs",
                   shapes=[list(a.shape), list(b.shape)])  # fmt: skip
        return
    distance, constant = cortical_tail_distance(a, b, nss)
    yield _row(subject, session, unit, "dtseries_p5_temporal_r", distance,
               constant_in_both=constant)  # fmt: skip


def _runs(ctx: Ctx, subject: str, session: str) -> list[str]:
    runs: set[str] = set()
    rel = f"derivatives/func/{subject}/{session}/_complete.json"
    for side, root in (("a", ctx.a), ("b", ctx.b)):
        dest = ctx.work / side / rel
        if ctx.store.fetch(root, rel, dest):
            doc = json.loads(dest.read_text())
            if doc.get("kind") == "func-session":
                runs |= {r for r in (doc.get("runs") or {}) if r.startswith(TASKS)}
    return sorted(runs)


def _sessions(store: Store, a: Root, b: Root, subject: str) -> list[str]:
    rel = f"derivatives/fastsurfer/{subject}/"
    out = set()
    for root in (a, b):
        for k in store.list(root, rel):
            part = k.removeprefix(rel).split("/", 1)[0]
            if part.startswith("ses-"):
                out.add(part)
    return sorted(out)


def _drop_run(work: Path, subject: str, session: str, run: str) -> None:
    """Delete one run's downloads from both roots once its comparisons are done.

    A run's MNI BOLD is the largest file the comparison reads (about 1 GB per root,
    compressed), and a 4-session subject has up to 24 runs. Kept to the end of the
    subject, they overran the pod's 16 GB scratch on the noise floor (2026-11-10:
    30 of 55 pods evicted, and no 4-session subject finished).
    """
    stem = f"{subject}_{session}_{run}"
    for root in ("a", "b"):
        for path in (work / root).rglob(f"*{stem}*"):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)


def compare_subject(store: Store, a: Root, b: Root, subject: str, template_mask: Path | None = None,
                    sessions: list[str] | None = None) -> list[dict]:  # fmt: skip
    """Every comparison row for one subject.

    Downloads go to a scratch directory per session, removed when the session is
    done, and each run's files are removed after that run (_drop_run): scratch
    holds one session's anatomy and one run at a time, not the whole subject.
    """
    rows: list[dict] = []
    for session in sessions or _sessions(store, a, b, subject):
        with tempfile.TemporaryDirectory(prefix=f"audit-compare-{session}-") as tmp:
            ctx = Ctx(store, a, b, Path(tmp), template_mask)
            for fn in (compare_anatomy, compare_subregions, compare_t1w_to_mni, compare_tissue):
                rows += _guarded(fn, ctx, subject, session)
            for run in _runs(ctx, subject, session):
                rows += _guarded(compare_bold_to_t1w, ctx, subject, session, run)
                rows += _guarded(compare_mni_bold, ctx, subject, session, run)
                rows += _guarded(compare_tier1, ctx, subject, session, run)
                rows += _guarded(compare_confounds, ctx, subject, session, run)
                rows += _guarded(compare_boldref, ctx, subject, session, run)
                conf = (
                    ctx.work
                    / "a"
                    / f"derivatives/func/{subject}/{session}/{subject}_{session}_{run}_desc-confounds_timeseries.tsv"
                )
                nss = _nss(conf) if conf.exists() else 0
                rows += _guarded(compare_grayordinates, ctx, subject, session, run, nss)
                _drop_run(ctx.work, subject, session, run)
    return rows


def _guarded(fn, ctx: Ctx, subject: str, session: str, *args) -> list[dict]:
    """A comparison that raises becomes a row, not a crash, and KEEPS the rows it
    produced before failing: one bad file must not cost the subject, nor the
    measures already taken."""
    rows: list[dict] = []
    try:
        for row in fn(ctx, subject, session, *args):
            rows.append(row)
    except Exception as exc:  # noqa: BLE001
        unit = fn.__name__.removeprefix("compare_") + (f"/{args[0]}" if args else "")
        rows.append(_row(subject, session, unit, fn.__name__, status="error",
                         error=f"{type(exc).__name__}: {exc}"))  # fmt: skip
    return rows


# ---------------------------------------------------------------------------
# Classification (task 3.4)
# ---------------------------------------------------------------------------

WITHIN_NOISE, HARDWARE, DEFECT, UNEXPLAINED = "within_noise", "hardware", "defect", "unexplained"

#: The smallest distance treated as a difference, for measures whose noise floor
#: can be exactly 0. Two identical fresh runs give 0, but a run compared with a
#: production tree written by other pods differs at rounding level. In the main
#: run's anatomy-identical subjects (2026-11-12): BOLD->T1w up to 0.008 mm,
#: CompCor subspaces up to 2.3e-3, DVARS/FD up to 2.3e-4, confound signals up to
#: 2.5e-3, and the grayordinate tail at 1e-11..1e-5. A floor above the tolerance
#: wins. Anatomy has none on purpose: same-card anatomy is bit-identical, and any
#: difference there is a finding.
ZERO_FLOOR_TOLERANCE: dict[str, float] = {
    "displacement_mm": 0.01,  # a hundredth of a millimetre
    "dtseries_p5_temporal_r": 1e-4,
    "confounds_signals_max_rel_diff": 5e-3,
    "confounds_dvars_fd_max_rel_diff": 1e-3,
    "confounds_motion_max_rel_diff": 1e-6,
    "confounds_design_max_rel_diff": 0.0,
}
_SUBSPACE_TOLERANCE = 5e-3  # every confounds_*_subspace_distance


def tolerance(measure: str) -> float:
    """The rounding-level distance below which `measure` counts as no difference."""
    if measure.endswith("_subspace_distance"):
        return _SUBSPACE_TOLERANCE
    return ZERO_FLOOR_TOLERANCE.get(measure, 0.0)


def classify(
    measure: str,
    distance: float,
    hardware_a: str,
    hardware_b: str,
    floor: dict[str, dict[str, float]],
) -> str:
    """Spec audit-comparison, "Each fresh-vs-production difference is classified".

    `floor[measure]` maps a hardware pair key to the LARGEST distance the noise
    floor produced for that measure on that pair: "cuda|cuda" for same-hardware
    runs, "cpu|cuda" (sorted) across. Same hardware: within_noise or defect.
    Different hardware: within_noise if inside the same-hardware floor, hardware
    if inside that pair's floor, defect if outside it, unexplained if that pair
    was never measured. Every floor is at least `tolerance(measure)`.
    """
    tol = tolerance(measure)
    by_pair = {k: max(v, tol) for k, v in floor.get(measure, {}).items()}
    same = by_pair.get(f"{hardware_a}|{hardware_a}") if hardware_a == hardware_b else None
    if hardware_a == hardware_b:
        if same is None:
            return UNEXPLAINED
        return WITHIN_NOISE if distance <= same else DEFECT
    pair = "|".join(sorted((hardware_a, hardware_b)))
    same_any = [v for k, v in by_pair.items() if len(set(k.split("|"))) == 1]
    if same_any and distance <= min(same_any):
        return WITHIN_NOISE
    if pair not in by_pair:
        return UNEXPLAINED
    return HARDWARE if distance <= by_pair[pair] else DEFECT


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_text(uri: str, s3=None) -> str:
    if uri.startswith("s3://"):
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    return Path(uri).read_text()


def _subjects(text: str, name: str) -> list[tuple[str, list[str] | None]]:
    """(subject, production sessions) from sample.jsonl, or (subject, None) from a subject_id CSV."""
    if name.endswith(".jsonl"):
        rows = (json.loads(line) for line in text.splitlines() if line.strip())
        return [(r["subject"], r.get("production_sessions")) for r in rows]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return (
        [(ln, None) for ln in lines[1:]]
        if lines and lines[0] == "subject_id"
        else [(ln, None) for ln in lines]
    )


def shard_of(items: list, shard: int, shards: int) -> list:
    """Every shards-th item from `shard`, as the backfill templates split their plans."""
    if not 0 <= shard < shards:
        raise ValueError(f"shard {shard} is not in [0, {shards})")
    return items[shard::shards]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--a-bucket", required=True)
    p.add_argument("--a-prefix", default="")
    p.add_argument("--b-bucket", required=True)
    p.add_argument("--b-prefix", default="")
    p.add_argument(
        "--subjects", required=True, help="sample.jsonl or a subject_id CSV; a path or s3:// URI"
    )
    p.add_argument("--template-mask", type=Path, help="TemplateFlow res-02 brain mask (tier-1)")
    p.add_argument("--local", action="store_true", help="buckets are local directories")
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    out = p.add_mutually_exclusive_group(required=True)
    out.add_argument("--out", type=Path, help="one local JSONL for every subject")
    out.add_argument(
        "--out-prefix",
        help="s3://bucket/prefix/: one {subject}.jsonl per subject, and a subject whose "
        "object exists is skipped, so a retried or resubmitted shard resumes",
    )
    p.add_argument("--region")
    args = p.parse_args(argv)

    if args.template_mask:
        from equivalence.run_endpoints import verify_template_mask

        verify_template_mask(args.template_mask)
    s3 = None
    if args.local:
        store: Store = LocalStore()
    else:
        import boto3

        s3 = boto3.client("s3", region_name=args.region)
        store = S3Store(s3)
    a, b = Root(args.a_bucket, args.a_prefix), Root(args.b_bucket, args.b_prefix)
    subjects = shard_of(
        _subjects(_read_text(args.subjects, s3), args.subjects), args.shard, args.shards
    )

    tally: Counter = Counter()
    if args.out:
        with args.out.open("w") as fh:
            for subject, sessions in subjects:
                for row in compare_subject(store, a, b, subject, args.template_mask, sessions):
                    fh.write(json.dumps(row) + "\n")
                    tally[row["status"]] += 1
    else:
        bucket, _, prefix = args.out_prefix.removeprefix("s3://").partition("/")
        for subject, sessions in subjects:
            key = f"{prefix}{subject}.jsonl"
            if s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1).get("KeyCount"):
                tally["subjects_skipped_done"] += 1
                continue
            rows = compare_subject(store, a, b, subject, args.template_mask, sessions)
            body = "".join(json.dumps(r) + "\n" for r in rows).encode()
            s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
            tally["subjects_done"] += 1
            tally.update(r["status"] for r in rows)
    print(
        json.dumps({"shard": args.shard, "shards": args.shards, "subjects": len(subjects), **tally})
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
