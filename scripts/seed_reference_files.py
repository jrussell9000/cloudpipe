#!/usr/bin/env python3
"""Put the public reference files the workflows read into a deployment's data bucket.

A new data bucket has no `config/` objects, and the workflows stage three kinds
of reference data from there (openspec/changes/publish-images-for-deployers, task
8.2). This script rebuilds each from its upstream source, never from another
deployment's bucket, and checks every result against a pinned digest before it
uploads anything:

| Object | Upstream | Transform |
|---|---|---|
| `config/MNI152NLin2009cAsym_res-01_T1w_brain.nii` | TemplateFlow `res-01_desc-brain_T1w` | decompressed |
| `config/MNI152NLin2009cAsym_T1w_brain_res-2_RAI.nii` | TemplateFlow `res-02_desc-brain_T1w` | decompressed, reoriented to LPS (AFNI's "RAI") |
| `config/fsLR/` (6 files) | HCPpipelines v6.0.0 `standard_mesh_atlases` | none |

The pinned digests are of what the reference deployment runs on. Its two MNI files
have the same voxels and affine as the transformed TemplateFlow files, and its six
fsLR files are byte-identical to HCPpipelines v6.0.0 (checked 2026-10-12). So a
deployment seeded here registers to exactly the same template.

It also writes `config/reference-sources.json`: each object, its source URL, the
checksums, and the license it is distributed under.

Not handled here, on purpose: `config/fslicense` (your own FreeSurfer license,
issued per person) and `config/nss_volumes.csv` (derived from your own ABCD data;
see `scripts/make_nss_volumes.py`).

    pixi run python scripts/seed_reference_files.py --bucket <data bucket> --dry-run
    pixi run python scripts/seed_reference_files.py --bucket <data bucket>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

TEMPLATEFLOW = "https://templateflow.s3.amazonaws.com/tpl-MNI152NLin2009cAsym"
HCP = "https://raw.githubusercontent.com/Washington-University/HCPpipelines/v6.0.0/global/templates/standard_mesh_atlases"

MNI_LICENSE = {
    "name": "ICBM 2009c Nonlinear Asymmetric template (Fonov et al.), MNI license",
    "url": f"{TEMPLATEFLOW}/LICENSE",
}
HCP_LICENSE = {
    "name": "HCPpipelines license",
    "url": "https://github.com/Washington-University/HCPpipelines/blob/v6.0.0/LICENSE.md",
}


@dataclass(frozen=True)
class Source:
    key: str
    url: str
    download_sha256: str
    license: dict[str, str]
    # NIfTI only: the voxel data and affine the result must have, after `reorient`.
    data_sha256: str | None = None
    affine: tuple[tuple[float, ...], ...] | None = None
    reorient: str | None = None  # target axis codes, e.g. "LPS"


SOURCES = (
    Source(
        key="config/MNI152NLin2009cAsym_res-01_T1w_brain.nii",
        url=f"{TEMPLATEFLOW}/tpl-MNI152NLin2009cAsym_res-01_desc-brain_T1w.nii.gz",
        download_sha256="97cee2cf7f51388d76d7b3632c8375d688d0fa171e1bede81fcef23726bb3d73",
        license=MNI_LICENSE,
        data_sha256="fd8b821f2128fdecb53660d31792340d891504ae6481802c6e7f573198e97456",
        affine=(
            (1.0, 0.0, 0.0, -96.0),
            (0.0, 1.0, 0.0, -132.0),
            (0.0, 0.0, 1.0, -78.0),
            (0.0, 0.0, 0.0, 1.0),
        ),
    ),
    Source(
        key="config/MNI152NLin2009cAsym_T1w_brain_res-2_RAI.nii",
        url=f"{TEMPLATEFLOW}/tpl-MNI152NLin2009cAsym_res-02_desc-brain_T1w.nii.gz",
        download_sha256="7b168153a9508f5f116ac37d33835fa44dd168ba7a9ae31a3b33e6f63bd23292",
        license=MNI_LICENSE,
        data_sha256="198c409fe69d0ff0a585cc00a442b747973c764d0e780f06d19027bde84203f6",
        affine=(
            (-2.0, 0.0, 0.0, 95.5),
            (0.0, -2.0, 0.0, 95.5),
            (0.0, 0.0, 2.0, -78.5),
            (0.0, 0.0, 0.0, 1.0),
        ),
        reorient="LPS",
    ),
    *(
        Source(
            key=f"config/fsLR/{name}",
            url=f"{HCP}/{path}{name}",
            download_sha256=digest,
            license=HCP_LICENSE,
        )
        for path, name, digest in (
            (
                "",
                "L.atlasroi.32k_fs_LR.shape.gii",
                "4ac9199dab151ccdc2a35bdddb5bac4f4907da7dfebd90807c09b82e2eb9d512",
            ),
            (
                "",
                "R.atlasroi.32k_fs_LR.shape.gii",
                "698f46b399f5a89829f83cc697e32dc8841031fbf31ffef75aaaa0c4a16c3015",
            ),
            (
                "resample_fsaverage/",
                "fs_LR-deformed_to-fsaverage.L.sphere.32k_fs_LR.surf.gii",
                "31987d139b6cdf1040188d1795bf23c213c57f8e84fb45f2c04823d996b0defc",
            ),
            (
                "resample_fsaverage/",
                "fs_LR-deformed_to-fsaverage.R.sphere.32k_fs_LR.surf.gii",
                "8c1f67fa8fb3024ad3b292be995d5c9bae07dc4dbdd53fbac48e511e1366553d",
            ),
            (
                "resample_fsaverage/",
                "fs_LR.L.midthickness_va_avg.32k_fs_LR.shape.gii",
                "e06c0f3e6069f9244e4dd74a4265323be3324a96c0f2bf9edd5ad9734d170f6d",
            ),
            (
                "resample_fsaverage/",
                "fs_LR.R.midthickness_va_avg.32k_fs_LR.shape.gii",
                "cb4b7680b8637c81398a99d4a034ee1680510ad6528d6948b4aa9c67b3e81867",
            ),
        )
    ),
)

MANIFEST_KEY = "config/reference-sources.json"


class Mismatch(Exception):
    """A download or a result is not the one pinned here. Nothing is uploaded."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as reply:  # noqa: S310 (pinned https URLs)
        return reply.read()


def build_nifti(source: Source, downloaded: bytes, workdir: Path) -> bytes:
    """Decompress, reorient if asked, and verify the voxels and affine. Returns the .nii bytes."""
    import nibabel as nib
    import numpy as np

    compressed = workdir / "in.nii.gz"
    compressed.write_bytes(downloaded)
    img = nib.load(compressed)
    if source.reorient:
        target = nib.orientations.axcodes2ornt(tuple(source.reorient))
        img = img.as_reoriented(
            nib.orientations.ornt_transform(nib.io_orientation(img.affine), target)
        )
    data = np.ascontiguousarray(np.asanyarray(img.dataobj))
    if sha256(data.tobytes()) != source.data_sha256:
        raise Mismatch(f"{source.key}: voxel data differs from the pinned template")
    if not np.allclose(img.affine, np.array(source.affine), atol=1e-6):
        raise Mismatch(f"{source.key}: affine differs from the pinned template")
    out = workdir / "out.nii"
    nib.save(nib.Nifti1Image(data, img.affine, img.header), out)
    return out.read_bytes()


def build(source: Source, downloaded: bytes, workdir: Path) -> bytes:
    if sha256(downloaded) != source.download_sha256:
        raise Mismatch(
            f"{source.url} has changed upstream: sha256 {sha256(downloaded)}, pinned {source.download_sha256}"
        )
    if source.data_sha256 is None:
        return downloaded
    return build_nifti(source, downloaded, workdir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", required=True, help="the deployment's data bucket")
    parser.add_argument(
        "--dry-run", action="store_true", help="download and verify; upload nothing"
    )
    args = parser.parse_args(argv)

    built: dict[str, bytes] = {}
    manifest = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, source in enumerate(SOURCES):
            work = Path(tmp) / str(i)
            work.mkdir()
            try:
                downloaded = fetch(source.url)
                built[source.key] = build(source, downloaded, work)
            except Mismatch as error:
                print(f"error: {error}. Nothing was uploaded.", file=sys.stderr)
                return 1
            manifest.append(
                {
                    "key": source.key,
                    "source": source.url,
                    "source_sha256": source.download_sha256,
                    "object_sha256": sha256(built[source.key]),
                    "license": source.license,
                }
            )
            print(f"verified {source.key} ({len(built[source.key])} bytes)")

    body = json.dumps({"objects": manifest}, indent=2).encode()
    if args.dry_run:
        print(
            f"dry run: would upload {len(built)} objects and {MANIFEST_KEY} to s3://{args.bucket}/"
        )
        return 0

    import boto3

    s3 = boto3.client("s3")
    for key, data in built.items():
        s3.put_object(Bucket=args.bucket, Key=key, Body=data, Metadata={"sha256": sha256(data)})
        print(f"uploaded s3://{args.bucket}/{key}")
    s3.put_object(Bucket=args.bucket, Key=MANIFEST_KEY, Body=body, ContentType="application/json")
    print(f"uploaded s3://{args.bucket}/{MANIFEST_KEY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
