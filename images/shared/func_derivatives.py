"""Publish one session's functional derivatives as exploded S3 objects (ADR 017).

Layout, flat in the session prefix:

    derivatives/func/{subj}/{ses}/{subj}_{ses}_{task}_{run}_space-MNI152NLin2009cAsym_bold.nii.gz
    derivatives/func/{subj}/{ses}/{subj}_{ses}_{task}_{run}_space-MNI152NLin2009cAsym_brainmask.nii.gz
    derivatives/func/{subj}/{ses}/{subj}_{ses}_{task}_{run}_desc-boldref.nii.gz
    derivatives/func/{subj}/{ses}/{subj}_{ses}_{task}_{run}_desc-confounds_timeseries.tsv
    derivatives/func/{subj}/{ses}/{subj}_{ses}_{task}_{run}_events.tsv        # n-back only
    derivatives/func/{subj}/{ses}/{subj}_{ses}_space-MNI152NLin2009cAsym_res-1_*   # session tissue maps
    derivatives/func/{subj}/{ses}/_complete.json                             # written LAST

`_complete.json` is the only existence test, exactly as for FastSurfer trees: a
prefix listing reads the same for a session whose upload died halfway. It lists
every run's files, so inventory decides func_exists per run from it.

This is not fs_derivatives.publish, for two reasons. A func pod usually redoes only
some of a session's runs (inventory skips the finished ones), so pruning the whole
prefix to the uploaded tree would delete the runs it did not touch; and the marker
must describe the whole session, not just this upload. So publish here merges:
the previous marker's runs survive unless this pod republished them, and only the
republished runs' stale objects are pruned. The ordering contract is the same:
marker removed first, then files, then prune, then the merged marker last, so a
crash anywhere leaves the session markerless, which reads as "absent" and reruns.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fs_derivatives import PublishError, _client, _transfer, log
from fs_derivatives import read_marker as _read_any_marker
from restore_links import COMPLETE_SIDECAR

KIND = "func-session"
SCHEMA_VERSION = 1

__all__ = ["publish_session", "read_marker", "run_id", "PublishError"]


def run_id(task: str, run: str) -> str:
    """The marker's key for one run: `{task}_{run}`, e.g. `task-rest_run-01`."""
    return f"{task}_{run}"


def read_marker(bucket: str, prefix: str, *, s3=None) -> dict | None:
    """The session's marker, or None if there is none (absent, or not a func marker)."""
    marker = _read_any_marker(bucket, prefix, s3=s3)
    return marker if marker and marker.get("kind") == KIND else None


def _listing(client, bucket: str, prefix: str) -> set[str]:
    keys = set()
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{prefix}/"
    ):
        keys.update(o["Key"] for o in page.get("Contents", []))
    return keys


def publish_session(
    tree: Path | str,
    bucket: str,
    prefix: str,
    *,
    runs: dict[str, list[str]],
    session_files: list[str],
    owner: str,
    source_step: str,
    s3=None,
) -> dict:
    """Upload this pod's session outputs and write the merged `_complete.json` last.

    `runs` maps run_id -> the file names (in `tree`) this pod wrote for that run;
    `session_files` are session-level names (the tissue maps), possibly empty.
    `owner` is the `{subj}_{ses}` file-name prefix, which scopes the prune.
    """
    tree = Path(tree)
    prefix = prefix.rstrip("/")
    names = [n for files in runs.values() for n in files] + list(session_files)
    if not names:
        raise PublishError(f"nothing to publish under {prefix}")
    missing = [n for n in names if not (tree / n).is_file()]
    if missing:
        raise PublishError(f"listed but not in {tree}: {missing[:3]}")
    client = _client(s3)

    previous = read_marker(bucket, prefix, s3=client)
    client.delete_object(Bucket=bucket, Key=f"{prefix}/{COMPLETE_SIDECAR}")

    def _put(worker_s3, name: str) -> str:
        key = f"{prefix}/{name}"
        with (tree / name).open("rb") as fh:
            worker_s3.put_object(Bucket=bucket, Key=key, Body=fh)
        return key

    written = set(_transfer(_put, names, s3))

    # Prune only what this publish supersedes: a republished run's objects that it
    # no longer produces, and the old session maps if new ones were written.
    present = _listing(client, bucket, prefix)
    owned = tuple(f"{prefix}/{owner}_{rid}_" for rid in runs)
    if session_files:
        owned += (f"{prefix}/{owner}_space-",)
    stale = sorted(k for k in present if k.startswith(owned) and k not in written)
    for i in range(0, len(stale), 1000):
        client.delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[i : i + 1000]]}
        )
    if stale:
        log(f"publish: pruned {len(stale)} stale object(s) of the republished runs")
    present -= set(stale)

    kept_runs = {
        rid: files
        for rid, files in ((previous or {}).get("runs") or {}).items()
        if rid not in runs and all(f"{prefix}/{f}" in present for f in files)
    }
    kept_session = []
    if not session_files and previous:
        kept_session = [
            f for f in previous.get("session_files") or [] if f"{prefix}/{f}" in present
        ]
    merged_runs = {**kept_runs, **runs}
    merged_session = list(session_files) or kept_session
    all_files = [f for files in merged_runs.values() for f in files] + merged_session
    marker = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "runs": {rid: sorted(files) for rid, files in sorted(merged_runs.items())},
        "session_files": sorted(merged_session),
        "file_count": len(all_files),
        "written_at": datetime.now(timezone.utc).isoformat(),
        "source_step": source_step,
    }
    # LAST: everything the marker names is in place.
    client.put_object(
        Bucket=bucket,
        Key=f"{prefix}/{COMPLETE_SIDECAR}",
        Body=json.dumps(marker, indent=2, sort_keys=True).encode(),
    )
    log(
        f"publish: {len(written)} objects ({len(runs)} runs this pod, "
        f"{len(kept_runs)} kept) -> s3://{bucket}/{prefix}/"
    )
    return marker
