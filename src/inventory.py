#!/usr/bin/env python3
"""
Subject data inventory for cloudpipe_minproc.

Discovers sessions and BOLD runs from S3, checks completion markers for
prior pipeline steps, and looks up per-session metadata (nss_frames).

Two modes, both driven from the master DAG:

`--mode inventory` (default) — the discovery pass, run before the anatomical phase.
  stdout                    JSON array; one object per session. Consumed by
                            Argo's result output parameter.
  /tmp/fastsurfer_exists.txt  "True" or "False"; gates the anatomical phase.
  /tmp/subregions_exists.txt  "True" or "False"; gates the subregion-segmentation phase.

`--mode published-sessions` — re-reads the completion markers AFTER the anatomical
phase and splits the inventory into the sessions that actually have derivatives and
those that do not. This is what the session-level fan-out is driven by; inventory's
own array cannot be, because it describes the INPUTS (issue #270).
  /tmp/session_items.json     the fan-out list: inventory items, published only.
  /tmp/rejected_sessions.json JSON array of session IDs that were expected to be
                              published and were not.
  /tmp/rejected_count.txt     len(rejected), as a string, for a `when:` guard.
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
      components_exist: _desc-grayordcomponents_bold.tar.gz (grayordinate extraction)
      surf_target_exists: _space-fsLR32k_bold.dtseries.nii  (fsLR/CIFTI assembly)
      surf_exists: components_exist OR surf_target_exists   (see below)

    func_exists and surf_exists gate independently: both derivatives come out of
    the same pod, but either can be missing on its own, and preproc.py takes the
    short path when only the grayordinate output is wanted.

    surf_target_exists gates the separate surface-resample step, which consumes
    the grayordinate components and cannot run before surf_exists is true.

    surf_exists means "grayordinate extraction finished for this run", NOT "its
    components tarball is still in S3". The two stopped being the same thing when
    surface-resample began deleting each run's components after verifying the
    assembled dtseries: the components are a bulky intermediate (~2.2 GiB per
    session, roughly half of all derivative storage) that is reconstructible from
    the volumetric output, so they are reclaimed rather than kept.

    Hence the OR below. The dtseries is strictly downstream of the components and
    is 1:1 with them per run, so its presence is *stronger* evidence that
    extraction succeeded than the components' own. Without the OR, every reclaimed
    run would read as surf_exists=false forever, waking the func-preproc pod on
    the `--emit grayordinate` short path to regenerate components that the next
    resample would immediately delete again — paying cpu-heavy compute on every
    subsequent workflow to reclaim storage that was already reclaimed.
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
            ("components_exist", surf_key),
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

        # See the docstring: extraction is done if EITHER the intermediate is
        # still present or the artifact built from it is. components_exist is
        # kept as its own key so callers can tell "never extracted" from
        # "extracted and since reclaimed" — the driver in
        # functional-preprocessing-workflow-template.yaml needs that distinction
        # to know whether a re-run could reuse the components on local disk.
        run["surf_exists"] = run["components_exist"] or run["surf_target_exists"]


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


def published_fastsurfer_sessions(s3, bucket: str, subj: str, sessions: list[str]) -> list[str]:
    """
    Return the subset of `sessions` whose FastSurfer tree is COMPLETE in S3.

    Order-preserving, and the same marker test as check_fastsurfer_derivatives —
    deliberately one implementation, so the boolean gate and the per-session
    verdict cannot drift apart. `_complete.json` and nothing else (ADR 017); see
    that function's docstring for why nothing else will do.

    The difference between this and the boolean is WHEN it is asked. The boolean
    runs before the anatomical phase and decides whether that phase runs at all.
    This runs after it, to decide which sessions the functional half may fan out
    over — the two are not the same question whenever the phase publishes some
    sessions and not others, which is exactly what the #248 completion guard is
    designed to do.
    """
    published = []
    for ses in sessions:
        key = f"derivatives/fastsurfer/{subj}/{ses}/_complete.json"
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] != "404":
                raise
            continue
        published.append(ses)
    return published


def split_published_sessions(
    s3, bucket: str, subj: str, inventory: list[dict]
) -> tuple[list[dict], list[str]]:
    """
    Split an inventory array into (fan-out items, rejected session IDs).

    Only sessions with `t1w_available` are candidates. A session without a T1w
    source is neither published nor rejected: it is intentionally unprocessed
    (it has no anatomical to register against), it was never a candidate for
    FastSurfer, and reporting it as a failure would mark most longitudinal
    subjects partial forever.

    Everything else divides in two, and the distinction is the whole point of
    issue #270: a candidate session with a marker is publishable work, and a
    candidate session WITHOUT one is a session the anatomical phase declined to
    publish — almost always the #248 completion guard rejecting a crashed
    `recon-surf` for that timepoint. Fanning out over the second kind produces a
    branch that dies at stage time on a missing input artifact, which surfaces as
    workflow phase `Error` and an `overall_status` of failed. The subject is then
    indistinguishable from one where nothing worked, when in the observed case
    (cloudpipe-knwr6 / sub-GNV5ZKU4) 25 of 27 pods succeeded.
    """
    candidates = [item for item in inventory if item.get("t1w_available")]
    names = [item["session"] for item in candidates]
    published = set(published_fastsurfer_sessions(s3, bucket, subj, names))

    items = [item for item in candidates if item["session"] in published]
    rejected = [ses for ses in names if ses not in published]
    return items, rejected


def check_fastsurfer_derivatives(s3, bucket: str, subj: str, sessions: list[str]) -> bool:
    """
    Return True if every session has a COMPLETE FastSurfer tree in S3.

    A missing tree for any session means the anatomical phase must run. The tree
    is produced by fastsurfer-long-parcellation and contains the longitudinal
    FreeSurfer outputs needed by registration and func-preproc.

    Completeness is tested by `_complete.json` and by nothing else (ADR 017).
    Under the old tarball layout, one head_object on the tarball key was a
    truthful "the whole thing is here", because a tarball is a single PUT. The
    exploded layout has no such natural atom: a prefix listing, an object count,
    or a probe for any individual file all answer "yes" for a tree whose upload
    died halfway — and on spot that needs no bug at all, just a preemption
    mid-upload. `_complete.json` is written after every other object, so its
    presence is the only thing that still means what the tarball key used to.

    Getting this wrong is not a subtle bug. This function is what makes the
    pipeline skip the entire anatomical phase, so a false True marks a subject
    permanently complete against a half-written tree — the #245 failure, which
    cost a whole batch's worth of subjects the last time it happened.

    The long-template is checked alongside the sessions, and must be: it is the
    other tree the anatomical phase publishes, subregion-segmentation takes it as
    a REQUIRED input artifact, and nothing else ever writes it. Omitting it makes
    "all sessions present, template absent" read as True, which skips the only
    phase that could produce the template — so subregion segmentation 404s on
    every future submission while registration and func-preproc keep succeeding,
    and the subject reports partial success instead of failure. The producer also
    publishes the template BEFORE the sessions so that state cannot be reached in
    the first place; this check is what lets a subject already in it recover.
    """
    published = set(published_fastsurfer_sessions(s3, bucket, subj, sessions))
    missing = [ses for ses in sessions if ses not in published]
    if missing:
        print(f"Missing FastSurfer derivatives for sessions: {missing}", file=sys.stderr)

    try:
        s3.head_object(
            Bucket=bucket, Key=f"derivatives/fastsurfer/{subj}/long-template/_complete.json"
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            print(f"Missing FastSurfer long-template derivative for {subj}", file=sys.stderr)
            return False
        raise

    return len(missing) == 0


def check_subregions_derivatives(s3, bucket: str, subj: str) -> bool:
    """
    Return True iff all five subregion-segmentation output trees are COMPLETE in S3.

    The outputs are subject-level (each tree covers all sessions), so this is a
    single subject-level flag mirroring fastsurfer_exists. Requiring all five
    (not any) means a partial or failed prior run re-runs cleanly; the phase's
    per-region resume guards then skip whatever regions did complete.

    As with check_fastsurfer_derivatives, completeness is `_complete.json` and
    nothing else (ADR 017). Note this check and the segmentation phase's own
    `checkpoint_restore` now agree by construction: both ask the same question of
    the same marker, so "already done in a prior run" and "skip this subject"
    cannot drift apart.

    hippoamyg is gated alongside the original four so the cohort stays uniform: a
    subject whose derivatives predate hippo-amygdala reports False and re-acquires
    the missing region on its next submission, rather than silently remaining a
    four-region outlier. Missing FastSurfer derivatives do not block this -- the
    anatomical phase regenerates them (it runs when fastsurfer-exists is False) --
    but for a subject whose derivatives have been cleaned from S3 that
    regeneration, not the segmentation, dominates the cost of the backfill.
    """
    regions = ("thalamus", "brainstem", "hippoamyg", "hypothalamic", "sclimbic")
    missing = []
    for region in regions:
        key = f"derivatives/subregions/{subj}/{region}/_complete.json"
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
                  "components_exist": bool, "surf_target_exists": bool}],
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


def run_published_sessions(s3, bucket: str, subj: str, inventory_json: str) -> None:
    """Write the three published-sessions outputs. See the module docstring."""
    inventory = json.loads(inventory_json)
    items, rejected = split_published_sessions(s3, bucket, subj, inventory)

    if rejected:
        # The single line an operator greps for. The workflow does NOT fail here:
        # a rejected session is a partial subject, and failing the whole subject
        # would discard the sessions that did work.
        print(
            f"WARNING: {len(rejected)} session(s) have no FastSurfer derivatives and "
            f"will not be processed: {rejected}",
            file=sys.stderr,
        )
    print(f"Fanning out over {len(items)} published session(s)", file=sys.stderr)

    with open("/tmp/session_items.json", "w") as fh:
        json.dump(items, fh)
    with open("/tmp/rejected_sessions.json", "w") as fh:
        json.dump(rejected, fh)
    with open("/tmp/rejected_count.txt", "w") as fh:
        fh.write(str(len(rejected)))


def main() -> None:
    parser = argparse.ArgumentParser(description="cloudpipe_minproc subject data inventory")
    parser.add_argument("--subject", required=True, help="BIDS subject ID (e.g. NDARINVXXXXXXXX)")
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument(
        "--mode",
        choices=("inventory", "published-sessions"),
        default="inventory",
        help="which pass to run (default: inventory)",
    )
    parser.add_argument(
        "--inventory-json",
        help="JSON array from the inventory pass; required for --mode published-sessions",
    )
    args = parser.parse_args()

    s3 = boto3.client("s3")

    if args.mode == "published-sessions":
        if not args.inventory_json:
            parser.error("--inventory-json is required with --mode published-sessions")
        run_published_sessions(s3, args.bucket, args.subject, args.inventory_json)
        return

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
