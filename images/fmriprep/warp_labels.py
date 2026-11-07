"""Warp each arm's DKT+aseg segmentation into MNI152NLin2009cAsym with its own transform.

The registration endpoint of the cloudpipe / fMRIPrep equivalence benchmark
(openspec/changes/add-fmriprep-equivalence-benchmark, task 5.9, design D17/D19):
each pipeline's own segmentation, warped by its own T1w→MNI transform, is later
scored against the template as segmented by its own engine. This script does the
warping only; the Dice is `src/equivalence/registration.py`, on the workstation.

Both arms are warped by the same program with the same settings, onto the same
reference: ANTs `antsApplyTransforms` (the one this image ships for fMRIPrep),
`GenericLabel` interpolation, onto TemplateFlow's `res-01` T1w grid, which is the
grid both registrations were estimated on (cloudpipe's
`config/MNI152NLin2009cAsym_res-01_T1w_brain.nii` has the same header).

- cloudpipe: `aparc.DKTatlas+aseg.mapped.mgz` from the session's FastSurfer tree,
  in that tree's `orig.mgz` space, the fireANTs moving image; transforms
  `_desc-t1w2mni_warp.nii.gz` then `_desc-t1w2mni_affine.mat` (ANTs order:
  listed last-applied first).
- fMRIPrep: `aparc.DKTatlas+aseg.mgz` from its own FreeSurfer tree, in fsnative;
  transforms `from-T1w_to-MNI152NLin2009cAsym_mode-image_xfm.h5` then
  `from-fsnative_to-T1w_mode-image_xfm.txt`.

Standard library plus nibabel (both in this image). Exit 0 only when both maps
are written; `_labels_complete.json`, written last, is the existence test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REFERENCE = "tpl-MNI152NLin2009cAsym/tpl-MNI152NLin2009cAsym_res-01_T1w.nii.gz"
OUTPUT = "{prefix}_arm-{arm}_space-MNI152NLin2009cAsym_res-1_desc-DKTaseg_dseg.nii.gz"
COMPLETE_MARKER = "_labels_complete.json"


def reference_image() -> Path:
    """The baked TemplateFlow res-01 T1w (images/fmriprep/Dockerfile)."""
    path = Path(os.environ["TEMPLATEFLOW_HOME"]) / REFERENCE
    if not path.is_file():
        raise SystemExit(f"{path} is not in the image's TemplateFlow home")
    return path


def to_nifti(src: Path, dest: Path) -> Path:
    """An integer NIfTI of a label volume, header affine kept (MGZ is not ITK-readable here)."""
    import nibabel as nib
    import numpy as np

    img = nib.load(str(src))
    data = np.asarray(img.dataobj).astype(np.int32)
    nib.save(nib.Nifti1Image(data, img.affine), str(dest))
    return dest


def apply_command(labels: Path, reference: Path, out: Path, transforms: list[Path]) -> list[str]:
    cmd = [
        "antsApplyTransforms",
        "-d",
        "3",
        "-i",
        str(labels),
        "-r",
        str(reference),
        "-o",
        str(out),
        "-n",
        "GenericLabel",
        "-u",
        "int",
        "--float",
        "0",
    ]
    for t in transforms:
        cmd += ["-t", str(t)]
    return cmd


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def arms(args) -> dict[str, tuple[Path, list[Path]]]:
    """arm -> (label volume, transforms in ANTs order)."""
    return {
        "cloudpipe": (args.cloudpipe_labels, [args.cloudpipe_warp, args.cloudpipe_affine]),
        "fmriprep": (
            args.fmriprep_labels,
            [args.fmriprep_t1w_to_mni, args.fmriprep_fsnative_to_t1w],
        ),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--prefix", required=True, help="sub-X_ses-Y")
    p.add_argument("--cloudpipe-labels", type=Path, required=True)
    p.add_argument("--cloudpipe-affine", type=Path, required=True)
    p.add_argument("--cloudpipe-warp", type=Path, required=True)
    p.add_argument("--fmriprep-labels", type=Path, required=True)
    p.add_argument("--fmriprep-fsnative-to-t1w", type=Path, required=True)
    p.add_argument("--fmriprep-t1w-to-mni", type=Path, required=True)
    p.add_argument("--reference", type=Path, help="defaults to the baked TemplateFlow res-01 T1w")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--scratch", type=Path, default=Path("/tmp/warp_labels"))
    args = p.parse_args(argv)

    reference = args.reference or reference_image()
    args.out.mkdir(parents=True, exist_ok=True)
    args.scratch.mkdir(parents=True, exist_ok=True)
    record = {"prefix": args.prefix, "reference": str(reference), "arms": {}}
    # Every input of both arms first: half a pair is no endpoint.
    for arm, (labels, transforms) in arms(args).items():
        for path in (labels, *transforms):
            if not path.is_file():
                raise SystemExit(f"{arm}: {path} is missing")
    for arm, (labels, transforms) in arms(args).items():
        nifti = to_nifti(labels, args.scratch / f"{arm}_labels.nii.gz")
        out = args.out / OUTPUT.format(prefix=args.prefix, arm=arm)
        cmd = apply_command(nifti, reference, out, transforms)
        print(" ".join(cmd), flush=True)
        subprocess.run(cmd, check=True)
        record["arms"][arm] = {
            "output": out.name,
            "command": cmd,
            "inputs": {p.name: sha256(p) for p in (labels, *transforms)},
        }
    version = subprocess.run(["antsApplyTransforms", "--version"], capture_output=True, text=True)
    record["ants_version"] = version.stdout.strip()
    (args.out / COMPLETE_MARKER).write_text(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
