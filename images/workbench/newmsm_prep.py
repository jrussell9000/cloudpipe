#!/usr/bin/env python3
"""The prep and publish halves of the `newmsm` step (openspec add-newmsm-surface-registration).

The step is one Argo containerSet pod (design D7):

    prep (this script, workbench image)
      -> register-l || register-r (newmsm, in the distroless cloudpipe/newmsm image)
        -> main = publish (this script, workbench image)

The newMSM image has no shell and no Python, so everything around the registration
lives here: turning the FastSurfer long-template's surfaces into the inputs smriprep
0.19.2 hands to MSM, and publishing the result as an exploded derivative tree.

    prep     surf/{lh,rh}.{sphere,sphere.reg,sulc} -> per hemisphere, in the workspace:
             {H}.msm_inmesh.surf.gii, {H}.msm_indata.shape.gii, {H}.msm_affine.mat,
             plus newmsm.conf (HCP's MSMSulcStrainFinalconf + --numthreads) and prep.json
    publish  the workspace's newMSM spheres -> derivatives/newmsm/{subj}/, with
             provenance.json, and `_complete.json` written last by fs_derivatives.publish

The chain is smriprep's (design D2), rebuilt on nibabel and wb_command so the pod
needs neither FreeSurfer nor nipype:

    1. sphere, sphere.reg -> GIFTI in tkr coordinates (MRIsConvert(to_scanner=False)
       + NormalizeSurf); sulc -> a shape GIFTI (MRIsConvertData)
    2. wb_command -surface-sphere-project-unproject   sphere.reg -> fsLR
    3. wb_command -surface-affine-regression           sphere -> that
       wb_command -surface-apply-affine
       wb_command -surface-modify-sphere 100
    4. sulc * -1, AnatomicalStructurePrimary set      (MetricMath(invert))

"Bit-identical" in checks (a) and (b) means identical DATA ARRAYS (coordinates,
triangles, values), as in the probe's harness and verify_fix.py. File bytes never
match across runs: wb_command and mris_convert stamp provenance metadata (command
line, working directory, date) into what they write.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import nibabel as nib
import numpy as np

HEMIS = {"L": ("lh", "CortexLeft"), "R": ("rh", "CortexRight")}

# The thread count the register containers run at. newMSM reads it from the CONFIG
# file, not the command line; the template's OMP_NUM_THREADS must match.
THREADS = 4

CONFIG_NAME = "MSMSulcStrainFinalconf"

# smriprep 0.19.2's copies (src/smriprep/data/{msm,atlases}), staged flat from the
# data bucket's config/newmsm/. A file that does not match is refused: a different
# atlas would register every subject to a different target, silently.
REFERENCE_SHA256 = {
    CONFIG_NAME: "d6a7e16123179d1b3de655dd7a103bcbfb2faffd1de561763f67ac404e89eb3d",
    "L.refsulc.164k_fs_LR.shape.gii": (
        "10f5e3fd775bb2f77278b0b69126f6f67abefb3c76de6f1233128d9914ddcb5c"
    ),
    "R.refsulc.164k_fs_LR.shape.gii": (
        "4eefc7807cdd9076cfc9ffc933da3d0c100a4770d5359c3e5f4fb37d71097520"
    ),
    "fsaverage.L_LR.spherical_std.164k_fs_LR.surf.gii": (
        "6968ef7d9638de54ade78cdbb7f0c6f25025c60477f72456ab2fb77c53a85cfa"
    ),
    "fsaverage.R_LR.spherical_std.164k_fs_LR.surf.gii": (
        "85e718e424a1d87521fb0f3942538db16d8c745fe5b9ee20405baac2aff1dbb1"
    ),
    "fsaverage.L.sphere.164k_fs_L.surf.gii": (
        "9bf5b673cc1c4738b1137ac53ff724ff0ef45e12f5550448efa2da50bb521c32"
    ),
    "fsaverage.R.sphere.164k_fs_R.surf.gii": (
        "a2fada23394286212f38cd44303bce0b3252759e09fe693f92d463286ca28b45"
    ),
    "fs_L-to-fs_LR_fsaverage.L_LR.spherical_std.164k_fs_L.surf.gii": (
        "a92b7c63eff6d04d8ce74b38e9f0060390b88d24b7c109bf13b194f0e8a54221"
    ),
    "fs_R-to-fs_LR_fsaverage.R_LR.spherical_std.164k_fs_R.surf.gii": (
        "d4b5808a75df1d9499f9f8339931fd385766ba625953823d662cb11f4b5e68cf"
    ),
}

# The FastSurfer long-template files the step stages (design D7). `white` is not
# read here; it is staged because smriprep's MRIsConvertData reads it beside sulc,
# and the D2 fallback needs it.
TEMPLATE_SURF_FILES = tuple(
    f"{fs}.{name}" for fs, _ in HEMIS.values() for name in ("sphere", "sphere.reg", "sulc", "white")
)

# Must equal src/inventory.py's NEWMSM_TEMPLATE_FIELD: the inventory reads it back to
# decide whether a registration is current (tests/images/workbench holds them together).
TEMPLATE_FIELD = "long_template_written_at"

# Sphere radius after -surface-modify-sphere, and what newMSM's output must keep.
SPHERE_RADIUS = 100.0


class PrepError(RuntimeError):
    """The step cannot proceed: a missing or wrong input, or an inconsistent mesh."""


def log(msg: str) -> None:
    print(f"[newmsm] {msg}", file=sys.stderr, flush=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def array_digest(*arrays: np.ndarray) -> str:
    """sha256 over the arrays' dtype, shape and bytes: what "bit-identical" compares."""
    h = hashlib.sha256()
    for a in arrays:
        a = np.ascontiguousarray(a)
        h.update(f"{a.dtype.str}{a.shape}".encode())
        h.update(a.tobytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# GIFTI I/O
# ---------------------------------------------------------------------------


def write_surface(coords: np.ndarray, faces: np.ndarray, structure: str, out: Path) -> None:
    """A spherical GIFTI surface in tkr coordinates, with no volume geometry.

    float32 coordinates and int32 triangles, as mris_convert writes them. The
    metadata is what NormalizeSurf leaves on a sphere that matters downstream:
    wb_command refuses a surface without AnatomicalStructurePrimary.
    """
    pointset_meta = {
        "AnatomicalStructurePrimary": structure,
        "GeometricType": "Spherical",
    }
    img = nib.gifti.GiftiImage(
        meta=nib.gifti.GiftiMetaData({"AnatomicalStructurePrimary": structure}),
        darrays=[
            nib.gifti.GiftiDataArray(
                data=np.asarray(coords, dtype=np.float32),
                intent="NIFTI_INTENT_POINTSET",
                datatype="NIFTI_TYPE_FLOAT32",
                encoding="GZipBase64Binary",
                meta=nib.gifti.GiftiMetaData(pointset_meta),
            ),
            nib.gifti.GiftiDataArray(
                data=np.asarray(faces, dtype=np.int32),
                intent="NIFTI_INTENT_TRIANGLE",
                datatype="NIFTI_TYPE_INT32",
                encoding="GZipBase64Binary",
            ),
        ],
    )
    nib.save(img, out)


def write_metric(values: np.ndarray, structure: str, name: str, out: Path) -> None:
    """A float32 shape GIFTI, as mris_convert -c writes and MetricMath re-saves it."""
    img = nib.gifti.GiftiImage(
        meta=nib.gifti.GiftiMetaData({"AnatomicalStructurePrimary": structure}),
        darrays=[
            nib.gifti.GiftiDataArray(
                data=np.asarray(values, dtype=np.float32),
                intent="NIFTI_INTENT_SHAPE",
                datatype="NIFTI_TYPE_FLOAT32",
                encoding="GZipBase64Binary",
                meta=nib.gifti.GiftiMetaData({"Name": name}),
            )
        ],
    )
    nib.save(img, out)


def read_surface(path: Path) -> tuple[np.ndarray, np.ndarray]:
    img = nib.load(str(path))
    return (
        np.asarray(img.agg_data("NIFTI_INTENT_POINTSET")),
        np.asarray(img.agg_data("NIFTI_INTENT_TRIANGLE")),
    )


def read_metric(path: Path) -> np.ndarray:
    data = nib.load(str(path)).agg_data()
    if isinstance(data, tuple):
        data = data[0]
    return np.asarray(data).ravel()


# ---------------------------------------------------------------------------
# prep
# ---------------------------------------------------------------------------


def verify_reference(ref_dir: Path) -> dict[str, Path]:
    """Every reference file present and matching its pin, or PrepError naming each one."""
    problems, paths = [], {}
    for name, want in REFERENCE_SHA256.items():
        path = ref_dir / name
        if not path.is_file():
            problems.append(f"missing input {path}")
        elif (got := sha256(path)) != want:
            problems.append(f"{path}: sha256 {got}, expected {want}")
        paths[name] = path
    if problems:
        raise PrepError(
            "reference files under config/newmsm/ are missing or do not match smriprep "
            "0.19.2's: " + "; ".join(problems)
        )
    return paths


def with_numthreads(conf_text: str, threads: int) -> str:
    """newMSM reads its thread count from the CONFIG file, not the command line."""
    if "--numthreads" in conf_text:
        raise PrepError("config already sets --numthreads; refusing to guess which wins")
    return conf_text.rstrip("\n") + f"\n--numthreads={threads}\n"


def wb(*args: str, wb_command: str = "wb_command") -> None:
    cmd = [wb_command, *args]
    log(" ".join(cmd))
    subprocess.run(cmd, check=True)


def prep_hemi(surf_dir: Path, refs: dict[str, Path], ws: Path, hemi: str, wb_command: str) -> dict:
    """D2 steps 1-4 for one hemisphere; returns its prep.json record."""
    fs, structure = HEMIS[hemi]
    sphere_c, sphere_f = nib.freesurfer.read_geometry(str(surf_dir / f"{fs}.sphere"))
    reg_c, reg_f = nib.freesurfer.read_geometry(str(surf_dir / f"{fs}.sphere.reg"))
    sulc = nib.freesurfer.read_morph_data(str(surf_dir / f"{fs}.sulc"))
    n = len(sphere_c)
    if not np.array_equal(sphere_f, reg_f):
        raise PrepError(f"{fs}: sphere and sphere.reg have different triangles")
    if len(sulc) != n:
        raise PrepError(f"{fs}: sulc has {len(sulc)} values but the sphere {n} vertices")

    # Step 1: GIFTI, tkr coordinates, no volume geometry.
    sphere = ws / f"{hemi}.sphere.surf.gii"
    sphere_reg = ws / f"{hemi}.sphere_reg.surf.gii"
    write_surface(sphere_c, sphere_f, structure, sphere)
    write_surface(reg_c, reg_f, structure, sphere_reg)

    # Step 2: sphere.reg (fsaverage space) carried onto fsLR.
    sphere_reg_fslr = ws / f"{hemi}.sphere_reg_fsLR.surf.gii"
    wb(
        "-surface-sphere-project-unproject",
        str(sphere_reg),
        str(refs[f"fsaverage.{hemi}.sphere.164k_fs_{hemi}.surf.gii"]),
        str(refs[f"fs_{hemi}-to-fs_LR_fsaverage.{hemi}_LR.spherical_std.164k_fs_{hemi}.surf.gii"]),
        str(sphere_reg_fslr),
        wb_command=wb_command,
    )

    # Step 3: MSM's input mesh: the native sphere, affinely aligned to that, radius 100.
    affine = ws / f"{hemi}.msm_affine.mat"
    sphere_aff = ws / f"{hemi}.sphere_affine.surf.gii"
    inmesh = ws / f"{hemi}.msm_inmesh.surf.gii"
    wb(
        "-surface-affine-regression",
        str(sphere),
        str(sphere_reg_fslr),
        str(affine),
        wb_command=wb_command,
    )
    wb("-surface-apply-affine", str(sphere), str(affine), str(sphere_aff), wb_command=wb_command)
    wb(
        "-surface-modify-sphere",
        str(sphere_aff),
        str(int(SPHERE_RADIUS)),
        str(inmesh),
        wb_command=wb_command,
    )

    # Step 4: inverted sulc. MetricMath without a subject id names the map sub-XYZ_*.
    indata = ws / f"{hemi}.msm_indata.shape.gii"
    write_metric(-np.asarray(sulc, dtype=np.float32), structure, f"sub-XYZ_{hemi}_sulc", indata)

    in_c, in_f = read_surface(inmesh)
    if not np.array_equal(in_f, sphere_f):
        raise PrepError(f"{fs}: wb_command changed the mesh's triangles")
    return {
        "n_vertices": int(n),
        "n_faces": int(len(sphere_f)),
        "msm_inmesh": {"file": inmesh.name, "array_sha256": array_digest(in_c, in_f)},
        "msm_indata": {"file": indata.name, "array_sha256": array_digest(read_metric(indata))},
    }


def cmd_prep(args: argparse.Namespace) -> int:
    ws = Path(args.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    refs = verify_reference(Path(args.ref_dir))
    conf = ws / "newmsm.conf"
    conf.write_text(with_numthreads(refs[CONFIG_NAME].read_text(), args.threads))
    record = {
        "threads": args.threads,
        "config_sha256": sha256(conf),
        "reference_sha256": REFERENCE_SHA256,
        "hemis": {},
    }
    for hemi in HEMIS:
        record["hemis"][hemi] = prep_hemi(Path(args.surf_dir), refs, ws, hemi, args.wb_command)
        log(f"prep {hemi}: {record['hemis'][hemi]['n_vertices']} vertices")
    (ws / "prep.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return 0


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


def tree_name(subj: str, hemi: str, kind: str) -> str:
    return {
        "sphere": f"{subj}_hemi-{hemi}_space-fsLR_desc-newmsm_sphere.surf.gii",
        "affine": f"{subj}_hemi-{hemi}_desc-newmsm_affine.mat",
    }[kind]


def check_registered(ws: Path, hemi: str) -> Path:
    """newMSM's output for `hemi`, checked against the mesh it was given.

    The registration moves vertices on the sphere; it must not change the mesh.
    Anything else is a sphere surface-resample's guard would refuse later, per
    session, so refuse it once here instead.
    """
    out = ws / f"{hemi}.sphere.reg.surf.gii"
    if not out.is_file():
        raise PrepError(f"newMSM wrote no {out.name}")
    reg_c, reg_f = read_surface(out)
    in_c, in_f = read_surface(ws / f"{hemi}.msm_inmesh.surf.gii")
    if len(reg_c) != len(in_c) or not np.array_equal(reg_f, in_f):
        raise PrepError(f"{out.name}: mesh differs from the input mesh")
    radius = np.linalg.norm(reg_c.astype(np.float64), axis=1)
    if not np.allclose(radius, SPHERE_RADIUS, rtol=1e-3):
        raise PrepError(f"{out.name}: not a radius-{SPHERE_RADIUS:g} sphere")
    return out


def input_etags(s3, bucket: str, subj: str) -> dict[str, str]:
    """The S3 ETag of every long-template file the step staged, by key."""
    keys = [f"derivatives/fastsurfer/{subj}/long-template/surf/{f}" for f in TEMPLATE_SURF_FILES]
    keys.append(f"derivatives/fastsurfer/{subj}/long-template/_complete.json")
    return {k: s3.head_object(Bucket=bucket, Key=k)["ETag"].strip('"') for k in keys}


def build_tree(ws: Path, tree: Path, subj: str, provenance: dict) -> None:
    tree.mkdir(parents=True, exist_ok=True)
    for hemi in HEMIS:
        (tree / tree_name(subj, hemi, "sphere")).write_bytes(
            check_registered(ws, hemi).read_bytes()
        )
        (tree / tree_name(subj, hemi, "affine")).write_bytes(
            (ws / f"{hemi}.msm_affine.mat").read_bytes()
        )
    (tree / "provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")


def cpu_model(cpuinfo: Path = Path("/proc/cpuinfo")) -> str | None:
    """The node's CPU model. Every container of the pod shares the node, so this is the
    CPU newMSM ran on. Recorded because newMSM's answer is reproducible only on like
    hardware: any last-bit difference moves it ~0.7 mm to an equally good registration
    (design D2), so a re-run on another CPU may differ and this explains why."""
    try:
        for line in cpuinfo.read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def provenance_record(ws: Path, template_marker: dict, newmsm_image: str, etags: dict) -> dict:
    written_at = template_marker.get("written_at")
    if not written_at:
        raise PrepError("the long-template _complete.json has no written_at")
    prep = json.loads((ws / "prep.json").read_text())
    return {
        "schema_version": 1,
        TEMPLATE_FIELD: written_at,
        # The digest fixes the binary; the build that produced it records the fork
        # commit (design D7c). The binary lives in the distroless container, which
        # this one cannot read.
        "newmsm_image": newmsm_image,
        "threads": prep["threads"],
        "cpu_model": cpu_model(),
        "config_sha256": prep["config_sha256"],
        "reference_sha256": prep["reference_sha256"],
        "input_etags": etags,
        "hemis": {hemi: dict(prep["hemis"][hemi]) for hemi in HEMIS},
    }


def cmd_publish(args: argparse.Namespace) -> int:
    import boto3
    import fs_derivatives
    import step_outcome

    if "@sha256:" not in args.newmsm_image:
        raise PrepError(f"--newmsm-image must be pinned by digest, got {args.newmsm_image!r}")
    ws = Path(args.workspace)
    subj = args.subject
    template_marker = json.loads(Path(args.template_marker).read_text())
    s3 = boto3.client("s3")
    provenance = provenance_record(
        ws, template_marker, args.newmsm_image, input_etags(s3, args.bucket, subj)
    )
    for hemi in HEMIS:
        reg_c, reg_f = read_surface(check_registered(ws, hemi))
        provenance["hemis"][hemi]["sphere"] = {
            "file": tree_name(subj, hemi, "sphere"),
            "array_sha256": array_digest(reg_c, reg_f),
        }

    tree = ws / "tree"
    build_tree(ws, tree, subj, provenance)
    fs_derivatives.publish(
        tree, args.bucket, f"derivatives/newmsm/{subj}", source_step="newmsm", s3=s3
    )
    # The template uploads this directory as an optional artifact; nothing else
    # creates it, and write_outcome does not.
    outcome_dir = Path(args.outcome_dir)
    outcome_dir.mkdir(parents=True, exist_ok=True)
    step_outcome.write_outcome(
        outcome_dir,
        workflow_name=args.workflow_name,
        workflow_uid=args.workflow_uid,
        subject=subj,
        session="na",
        step="newmsm",
        task="na",
        run="na",
        status="succeeded",
    )
    log(f"published derivatives/newmsm/{subj}/")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("prep", help="FastSurfer surf/ -> newMSM inputs and config")
    sp.add_argument("--surf-dir", required=True, help="the staged long-template surf/")
    sp.add_argument("--ref-dir", required=True, help="the staged config/newmsm/ files")
    sp.add_argument("--workspace", required=True)
    sp.add_argument("--threads", type=int, default=THREADS)
    sp.add_argument("--wb-command", default=os.environ.get("WB_COMMAND", "wb_command"))
    sp.set_defaults(func=cmd_prep)

    pp = sub.add_parser("publish", help="newMSM spheres -> derivatives/newmsm/{subj}/")
    pp.add_argument("--subject", required=True)
    pp.add_argument("--bucket", required=True)
    pp.add_argument("--workspace", required=True)
    pp.add_argument(
        "--template-marker", required=True, help="the staged long-template _complete.json"
    )
    pp.add_argument(
        "--newmsm-image", required=True, help="the register containers' image, by digest"
    )
    pp.add_argument("--outcome-dir", required=True)
    pp.add_argument("--workflow-name", required=True)
    pp.add_argument("--workflow-uid", default="")
    pp.set_defaults(func=cmd_publish)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except PrepError as e:
        log(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
