"""Export a session's surface geometry as GIFTI, once per session.

Stage 2 (fsLR resampling) needs three things from FastSurfer per hemisphere:
the spherical registration it resamples along, and white/pial to derive the
midthickness that ADAP_BARY_AREA uses for area correction. Those live inside
the ~2 GB per-session FastSurfer tarball.

Rather than have Stage 2 download that tarball — which would undo the point of
splitting the stages, since its whole advantage is that its inputs are small —
the func-preproc pod already has the tree extracted and exports just these
surfaces. They are per *session*, not per run, so this runs once and writes to
a sibling prefix of the per-run components.

Written with nibabel rather than `mris_convert` so it needs no FreeSurfer in
the AFNI image; nibabel reads FreeSurfer geometry and writes GIFTI directly.

Usage:
    python surf_geometry.py --surf-dir <fastsurfer>/surf --outdir <dir> \
        --subj sub-X --session ses-00A
"""

from __future__ import annotations

import argparse
from pathlib import Path

import nibabel as nib
import numpy as np

# GIFTI intents for a surface: one coordinate array plus one triangle array.
POINTSET = "NIFTI_INTENT_POINTSET"
TRIANGLE = "NIFTI_INTENT_TRIANGLE"

HEMIS = (("L", "lh", "CortexLeft"), ("R", "rh", "CortexRight"))


def _write_surface(coords: np.ndarray, faces: np.ndarray, structure: str, out: Path) -> None:
    """Write a GIFTI surface tagged with its anatomical structure.

    Without AnatomicalStructurePrimary, wb_command refuses the surface — it
    cannot tell which hemisphere the mesh belongs to.
    """
    gii = nib.gifti.GiftiImage(
        meta=nib.gifti.GiftiMetaData({"AnatomicalStructurePrimary": structure}),
        darrays=[
            nib.gifti.GiftiDataArray(
                data=coords.astype(np.float32),
                intent=POINTSET,
                datatype="NIFTI_TYPE_FLOAT32",
                encoding="GZipBase64Binary",
            ),
            nib.gifti.GiftiDataArray(
                data=faces.astype(np.int32),
                intent=TRIANGLE,
                datatype="NIFTI_TYPE_INT32",
                encoding="GZipBase64Binary",
            ),
        ],
    )
    nib.save(gii, out)
    print(f"  wrote {out.name}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--surf-dir", required=True, type=Path)
    p.add_argument("--outdir", required=True, type=Path)
    p.add_argument("--subj", required=True)
    p.add_argument("--session", required=True)
    args = p.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)
    stem = f"{args.subj}_{args.session}"

    for hemi, fs_hemi, structure in HEMIS:
        white, faces = nib.freesurfer.read_geometry(str(args.surf_dir / f"{fs_hemi}.white"))
        pial, _ = nib.freesurfer.read_geometry(str(args.surf_dir / f"{fs_hemi}.pial"))
        if white.shape != pial.shape:
            raise ValueError(
                f"{fs_hemi}: white/pial vertex counts differ ({white.shape[0]} vs {pial.shape[0]})"
            )

        # Midthickness is not emitted by FastSurfer; it is the standard
        # white/pial midpoint, and is what area correction is measured on.
        _write_surface(
            (white + pial) / 2.0,
            faces,
            structure,
            args.outdir / f"{stem}_hemi-{hemi}_midthickness.surf.gii",
        )

        sphere, sph_faces = nib.freesurfer.read_geometry(
            str(args.surf_dir / f"{fs_hemi}.sphere.reg")
        )
        if sphere.shape[0] != white.shape[0]:
            raise ValueError(
                f"{fs_hemi}: sphere.reg has {sphere.shape[0]} vertices but "
                f"white has {white.shape[0]} — surfaces are not in register"
            )
        _write_surface(
            sphere,
            sph_faces,
            structure,
            args.outdir / f"{stem}_hemi-{hemi}_sphere.reg.surf.gii",
        )


if __name__ == "__main__":
    main()
