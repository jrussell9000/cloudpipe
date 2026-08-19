"""Resample one run's cortical components to fsLR 32k and assemble a CIFTI.

Stage 2 of surface functional processing. Consumes only what Stage 1 already
reduced — per-hemisphere fsnative timeseries, the subcortical volume block, and
the session's surface geometry — plus the static fsLR meshes. It never touches
the raw BOLD, the MNI BOLD, or the FastSurfer tarball, which is what makes it
cheap to run in a pod of its own.

Steps per run:
  1. -metric-resample  native fsnative -> fsLR 32k, along sphere.reg,
     ADAP_BARY_AREA with midthickness area correction
  2. -volume-label-import  turn Stage 1's integer labels + list into a
     Workbench label volume
  3. -cifti-create-dense-timeseries  cortex + subcortex into one dtseries

Mesh filenames are passed in rather than hardcoded: they come from the HCP
standard_mesh_atlases package staged in S3 config/, and pinning names here
would turn a staging change into a code change.

Note the subcortical block is on MNI152NLin2009cAsym, not the MNI152NLin6Asym
grid standard 91282-grayordinate files use, so the total grayordinate count
will not be 91282. That is intended — see the openspec change
`add-surface-func-processing` Decision 2a.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import nibabel as nib
import numpy as np

HEMIS = ("L", "R")


def run(cmd: list) -> None:
    printable = " ".join(str(c) for c in cmd)
    print(f">>> {printable}", flush=True)
    subprocess.run([str(c) for c in cmd], check=True)


def resample_hemi(
    metric_in: Path,
    current_sphere: Path,
    new_sphere: Path,
    current_midthickness: Path,
    target_area: Path,
    area_mode: str,
    work: Path,
    metric_out: Path,
) -> Path:
    """Native surface metric -> fsLR 32k.

    ADAP_BARY_AREA rather than BARYCENTRIC: it uses all source data when
    downsampling (~150k native vertices to 32k), where barycentric would
    sample only the enclosing triangle and discard most of it. wb_command
    requires exactly one area option with this method.

    Area mode matters because the two options take different file types, and
    the HCP standard_mesh_atlases pack distributes the fsLR target as vertex-
    area *metrics* (`midthickness_va_avg.shape.gii`), not as a midthickness
    surface. So `metrics` is the default and the subject side is converted to
    match; `surfs` is kept for a staging that does supply a 32k midthickness
    surface.
    """
    if area_mode == "metrics":
        current_area = work / f"{current_midthickness.stem}.va.shape.gii"
        run(["wb_command", "-surface-vertex-areas", current_midthickness, current_area])
        area_opt = ["-area-metrics", current_area, target_area]
    else:
        area_opt = ["-area-surfs", current_midthickness, target_area]

    run(
        [
            "wb_command",
            "-metric-resample",
            metric_in,
            current_sphere,
            new_sphere,
            "ADAP_BARY_AREA",
            metric_out,
            *area_opt,
        ]
    )
    return metric_out


def assemble(
    out: Path,
    left: Path,
    right: Path,
    subcort_bold: Path,
    subcort_labels_wb: Path,
    tr: float,
    roi_left: Path | None,
    roi_right: Path | None,
) -> Path:
    cmd = [
        "wb_command",
        "-cifti-create-dense-timeseries",
        out,
        "-volume",
        subcort_bold,
        subcort_labels_wb,
        "-left-metric",
        left,
    ]
    if roi_left is not None:
        cmd += ["-roi-left", roi_left]
    cmd += ["-right-metric", right]
    if roi_right is not None:
        cmd += ["-roi-right", roi_right]
    # Without -timestep the dtseries claims a 1 s TR and every downstream
    # frequency-domain analysis is silently wrong.
    cmd += ["-timestep", f"{tr:.6f}"]
    run(cmd)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--components",
        required=True,
        type=Path,
        help="Extracted Stage 1 components dir for this run",
    )
    p.add_argument(
        "--anat",
        required=True,
        type=Path,
        help="Session surface geometry dir (sphere.reg, midthickness)",
    )
    p.add_argument(
        "--meshes", required=True, type=Path, help="Static fsLR mesh dir staged from config/"
    )
    p.add_argument("--outdir", required=True, type=Path)
    p.add_argument("--subj", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--run", required=True)
    p.add_argument(
        "--target-sphere",
        required=True,
        help="Target sphere filename template with {hemi}. Must be "
        "the fs_LR-deformed_to-fsaverage variant: FreeSurfer's "
        "sphere.reg lands in fsaverage space, and a plain fsLR "
        "sphere is not in register with it.",
    )
    p.add_argument(
        "--target-area",
        required=True,
        help="Target area filename template with {hemi} — vertex-area "
        "metric (.shape.gii) or midthickness surface, per "
        "--area-mode",
    )
    p.add_argument(
        "--area-mode",
        default="metrics",
        choices=["metrics", "surfs"],
        help="Area-correction file type for ADAP_BARY_AREA. 'metrics' "
        "matches what standard_mesh_atlases distributes.",
    )
    p.add_argument(
        "--target-roi",
        default="",
        help="Optional atlas ROI filename template with {hemi}. "
        "Supplying it removes the medial wall, giving the "
        "standard 59412 cortical grayordinates.",
    )
    args = p.parse_args()

    prefix = f"{args.subj}_{args.session}_{args.task}_{args.run}"
    ses_stem = f"{args.subj}_{args.session}"
    args.outdir.mkdir(parents=True, exist_ok=True)
    work = args.outdir / "_work"
    work.mkdir(exist_ok=True)

    subcort_bold = args.components / f"{prefix}_space-MNI152NLin2009cAsym_desc-subcort_bold.nii.gz"
    subcort_labels = (
        args.components / f"{prefix}_space-MNI152NLin2009cAsym_desc-subcort_dseg.nii.gz"
    )
    label_list = args.components / f"{prefix}_desc-subcort_labellist.txt"
    for f in (subcort_bold, subcort_labels, label_list):
        if not f.exists():
            sys.exit(f"missing Stage 1 component: {f}")

    # TR rides in the subcortical block's NIfTI header (pixdim[4]), carried
    # through from the MNI BOLD, so no sidecar is needed here.
    hdr = nib.load(subcort_bold).header
    tr = float(hdr.get_zooms()[3])
    # A TR outside this range is almost always a units error, not a real
    # sequence: wb_command warns "non-time units code 0 ... pretending units
    # are seconds" when the header does not declare them, and a pixdim[4] of
    # 800 (milliseconds) would sail through a bare >0 check and silently make
    # every frequency-domain analysis downstream wrong by 1000x.
    if not np.isfinite(tr) or not (0.1 <= tr <= 10.0):
        sys.exit(
            f"{subcort_bold.name}: implausible TR {tr} s (pixdim[4]={tr}, "
            f"xyzt_units={hdr.get('xyzt_units')}). Expected 0.1-10 s — check "
            f"whether the source header declares milliseconds."
        )
    print(f"[assemble] {prefix}: TR={tr:.4f}s", flush=True)

    resampled: dict[str, Path] = {}
    rois: dict[str, Path | None] = {}
    for hemi in HEMIS:
        metric_in = args.components / f"{prefix}_hemi-{hemi}_space-fsnative_bold.func.gii"
        if not metric_in.exists():
            sys.exit(f"missing Stage 1 component: {metric_in}")
        target_area = args.meshes / args.target_area.format(hemi=hemi)
        if not target_area.exists():
            sys.exit(
                f"missing staged mesh: {target_area}. Check --target-area and "
                f"--area-mode against what is actually in config/fsLR/ — the "
                f"defaults follow the HCP standard_mesh_atlases naming."
            )
        resampled[hemi] = resample_hemi(
            metric_in=metric_in,
            current_sphere=args.anat / f"{ses_stem}_hemi-{hemi}_sphere.reg.surf.gii",
            new_sphere=args.meshes / args.target_sphere.format(hemi=hemi),
            current_midthickness=args.anat / f"{ses_stem}_hemi-{hemi}_midthickness.surf.gii",
            target_area=target_area,
            area_mode=args.area_mode,
            work=work,
            metric_out=work / f"{prefix}_hemi-{hemi}_space-fsLR32k_bold.func.gii",
        )
        # Existence-checked like target_area above: --target-roi is the other
        # staged-mesh path, and a typo in it otherwise surfaces as a wb_command
        # failure two calls later with no mention of which flag was wrong.
        # Empty (the default) still means "no ROI", not "a missing file".
        if args.target_roi:
            roi = args.meshes / args.target_roi.format(hemi=hemi)
            if not roi.exists():
                sys.exit(
                    f"missing staged mesh: {roi}. Check --target-roi against "
                    f"what is actually in config/fsLR/."
                )
            rois[hemi] = roi
        else:
            rois[hemi] = None

    labels_wb = work / f"{prefix}_desc-subcort_labels_wb.nii.gz"
    run(
        [
            "wb_command",
            "-volume-label-import",
            subcort_labels,
            label_list,
            labels_wb,
            "-discard-others",
        ]
    )

    dtseries = args.outdir / f"{prefix}_space-fsLR32k_bold.dtseries.nii"
    assemble(
        out=dtseries,
        left=resampled["L"],
        right=resampled["R"],
        subcort_bold=subcort_bold,
        subcort_labels_wb=labels_wb,
        tr=tr,
        roi_left=rois["L"],
        roi_right=rois["R"],
    )

    cifti = nib.load(dtseries)
    n_grayord = cifti.shape[1]
    n_frames = cifti.shape[0]
    print(
        f"[assemble] wrote {dtseries.name}: {n_grayord} grayordinates x {n_frames} frames",
        flush=True,
    )

    for f in work.iterdir():
        f.unlink()
    work.rmdir()


if __name__ == "__main__":
    main()
