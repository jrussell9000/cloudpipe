"""Bake the TemplateFlow files the benchmark's fMRIPrep needs into the image (design D10).

The cluster is private, and TemplateFlow fetches a missing file from its S3 bucket
the first time a workflow asks for it, so a file not baked here becomes a run that
fails hours in — after anatomy, at the step that first touches it. `--cifti-output`
makes that worse: it pulls its spaces in implicitly, so the command line does not
name every template it needs.

Upstream's image already carries `scripts/fetch_templates.py`'s selection. This adds
the rest of each template fMRIPrep reaches for under the benchmark's command line,
generously rather than file by file, because the cost of a missing file is a lost
pilot session and the cost of a spare one is disk:

    MNI152NLin2009cAsym  whole template            the output space
    MNI152NLin6Asym      res-1, res-2, untagged     the CIFTI subcortical grid
    OASIS30ANTs          whole template            skull-strip target
    fsLR, fsaverage      whole templates           grayordinates, MSM, aparc tables

MNI152NLin6Asym is the one trimmed: its DiFuMo probabilistic atlases (2.5 GB) and
the MNIInfant transforms (1.5 GB) are never read here.

TemplateFlow's home holds a skeleton of every file, each EMPTY until fetched, so a
zero-byte `tpl-*` file inside the scope is exactly a file a run would download.

    python bake_templates.py fetch     # fill every empty in-scope file from S3
    python bake_templates.py verify    # run under `RUN --network=none`

`verify` is the build's proof. It fails on any empty in-scope file, then asks
TemplateFlow itself, with no network, for each file fMRIPrep's code asks for
under this command line, so a file that only the API could reach (a wrong
TEMPLATEFLOW_HOME, a layout the skeleton does not index) fails the build rather
than the run. Standard library only, except `verify`'s TemplateFlow import.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

TF_S3_ROOT = "https://templateflow.s3.amazonaws.com"


def _everything(name: str) -> bool:
    return True


def _mni6(name: str) -> bool:
    if "atlas-DiFuMo" in name or "from-MNIInfant" in name:
        return False
    return "_res-" not in name or "_res-01_" in name or "_res-02_" in name


#: Template -> which of its `tpl-*` files are baked.
SCOPE: dict[str, Callable[[str], bool]] = {
    "MNI152NLin2009cAsym": _everything,
    "MNI152NLin6Asym": _mni6,
    "OASIS30ANTs": _everything,
    "fsLR": _everything,
    "fsaverage": _everything,
}

#: What fMRIPrep 25.2.5 (smriprep 0.19.2, niworkflows 1.14.4) asks TemplateFlow for
#: under the benchmark's command line: `--output-spaces MNI152NLin2009cAsym:res-2`,
#: `--cifti-output 91k`, MSM on, an existing FreeSurfer tree. Read from their
#: source; the pilot's run is what proves it complete.
QUERIES: list[tuple[str, dict]] = [
    # Skull-stripping (niworkflows.anat.ants, smriprep's default template).
    ("OASIS30ANTs", {"resolution": 1, "desc": None, "label": None, "suffix": "T1w"}),
    ("OASIS30ANTs", {"resolution": 1, "label": "brain", "suffix": "probseg"}),
    ("OASIS30ANTs", {"resolution": 1, "desc": "BrainCerebellumExtraction", "suffix": "mask"}),
    ("OASIS30ANTs", {"resolution": 1, "label": "WM", "suffix": "probseg"}),
    ("OASIS30ANTs", {"resolution": 1, "label": "BS", "suffix": "probseg"}),
    # The output space (smriprep TemplateFlowSelect, niworkflows norm and func.util).
    ("MNI152NLin2009cAsym", {"resolution": 2, "desc": None, "suffix": "T1w"}),
    ("MNI152NLin2009cAsym", {"resolution": 2, "desc": "brain", "suffix": "mask"}),
    ("MNI152NLin2009cAsym", {"resolution": 1, "label": "brain", "suffix": "probseg"}),
    ("MNI152NLin2009cAsym", {"resolution": 1, "desc": "carpet", "suffix": "dseg"}),
    ("MNI152NLin2009cAsym", {"resolution": 2, "desc": "fMRIPrep", "suffix": "boldref"}),
    # CIFTI 91k's subcortical grid (niworkflows.interfaces.cifti).
    ("MNI152NLin6Asym", {"resolution": 2, "desc": None, "suffix": "T1w"}),
    ("MNI152NLin6Asym", {"resolution": 2, "desc": "brain", "suffix": "mask"}),
    ("MNI152NLin6Asym", {"resolution": 2, "atlas": "HCP", "suffix": "dseg"}),
    # Cortical grayordinates (fmriprep bold.resampling, smriprep surfaces and cifti).
    ("fsLR", {"density": "32k", "suffix": "sphere", "extension": ".surf.gii"}),
    ("fsLR", {"density": "32k", "desc": "nomedialwall", "suffix": "dparc"}),
    ("fsaverage", {"density": "164k", "desc": "std", "suffix": "sphere"}),
    ("fsaverage", {"density": "164k", "suffix": "midthickness"}),
    ("fsaverage", {"density": "164k", "suffix": "sulc"}),
    # Written at the end of every run that has FreeSurfer anatomy (fmriprep.cli.run).
    (
        "fsaverage",
        {
            "hemi": None,
            "atlas": None,
            "segmentation": "aparc",
            "suffix": "dseg",
            "extension": [".tsv"],
        },
    ),
]


def tf_home() -> Path:
    home = os.environ.get("TEMPLATEFLOW_HOME")
    if not home:
        raise SystemExit("TEMPLATEFLOW_HOME is not set; refusing to guess where the image keeps it")
    return Path(home)


def missing(home: Path) -> list[Path]:
    """Every in-scope `tpl-*` file still at zero bytes, i.e. not yet fetched."""
    out = []
    for template, wanted in SCOPE.items():
        root = home / f"tpl-{template}"
        if not root.is_dir():
            raise SystemExit(
                f"{root}: no such template directory; the skeleton is not where expected"
            )
        out += sorted(
            p
            for p in root.rglob("tpl-*")
            if p.is_file() and wanted(p.name) and p.stat().st_size == 0
        )
    return out


#: Tries per file. A reset mid-fetch is ordinary over a few hundred downloads (one
#: hit the first local bake, 2026-10-26); a file still failing after these is not.
ATTEMPTS = 4


def _download(url: str, partial: Path, opener) -> tuple[int, int]:
    """One attempt: bytes written to `partial`, and the length the server declared."""
    with opener(url, timeout=120) as reply:
        expected = int(reply.headers.get("Content-Length", -1))
        with partial.open("wb") as out:
            while chunk := reply.read(1 << 20):
                out.write(chunk)
    return partial.stat().st_size, expected


def fetch_one(home: Path, path: Path, opener=urllib.request.urlopen, sleep=time.sleep) -> int:
    """Download one skeleton file from TemplateFlow's bucket, checking its length.

    Written to a `.part` file and moved into place only when whole, so an
    interrupted fetch leaves the skeleton's empty file, which `missing` still
    reports, rather than a truncated one it would not.
    """
    url = f"{TF_S3_ROOT}/{path.relative_to(home).as_posix()}"
    partial = path.with_name(path.name + ".part")
    problem = ""
    for attempt in range(ATTEMPTS):
        if attempt:
            sleep(2**attempt)
        try:
            got, expected = _download(url, partial, opener)
        except OSError as exc:  # URLError, a reset, a timeout
            problem = f"{type(exc).__name__}: {exc}"
            continue
        if got and (expected < 0 or got == expected):
            partial.replace(path)
            return got
        problem = f"read {got} bytes, expected {expected}"
    partial.unlink(missing_ok=True)
    raise SystemExit(f"{url}: {problem} (after {ATTEMPTS} attempts)")


def fetch(home: Path, opener=urllib.request.urlopen, sleep=time.sleep) -> int:
    todo = missing(home)
    total = sum(fetch_one(home, p, opener, sleep) for p in todo)
    print(f"fetched {len(todo)} file(s), {total / 1e6:.0f} MB, into {home}")
    return 0


def verify(home: Path, get: Callable | None = None) -> int:
    empty = missing(home)
    if empty:
        listed = "\n  ".join(str(p.relative_to(home)) for p in empty[:20])
        print(f"{len(empty)} in-scope template file(s) are not baked:\n  {listed}", file=sys.stderr)
        return 1
    if get is None:
        from templateflow.api import get
    failed = []
    for template, query in QUERIES:
        try:
            found = get(template, raise_empty=True, **query)
        except Exception as exc:  # noqa: BLE001 — every failure is reported, then fails the build
            failed.append(f"{template} {query}: {type(exc).__name__}: {exc}")
            continue
        for path in found if isinstance(found, list) else [found]:
            if Path(path).stat().st_size == 0:
                failed.append(f"{template} {query}: {path} is empty")
    for line in failed:
        print(line, file=sys.stderr)
    if failed:
        return 1
    print(f"{len(QUERIES)} TemplateFlow queries resolved offline from {home}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("fetch", "verify"))
    args = parser.parse_args(argv)
    home = tf_home()
    return fetch(home) if args.mode == "fetch" else verify(home)


if __name__ == "__main__":
    raise SystemExit(main())
