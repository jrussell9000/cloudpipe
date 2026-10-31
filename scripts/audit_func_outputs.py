#!/usr/bin/env python3
"""Audit shipped func derivatives the way a downstream user reads them.

`reprocess_func.py verify` proves every run's outputs EXIST, are fresh, and that
the in-pod read-back guard passed. It never opens a file. Two defects got past
that kind of check: the MNI BOLD shipped as float16 bit patterns in x<->z
transposed order for four months (2026-05-25 to 10-06), and #884's plain-base64
GIFTI made surface-resample abort for 618 of 2,000 subjects while every workflow
said Succeeded. Both would have been obvious to anyone who loaded one output.

This loads them. For a seeded random sample of subjects it picks one session and
one run (or every run, with --all-runs), downloads that run's MNI BOLD, brain
mask, confounds, fsLR32k dtseries and the session's tissue maps into a scratch
directory, checks them against each other, against the MNI template and against
the run's own FuncQC record, and deletes them before the next unit (the DUA:
nothing is kept). Read-only on S3.

It reads every file with plain nibabel/pandas. The one piece of pipeline code it
reuses is the FuncQC recompute (src/equivalence/recompute.py), on purpose: that
check asks whether the in-pod record describes the published file, so it has to
measure the file with the same ruler.

The thresholds are calibrated on known-good runs (the 2026-10-25 smoke subject)
and on the shapes the known defects take; each says which.

    pixi run -e ops python scripts/audit_func_outputs.py \\
        --subjects data/reprocess/func_reprocess_03_04.csv --sample 50 \\
        --data-bucket "$CLOUDPIPE_BUCKET" --metrics-bucket "$CLOUDPIPE_METRICS_BUCKET" \\
        --out audit_03_04.jsonl --mosaics audit_03_04_png

Exit status 1 if any check failed, 0 otherwise.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

import boto3
import nibabel as nib
import numpy as np
import pandas as pd
from tqdm import tqdm

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from equivalence.recompute import (  # noqa: E402
    RunInputs,
    calibration_mismatches,
    recompute_func_qc,
)

SPACE = "space-MNI152NLin2009cAsym"
TEMPLATE_2MM = "config/MNI152NLin2009cAsym_T1w_brain_res-2_RAI.nii"
TEMPLATE_1MM = "config/MNI152NLin2009cAsym_res-01_T1w_brain.nii"
TISSUES = ("GM", "WM", "CSF")
DSEG_INDEX = {"GM": 1, "WM": 2, "CSF": 3}
# fsLR32k with the medial wall removed (the atlasroi shapes in config/fsLR/).
CORTEX_VERTICES = {"CIFTI_STRUCTURE_CORTEX_LEFT": 29696, "CIFTI_STRUCTURE_CORTEX_RIGHT": 29716}

# --- Thresholds -------------------------------------------------------------
# Fraction of in-mask values that are whole numbers. The float16-as-DT_UINT16
# defect stored integers (bit patterns read as uint16), so it was 1.0; a good
# float32 run is ~0.002.
MAX_INTEGER_FRACTION = 0.05
# In-mask voxels whose time series is constant. The transpose defect zeroed ~18%
# of the brain (mask times the transposed volume); a good run has none.
MAX_DEAD_VOXEL_FRACTION = 0.01
# Mirror symmetry of the temporal mean image. The brain is near-symmetric
# left-right, so flipping x correlates better than flipping z. The transposed
# files: x 0.25, z 0.92 (x - z = -0.67). Good runs (36, 2026-10-29): x - z
# 0.058 to 0.56. Only z is gated: in high-motion good runs the y-flip came
# within 0.007 of the x-flip (#944), so x - max(y, z) does not separate them.
MIN_X_OVER_Z_FLIP = 0.0
# Dice of the BOLD brain mask (and of the dseg) with the template's brain.
# Good: 0.966 and 0.943.
MIN_BOLD_MASK_DICE = 0.90
MIN_DSEG_DICE = 0.85
# Global signal of the MNI file against the confounds' (native-space) column,
# over steady-state frames. Good: 0.999. A frame shuffle or a dropped frame
# breaks it, and so does a lag: the peak must be at lag 0.
MIN_GS_CORR = 0.95
MAX_LAG = 5
# Cortical grayordinate mean against the MNI global signal. Good: 0.98.
MIN_CORTEX_GS_CORR = 0.90
# Subcortical grayordinates outside the BOLD brain mask are zero by design
# (preproc.py extract_subcortical). Good: 15 of 33,081.
MAX_ZERO_SUBCORTICAL_FRACTION = 0.01
# Per-class agreement between argmax(probseg) and the FastSurfer dseg. They are
# different segmentations (intensity vs anatomy), so they disagree at edges and
# in WM-bright deep grey; good sessions agree 0.68-0.91 per class. A probseg
# whose classes collapsed scores 0.0-0.3 on WM.
MIN_PROBSEG_AGREEMENT = 0.5


@dataclass
class Check:
    name: str
    ok: bool
    value: object = None
    detail: str = ""


def _r(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def _dice(a: np.ndarray, b: np.ndarray) -> float:
    total = int(a.sum()) + int(b.sum())
    return 2.0 * int((a & b).sum()) / total if total else float("nan")


# --- MNI BOLD -------------------------------------------------------------


def check_bold_header(img, template, record: dict) -> list[Check]:
    h = img.header
    zooms = h.get_zooms()
    checks = [
        Check("bold.dtype_float32", int(h["datatype"]) == 16, int(h["datatype"])),
        Check(
            "bold.scaling_identity",
            (np.isnan(h["scl_slope"]) or h["scl_slope"] in (0, 1))
            and (np.isnan(h["scl_inter"]) or h["scl_inter"] == 0),
            f"{h['scl_slope']} {h['scl_inter']}",
        ),
        Check("bold.grid_is_template", img.shape[:3] == template.shape[:3], list(img.shape[:3])),
        Check(
            "bold.affine_is_template",
            bool(np.allclose(img.affine, template.affine, atol=1e-3)),
            np.round(img.affine, 3).tolist(),
        ),
        Check(
            "bold.voxel_2mm",
            bool(np.allclose(zooms[:3], 2.0, atol=1e-4)),
            [float(z) for z in zooms],
        ),
    ]
    if record:
        checks += [
            Check(
                "bold.tr_matches_record",
                abs(float(zooms[3]) - float(record.get("tr_seconds", -1))) < 1e-3,
                f"{float(zooms[3]):.6f} vs {record.get('tr_seconds')}",
            ),
            Check(
                "bold.frames_match_record",
                img.shape[3] == record.get("n_frames"),
                f"{img.shape[3]} vs {record.get('n_frames')}",
            ),
        ]
    return checks


def check_bold_values(data: np.ndarray, mask: np.ndarray) -> list[Check]:
    """`data` is the full 4D array, `mask` the run's MNI brain mask (bool)."""
    inside = data[mask]
    finite = bool(np.isfinite(inside).all())
    outside_nonzero = int(np.count_nonzero(data[~mask]))
    frame = inside[:, inside.shape[1] // 2]
    integer_fraction = float(np.mean(frame == np.round(frame)))
    dead = float(np.mean(inside.std(axis=1) == 0))
    return [
        Check("bold.finite", finite),
        Check("bold.zero_outside_mask", outside_nonzero == 0, outside_nonzero),
        Check(
            "bold.not_integer_valued",
            integer_fraction <= MAX_INTEGER_FRACTION,
            round(integer_fraction, 4),
            "whole-number values are the float16-bit-pattern signature",
        ),
        Check(
            "bold.no_dead_voxels",
            dead <= MAX_DEAD_VOXEL_FRACTION,
            round(dead, 4),
            "constant in-mask voxels; the x<->z transpose zeroed ~18%",
        ),
        Check("bold.median_intensity", True, round(float(np.median(inside.mean(axis=1))), 1)),
    ]


def check_orientation(
    mean: np.ndarray, mask: np.ndarray, template_brain: np.ndarray
) -> list[Check]:
    flips = {ax: _r(mean[mask], np.flip(mean, axis=i)[mask]) for i, ax in enumerate("xyz")}
    margin = flips["x"] - flips["z"]
    dice = _dice(mask, template_brain)
    return [
        Check(
            "bold.left_right_symmetry",
            margin > MIN_X_OVER_Z_FLIP,
            {k: round(v, 3) for k, v in flips.items()},
            "x-flip must beat z-flip; the transposed files had z-flip highest",
        ),
        Check("bold.mask_matches_template", dice >= MIN_BOLD_MASK_DICE, round(dice, 4)),
    ]


def check_time(gs_mni: np.ndarray, confounds: pd.DataFrame, nss: int) -> list[Check]:
    gs = confounds["global_signal"].to_numpy(dtype=np.float64)
    if len(gs) != len(gs_mni):
        return [
            Check("bold.gs_matches_confounds", False, f"{len(gs_mni)} frames vs {len(gs)} rows")
        ]
    a, b = gs[nss:], gs_mni[nss:]
    r = _r(a, b)
    n = len(a)
    lags = {
        k: _r(a[MAX_LAG : n - MAX_LAG], b[MAX_LAG + k : n - MAX_LAG + k])
        for k in range(-MAX_LAG, MAX_LAG + 1)
    }
    best = max(lags, key=lambda k: -np.inf if np.isnan(lags[k]) else lags[k])
    return [
        Check("bold.gs_matches_confounds", r >= MIN_GS_CORR, round(r, 4)),
        Check("bold.gs_lag_zero", best == 0, best),
    ]


# --- Confounds --------------------------------------------------------------

FIRST_ROW_NA = ("framewise_displacement", "dvars")
REQUIRED_CONFOUNDS = (
    "rot_x",
    "rot_y",
    "rot_z",
    "trans_x",
    "trans_y",
    "trans_z",
    "framewise_displacement",
    "dvars",
    "global_signal",
)


def check_confounds(conf: pd.DataFrame, n_frames: int, tr: float, nss: int) -> list[Check]:
    checks = [
        Check("confounds.rows_match_frames", len(conf) == n_frames, f"{len(conf)} vs {n_frames}")
    ]
    missing = [c for c in REQUIRED_CONFOUNDS if c not in conf]
    checks.append(Check("confounds.required_columns", not missing, missing))
    if missing:
        return checks
    first_row_na = [
        c
        for c in conf
        if c in FIRST_ROW_NA or c.endswith("_derivative1") or c.endswith("_derivative1_power2")
    ]
    bad_na = [
        c
        for c in conf
        if (conf[c].iloc[1:].isna().any() if c in first_row_na else conf[c].isna().any())
    ]
    checks.append(Check("confounds.no_unexpected_na", not bad_na, bad_na[:5]))
    nss_cols = sorted(c for c in conf if c.startswith("non_steady_state_outlier_"))
    one_hot = len(nss_cols) == nss and all(
        conf[c].to_numpy().tolist() == [1 if t == i else 0 for t in range(len(conf))]
        for i, c in enumerate(nss_cols)
    )
    checks.append(Check("confounds.nss_indicators", one_hot, f"{len(nss_cols)} cols, nss {nss}"))
    compcor = [c for c in conf if "comp_cor" in c]
    nss_rows_zero = all((conf[c].iloc[:nss] == 0).all() for c in compcor)
    checks.append(Check("confounds.compcor_zero_on_nss", nss_rows_zero, len(compcor)))
    # preproc.py compute_confounds: rotations are degrees, 50 mm head radius.
    rot = conf[["rot_z", "rot_x", "rot_y"]].to_numpy() * (np.pi / 180.0) * 50.0
    trans = conf[["trans_z", "trans_x", "trans_y"]].to_numpy()
    fd = np.abs(np.diff(np.hstack([rot, trans]), axis=0)).sum(axis=1)
    err = float(np.nanmax(np.abs(fd - conf["framewise_displacement"].to_numpy()[1:])))
    checks.append(Check("confounds.fd_recomputes", err < 1e-6, err))
    # nipype's cosine basis (fMRIPrep): over the steady-state frames, zero on the
    # NSS rows, max(floor(2 * N * TR / 128), 1) - 1 columns for N = T - nss. A
    # TSV written before 2026-10-27 spans every frame with floor(2 * T * TR / 128).
    cos_cols = sorted(c for c in conf if c.startswith("cosine_"))
    want_cos = max(int(np.floor(2 * (n_frames - nss) * tr / 128.0)), 1) - 1
    checks.append(
        Check("confounds.cosine_count", len(cos_cols) == want_cos, f"{len(cos_cols)} vs {want_cos}")
    )
    nss_rows_zero = all((conf[c].iloc[:nss] == 0).all() for c in cos_cols)
    checks.append(Check("confounds.cosines_zero_on_nss", nss_rows_zero, len(cos_cols)))
    return checks


# --- dtseries ---------------------------------------------------------------


def check_dtseries(
    cifti,
    bold: np.ndarray,
    bold_affine: np.ndarray,
    gs_mni: np.ndarray,
    tr: float,
    nss: int,
    record: dict,
) -> list[Check]:
    series, models = cifti.header.get_axis(0), cifti.header.get_axis(1)
    data = np.asanyarray(cifti.dataobj)
    checks = [
        Check(
            "dtseries.frames_match_bold",
            series.size == bold.shape[3],
            f"{series.size} vs {bold.shape[3]}",
        ),
        Check("dtseries.step_is_tr", abs(float(series.step) - tr) < 1e-3, float(series.step)),
        Check("dtseries.finite", bool(np.isfinite(data).all())),
    ]
    structures = {name: slc for name, slc, _ in models.iter_structures()}
    cortex = {
        n: (structures[n].stop or data.shape[1]) - structures[n].start
        for n in CORTEX_VERTICES
        if n in structures
    }
    checks.append(Check("dtseries.cortex_vertices", cortex == CORTEX_VERTICES, cortex))
    vol = models.volume_mask
    n_sub = int(vol.sum())
    if record.get("subcort_n_voxels") is not None:
        checks.append(
            Check(
                "dtseries.subcortical_matches_record",
                n_sub == record["subcort_n_voxels"],
                f"{n_sub} vs {record['subcort_n_voxels']}",
            )
        )
    checks.append(
        Check(
            "dtseries.volume_grid_is_bold",
            models.volume_shape == bold.shape[:3]
            and bool(np.allclose(models.affine, bold_affine, atol=1e-3)),
            list(models.volume_shape),
        )
    )
    vox = models.voxel[vol]
    sub = data[:, vol]
    from_bold = bold[vox[:, 0], vox[:, 1], vox[:, 2], :].T
    scale = float(np.abs(from_bold).max()) or 1.0
    diff = float(np.abs(sub - from_bold).max()) / scale
    checks.append(
        Check(
            "dtseries.subcortical_equals_bold",
            diff <= 1e-6,
            diff,
            "each subcortical grayordinate is a voxel of the MNI BOLD",
        )
    )
    dead = np.abs(sub).max(axis=0) == 0
    zero_sub = float(dead.mean()) if n_sub else 0.0
    names = np.asarray(models.name)[vol]
    by_structure = {
        str(n).removeprefix("CIFTI_STRUCTURE_"): int(c)
        for n, c in zip(*np.unique(names[dead], return_counts=True), strict=True)
    }
    checks.append(
        Check(
            "dtseries.subcortical_zero_fraction",
            zero_sub <= MAX_ZERO_SUBCORTICAL_FRACTION,
            {"fraction": round(zero_sub, 5), "by_structure": by_structure},
        )
    )
    ctx = data[:, ~vol]
    zero_ctx = int((np.abs(ctx).max(axis=0) == 0).sum())
    checks.append(Check("dtseries.no_zero_cortex", zero_ctx == 0, zero_ctx))
    r = _r(ctx.mean(axis=1)[nss:], gs_mni[nss:])
    checks.append(Check("dtseries.cortex_tracks_gs", r >= MIN_CORTEX_GS_CORR, round(r, 4)))
    return checks


# --- Tissue maps ------------------------------------------------------------


def check_tissue(dseg_img, masks: dict, probseg: dict, template1) -> list[Check]:
    dseg = np.asanyarray(dseg_img.dataobj)
    checks = [
        Check(
            "tissue.grid_is_template",
            dseg_img.shape == template1.shape
            and bool(np.allclose(dseg_img.affine, template1.affine, atol=1e-3)),
            list(dseg_img.shape),
        ),
        Check(
            "tissue.dseg_labels",
            set(np.unique(dseg).tolist()) <= {0, 1, 2, 3},
            sorted(np.unique(dseg).tolist())[:8],
        ),
    ]
    mismatched = [t for t in TISSUES if not np.array_equal(masks[t] > 0, dseg == DSEG_INDEX[t])]
    checks.append(Check("tissue.masks_equal_dseg", not mismatched, mismatched))
    tpl_brain = np.asanyarray(template1.dataobj) > 0
    dice = _dice(dseg > 0, tpl_brain)
    checks.append(Check("tissue.dseg_matches_template", dice >= MIN_DSEG_DICE, round(dice, 4)))
    pr = np.stack([probseg[t] for t in TISSUES])
    total = pr.sum(axis=0)
    inside = total > 0
    in_range = bool(((pr >= 0) & (pr <= 1 + 1e-5)).all())
    sums_ok = float(np.mean(np.abs(total[inside] - 1) < 1e-3)) if inside.any() else 0.0
    checks.append(
        Check("tissue.probseg_sums_to_one", in_range and sums_ok >= 0.999, round(sums_ok, 5))
    )
    argmax = np.argmax(pr, axis=0) + 1
    agree = {
        t: round(float(np.mean(argmax[dseg == DSEG_INDEX[t]] == DSEG_INDEX[t])), 3) for t in TISSUES
    }
    volume_ml = {t: round(float(probseg[t].sum()) / 1000.0) for t in TISSUES}
    checks.append(
        Check(
            "tissue.probseg_classes_match_dseg",
            min(agree.values()) >= MIN_PROBSEG_AGREEMENT,
            {
                "agreement": agree,
                "probseg_ml": volume_ml,
                "dseg_ml": {t: round(int((dseg == DSEG_INDEX[t]).sum()) / 1000.0) for t in TISSUES},
            },
        )
    )
    return checks


# --- QC record --------------------------------------------------------------


def _within_rounding(a: object, b: object) -> bool:
    """Equal up to the last stored digit (or 1e-4 relative).

    The calibration gate in src/equivalence wants exact equality, because it runs
    the same code on the same machine. Here the recompute runs on a different CPU
    and BLAS, and aqi (a mean of 375 Spearman correlations, stored to 6 places)
    came back 1e-6 off on 2 of 10 runs. A wrong file moves these fields by 10% or
    more (gcor 0.236 against 0.340 on the transposed files).
    """
    if not isinstance(a, int | float) or not isinstance(b, int | float):
        return False
    return abs(a - b) <= max(2e-6, 1e-4 * abs(a))


def check_record(record: dict, recomputed: dict | None) -> list[Check]:
    checks = [Check("record.present", bool(record))]
    if not record:
        return checks
    checks.append(
        # 1.3 records await the confounds-only repair; their BOLD and IQMs are 1.4's.
        Check(
            "record.schema",
            record.get("schema_version") in ("1.3", "1.4"),
            record.get("schema_version"),
        )
    )
    if recomputed is not None:
        mism = [
            str(m)
            for m in calibration_mismatches(record, recomputed)
            if not _within_rounding(m.in_pod, m.recomputed)
        ]
        checks.append(
            Check(
                "record.recomputes_from_file",
                not mism,
                mism[:4],
                "the in-pod FuncQC record must describe the published file",
            )
        )
    return checks


# --- S3 plumbing ------------------------------------------------------------


def read_subjects(paths: list[Path]) -> list[str]:
    subjects: list[str] = []
    for p in paths:
        with p.open() as f:
            subjects += [
                row["subject_id"].strip() for row in csv.DictReader(f) if row.get("subject_id")
            ]
    return list(dict.fromkeys(subjects))


def session_markers(s3, bucket: str, subject: str) -> dict[str, dict]:
    """ses -> its _complete.json, for every exploded session of `subject`."""
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"derivatives/func/{subject}/", Delimiter="/")
    out = {}
    for p in resp.get("CommonPrefixes", []):
        ses = p["Prefix"].rstrip("/").rsplit("/", 1)[-1]
        try:
            body = s3.get_object(Bucket=bucket, Key=f"{p['Prefix']}_complete.json")["Body"].read()
        except s3.exceptions.NoSuchKey:
            continue
        out[ses] = json.loads(body)
    return out


def fetch_record(s3, bucket: str, unit: str, when) -> dict:
    """The run's raw FuncQC record merged with its surface record; {} if neither is found."""
    record: dict = {}
    for prefix, suffix in (("func-preproc", "_qc.json"), ("surface-sample", "_surf_qc.json")):
        for days in (0, -1, 1):
            dt = (when + timedelta(days=days)).strftime("%Y-%m-%d")
            try:
                body = s3.get_object(Bucket=bucket, Key=f"metrics/{prefix}/dt={dt}/{unit}{suffix}")
            except s3.exceptions.NoSuchKey:
                continue
            for k, v in json.loads(body["Body"].read()).items():
                record.setdefault(k, v)
            break
    return record


def download(s3, bucket: str, key: str, dest: Path) -> Path:
    s3.download_file(bucket, key, str(dest))
    return dest


# --- One unit ---------------------------------------------------------------


def audit_run(s3, args, subject: str, ses: str, run_key: str, tmp: Path, refs: dict) -> list[Check]:
    unit = f"{subject}_{ses}_{run_key}"
    base = f"derivatives/func/{subject}/{ses}/{unit}_"
    bold_key = f"{base}{SPACE}_bold.nii.gz"
    head = s3.head_object(Bucket=args.data_bucket, Key=bold_key)
    paths = {
        "bold": download(s3, args.data_bucket, bold_key, tmp / "bold.nii.gz"),
        "mask": download(
            s3, args.data_bucket, f"{base}{SPACE}_brainmask.nii.gz", tmp / "mask.nii.gz"
        ),
        "conf": download(
            s3, args.data_bucket, f"{base}desc-confounds_timeseries.tsv", tmp / "conf.tsv"
        ),
    }
    record = fetch_record(s3, args.metrics_bucket, unit, head["LastModified"])
    nss = int(record.get("n_nss_frames", 0))
    img = nib.load(paths["bold"])
    checks = check_bold_header(img, refs["t2"], record)
    data = np.asanyarray(img.dataobj).astype(np.float32, copy=False)
    mask = np.asanyarray(nib.load(paths["mask"]).dataobj) > 0
    conf = pd.read_csv(paths["conf"], sep="\t", na_values="n/a")
    tr = float(img.header.get_zooms()[3])
    checks += check_bold_values(data, mask)
    mean = data.mean(axis=3, dtype=np.float64)
    checks += check_orientation(mean, mask, np.asanyarray(refs["t2"].dataobj) > 0)
    gs_mni = data[mask].mean(axis=0, dtype=np.float64)
    checks += check_time(gs_mni, conf, nss)
    checks += check_confounds(conf, data.shape[3], tr, nss)
    if args.mosaics:
        write_mosaic(args.mosaics / f"{unit}_bold.png", mean, np.asanyarray(refs["t2"].dataobj))

    dts_key = (
        f"derivatives/func_surf/{subject}/{ses}/fsLR32k/{unit}_space-fsLR32k_bold.dtseries.nii"
    )
    try:
        dts = nib.load(download(s3, args.data_bucket, dts_key, tmp / "bold.dtseries.nii"))
        checks += check_dtseries(dts, data, img.affine, gs_mni, tr, nss, record)
        del dts
    except s3.exceptions.ClientError as exc:
        checks.append(Check("dtseries.present", False, type(exc).__name__))
    finally:
        (tmp / "bold.dtseries.nii").unlink(missing_ok=True)
    del data

    recomputed = None
    if record and not args.skip_recompute:
        recomputed = recompute_func_qc(
            RunInputs(
                paths["bold"],
                paths["mask"],
                paths["conf"],
                subject,
                ses,
                *run_key.split("_"),
                n_nss_frames=nss,
            ),
            arm="audit",
        )
    checks += check_record(record, recomputed)
    return checks


def audit_tissue(s3, args, subject: str, ses: str, tmp: Path, refs: dict) -> list[Check]:
    base = f"derivatives/func/{subject}/{ses}/{subject}_{ses}_{SPACE}_res-1_"
    try:
        dseg = nib.load(download(s3, args.data_bucket, f"{base}dseg.nii.gz", tmp / "dseg.nii.gz"))
        masks, probseg = {}, {}
        for t in TISSUES:
            masks[t] = np.asanyarray(
                nib.load(
                    download(
                        s3,
                        args.data_bucket,
                        f"{base}label-{t}_mask.nii.gz",
                        tmp / f"{t}_mask.nii.gz",
                    )
                ).dataobj
            )
            probseg[t] = np.asanyarray(
                nib.load(
                    download(
                        s3,
                        args.data_bucket,
                        f"{base}label-{t}_probseg.nii.gz",
                        tmp / f"{t}_probseg.nii.gz",
                    )
                ).dataobj
            ).astype(np.float32)
    except s3.exceptions.ClientError as exc:
        return [Check("tissue.present", False, type(exc).__name__)]
    checks = check_tissue(dseg, masks, probseg, refs["t1"])
    if args.mosaics:
        write_tissue_mosaic(
            args.mosaics / f"{subject}_{ses}_tissue.png",
            np.asanyarray(dseg.dataobj),
            probseg,
            np.asanyarray(refs["t1"].dataobj),
        )
    return checks


# --- Mosaics ----------------------------------------------------------------


def _slices(vol: np.ndarray) -> list[np.ndarray]:
    x, y, z = (s // 2 for s in vol.shape[:3])
    return [np.rot90(vol[x, :, :]), np.rot90(vol[:, y, :]), np.rot90(vol[:, :, z])]


def write_mosaic(path: Path, mean: np.ndarray, template: np.ndarray) -> None:
    """Mean BOLD on three mid slices with the template's brain outline."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(9, 3.2))
    for ax, img, tpl in zip(axes, _slices(mean), _slices(template), strict=True):
        ax.imshow(img, cmap="gray")
        ax.contour(tpl > 0, levels=[0.5], colors="r", linewidths=0.5)
        ax.axis("off")
    fig.suptitle(path.stem, fontsize=8)
    fig.savefig(path, dpi=90, bbox_inches="tight")
    plt.close(fig)


def write_tissue_mosaic(path: Path, dseg: np.ndarray, probseg: dict, template: np.ndarray) -> None:
    """Row 1: the dseg; row 2: probseg as RGB (R=CSF, G=GM, B=WM). Same colours should mean same tissue."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rgb = np.stack([probseg["CSF"], probseg["GM"], probseg["WM"]], axis=-1)
    seg_rgb = np.stack([dseg == 3, dseg == 1, dseg == 2], axis=-1).astype(np.float32)
    fig, axes = plt.subplots(2, 3, figsize=(9, 6.4))
    for col, (a, b) in enumerate(zip(_slices(seg_rgb), _slices(rgb), strict=True)):
        axes[0, col].imshow(np.clip(a, 0, 1))
        axes[1, col].imshow(np.clip(b, 0, 1))
        axes[0, col].axis("off")
        axes[1, col].axis("off")
    fig.suptitle(f"{path.stem}: top dseg, bottom probseg (R=CSF G=GM B=WM)", fontsize=8)
    fig.savefig(path, dpi=90, bbox_inches="tight")
    plt.close(fig)


# --- Driver -----------------------------------------------------------------


def plan_units(s3, args, subjects: list[str], rng: random.Random):
    """(subject, session, [run keys]) for each sampled subject that has an exploded session."""
    for subject in subjects:
        markers = session_markers(s3, args.data_bucket, subject)
        if not markers:
            yield subject, None, []
            continue
        ses = rng.choice(sorted(markers))
        runs = sorted(markers[ses].get("runs", {}))
        if runs and not args.all_runs:
            runs = [rng.choice(runs)]
        yield subject, ses, runs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--subjects", type=Path, nargs="+", required=True, help="CSV(s) with subject_id"
    )
    ap.add_argument("--sample", type=int, default=50, help="subjects to sample (0 = all)")
    ap.add_argument("--seed", type=int, default=20261026)
    ap.add_argument("--all-runs", action="store_true", help="every run of the chosen session")
    ap.add_argument(
        "--skip-recompute", action="store_true", help="skip the FuncQC recompute (~40 s/run)"
    )
    ap.add_argument("--data-bucket", required=True)
    ap.add_argument("--region", help="default: the AWS profile's")
    ap.add_argument("--metrics-bucket", required=True)
    ap.add_argument("--scratch", type=Path, default=Path.home() / "audit-scratch")
    ap.add_argument("--out", type=Path, required=True, help="JSONL, one line per checked unit")
    ap.add_argument("--mosaics", type=Path, help="write PNG mosaics here")
    args = ap.parse_args(argv)

    s3 = boto3.client("s3", region_name=args.region)
    rng = random.Random(args.seed)
    subjects = read_subjects(args.subjects)
    if args.sample and args.sample < len(subjects):
        subjects = rng.sample(subjects, args.sample)
    if args.mosaics:
        args.mosaics.mkdir(parents=True, exist_ok=True)
    args.scratch.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="audit-", dir=args.scratch))
    failures: dict[str, list[str]] = {}
    totals: dict[str, int] = {}
    try:
        refs = {
            "t2": nib.load(download(s3, args.data_bucket, TEMPLATE_2MM, root / "tpl2.nii")),
            "t1": nib.load(download(s3, args.data_bucket, TEMPLATE_1MM, root / "tpl1.nii")),
        }
        with args.out.open("a") as out:
            for subject, ses, runs in tqdm(
                list(plan_units(s3, args, subjects, rng)),
                desc="units",
                unit="subject",
                disable=None,
                dynamic_ncols=True,
            ):
                if ses is None:
                    results = {f"{subject}": [Check("session.present", False)]}
                else:
                    results = {}
                    tmp = Path(tempfile.mkdtemp(dir=root))
                    try:
                        results[f"{subject}_{ses}_tissue"] = audit_tissue(
                            s3, args, subject, ses, tmp, refs
                        )
                        for run_key in runs:
                            unit = f"{subject}_{ses}_{run_key}"
                            try:
                                results[unit] = audit_run(
                                    s3, args, subject, ses, run_key, tmp, refs
                                )
                            except Exception as exc:  # one unreadable run must not end the audit
                                results[unit] = [
                                    Check("run.readable", False, f"{type(exc).__name__}: {exc}")
                                ]
                            for f in tmp.iterdir():
                                f.unlink()
                    finally:
                        shutil.rmtree(tmp, ignore_errors=True)
                for unit, checks in results.items():
                    out.write(
                        json.dumps(
                            {"unit": unit, "checks": [asdict(c) for c in checks]}, default=str
                        )
                        + "\n"
                    )
                    out.flush()
                    for c in checks:
                        totals[c.name] = totals.get(c.name, 0) + 1
                        if not c.ok:
                            failures.setdefault(c.name, []).append(unit)
                            tqdm.write(f"FAIL {unit} {c.name}: {c.value} {c.detail}".rstrip())
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n{'check':40} {'failed':>7} / units")
    for name in sorted(totals):
        print(f"{name:40} {len(failures.get(name, [])):>7} / {totals[name]}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
