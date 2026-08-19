#!/usr/bin/env python3
"""
bold_to_t1w.py — BOLD → T1w rigid registration via SynthMorph

Contrast-agnostic rigid registration using mri_synthmorph. SynthMorph
operates on intensity-normalized images and is robust to EPI-T1w contrast
differences, making it suitable for ABCD BOLD data at 2.4mm resolution where
boundary-based (BBR) methods cannot detect reliable gray/white contrast.

Inputs (via CLI args):
  --bold         4D BOLD NIfTI
  --nss-frames   Number of NSS frames to skip (takes precedence over --json)
  --json         BIDS sidecar JSON (optional; used to skip NSS volumes)
  --subjects-dir FastSurfer subjects directory (SUBJECTS_DIR)
  --subject      Subject ID as it appears under --subjects-dir
  --out-dir      Output directory
  --prefix       Output filename prefix
  --subj/--ses/--task/--run/--pipeline  QC record provenance

Outputs written to --out-dir:
  <prefix>_desc-bold2t1w_ref.nii.gz       temporal-mean BOLD reference (QC)
  <prefix>_desc-bold2t1w.lta              SynthMorph transform (T1w_RAS → BOLD_RAS —
                                          note the direction is opposite to the
                                          filename; see parse_lta_matrix)
  <prefix>_desc-bold2t1w_itk.txt          ANTs/ITK affine (via lta_convert)
  <prefix>_desc-bold2t1w_warped.nii.gz    BOLD ref warped to T1w space (QC)
  <prefix>_desc-bold2t1w_brainmask.nii.gz T1w brain mask in BOLD space

QC written to /tmp/<prefix>_bold_to_t1w_reg_qc.json (Argo uploads as artifact):
  schema_version: "2.6"
  method: "synthmorph"
  nmi: float  — Studholme normalized MI between warped BOLD and T1w (within brain
                mask). Recorded, not gated. NOTE the docstring range on
                normalized_mutual_information (1.0 independent → 2.0 determined)
                is the THEORETICAL bound, not an operating scale: for
                cross-contrast EPI↔T1w here a good registration scores ~1.019 and
                identity scores ~1.011. Do not read ~1.02 as failed alignment.
                > 0 still marks a completed run.
  nmi_identity: float — NMI of the same BOLD reference resampled with NO
                transform (schema 2.4). The per-session no-registration baseline.
  nmi_gain: float — nmi - nmi_identity, i.e. what the fitted transform actually
                bought (schema 2.4). THE GATED METRIC. Absolute nmi is not
                comparable across sessions (the identity baseline alone spans
                ~0.004, about the size of the gain itself), so the gate scores
                each run against its own baseline. Measured 0.0046–0.0089 over 18
                healthy runs.
  rigid_disp_mean_mm / rigid_disp_max_mm / rigid_rot_deg: float — magnitude of the
                rigid transform over the brain (schema 2.2). RECORDED-ONLY. They
                gated in schema 2.3 and that was wrong: on ABCD minproc input the
                BOLD is never resampled out of scanner space, so these track the
                field-of-view prescription (session-level constants, 69x
                between/within SD ratio) and correlate positively with quality.
  mhd_mm: float — modified Hausdorff distance (mm) between the T1w and warped-EPI
                brain surfaces (schema 2.2, recorded-only). -1.0 = could not
                compute (empty EPI skull-strip). See _write_qc.
  seg_bbr_contrast: float — GM/WM contrast of the warped BOLD sampled through
                aseg.auto.mgz (schema >=2.5, recorded-only). -999.0 = could not
                compute. See registration_qc.segmentation_alignment.
  ngf: float — normalized gradient field, edge-direction agreement in [0, 1]
                (schema >=2.5, recorded-only). See registration_qc.
                normalized_gradient_field.
  seg_bbr_contrast_identity / ngf_identity: float — the same two metrics on the
                identity-resampled BOLD, i.e. with no registration applied.
                Retained because they are the reference each metric is read
                against; the differences are derivable in SQL and no longer
                stored (see _write_qc for the schema 2.6 removals).
  verdict: str — "pass" | "fail" from the bold_to_t1w gate: fail if
                nmi_gain <= 0, else pass. This is a SANITY FLOOR, not a quality
                bound — it fires only when the fitted transform is no better than
                doing no registration at all, so a 'pass' is not a claim that the
                run is well registered. Quality is recorded, never gated; see
                _BOLD_T1W_THRESHOLDS for why. There is no 'warn' any more.
                A 'fail' exits the step 65 so the driver discards the outputs; the
                QC record (verdict='fail') still uploads. '' on the early-exit path.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import nibabel as nib
import numpy as np
from registration_qc import (
    _BOLD_T1W_THRESHOLDS,
    modified_hausdorff_distance,
    normalized_gradient_field,
    normalized_mutual_information,
    rigid_transform_metrics,
    segmentation_alignment,
    verdict,
)
from scipy import ndimage

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reference frame extraction
# ---------------------------------------------------------------------------


def get_first_steady_state_frame(nss_frames: int | None, json_sidecar: Path | None) -> int:
    if nss_frames is not None:
        log.info(f"Using --nss-frames override: skipping {nss_frames} NSS volume(s)")
        return nss_frames
    if json_sidecar is not None and json_sidecar.exists():
        with open(json_sidecar) as f:
            meta = json.load(f)
        nss = meta.get("NumberOfVolumesDiscardedByScanner", 0) + meta.get(
            "NumberOfVolumesDiscardedByUser", 0
        )
        if nss:
            log.info(f"Sidecar: skipping {nss} NSS volume(s)")
        return nss
    return 0


def build_reference(
    bold: Path, nss_frames: int | None, json_sidecar: Path | None, out: Path
) -> Path:
    """Compute the temporal mean of steady-state BOLD frames as the registration reference."""
    frame_idx = get_first_steady_state_frame(nss_frames, json_sidecar)
    bold_img = nib.load(str(bold))
    all_data = np.asarray(bold_img.dataobj)
    if all_data.ndim == 4 and all_data.shape[3] > frame_idx:
        n_frames = all_data.shape[3] - frame_idx
        log.info(
            f"Temporal mean of {n_frames} steady-state frames "
            f"(skipping {frame_idx} NSS frames) as registration reference"
        )
        data = all_data[..., frame_idx:].mean(axis=-1).astype(np.float32)
    else:
        log.info(f"Extracting frame {frame_idx} from BOLD as registration reference")
        data = all_data[..., frame_idx] if all_data.ndim == 4 else all_data
    ref_img = nib.Nifti1Image(data, bold_img.affine, bold_img.header)
    ref_img.header.set_data_shape(data.shape)
    nib.save(ref_img, str(out))
    return out


# ---------------------------------------------------------------------------
# BBR init detection (FreeSurfer 7.4.1 fallback awareness)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _run_synthmorph(ref: Path, t1w: Path, out_lta: Path) -> None:
    """Run mri_synthmorph rigid registration (contrast-agnostic)."""
    cmd = [
        "mri_synthmorph",
        "-m",
        "rigid",
        "-t",
        str(out_lta),
        str(ref),
        str(t1w),
    ]
    log.info(f"SynthMorph: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    log.info(f"SynthMorph LTA written: {out_lta}")


# ---------------------------------------------------------------------------
# LTA utilities
# ---------------------------------------------------------------------------


def parse_lta_matrix(lta_path: Path) -> np.ndarray:
    """Read the 4×4 RAS-to-RAS matrix from an LTA file or plain matrix file.

    mri_synthmorph in FreeSurfer 7.4.1 writes a headerless plain 4×4 matrix
    rather than a proper LTA. This function handles both:
      1. Standard LTA: scan for 'N 4 4' marker (variable whitespace)
      2. Plain matrix: entire file is 4 rows of 4 floats (no header)

    DIRECTION — the matrix maps T1w_RAS → BOLD_RAS (a pullback), which is the
    opposite of what the `bold2t1w` filenames suggest. The bare 4×4 declares no
    direction at all (no LTA type field, no volume geometry), so this docstring
    is the only place the convention is recorded; every caller depends on it.

    Measured 2026-07-23 over 18 runs, MI against T1.mgz within brainmask.mgz:
    applying the matrix as a BOLD→T1w map scored 0.0298, its inverse scored
    0.1361 (winning 18/18), and identity — no registration at all — scored
    0.0607. Reading it forward was worse than not registering.

    Callers resampling *into* T1w space, or carrying T1w coordinates into BOLD,
    use it as-is; callers carrying BOLD coordinates into T1w invert it. Do NOT
    invert here: write_itk_from_lta needs the raw orientation.
    """
    with open(lta_path) as f:
        lines = f.readlines()

    # Standard LTA: find 'N 4 4' dimension marker
    dim_pat = re.compile(r'^\s*\d+\s+4\s+4\s*$')
    for i, line in enumerate(lines):
        if dim_pat.match(line):
            rows = []
            for j in range(1, 5):
                rows.append([float(x) for x in lines[i + j].split()])
            return np.array(rows)

    # Plain matrix fallback (mri_synthmorph 7.4.1 writes just the 4×4 numbers)
    data_lines = [ln for ln in lines if ln.strip() and not ln.strip().startswith('#')]
    try:
        mat = np.array([[float(x) for x in ln.split()] for ln in data_lines])
        if mat.shape == (4, 4):
            log.info("LTA parsed as plain 4×4 matrix (no LTA header)")
            return mat
    except (ValueError, IndexError):
        pass

    log.error("LTA parse failed — file content:\n" + ''.join(lines[:40]))
    raise ValueError(f"Could not parse 4×4 matrix from {lta_path}")


def write_itk_from_lta(lta_path: Path, itk_path: Path) -> None:
    """Convert a FreeSurfer RAS-to-RAS LTA to ITK LPS-to-LPS affine format.

    lta_convert --outitk crashes with munmap_chunk on SynthMorph LTAs (FreeSurfer
    7.4.1 heap corruption when --src/--trg are provided with a geometry-less LTA).
    This hand-rolls the equivalent conversion in pure Python:
      A_lps = D @ R @ D,  t_lps = D @ t,  D = diag(-1, -1, 1)
    FixedParameters are 0 0 0 (LTA encodes no rotation center).

    No inversion, deliberately: ITK affines are pullbacks (fixed → moving), and
    the SynthMorph matrix is already one (T1w_RAS → BOLD_RAS, with T1w fixed).
    A basis flip is the whole conversion. This is the only transform path that
    was correct before 2026-07-23 — precisely because it never restated the
    direction and let ITK's own semantics carry it.
    """
    T = parse_lta_matrix(lta_path)
    D = np.diag([-1.0, -1.0, 1.0])
    A = D @ T[:3, :3] @ D
    v = D @ T[:3, 3]
    params = ' '.join(f'{x:.15g}' for x in list(A.ravel()) + list(v))
    with open(itk_path, 'w') as f:
        f.write('#Insight Transform File V1.0\n')
        f.write('#Transform 0\n')
        f.write('Transform: AffineTransform_double_3_3\n')
        f.write(f'Parameters: {params}\n')
        f.write('FixedParameters: 0 0 0\n')
    log.info(f"ANTs/ITK transform written: {itk_path}")


# ---------------------------------------------------------------------------
# Volume resampling (Python-based, no mri_vol2vol dependency)
# ---------------------------------------------------------------------------


def resample_with_matrix(
    moving_path: Path, T: np.ndarray, reference_path: Path, order: int = 1
) -> np.ndarray:
    """Resample moving_path onto reference_path's grid under RAS transform `T`.

    Pull-resampling walks every *reference* voxel back into moving space, so it
    needs reference_RAS → moving_RAS. At this module's call sites the reference
    is T1w and the moving image is the BOLD ref, which is exactly the direction
    the SynthMorph matrix already points (see parse_lta_matrix) — so it is
    applied as-is, with no inversion:
      v_mov = A_mov⁻¹ @ T @ A_ref @ v_ref

    The exception is mask_to_bold, which resamples in the opposite direction
    (mask onto BOLD grid) and therefore passes the inverse matrix deliberately.

    Passing the identity for `T` yields the no-registration baseline: the same
    BOLD reference on the same grid, related only by the two images' affines.
    That is the reference point nmi_gain is measured against (see main()).
    """
    mov_img = nib.load(str(moving_path))
    ref_img = nib.load(str(reference_path))
    mov_data = np.asarray(mov_img.dataobj)
    if mov_data.ndim == 4:
        mov_data = mov_data[..., 0]

    M = np.linalg.inv(mov_img.affine) @ T @ ref_img.affine

    shape = ref_img.shape[:3]
    i, j, k = np.mgrid[: shape[0], : shape[1], : shape[2]]
    n = i.size
    vox = np.ones((4, n), dtype=np.float64)
    vox[0] = i.ravel()
    vox[1] = j.ravel()
    vox[2] = k.ravel()
    # On the 256^3 conformed grid these three int64 arrays are ~0.40 GB and are
    # dead the moment vox is filled; without the del they stay live through
    # map_coordinates. Same reason as the dels in images/afni/preproc.py.
    del i, j, k

    # mov_coords is a VIEW of the (4, N) product, so the full ~0.54 GB float64
    # result stays alive until map_coordinates returns. Nothing to free here —
    # noted so a future reader does not add a misleading `del`.
    mov_coords = (M @ vox)[:3]
    return ndimage.map_coordinates(
        mov_data.astype(np.float32),
        mov_coords,
        order=order,
        mode='constant',
        cval=0,
    ).reshape(shape)


def apply_lta(
    moving_path: Path, lta_path: Path, reference_path: Path, out_path: Path, order: int = 1
) -> None:
    """Resample moving_path into the space of reference_path using the LTA."""
    resampled = resample_with_matrix(
        moving_path, parse_lta_matrix(lta_path), reference_path, order=order
    )
    ref_img = nib.load(str(reference_path))
    out_img = nib.Nifti1Image(resampled, ref_img.affine, ref_img.header)
    out_img.header.set_data_shape(resampled.shape)
    nib.save(out_img, str(out_path))


def mask_to_bold(subjects_dir: Path, subject: str, ref: Path, lta: Path, out: Path) -> Path:
    """Warp the FastSurfer brain mask (T1w space) into BOLD space.

    Pull-resampling from the BOLD grid needs BOLD_RAS → T1w_RAS to look up each
    BOLD voxel's location in the mask. That is the *inverse* of the SynthMorph
    matrix, which runs T1w_RAS → BOLD_RAS (see parse_lta_matrix) — so unlike
    every other call site this one passes the inverse, and that inversion is the
    whole content of this function. It shipped inverted once (fixed cd33678),
    which is why the resampling itself lives in resample_with_matrix and is not
    re-implemented here: a second copy is a second place to get the direction
    wrong.

    Nearest-neighbour (order=0) because the input is a label volume — linear
    interpolation would produce fractional mask values.
    """
    brainmask_mgz = subjects_dir / subject / "mri" / "brainmask.mgz"
    T_inv = np.linalg.inv(parse_lta_matrix(lta))

    sampled = resample_with_matrix(brainmask_mgz, T_inv, ref, order=0)

    ref_img = nib.load(str(ref))
    out_img = nib.Nifti1Image((sampled > 0).astype(np.uint8), ref_img.affine, ref_img.header)
    out_img.header.set_data_shape(sampled.shape)
    nib.save(out_img, str(out))
    log.info(f"BOLD brain mask written: {out}")
    return out


# ---------------------------------------------------------------------------
# EPI brain mask (for boundary-agreement QC)
# ---------------------------------------------------------------------------


def _otsu_threshold(vals: np.ndarray) -> float:
    """Otsu's threshold over a 1-D array of intensities (256-bin histogram)."""
    hist, edges = np.histogram(vals, bins=256)
    hist = hist.astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 0.0
    centers = (edges[:-1] + edges[1:]) / 2.0
    w_bg = np.cumsum(hist)  # weight of the "below threshold" class
    w_fg = total - w_bg
    cum = np.cumsum(hist * centers)
    mean_bg = np.divide(cum, w_bg, out=np.zeros_like(cum), where=w_bg > 0)
    mean_fg = np.divide(cum[-1] - cum, w_fg, out=np.zeros_like(cum), where=w_fg > 0)
    between = w_bg * w_fg * (mean_bg - mean_fg) ** 2
    return float(centers[int(np.argmax(between))])


def bold_brain_mask(warped: np.ndarray) -> np.ndarray:
    """Skull-strip a warped BOLD reference into a boolean brain mask.

    Used only for the boundary-agreement QC (modified_hausdorff_distance) — the
    pipeline never produces a BOLD brain mask otherwise (it warps the T1w mask
    *into* BOLD; see mask_to_bold). Otsu-threshold the in-FOV (> 0) voxels, keep
    the largest connected component, then fill interior holes. This is a soft
    approximation: ABCD EPI is 2.4 mm and low-contrast, so the brain edge is
    fuzzy and bright non-brain (eyes, vessels) can leak in. That imperfection is
    exactly why mhd_mm ships recorded-only — the mask, not the registration, sets
    its achievable floor.
    """
    fg = warped[warped > 0]
    if fg.size == 0:
        return np.zeros(warped.shape, dtype=bool)
    mask = warped > _otsu_threshold(fg)
    labels, n = ndimage.label(mask)
    if n == 0:
        return mask
    # Largest non-background component (bincount index 0 is background).
    largest = int(np.argmax(np.bincount(labels.ravel())[1:])) + 1
    return ndimage.binary_fill_holes(labels == largest)


def rigid_metrics_from_lta(lta_path: Path, t1w_mask_img, brain_mask: np.ndarray) -> dict:
    """rigid_transform_metrics over the T1w brain, straight from the LTA file.

    A three-line wrapper that exists to be *importable*. The pairing it makes is
    the part that can silently go wrong: parse_lta_matrix returns a T1w_RAS →
    BOLD_RAS map, so the points fed alongside it must be T1w-space RAS in mm —
    obtained by pushing the brain-mask voxel indices through that same mask
    image's affine. Passing voxel indices instead breaks it without surfacing an
    error: FreeSurfer's conformed grid is LIA, so indices differ from RAS by an
    axis permutation with sign flips on top of a ~128 mm origin shift, and the
    resulting displacement is neither obviously large nor obviously small. On the
    test phantom it reads 6.4 mm where the truth is 9.2 mm — wrong by a third,
    and entirely plausible as a log line.

    Inline in main() that could not be tested except by re-typing the expression
    in the test, which cannot catch a matched pair of errors — the failure mode
    that let the LTA-direction bug (cd33678) ship for months behind four mutually
    agreeing docstrings.

    What this CANNOT check is the matrix's direction. For a rigid T, left-
    multiplying by the orthogonal R gives ‖T⁻¹x − x‖ = ‖R(Rᵀx − Rᵀt − x)‖ =
    ‖x − t − Rx‖ = ‖Tx − x‖ at every point, and arccos((tr R − 1)/2) is equal for
    R and Rᵀ — so all three metrics are numerically identical under inversion.
    Do not reach for them to validate direction; nmi_gain is what catches that.
    See tests/images/freesurfer/test_lta_direction.py.
    """
    lta_matrix = parse_lta_matrix(lta_path)
    brain_idx = np.argwhere(brain_mask)
    brain_ras = brain_idx @ t1w_mask_img.affine[:3, :3].T + t1w_mask_img.affine[:3, 3]
    return rigid_transform_metrics(lta_matrix, brain_ras)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BOLD → T1w rigid registration via SynthMorph")
    p.add_argument("--bold", required=True, type=Path)
    p.add_argument(
        "--nss-frames",
        required=False,
        type=lambda x: int(x) if x != '' else None,
        default=None,
        help="Number of NSS frames to skip; takes precedence over --json",
    )
    p.add_argument(
        "--json",
        required=False,
        type=Path,
        default=None,
        help="BIDS sidecar JSON for NSS volume detection",
    )
    p.add_argument("--subjects-dir", required=True, type=Path)
    p.add_argument("--subject", required=True)
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--prefix", required=True)
    p.add_argument("--subj", default="", help="Subject ID written into RegistrationQC metrics JSON")
    p.add_argument(
        "--ses", default="", help="Session label written into RegistrationQC metrics JSON"
    )
    p.add_argument("--task", default="", help="Task label written into RegistrationQC metrics JSON")
    p.add_argument("--run", default="", help="Run label written into RegistrationQC metrics JSON")
    p.add_argument(
        "--pipeline",
        default="cloudpipe_minproc",
        help="Pipeline name written into RegistrationQC metrics JSON",
    )
    return p.parse_args()


def _write_qc(args: argparse.Namespace, measured: dict | None = None) -> None:
    """Write RegistrationQC JSON (schema 2.6) to /tmp for Argo artifact upload.

    Takes the single `measured` record main() also hands to verdict(), so every
    field has exactly one source. `nmi` used to be a separate positional
    parameter while every other metric arrived in a dict, which meant the record
    could disagree with what was gated. Omit `measured` entirely for the
    early-exit path: every field then takes its "not computed" default below.

    Only fields this step actually computes are emitted. Schema 1.2 additionally
    wrote `dice`, `bbr_cost`, `bbr_converged`, `bbr_init_used` and `jac_det_*` as
    hardcoded 0.0/None literals. Nothing computed them, so dashboards read the
    zeros as real measurements — the "Mean Dice (BOLD→T1w)" panel reported 0.0 and
    "% Runs Low Dice" reported 100% for every run. They are dropped rather than
    kept as fake zeros:

      - `dice`: no BOLD brain mask is computed (only the T1w mask is warped *into*
        BOLD space), and cross-contrast EPI↔T1w Dice is not meaningful regardless.
      - `bbr_*`: BBR was removed in 61ccff7 in favour of SynthMorph-only, because
        ABCD's 2.4 mm EPI lacks the gray/white contrast bbregister requires.
      - `jac_det_*`: SynthMorph runs with `-m rigid`; a rigid transform's Jacobian
        determinant is identically 1 everywhere, so the statistics carry no signal.

    `nmi` was the only quality metric through schema 2.1; it replaced the raw
    `mi` (nats) emitted by 2.0, because raw MI is computed over surviving voxels
    and inflates as brain overlap collapses, so a broken registration could
    out-score a healthy one (2655eea). NMI is overlap-invariant and lands
    ~1.0–1.4 for healthy EPI↔T1w runs. It is 0.0 on the early-exit failure path —
    an impossible value for real tissue (NMI >= 1) — so consumers still filter on
    `nmi > 0` to exclude failed runs.

    Schema 2.2 added three transform-only fields (rigid_disp_mean_mm,
    rigid_disp_max_mm, rigid_rot_deg) and one boundary field (mhd_mm). The rigid_*
    trio reads only the transform, so it is robust and orthogonal to `nmi`; mhd_mm
    depends on an EPI skull-strip and carries a -1.0 "could not compute" sentinel.
    On the early-exit failure path (`measured` is None) the rigid_* fields are 0.0,
    mhd_mm is the -1.0 sentinel, and `verdict` is '' (not evaluated).

    Schema 2.3 added `verdict` and gated the run on rigid_rot_deg /
    rigid_disp_max_mm. Schema 2.4 REVERTS that gate and replaces it with
    `nmi_gain`, adding `nmi_identity` and `nmi_gain`. The rigid_* trio is
    recorded-only again (its schema 2.2 status), because on ABCD minimally
    preprocessed input those metrics track the BOLD-vs-T1w field-of-view
    prescription, not registration quality — they are session-level constants
    (69x between/within-session SD ratio) and correlate POSITIVELY with nmi. The
    2.3 gate failed 92 of 110 runs in the 2026-07-29 batch and passed none of
    them, discarding whole sessions on scanner positioning; see
    _BOLD_T1W_THRESHOLDS and the section-0 writeup in
    docs/investigations/2026-07-29-bold-to-t1w-qc-handoff.md.

    `nmi_identity` is the NMI of the same BOLD reference resampled with identity
    — no registration at all — and `nmi_gain` is `nmi - nmi_identity`. Only
    nmi_gain gates. On the early-exit failure path both are 0.0.

    2026-07-30 moved that gate from a fitted constant (fail 0.0005 / warn 0.002)
    to the structural bound `nmi_gain <= 0`, and dropped the warn band. The gate
    now detects only "the transform bought nothing", never "the transform is
    poor" — a false fail is permanent here (deterministic registration, exit 65
    excluded from the retry expression) while a false pass is recoverable from
    Athena, so quality judgements do not belong behind it. See
    _BOLD_T1W_THRESHOLDS.

    Schema 2.5 added two boundary-sensitive metric families, both RECORDED-ONLY:
    `seg_bbr_contrast` from registration_qc.segmentation_alignment and `ngf` from
    registration_qc.normalized_gradient_field, each with an identity baseline.
    They exist because `nmi` pools the whole brain into one joint histogram and
    so is nearly blind to a few millimetres of boundary slip; these evaluate at
    tissue interfaces instead. seg_* carries a -999.0 "could not compute"
    sentinel (absent aseg.auto.mgz, empty label, or a zero denominator) — 0.0 is
    a legitimate seg_bbr_contrast, so it cannot serve as the sentinel there the
    way it does for nmi. `ngf` uses 0.0, which is its natural floor.

    Schema 2.6 REMOVES three of those fields on Phase A evidence
    (handoffs/bold-t1w-qc-gate-calibration/RESULTS.md): `seg_bbr_contrast_gain`
    and `ngf_gain`, because every _gain measured worse than its absolute
    counterpart and both stay derivable from the retained operands; and
    `seg_ventricle_ratio`, which was non-monotone in misregistration. See
    RegistrationQC in src/metrics/schemas.py for the full rationale.

    Nothing here gates. The gate is the structural floor on nmi_gain; these are
    the candidates for a future WITHIN-SESSION relative check, which is the shape
    Phase A supports and an absolute bound is not.

    main() exits 65 on a 'fail' AFTER this record is written, so the QC (with
    verdict='fail') still uploads while the driver discards the run's outputs —
    see main().
    """
    if not (args.subj and args.ses):
        return
    m = measured or {}
    qc = {
        'schema_version': '2.6',
        'pipeline': args.pipeline,
        'subject': args.subj,
        'session': args.ses,
        'registration_type': 'bold_to_t1w',
        'method': 'synthmorph',
        'nmi': m.get('nmi', 0.0),
        'nmi_identity': m.get('nmi_identity', 0.0),
        'nmi_gain': m.get('nmi_gain', 0.0),
        'rigid_disp_mean_mm': m.get('rigid_disp_mean_mm', 0.0),
        'rigid_disp_max_mm': m.get('rigid_disp_max_mm', 0.0),
        'rigid_rot_deg': m.get('rigid_rot_deg', 0.0),
        'mhd_mm': m.get('mhd_mm', -1.0),
        'seg_bbr_contrast': m.get('seg_bbr_contrast', -999.0),
        'seg_bbr_contrast_identity': m.get('seg_bbr_contrast_identity', -999.0),
        'ngf': m.get('ngf', 0.0),
        'ngf_identity': m.get('ngf_identity', 0.0),
        'verdict': m.get('verdict', ''),
        'task': args.task,
        'run': args.run,
        'completed_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    metrics_path = Path(
        f'/tmp/{args.subj}_{args.ses}_{args.task}_{args.run}_bold_to_t1w_reg_qc.json'
    )
    metrics_path.write_text(json.dumps(qc))
    log.info(f"Registration QC metrics written: {metrics_path}")


def main() -> None:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    pfx = args.out_dir / args.prefix

    log.info(f"=== bold_to_t1w (synthmorph): {args.prefix} ===")
    log.info(f"SUBJECTS_DIR : {args.subjects_dir}")
    log.info(f"Subject      : {args.subject}")

    ref = Path(str(pfx) + "_desc-bold2t1w_ref.nii.gz")
    out_lta = Path(str(pfx) + "_desc-bold2t1w.lta")
    warped = Path(str(pfx) + "_desc-bold2t1w_warped.nii.gz")
    itk_mat = Path(str(pfx) + "_desc-bold2t1w_itk.txt")
    bold_mask = Path(str(pfx) + "_desc-bold2t1w_brainmask.nii.gz")

    subjects_dir = args.subjects_dir
    subject = args.subject
    t1w = subjects_dir / subject / "mri" / "T1.mgz"

    for path in [t1w, subjects_dir / subject / "mri" / "brainmask.mgz"]:
        if not path.exists():
            log.error(f"Required FastSurfer output not found: {path}")
            _write_qc(args)
            sys.exit(1)

    # Step 1: temporal-mean BOLD reference
    build_reference(args.bold, args.nss_frames, args.json, ref)

    # Step 2: SynthMorph rigid registration
    _run_synthmorph(ref, t1w, out_lta)

    # Step 3: convert LTA → ITK affine (pure Python — bypasses lta_convert munmap bug)
    write_itk_from_lta(out_lta, itk_mat)

    # Step 4: warp BOLD ref into T1w space for visual QC
    apply_lta(ref, out_lta, t1w, warped, order=1)
    log.info(f"Warped QC image written: {warped}")

    # Step 5: warp T1w brain mask into BOLD space
    mask_to_bold(subjects_dir, subject, ref, out_lta, bold_mask)

    # Step 6: QC metrics
    warped_img = nib.load(str(warped))
    warped_data = warped_img.get_fdata(dtype=np.float32)
    t1w_mask_path = subjects_dir / subject / "mri" / "brainmask.mgz"
    t1w_mask_img = nib.load(str(t1w_mask_path))
    t1w_mask_data = t1w_mask_img.get_fdata(dtype=np.float32)
    t1w_data = nib.load(str(t1w)).get_fdata(dtype=np.float32)
    brain_mask = t1w_mask_data > 0

    # Intensity agreement (primary metric): Studholme normalized MI.
    nmi_val = normalized_mutual_information(warped_data, t1w_data, mask=brain_mask)
    log.info(f"NMI (BOLD→T1w, within brain mask): {nmi_val:.4f}")

    # Identity baseline: the same BOLD reference resampled onto the same grid
    # with NO transform. Absolute NMI is not comparable across sessions — the
    # identity baseline alone moved 1.00947–1.01329 over three measured sessions,
    # a spread comparable to the entire improvement signal — so the gated metric
    # is the *gain* this registration buys over doing nothing. Self-calibrating
    # across site, protocol and population; see _BOLD_T1W_THRESHOLDS.
    identity_data = resample_with_matrix(ref, np.eye(4), t1w, order=1)
    nmi_identity = normalized_mutual_information(identity_data, t1w_data, mask=brain_mask)
    nmi_gain = nmi_val - nmi_identity
    log.info(f"NMI identity baseline: {nmi_identity:.4f}  →  gain {nmi_gain:+.5f}")

    # Transform-only magnitude (schema 2.2): how far the rigid transform actually
    # moves the brain. Uses the T1w brain-mask voxel RAS coordinates — no EPI
    # intensities, so robust and orthogonal to NMI. RECORDED-ONLY: these gated in
    # schema 2.3 and that was wrong (see the module docstring), so nothing below
    # reads them for the verdict.
    rigid = rigid_metrics_from_lta(out_lta, t1w_mask_img, brain_mask)
    log.info(
        f"Rigid transform: disp mean {rigid['rigid_disp_mean_mm']:.2f} mm, "
        f"max {rigid['rigid_disp_max_mm']:.2f} mm, rotation {rigid['rigid_rot_deg']:.2f} deg"
    )

    # Boundary agreement (schema 2.2, recorded-only): modified Hausdorff distance
    # between the T1w brain mask and an Otsu skull-strip of the warped BOLD, both
    # on the conformed grid. Soft EPI edges cap its accuracy — see bold_brain_mask.
    bold_mask_arr = bold_brain_mask(warped_data)
    spacing = tuple(float(z) for z in warped_img.header.get_zooms()[:3])
    mhd = modified_hausdorff_distance(brain_mask, bold_mask_arr, voxel_spacing=spacing)
    log.info(f"Modified Hausdorff distance (T1w vs EPI brain surface): {mhd:.2f} mm")

    # Edge and segmentation alignment (schema >=2.5, recorded-only). Both address
    # the same structural limitation of nmi: it pools every brain voxel into one
    # joint histogram, so the alignment-sensitive part (MI ~0.13 nats) is diluted
    # ~50x by the joint entropy (~7 nats), which is why the entire usable nmi
    # range here is ~1.011-1.020. These two evaluate only where the alignment
    # information actually lives — tissue boundaries.
    #
    # Each is paired with its own identity baseline for the same reason nmi_gain
    # exists: absolute cross-modal similarity is not portable across session,
    # site or protocol, so only the gain over "no registration at all" is
    # comparable between runs. identity_data is already in memory from the
    # nmi_identity computation above, so each baseline costs one extra
    # evaluation and no extra resampling.
    #
    # aseg.auto.mgz is optional: it ships in the templated FastSurfer tarball
    # this step already unpacks, and is on the same conformed grid as T1.mgz, so
    # no resampling is needed. If it is absent the segmentation metrics take
    # their -999.0 sentinels rather than failing the run — this is recorded-only
    # QC and must never be able to discard a registration.
    seg = {'seg_bbr_contrast': -999.0}
    seg_identity = {'seg_bbr_contrast': -999.0}
    aseg_path = subjects_dir / subject / "mri" / "aseg.auto.mgz"
    if aseg_path.exists():
        aseg_data = nib.load(str(aseg_path)).get_fdata(dtype=np.float32)
        seg = segmentation_alignment(warped_data, aseg_data)
        seg_identity = segmentation_alignment(identity_data, aseg_data)
        log.info(
            f"Segmentation alignment: bbr_contrast {seg['seg_bbr_contrast']:.4f} "
            f"(identity {seg_identity['seg_bbr_contrast']:.4f})"
        )
    else:
        log.warning(f"aseg not found ({aseg_path}); segmentation metrics unavailable")

    ngf = normalized_gradient_field(warped_data, t1w_data, mask=brain_mask)
    ngf_identity = normalized_gradient_field(identity_data, t1w_data, mask=brain_mask)
    log.info(f"NGF (edge alignment): {ngf:.4f} (identity {ngf_identity:.4f})")

    # QC gate (schema 2.3): only the transform-only magnitude metrics gate — they
    # cannot be fooled the way nmi can (2655eea) or blunted by the soft EPI
    # skull-strip that limits mhd_mm. Write the record (with the verdict) FIRST so
    # it always uploads, then exit non-zero on a fail: the driver discards this
    # run's outputs on any non-zero exit, so no completion marker reaches S3 and
    # the expensive functional-preprocessing step never runs on a broken
    # registration. SynthMorph is deterministic, so a resubmit would re-register
    # to the identical fail — the gate drops the run, it does not enable a retry.
    # Build the measured record ONCE and hand the whole thing to verdict(), which
    # selects what gates from _BOLD_T1W_THRESHOLDS alone. Pre-filtering this down
    # to the gated keys would make the gate a two-place edit — table plus filter —
    # and a key present in one but not the other fails open silently.
    measured = {
        **rigid,
        'nmi': nmi_val,
        'mhd_mm': mhd,
        'nmi_identity': nmi_identity,
        'nmi_gain': nmi_gain,
        'seg_bbr_contrast': seg['seg_bbr_contrast'],
        'seg_bbr_contrast_identity': seg_identity['seg_bbr_contrast'],
        'ngf': ngf,
        'ngf_identity': ngf_identity,
    }
    reg_verdict = verdict(measured, _BOLD_T1W_THRESHOLDS)
    log.info(f"QC verdict (bold_to_t1w): {reg_verdict}")
    _write_qc(args, {**measured, 'verdict': reg_verdict})

    if reg_verdict == 'fail':
        log.error(
            f"QC verdict: FAIL — registration scored {nmi_gain:+.5f} NMI against the "
            f"identity baseline ({nmi_identity:.4f} → {nmi_val:.4f}): it is no better "
            "than applying no registration at all, so the transform is degenerate "
            "rather than merely poor. Exiting 65; the driver will discard this run's "
            "outputs. Note this is a sanity floor, NOT a quality bound — a run that "
            "passes it is not thereby certified well-registered; consult the recorded "
            "metrics in Athena for that."
        )
        sys.exit(65)

    log.info("=== bold_to_t1w complete ===")
    log.info(f"  Reference    : {ref}")
    log.info(f"  LTA          : {out_lta}")
    log.info(f"  ITK transform: {itk_mat}")
    log.info(f"  Warped ref   : {warped}  (QC)")
    log.info(f"  Brain mask   : {bold_mask}")


if __name__ == "__main__":
    main()
