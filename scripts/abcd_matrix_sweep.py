"""Re-test ABCD's registration_matrix_T1 against SynthMorph BOLD-to-T1w (ADR 012 re-test).

Enumerates candidate readings of the sidecar-shipped registration_matrix_T1 (direction,
RAS/LPS frame, conformed-vs-native T1w space) and scores each with the same NMI-gain
metric used for the SynthMorph reference, per the decision rule in
docs/investigations/2026-07-29-abcd-matrix-retest-handoff.md.

Usage:
    pixi run python scripts/abcd_matrix_sweep.py --runs-file runs.txt --out sweep_results.csv

runs.txt lines: <subject> <session> <task> <run>
Expects, per run, already downloaded locally under --data-root:
    <data-root>/<subject>/<session>/anat/<subject>_<session>_run-01_T1w.nii.gz
    <data-root>/<subject>/<session>/func/<subject>_<session>_task-<task>_run-<run>_bold.json
    <data-root>/<subject>/<session>/fastsurfer/mri/{T1.mgz,brainmask.mgz}
    <data-root>/<subject>/<session>/registration/bold_to_t1w_task-<task>_run-<run>/
        <subject>_<session>_task-<task>_run-<run>_desc-bold2t1w_ref.nii.gz
        <subject>_<session>_task-<task>_run-<run>_desc-bold2t1w.lta
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import nibabel as nib
import numpy as np
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "images" / "shared"))
from registration_qc import normalized_mutual_information  # noqa: E402

RAS_LPS_FLIP = np.diag([-1.0, -1.0, 1.0, 1.0])


def resample_with_matrix(
    moving_path: Path, T: np.ndarray, reference_path: Path, order: int = 1
) -> np.ndarray:
    """Resample moving_path onto reference_path's grid under RAS transform T
    (reference_RAS -> moving_RAS). Identical to the PR #89 helper of the same name
    in images/freesurfer/bold_to_t1w.py; duplicated here so this investigation
    script has no dependency on an unmerged branch.
    """
    mov_img = nib.load(str(moving_path))
    ref_img = nib.load(str(reference_path))
    mov_data = np.asarray(mov_img.dataobj)
    if mov_data.ndim == 4:
        mov_data = mov_data[..., 0]

    M = np.linalg.inv(mov_img.affine) @ T @ ref_img.affine

    shape = ref_img.shape[:3]
    i, j, k = np.mgrid[: shape[0], : shape[1], : shape[2]]
    vox = np.ones((4, i.size))
    vox[0], vox[1], vox[2] = i.ravel(), j.ravel(), k.ravel()

    mov_coords = (M @ vox)[:3]
    return ndimage.map_coordinates(
        mov_data.astype(np.float32), mov_coords, order=order, mode="constant", cval=0
    ).reshape(shape)


def parse_lta_matrix(lta_path: Path) -> np.ndarray:
    with open(lta_path) as f:
        lines = f.readlines()
    dim_pat = re.compile(r"^\s*\d+\s+4\s+4\s*$")
    for i, line in enumerate(lines):
        if dim_pat.match(line):
            return np.array([[float(x) for x in lines[i + j].split()] for j in range(1, 5)])
    data_lines = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    mat = np.array([[float(x) for x in ln.split()] for ln in data_lines])
    if mat.shape == (4, 4):
        return mat
    raise ValueError(f"Could not parse 4x4 matrix from {lta_path}")


@dataclass
class Candidate:
    name: str
    T: np.ndarray  # maps reference(T1.mgz)_RAS -> moving(BOLD)_RAS


def build_candidates(
    M_abcd: np.ndarray,
    A_t1c: np.ndarray,
    A_t1raw: np.ndarray,
) -> list[Candidate]:
    """Enumerate direction x frame x T1w-space-composition candidates.

    S1 (ras-direct): assume T1.mgz RAS == raw T1w RAS directly (no correction).
    S2 (vox2vox): voxel-to-voxel correspondence between the conformed and native
        T1w grids (ADR 012's "voxel-to-voxel" attempt), i.e. A_t1raw @ inv(A_t1c).
    F_bold / F_t1: optional RAS<->LPS flip on either side of M.
    """
    S_variants = {
        "ras-direct": np.eye(4),
        "vox2vox": A_t1raw @ np.linalg.inv(A_t1c),
    }
    candidates = []
    for dir_name, M_dir in [("fwd", M_abcd), ("inv", np.linalg.inv(M_abcd))]:
        for s_name, S in S_variants.items():
            for f_bold_name, F_bold in [("id", np.eye(4)), ("lps", RAS_LPS_FLIP)]:
                for f_t1_name, F_t1 in [("id", np.eye(4)), ("lps", RAS_LPS_FLIP)]:
                    T = F_bold @ M_dir @ F_t1 @ S
                    name = f"M-{dir_name}_{s_name}_boldflip-{f_bold_name}_t1flip-{f_t1_name}"
                    candidates.append(Candidate(name, T))
    return candidates


def score_run(run_dir: Path, subject: str, session: str, task: str, run: str) -> dict:
    reg_dir = run_dir / "registration" / f"bold_to_t1w_task-{task}_run-{run}"
    bold_ref = reg_dir / f"{subject}_{session}_task-{task}_run-{run}_desc-bold2t1w_ref.nii.gz"
    lta_path = reg_dir / f"{subject}_{session}_task-{task}_run-{run}_desc-bold2t1w.lta"
    sidecar = run_dir / "func" / f"{subject}_{session}_task-{task}_run-{run}_bold.json"
    t1_path = run_dir / "fastsurfer" / "mri" / "T1.mgz"
    brainmask_path = run_dir / "fastsurfer" / "mri" / "brainmask.mgz"
    t1raw_candidates = list((run_dir / "anat").glob(f"{subject}_{session}_run-*_T1w.nii.gz"))
    if not t1raw_candidates:
        raise FileNotFoundError(f"No raw T1w NIfTI found under {run_dir / 'anat'}")
    t1raw_path = t1raw_candidates[0]

    with open(sidecar) as f:
        meta = json.load(f)
    M_abcd = np.array(meta["registration_matrix_T1"])

    t1_img = nib.load(str(t1_path))
    t1_data = np.asarray(t1_img.dataobj)
    brainmask = np.asarray(nib.load(str(brainmask_path)).dataobj) > 0
    A_t1c = t1_img.affine
    A_t1raw = nib.load(str(t1raw_path)).affine

    identity_warped = resample_with_matrix(bold_ref, np.eye(4), t1_path, order=1)
    identity_nmi = normalized_mutual_information(identity_warped, t1_data, mask=brainmask)

    synth_T = parse_lta_matrix(lta_path)
    synth_warped = resample_with_matrix(bold_ref, synth_T, t1_path, order=1)
    synth_nmi = normalized_mutual_information(synth_warped, t1_data, mask=brainmask)

    results = {
        "subject": subject,
        "session": session,
        "task": task,
        "run": run,
        "identity_nmi": identity_nmi,
        "synthmorph_gain": synth_nmi - identity_nmi,
    }

    candidates = build_candidates(M_abcd, A_t1c, A_t1raw)
    for i, cand in enumerate(candidates, 1):
        try:
            warped = resample_with_matrix(bold_ref, cand.T, t1_path, order=1)
            nmi = normalized_mutual_information(warped, t1_data, mask=brainmask)
            results[f"gain__{cand.name}"] = nmi - identity_nmi
        except Exception as e:  # noqa: BLE001 - record failure, keep sweeping
            results[f"gain__{cand.name}"] = float("nan")
            print(
                f"  [{subject}/{session}/{task}_{run}] candidate {cand.name} failed: {e}",
                file=sys.stderr,
            )
        print(f"  ({i}/{len(candidates)}) {cand.name} done", flush=True)

    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-file", type=Path, required=True)
    ap.add_argument("--data-root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    rows = []
    with open(args.runs_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            subject, session, task, run = line.split()
            run_dir = args.data_root / subject / session
            print(f"Scoring {subject}/{session} task-{task} run-{run} ...", flush=True)
            rows.append(score_run(run_dir, subject, session, task, run))

    import csv

    fieldnames = sorted({k for row in rows for k in row})
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {args.out}")


if __name__ == "__main__":
    main()
