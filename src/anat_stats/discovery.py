"""Enumerate the stats objects to aggregate, gated on `_complete.json`.

`_complete.json` is the only existence test (ADR 017). It is written after every
other object in a tree, so a prefix listing, an object count, or a probe for any
single file all answer "yes" for a half-uploaded tree — and reaching that state
needs no bug, just a spot preemption mid-upload. A stats file present under a
tree with no marker is therefore skipped, not read.

The marker sits at DIFFERENT DEPTHS in the two trees, which is the easiest thing
to get wrong here:

    derivatives/fastsurfer/{subj}/{ses}/_complete.json           per session
    derivatives/fastsurfer/{subj}/long-template/_complete.json   per subject
    derivatives/subregions/{subj}/{region}/_complete.json        per REGION TREE,
                                                                covering all its
                                                                sessions

Files are found by LISTING each subject's prefix rather than by probing a
hardcoded set of filenames. That costs one or two extra LIST calls per subject
(~$0.12 across the cohort) and buys two things: a new stats file in a future
FastSurfer release is picked up with no code change, and a MISSING file shows up
as a smaller row count rather than as a 404 to swallow. A hardcoded list is how
the previous aggregation script came to glob `lh.aparc.stats` — a name FastSurfer
never writes — and report success while reading nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

COMPLETE_MARKER = "_complete.json"
FASTSURFER_PREFIX = "derivatives/fastsurfer/"
SUBREGION_PREFIX = "derivatives/subregions/"

LONG_TEMPLATE = "long-template"

# Format dispatch. `[lr]h.curv.stats` carries the .stats extension but is
# `mris_curvature_stats` free text, not a FreeSurfer table — it must be checked
# BEFORE the generic .stats rule.
FMT_CURVATURE = "curvature"
FMT_FREESURFER = "freesurfer"
FMT_VOLUME_TABLE = "volume_table"

KIND_FASTSURFER = "fastsurfer"
KIND_SUBREGION = "subregion"


@dataclass(frozen=True)
class StatsFile:
    """One object to fetch and parse, with the identity its key implies.

    The subregion outputs carry no subject or session inside the file, so this
    identity is the only thing that ties their numbers to a scan.
    """

    key: str
    subject: str
    session: str
    source: str
    kind: str
    fmt: str


def classify(filename: str) -> str | None:
    """Return the parser format for a filename, or None if it is not a stats file."""
    if filename.endswith("curv.stats"):
        return FMT_CURVATURE
    if filename.endswith(".stats"):
        return FMT_FREESURFER
    if filename.endswith(".txt"):
        return FMT_VOLUME_TABLE
    return None


def list_subjects(s3, bucket: str) -> list[str]:
    """Subjects present under either derivatives tree, sorted.

    Both trees are listed and unioned rather than driving off FastSurfer alone:
    a subject with subregion output and no retained FastSurfer tree is a real
    (if odd) state, and dropping it silently would understate coverage.
    """
    subjects: set[str] = set()
    for prefix in (FASTSURFER_PREFIX, SUBREGION_PREFIX):
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
            for entry in page.get("CommonPrefixes", []):
                subject = entry["Prefix"].removeprefix(prefix).rstrip("/")
                if subject:
                    subjects.add(subject)
    return sorted(subjects)


def _list_keys(s3, bucket: str, prefix: str) -> list[str]:
    paginator = s3.get_paginator("list_objects_v2")
    return [
        obj["Key"]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for obj in page.get("Contents", [])
    ]


def _complete_units(keys: list[str], prefix: str, depth: int) -> set[str]:
    """Units whose `_complete.json` is present, named by their first `depth` path parts."""
    complete: set[str] = set()
    for key in keys:
        parts = key.removeprefix(prefix).split("/")
        if parts[-1] == COMPLETE_MARKER and len(parts) == depth + 1:
            complete.add("/".join(parts[:depth]))
    return complete


def fastsurfer_files(keys: list[str], subject: str) -> Iterator[StatsFile]:
    """Stats files under complete FastSurfer units (each session, plus the template)."""
    prefix = f"{FASTSURFER_PREFIX}{subject}/"
    complete = _complete_units(keys, prefix, depth=1)
    for key in keys:
        parts = key.removeprefix(prefix).split("/")
        # {unit}/stats/{filename} — stats live nowhere else in the tree.
        if len(parts) != 3 or parts[1] != "stats":
            continue
        unit, filename = parts[0], parts[2]
        if unit not in complete:
            continue
        if fmt := classify(filename):
            # Only .stats is a stats file here; the tree also holds e.g.
            # stats/aseg.auto.mgz, a binary that the extension check excludes.
            if fmt == FMT_VOLUME_TABLE:
                continue
            yield StatsFile(key, subject, unit, filename, KIND_FASTSURFER, fmt)


def subregion_files(keys: list[str], subject: str) -> Iterator[StatsFile]:
    """Volume tables under complete subregion region-trees.

    `source` is prefixed with the region because the filenames alone are not
    unique across regions in any meaningful sense, and because the region is the
    unit the completeness marker actually covers.
    """
    prefix = f"{SUBREGION_PREFIX}{subject}/"
    complete = _complete_units(keys, prefix, depth=1)
    for key in keys:
        parts = key.removeprefix(prefix).split("/")
        # {region}/{ses}/mri/{filename}
        if len(parts) != 4 or parts[2] != "mri":
            continue
        region, session, filename = parts[0], parts[1], parts[3]
        if region not in complete:
            continue
        if classify(filename) == FMT_VOLUME_TABLE:
            yield StatsFile(
                key, subject, session, f"{region}/{filename}", KIND_SUBREGION, FMT_VOLUME_TABLE
            )


def discover_subject(s3, bucket: str, subject: str) -> list[StatsFile]:
    """Every stats object to read for one subject, across both derivative trees."""
    fastsurfer_keys = _list_keys(s3, bucket, f"{FASTSURFER_PREFIX}{subject}/")
    subregion_keys = _list_keys(s3, bucket, f"{SUBREGION_PREFIX}{subject}/")
    return [
        *fastsurfer_files(fastsurfer_keys, subject),
        *subregion_files(subregion_keys, subject),
    ]
