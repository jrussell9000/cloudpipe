#!/usr/bin/env python3
"""
Subject data inventory for cloudpipe_minproc.

Discovers sessions and BOLD runs from S3, checks completion markers for
prior pipeline steps, and looks up per-session metadata (nss_frames).

Outputs:
  stdout                    JSON array; one object per session. Consumed by
                            Argo's result output parameter, which feeds the
                            session-level withParam fan-out in the master DAG.
  /tmp/fastsurfer_exists.txt  "True" or "False"; gates the anatomical phase.
  /tmp/subregions_exists.txt  "True" or "False"; gates the subregion-segmentation phase.
"""

import argparse
import csv
import io
import json
import re
import sys

import boto3
from botocore.exceptions import ClientError

# cloudpipe_minproc processes rest and nback BOLD only.
# sst, mid, and dwi are excluded: insufficient run count for functional
# connectivity analysis and not part of the current analysis protocol.
TARGET_TASKS = {"rest", "nback"}


class InventoryError(Exception):
    """
    A subject cannot be inventoried — missing config or metadata in S3.

    Raised instead of calling sys.exit() so the discovery functions stay
    usable as a library (and testable without pytest.raises(SystemExit)).
    main() catches it and is the only place that sets the exit status, which
    is what Argo gates the inventory step on.
    """


def list_sessions(s3, bucket: str, subj: str) -> list[str]:
    """Return session IDs found under mmps_mproc/{subj}/ in S3."""
    paginator = s3.get_paginator("list_objects_v2")
    prefix = f"mmps_mproc/{subj}/"
    sessions = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            sessions.append(cp["Prefix"][len(prefix) :].strip("/"))
    return sessions


def list_bold_runs(s3, bucket: str, subj: str, ses: str) -> list[dict]:
    """
    Return unique (task, run) pairs from mmps_mproc/{subj}/{ses}/func/.

    Filters to TARGET_TASKS. Deduplicates — .nii and .nii.gz for the
    same (task, run) yield one entry.
    """
    paginator = s3.get_paginator("list_objects_v2")
    prefix = f"mmps_mproc/{subj}/{ses}/func/"
    seen: set[tuple[str, str]] = set()
    runs = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith((".nii", ".nii.gz")):
                continue
            fname = key.split("/")[-1]
            task_m = re.search(r"task-([a-zA-Z0-9]+)", fname)
            run_m = re.search(r"run-(\d+)", fname)
            if not task_m or not run_m:
                continue
            if task_m.group(1) not in TARGET_TASKS:
                continue
            entry = {"task": f"task-{task_m.group(1)}", "run": f"run-{run_m.group(1)}"}
            key_tuple = (entry["task"], entry["run"])
            if key_tuple not in seen:
                seen.add(key_tuple)
                runs.append(entry)
    return runs


def check_run_completion(s3, bucket: str, subj: str, ses: str, runs: list[dict]) -> None:
    """
    Add b2t_exists, func_exists and surf_exists flags to each run dict (in-place).

    Completion markers (terminal output files — presence proves the step finished):
      b2t_exists:  _desc-bold2t1w_itk.txt      (bold-to-t1w)
      func_exists: _space-MNI152NLin2009cAsym_bold.tar.gz  (functional preprocessing)
      surf_exists: _desc-grayordcomponents_bold.tar.gz     (grayordinate extraction)
      surf_target_exists: _space-fsLR32k_bold.dtseries.nii (fsLR/CIFTI assembly)

    func_exists and surf_exists gate independently: both derivatives come out of
    the same pod, but either can be missing on its own, and preproc.py takes the
    short path when only the grayordinate output is wanted.

    surf_target_exists gates the separate surface-resample step, which consumes
    the grayordinate components and cannot run before surf_exists is true.
    """
    for run in runs:
        task, run_id = run["task"], run["run"]
        prefix = f"{subj}_{ses}_{task}_{run_id}"
        b2t_key = (
            f"derivatives/registration/{subj}/{ses}/"
            f"bold_to_t1w_{task}_{run_id}/{prefix}_desc-bold2t1w_itk.txt"
        )
        func_key = f"derivatives/func/{subj}/{ses}/{prefix}_space-MNI152NLin2009cAsym_bold.tar.gz"
        surf_key = (
            f"derivatives/func_surf/{subj}/{ses}/components/"
            f"{prefix}_desc-grayordcomponents_bold.tar.gz"
        )
        surf_target_key = (
            f"derivatives/func_surf/{subj}/{ses}/fsLR32k/{prefix}_space-fsLR32k_bold.dtseries.nii"
        )
        for field, s3_key in (
            ("b2t_exists", b2t_key),
            ("func_exists", func_key),
            ("surf_exists", surf_key),
            ("surf_target_exists", surf_target_key),
        ):
            try:
                s3.head_object(Bucket=bucket, Key=s3_key)
                run[field] = True
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    run[field] = False
                else:
                    raise


def check_t1w_completion(s3, bucket: str, subj: str, ses: str) -> bool:
    """
    Return True if t1w-to-mni has completed for this session.

    Completion marker: _desc-t1w2mni_affine.mat (terminal output of fst1w_to_mni.py).
    """
    key = f"derivatives/registration/{subj}/{ses}/t1w_to_mni/{subj}_{ses}_desc-t1w2mni_affine.mat"
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise


def check_t1w_source_exists(s3, bucket: str, subj: str, ses: str) -> bool:
    """
    Return True if the minimally-preprocessed T1w scan exists in S3 for this session.

    Not every longitudinal session includes an anatomical scan (ABCD collects T1w
    less often than functional runs), so this must be checked explicitly rather
    than assumed for every session list_sessions() returns.
    """
    key = f"mmps_mproc/{subj}/{ses}/anat/{subj}_{ses}_run-01_T1w.nii.gz"
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise


def load_nss_volumes(s3, bucket: str, subj: str) -> dict[str, str]:
    """
    Download config/nss_volumes.csv and return {session: nss_frames} for subj.

    nss_frames is the number of non-steady-state volumes at the start of each
    BOLD run that must be trimmed before preprocessing (scanner magnetisation
    equilibrium is not reached for the first few TRs). The count varies by
    acquisition session; it is stored in nss_volumes.csv rather than the BIDS
    sidecar because the sidecar values are unreliable for some ABCD releases.

    Raises InventoryError if the CSV is missing from S3.
    """
    try:
        obj = s3.get_object(Bucket=bucket, Key="config/nss_volumes.csv")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
            raise InventoryError(f"s3://{bucket}/config/nss_volumes.csv not found") from e
        raise
    content = obj["Body"].read().decode("utf-8")
    return {
        row["session"]: row["nss_frames"]
        for row in csv.DictReader(io.StringIO(content))
        if row["subject_id"] == subj
    }


def check_fastsurfer_derivatives(s3, bucket: str, subj: str, sessions: list[str]) -> bool:
    """
    Return True if every session has a FastSurfer templated tarball in S3.

    A missing tarball for any session means the anatomical phase must run.
    The tarball is produced by fastsurfer-long-parcellation and contains the
    longitudinal FreeSurfer outputs needed by registration and func-preproc.
    """
    missing = []
    for ses in sessions:
        key = f"derivatives/fastsurfer/{subj}/{subj}_{ses}_templated.tar.gz"
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                missing.append(ses)
            else:
                raise
    if missing:
        print(f"Missing FastSurfer derivatives for sessions: {missing}", file=sys.stderr)
    return len(missing) == 0


def check_subregions_derivatives(s3, bucket: str, subj: str) -> bool:
    """
    Return True iff all five subregion-segmentation output tarballs exist in S3.

    The outputs are subject-level (each tarball bundles all sessions), so this is
    a single subject-level flag mirroring fastsurfer_exists. Requiring all five
    (not any) means a partial or failed prior run re-runs cleanly; the phase's
    per-region resume guards then skip whatever regions did complete.

    hippoamyg is gated alongside the original four so the cohort stays uniform: a
    subject whose tarballs predate hippo-amygdala reports False and re-acquires
    the missing region on its next submission, rather than silently remaining a
    four-region outlier. Missing FastSurfer derivatives do not block this -- the
    anatomical phase regenerates them (it runs when fastsurfer-exists is False) --
    but for a subject whose derivatives have been cleaned from S3 that
    regeneration, not the segmentation, dominates the cost of the backfill.
    """
    regions = ("thalamus", "brainstem", "hippoamyg", "hypothalamic", "sclimbic")
    missing = []
    for region in regions:
        key = f"derivatives/subregions/{subj}/{subj}_{region}.tar.gz"
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                missing.append(region)
            else:
                raise
    if missing:
        print(f"Missing subregion derivatives: {missing}", file=sys.stderr)
    return len(missing) == 0


def build_inventory(s3, bucket: str, subj: str) -> tuple[list[dict], bool, bool]:
    """
    Run all discovery and completion checks; return
    (session_list, fastsurfer_exists, subregions_exists).

    session_list — one dict per session:
      {
        "session": str,
        "runs": [{"task": str, "run": str, "b2t_exists": bool,
                  "func_exists": bool, "surf_exists": bool,
                  "surf_target_exists": bool}],
        "t1w_to_mni_exists": bool,
        "nss_frames": str,
      }
    fastsurfer_exists — True iff all sessions have valid templated tarballs.
    subregions_exists — True iff all four subregion output tarballs exist.

    Raises InventoryError if nss_volumes.csv is missing or has no entry for a
    discovered session.
    """
    sessions = list_sessions(s3, bucket, subj)
    nss_map = load_nss_volumes(s3, bucket, subj)

    result = []
    t1w_sessions = []
    for ses in sessions:
        runs = list_bold_runs(s3, bucket, subj, ses)
        check_run_completion(s3, bucket, subj, ses, runs)
        t1w_exists = check_t1w_completion(s3, bucket, subj, ses)
        t1w_available = check_t1w_source_exists(s3, bucket, subj, ses)
        if t1w_available:
            t1w_sessions.append(ses)

        if ses not in nss_map:
            raise InventoryError(f"no nss_frames entry for {subj}/{ses} in nss_volumes.csv")
        nss_val = nss_map[ses]
        if not nss_val:
            print(
                f"WARNING: empty nss_frames for {subj}/{ses}; "
                f"bold_to_t1w will fall back to BIDS sidecar JSON",
                file=sys.stderr,
            )

        result.append(
            {
                "session": ses,
                "runs": runs,
                "t1w_to_mni_exists": t1w_exists,
                "t1w_available": t1w_available,
                "nss_frames": nss_val,
            }
        )

    # Only sessions with a T1w source can ever produce a templated tarball —
    # judging fastsurfer_exists against anat-less sessions would keep the
    # anatomical phase re-running forever since it could never be satisfied.
    fs_exists = check_fastsurfer_derivatives(s3, bucket, subj, t1w_sessions)
    subregions_exists = check_subregions_derivatives(s3, bucket, subj)
    return result, fs_exists, subregions_exists


def main() -> None:
    parser = argparse.ArgumentParser(description="cloudpipe_minproc subject data inventory")
    parser.add_argument("--subject", required=True, help="BIDS subject ID (e.g. NDARINVXXXXXXXX)")
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    args = parser.parse_args()

    s3 = boto3.client("s3")
    try:
        session_list, fs_exists, subregions_exists = build_inventory(s3, args.bucket, args.subject)
    except InventoryError as e:
        # Sole exit-status decision for the step; the marker files and the JSON
        # array are deliberately left unwritten so no downstream fan-out reads
        # a partial inventory.
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    with open("/tmp/fastsurfer_exists.txt", "w") as fh:
        fh.write("True" if fs_exists else "False")

    with open("/tmp/subregions_exists.txt", "w") as fh:
        fh.write("True" if subregions_exists else "False")

    print(json.dumps(session_list))


if __name__ == "__main__":
    main()
