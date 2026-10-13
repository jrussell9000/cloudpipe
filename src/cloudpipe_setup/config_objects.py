"""The `config/` objects a deployment's data bucket must hold before a workflow runs.

A new data bucket holds none of them, and nothing used to say so. Workflows then
fail at artifact staging, which is the first step of each pod: after the install,
after the first subject has been staged in, and well away from the cause
(openspec/changes/publish-images-for-deployers, group 8).

The set is DERIVED, not listed. A key is required if a published workflow
template stages it as an S3 artifact (`key: config/<name>`) or if code the
workflows run reads it by name (`Key="config/<name>"`). So a template that starts staging a
new object is covered without anyone editing this file, and the test of this
module fails if that object has no provider.

Each key has exactly one provider, because each kind has a different owner:

* `fslicense` is the deployer's FreeSurfer license. It is issued per registrant,
  so it is never published and never seeded; the deployer supplies their own.
* The MNI templates and the fsLR meshes are public reference data, rebuilt from
  their upstream sources by `scripts/seed_reference_files.py`, with pinned
  checksums. Nothing is copied from this deployment's bucket.
* `nss_volumes.csv` is derived from the deployer's own ABCD scanner table by
  `scripts/make_nss_volumes.py`. This deployment's copy is keyed by subject ID and
  is never published.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TEMPLATE_ROOTS = ("argo/workflows",)
CODE_ROOTS = ("src", "images")

# An Argo S3 artifact key, quoted or not. Templated keys ({{...}}) are per-subject
# outputs, never config, and the character class excludes them.
_ARTIFACT_KEY = re.compile(r"""^\s*key:\s*['"]?(config/[A-Za-z0-9_.\-/]+?)['"]?\s*$""", re.M)
# A key named in code, as boto3 takes it.
_CODE_KEY = re.compile(r"""\bKey\s*=\s*['"](config/[A-Za-z0-9_.\-/]+)['"]""")

LICENSE_REGISTRATION = "https://surfer.nmr.mgh.harvard.edu/registration.html"


@dataclass(frozen=True)
class Provider:
    """Who supplies a `config/` object, and what to tell a deployer who lacks it."""

    name: str
    remedy: str


LICENSE = Provider(
    "your FreeSurfer license",
    "FreeSurfer licenses are issued per person, free, at "
    f"{LICENSE_REGISTRATION}. No published file carries one. Set `freesurfer_license_path` "
    "in your answers (`pixi run cloudpipe setup`) and re-run the bootstrap command it prints, "
    "which uploads it to config/fslicense; or upload it yourself with "
    "`aws s3 cp <path to license.txt> s3://<your data bucket>/config/fslicense`.",
)
REFERENCE = Provider(
    "public reference data",
    "Run `pixi run python scripts/seed_reference_files.py --bucket <your data bucket>`. It "
    "downloads each file from its upstream source (TemplateFlow, HCPpipelines), checks it "
    "against a pinned checksum, and uploads it.",
)
NSS = Provider(
    "your NSS table",
    "Run `pixi run python scripts/make_nss_volumes.py --mr-info <path>/mr_y_adm__info` on the "
    "ABCD table from your own data access, then upload the result to config/nss_volumes.csv. "
    "This deployment's table is keyed by subject ID and is never published.",
)

PROVIDERS: dict[str, Provider] = {
    "config/fslicense": LICENSE,
    "config/MNI152NLin2009cAsym_res-01_T1w_brain.nii": REFERENCE,
    "config/MNI152NLin2009cAsym_T1w_brain_res-2_RAI.nii": REFERENCE,
    "config/fsLR": REFERENCE,
    "config/nss_volumes.csv": NSS,
}


def required_keys(repo: Path) -> dict[str, list[str]]:
    """Every `config/` key the workflows read, with the files that read it."""
    found: dict[str, list[str]] = {}

    def add(key: str, path: Path) -> None:
        key = key.rstrip("/")
        found.setdefault(key, [])
        rel = str(path.relative_to(repo))
        if rel not in found[key]:
            found[key].append(rel)

    for top in TEMPLATE_ROOTS:
        for path in sorted((repo / top).rglob("*.yaml")):
            for key in _ARTIFACT_KEY.findall(path.read_text(encoding="utf-8")):
                add(key, path)
    for top in CODE_ROOTS:
        for path in sorted((repo / top).rglob("*.py")):
            for key in _CODE_KEY.findall(path.read_text(encoding="utf-8")):
                add(key, path)
    return dict(sorted(found.items()))


def provider(key: str) -> Provider | None:
    return PROVIDERS.get(key.rstrip("/"))
