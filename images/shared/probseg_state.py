"""Whether a func session's published probseg maps are the fixed ones (#911).

Before #911 the DIPY HMRF tissue maps (`label-{GM,WM,CSF}_probseg`) named the
wrong tissue in about half of all sessions. A session's maps are FIXED when it has
a TissueQC row newer than every one of its probseg objects. Only the fixed code
writes rows, and every writer puts the row after the objects, so no time cutoff is
needed, or possible: a workflow still running a pre-fix image republishes old maps
without a row, which makes the objects newer than the row again.

One rule, three readers: the afni backfill pod (probseg_backfill.py), its plan and
verify script (scripts/backfill_probseg.py), and first-level's session listing,
which runs resting-state analyses only on sessions whose maps are fixed.
"""

from __future__ import annotations

from datetime import datetime

SPACE = "space-MNI152NLin2009cAsym_res-1"
TISSUES = ("GM", "WM", "CSF")
ROW_PREFIX = "metrics/tissue-masks/"


def probseg_names(subj: str, ses: str) -> list[str]:
    return [f"{subj}_{ses}_{SPACE}_label-{t}_probseg.nii.gz" for t in TISSUES]


def session_state(
    listed: bool, probseg_at: list[datetime | None], row_at: datetime | None, marker_at
) -> str:
    """`fixed`, `rejected`, `stale` or `no-probseg` for one session.

    `listed`: the marker names the probseg. `probseg_at`: each probseg object's
    LastModified, None if absent. `row_at`: the session's newest TissueQC row,
    None if none. `marker_at`: the marker's LastModified.
    """
    present = [t for t in probseg_at if t is not None]
    if listed:
        if row_at is not None and len(present) == len(probseg_at) and row_at >= max(present):
            return "fixed"
        return "stale"
    if present:
        return "stale"  # a rejection that died between its marker and its delete
    if row_at is not None and marker_at is not None and row_at >= marker_at:
        return "rejected"
    return "no-probseg"


def tissue_rows(s3, bucket: str) -> dict[tuple[str, str], datetime]:
    """(subject, session) -> its newest TissueQC row's LastModified."""
    out: dict[tuple[str, str], datetime] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=ROW_PREFIX):
        for o in page.get("Contents", []):
            name = o["Key"].rsplit("/", 1)[1]
            if not name.endswith("_tissue_qc.json"):
                continue
            subj, _, rest = name.partition("_")
            unit = (subj, rest.removesuffix("_tissue_qc.json"))
            if unit not in out or o["LastModified"] > out[unit]:
                out[unit] = o["LastModified"]
    return out
