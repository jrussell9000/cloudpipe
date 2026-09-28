"""Discover one subject's BIDS files on the source collection and transfer them via Globus.

Runs as the `globus-transfer` step of the cloudpipe WorkflowTemplate, once per
subject. Four behaviours here are deliberate and each has a failure behind it:

**Tasks are labelled by workflow run, not by subject.** The label used to be
`cloudpipe-{subject}`, and any ACTIVE task with that label was adopted. That is
right for a pod retry within one run, and wrong for a *new* run after a
terminated one: the new run adopted a transfer it did not start. It is also wrong
for a run that reuses an earlier run's generated name, which Argo does once the
earlier Workflow object is garbage-collected (5 repeats in 12,401 runs by
2026-09-16). So the label carries a prefix of `{{workflow.uid}}` — stable across
pod retries, unique across runs — and anything else for the subject is cancelled.

**Submission is idempotent within an attempt.** One `submission_id` is fetched
before the retry loop and reused on every retry. If Globus accepts a submission
and the response is lost, the retry gets `Duplicate` with the original task ID
rather than creating a second task.

**Absent directories are reported, not skipped silently.** A session with no
`func/` used to vanish from the transfer without a trace. Every requested
directory is now recorded as found or absent, and one `TRANSFER-SUMMARY {json}`
line says so in a form the archived logs can be queried for.

**Globus errors are translated.** A lapsed High Assurance session arrives as
`not_from_allowed_domain`, which reads as a gateway misconfiguration; it cost a
300-subject batch on 2026-08-17. The translation is the operator CLI's own module,
copied into this image (see the Dockerfile), so the pod and the CLI cannot
disagree about what an error means. The raw error is always printed too.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import globus_sdk

from globus_admin.errors import globus_error
from globus_admin.exits import CliError

SUBMIT_RETRY_LIMIT = 10
SUBMIT_RETRY_INITIAL_WAIT = 60  # seconds
SUBMIT_RETRY_MAX_WAIT = 600  # seconds
POLL_INTERVAL_SECS = 60

LABEL_PREFIX = "cloudpipe-"

# How much of `{{workflow.uid}}` goes into the label: 12 hex characters, 48 bits.
#
# The label only has to tell apart runs of the *same subject*, of which there are
# a handful at most; at 48 random bits (Kubernetes UIDs are random v4 UUIDs) the
# chance of two runs of one subject colliding is about 1 in 10^14. The full
# 32-character UID would also fit — the Transfer API allows 128 characters — but
# buys nothing measurable and makes every label in the Globus web app and the
# logs 20 characters harder to read.
RUN_ID_LENGTH = 12

# Transfer API rules for `label`, from https://docs.globus.org/api/transfer/task_submit :
# "Maximum length is 128 characters. Newline characters (\r or \n) and trailing
# or leading whitespace are not allowed."
LABEL_MAX_LENGTH = 128

LIVE_STATUSES = ("ACTIVE", "INACTIVE")
SUMMARY_PREFIX = "TRANSFER-SUMMARY "

# Maps scan type names to their BIDS subdirectory and a filename substring
# that uniquely identifies files of that type.  All sidecar files (.json,
# .tsv, .bval, .bvec) that share the same basename stem are included.
SCAN_TYPE_MAP = {
    "T1w": ("anat", "_T1w"),
    "T2w": ("anat", "_T2w"),
    "rest": ("func", "_task-rest"),
    "nback": ("func", "_task-nback"),
    "sst": ("func", "_task-sst"),
    "mid": ("func", "_task-mid"),
    "dwi": ("dwi", "_dwi"),
}

VALID_SCAN_TYPES = set(SCAN_TYPE_MAP)
FUNCTIONAL_DIR = "func"

Log = Callable[[str], None]


def _log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def run_id(workflow_uid: str) -> str:
    """The run-identifying part of a label: the first RUN_ID_LENGTH hex chars of the UID.

    Refuses anything that is not a UUID. The two realistic mistakes are passing
    the workflow *name* (which repeats, and is the bug being fixed) and passing
    `{{workflow.uid}}` unsubstituted; both would produce a label that looks fine
    and silently defeats the point.
    """
    compact = workflow_uid.strip().replace("-", "").lower()
    if len(compact) != 32 or any(c not in "0123456789abcdef" for c in compact):
        raise ValueError(
            f"--workflow-uid must be the workflow's UID ({{{{workflow.uid}}}}), "
            f"got {workflow_uid!r}"
        )
    return compact[:RUN_ID_LENGTH]


def legacy_label(subject_id: str) -> str:
    """The label every task had before run scoping. Still recognised, for cancellation."""
    return f"{LABEL_PREFIX}{subject_id}"


def build_label(subject_id: str, workflow_uid: str | None = None) -> str:
    """`cloudpipe-{subject}-{run}`, or the legacy label when no run identity is given.

    The legacy form stays available so this image can ship before the template
    starts passing `--workflow-uid`. The template pins this image by digest, so
    the two land in separate steps; an image that rejected the old arguments
    would fail every transfer in between.
    """
    label = legacy_label(subject_id)
    if workflow_uid:
        label = f"{label}-{run_id(workflow_uid)}"
    validate_label(label)
    return label


def validate_label(label: str) -> None:
    """Raise if `label` breaks a Transfer API rule — before Globus rejects a submission."""
    if len(label) > LABEL_MAX_LENGTH:
        raise ValueError(f"label is {len(label)} characters; the Transfer API allows 128")
    if "\n" in label or "\r" in label:
        raise ValueError("label contains a newline, which the Transfer API rejects")
    if label != label.strip():
        raise ValueError("label has leading or trailing whitespace, which the Transfer API rejects")


def is_subject_task(label: str, subject_id: str) -> bool:
    """Whether a task label belongs to this subject, in either the legacy or run form.

    The trailing hyphen matters: without it, subject `sub-A1` would claim the
    tasks of `sub-A12`.
    """
    legacy = legacy_label(subject_id)
    return label == legacy or label.startswith(f"{legacy}-")


# ---------------------------------------------------------------------------
# Existing tasks: adopt our own, cancel everyone else's
# ---------------------------------------------------------------------------


def list_live_tasks(transfer_client: Any) -> list[dict[str, Any]]:
    """Every ACTIVE or INACTIVE task this identity owns, across all pages.

    The old single `task_list` call saw one page, so with a large backlog the
    task being looked for could simply not be in the response. Collected into a
    list before anything is cancelled, because cancelling while paginating a
    status-filtered listing shifts the offsets and skips entries.
    """
    paginator = transfer_client.paginated.task_list(filter=f"status:{','.join(LIVE_STATUSES)}")
    return list(paginator.items())


def settle_existing_tasks(
    transfer_client: Any, subject_id: str, label: str, *, log: Log = _log
) -> str | None:
    """Adopt this run's own ACTIVE task, if any; cancel every other live task for the subject.

    Returns the adopted task ID, or None when a new submission is needed.
    """
    adopted: str | None = None
    for task in list_live_tasks(transfer_client):
        task_label = str(task.get("label") or "")
        if not is_subject_task(task_label, subject_id):
            continue

        task_id = str(task["task_id"])
        status = str(task.get("status", ""))

        if task_label == label and status == "ACTIVE" and adopted is None:
            log(f"Adopting ACTIVE transfer task {task_id} ({label}): a retry of this same run.")
            adopted = task_id
            continue

        why = _why_cancel(task_label, label, status, subject_id)
        log(f"Cancelling {status} task {task_id} ({task_label}): {why}")
        try:
            transfer_client.cancel_task(task_id)
        except globus_sdk.TransferAPIError as e:
            # Not fatal here: if it is still running over the same paths, the
            # submission below fails with a Conflict, which is the clearer error.
            log(f"Warning: could not cancel task {task_id}: {e}")
    return adopted


def _why_cancel(task_label: str, label: str, status: str, subject_id: str) -> str:
    if task_label == label and status == "INACTIVE":
        return "it belongs to this run but is suspended or errored, and will not recover on its own"
    if task_label == label:
        return "a second live task for this same run"
    if task_label == legacy_label(subject_id):
        return "left by a run that predates run-scoped labels"
    return (
        "left by a different workflow run — terminated, or one that reused this "
        "workflow's generated name"
    )


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


def build_transfer_data(
    *,
    label: str,
    source_collection_id: str,
    source_base_path: str,
    dest_collection_id: str,
    dest_base_path: str,
    rel_paths: Iterable[str],
) -> globus_sdk.TransferData:
    tdata = globus_sdk.TransferData(
        source_collection_id,
        dest_collection_id,
        label=label,
        sync_level="checksum",
        encrypt_data=True,
        notify_on_succeeded=False,
        notify_on_failed=True,
    )
    base_src = source_base_path.rstrip("/")
    base_dst = dest_base_path.rstrip("/")
    for rel in rel_paths:
        tdata.add_item(f"{base_src}/{rel}", f"{base_dst}/{rel}")
    return tdata


def submit_transfer(
    transfer_client: Any,
    tdata: Any,
    *,
    file_count: int,
    sleep: Callable[[float], None] = time.sleep,
    log: Log = _log,
) -> str:
    """Submit once, retrying transient failures with the same `submission_id`.

    The ID is fetched here, before the loop, and set on the request explicitly.
    `submit_transfer` would fill one in on its own first call and happen to keep
    it on the same object — relying on that would make idempotency an accident of
    the SDK's implementation rather than something this code states.

    Retried: TooManyPendingJobs (Globus is busy), 5xx, and network failures —
    the last two being exactly the cases where the submission may have been
    accepted without the response arriving.
    """
    submission_id = str(transfer_client.get_submission_id()["value"])
    tdata["submission_id"] = submission_id

    wait = SUBMIT_RETRY_INITIAL_WAIT
    last_error: Exception | None = None
    for attempt in range(1, SUBMIT_RETRY_LIMIT + 1):
        try:
            result = transfer_client.submit_transfer(tdata)
        except globus_sdk.TransferAPIError as e:
            if e.code == "ClientError.Conflict.TooManyPendingJobs":
                reason = "TooManyPendingJobs"
            elif (e.http_status or 0) >= 500:
                reason = f"Globus returned {e.http_status}"
            else:
                raise
            last_error = e
        except globus_sdk.NetworkError as e:
            reason = f"network error ({type(e).__name__}); the submission may have been accepted"
            last_error = e
        else:
            task_id = str(result["task_id"])
            if result.get("code") == "Duplicate":
                # HTTP 200, not an error: the documented answer to a resubmitted
                # submission_id that Globus already accepted.
                log(
                    f"Submission {submission_id} had already been accepted as task {task_id} "
                    "(Duplicate); using that task."
                )
            else:
                log(f"Submitted transfer task {task_id} ({file_count} files)")
            return task_id

        log(
            f"{reason} (attempt {attempt}/{SUBMIT_RETRY_LIMIT}); retrying in {wait}s "
            f"with the same submission id {submission_id}"
        )
        sleep(wait)
        wait = min(wait * 2, SUBMIT_RETRY_MAX_WAIT)

    raise RuntimeError(
        f"Gave up submitting transfer after {SUBMIT_RETRY_LIMIT} attempts"
    ) from last_error


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass
class Discovery:
    """What was found for one subject, per session and per BIDS directory."""

    subject_id: str
    scan_types: list[str]
    sessions: list[str] = field(default_factory=list)
    rel_paths: list[str] = field(default_factory=list)
    found: list[dict[str, Any]] = field(default_factory=list)
    absent: list[dict[str, str]] = field(default_factory=list)
    files_by_scan_type: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "subject": self.subject_id,
            "sessions": self.sessions,
            "scan_types": self.scan_types,
            "files": len(self.rel_paths),
            "files_by_scan_type": self.files_by_scan_type,
            "found": self.found,
            "absent": self.absent,
        }

    def functional_types_missing_everywhere(self) -> list[str]:
        """Requested functional scan types with no file in any session."""
        return [
            scan_type
            for scan_type in self.scan_types
            if SCAN_TYPE_MAP[scan_type][0] == FUNCTIONAL_DIR
            and self.files_by_scan_type.get(scan_type, 0) == 0
        ]


def ls(transfer_client: Any, collection_id: str, path: str) -> list:
    """Return a list of items from a single Globus ls call."""
    return list(transfer_client.operation_ls(collection_id, path=path))


def _is_not_found(e: Exception) -> bool:
    return getattr(e, "http_status", None) == 404 or str(getattr(e, "code", "") or "").startswith(
        "ClientError.NotFound"
    )


def discover_files(
    transfer_client: Any,
    source_collection_id: str,
    source_base_path: str,
    scan_types: list[str],
    *,
    subject_id: str = "",
) -> Discovery:
    """
    Walk the BIDS directory tree under source_base_path and record, per session
    and per requested directory, what was found.

    Expected source layout (source_base_path is the subject root):
        ses-<session>/
            anat/   ← T1w, T2w
            func/   ← rest, nback, sst, mid
            dwi/    ← dwi

    A missing subject root, or no session directories, fails: nothing can be
    transferred and the cause is upstream. A missing *directory* inside a
    session does not — ABCD sessions legitimately lack modalities — but it is
    recorded, so the absence is visible rather than silent. Any listing error
    other than not-found propagates.
    """
    base = source_base_path.rstrip("/")
    result = Discovery(subject_id=subject_id, scan_types=list(scan_types))
    result.files_by_scan_type = {scan_type: 0 for scan_type in scan_types}

    # bids_dir → [(scan_type, filename substring)]
    dir_patterns: dict[str, list[tuple[str, str]]] = {}
    for scan_type in scan_types:
        bids_dir, pattern = SCAN_TYPE_MAP[scan_type]
        dir_patterns.setdefault(bids_dir, []).append((scan_type, pattern))

    try:
        top_entries = ls(transfer_client, source_collection_id, base)
    except globus_sdk.TransferAPIError as e:
        raise globus_error(e, context=f"subject root {base}") from e

    result.sessions = sorted(
        e["name"] for e in top_entries if e["type"] == "dir" and e["name"].startswith("ses-")
    )
    if not result.sessions:
        raise RuntimeError(f"No session directories found under {base}. Check source-base-path.")
    print(f"Found {len(result.sessions)} session(s): {result.sessions}", flush=True)

    for session in result.sessions:
        for bids_dir, patterns in dir_patterns.items():
            dir_path = f"{base}/{session}/{bids_dir}"
            try:
                entries = ls(transfer_client, source_collection_id, dir_path)
            except globus_sdk.TransferAPIError as e:
                if _is_not_found(e):
                    result.absent.append({"session": session, "dir": bids_dir})
                    continue
                raise

            matched = _collect_directory(result, session, bids_dir, patterns, entries)
            result.found.append({"session": session, "dir": bids_dir, "files": matched})

    return result


def _collect_directory(
    result: Discovery,
    session: str,
    bids_dir: str,
    patterns: list[tuple[str, str]],
    entries: list[dict[str, Any]],
) -> int:
    """Add one directory's matching files to `result`; return how many matched.

    A file counts toward every scan type whose pattern it contains. A `.tsv` in
    `func/` travels even when it names no requested task (events files for the
    requested runs do name them; this keeps any that do not).
    """
    matched = 0
    for entry in entries:
        if entry["type"] != "file":
            continue
        name = entry["name"]
        types_here = [scan_type for scan_type, pattern in patterns if pattern in name]
        if not types_here and not (bids_dir == FUNCTIONAL_DIR and name.endswith(".tsv")):
            continue
        for scan_type in types_here:
            result.files_by_scan_type[scan_type] += 1
        result.rel_paths.append(f"{session}/{bids_dir}/{name}")
        matched += 1
    return matched


def emit_summary(discovery: Discovery, *, log: Log = _log) -> None:
    """Exactly one machine-readable line, then a plain WARNING where one is due.

    The summary line is the contract: something reading archived pod logs finds
    the one line starting `TRANSFER-SUMMARY ` and parses the rest as JSON.
    """
    log(SUMMARY_PREFIX + json.dumps(discovery.summary(), sort_keys=True))
    for absence in discovery.absent:
        log(f"Note: {absence['session']} has no {absence['dir']}/ directory on the source.")
    missing = discovery.functional_types_missing_everywhere()
    if missing:
        log(
            f"WARNING: requested functional scan type(s) {missing} were found in no session "
            f"for {discovery.subject_id}. Functional preprocessing will have nothing to do "
            "for them."
        )


# ---------------------------------------------------------------------------
# Pre-flight, polling, and the entry point
# ---------------------------------------------------------------------------


def build_transfer_client(
    native_app_client_id: str, refresh_token: str
) -> globus_sdk.TransferClient:
    native_client = globus_sdk.NativeAppAuthClient(native_app_client_id)
    authorizer = globus_sdk.RefreshTokenAuthorizer(refresh_token, native_client)

    auth_client = globus_sdk.AuthClient(authorizer=authorizer)
    try:
        userinfo = auth_client.userinfo()
        print(f"Authenticated as: {userinfo.get('email') or userinfo.get('sub')}", flush=True)
    except Exception as e:
        print(f"Warning: could not retrieve userinfo: {e}", flush=True)

    return globus_sdk.TransferClient(authorizer=authorizer)


def verify_dest_collection(
    transfer_client: Any, dest_collection_id: str, dest_base_path: str
) -> None:
    """Fail fast if the destination is unreachable — the check that catches a lapsed session.

    A 404 passes: the path may not exist yet, and the request still proved the
    session and the gateway's S3 credential are good.
    """
    try:
        transfer_client.operation_ls(dest_collection_id, path=dest_base_path)
    except globus_sdk.TransferAPIError as e:
        if _is_not_found(e):
            print(
                "Destination collection pre-flight check passed (path not yet created).", flush=True
            )
            return
        raise globus_error(e, context="destination pre-flight") from e
    print("Destination collection pre-flight check passed.", flush=True)


def wait_for_transfer(transfer_client: Any, task_id: str) -> None:
    while True:
        task = transfer_client.get_task(task_id)
        status = task["status"]
        files_transferred = task.get("files_transferred", 0)
        files_total = task.get("files", 0)
        print(f"Task {task_id}: {status} ({files_transferred}/{files_total} files)", flush=True)

        if status == "SUCCEEDED":
            return
        elif status == "FAILED":
            details = task.get("nice_status_details") or task.get("nice_status") or "unknown error"
            raise RuntimeError(f"Globus transfer {task_id} failed: {details}")
        elif status == "CANCELLED":
            raise RuntimeError(f"Globus transfer {task_id} was cancelled externally")

        time.sleep(POLL_INTERVAL_SECS)


def report_error(err: CliError) -> None:
    """Print a translated Globus error for whoever reads the pod log.

    The session-expired line is fixed text on purpose: it is the one failure an
    operator must recognise at a glance, and a fixed string is greppable across
    300 archived logs.
    """
    if err.code == "globus.session_expired":
        print("GLOBUS SESSION EXPIRED: run 'pixi run globus login'", file=sys.stderr, flush=True)
        print(f"  raw: {err.raw}", file=sys.stderr, flush=True)
        print(f"  why: {err.message}", file=sys.stderr, flush=True)
        return
    print(f"GLOBUS ERROR [{err.code}]: {err.message}", file=sys.stderr, flush=True)
    if err.remedy:
        print(f"  next step: {err.remedy.text}", file=sys.stderr, flush=True)
    if err.raw:
        print(f"  raw: {err.raw}", file=sys.stderr, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover and transfer BIDS scan files for one subject via Globus."
    )
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--source-collection-id", required=True)
    parser.add_argument(
        "--source-base-path",
        required=True,
        help="Root on the source collection; the subject ID is appended, e.g. /abcd/raw",
    )
    parser.add_argument("--dest-collection-id", required=True)
    parser.add_argument(
        "--dest-base-path",
        required=True,
        help="Root on the destination collection; the subject ID is appended, e.g. /mmps_mproc",
    )
    parser.add_argument(
        "--scan-types",
        required=True,
        help=(
            f"JSON array of scan types to transfer. "
            f"Valid values: {sorted(VALID_SCAN_TYPES)}. "
            f"Example: '[\"T1w\", \"rest\", \"dwi\"]'"
        ),
    )
    parser.add_argument(
        "--workflow-uid",
        default=None,
        help=(
            "The Argo workflow UID ({{workflow.uid}}). Scopes the task label to this run. "
            "Optional only for compatibility with templates that predate it."
        ),
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace, transfer_client: Any) -> None:
    scan_types = json.loads(args.scan_types)
    invalid = set(scan_types) - VALID_SCAN_TYPES
    if invalid:
        raise ValueError(
            f"Unknown scan type(s): {sorted(invalid)}. Valid: {sorted(VALID_SCAN_TYPES)}"
        )

    label = build_label(args.subject_id, args.workflow_uid)
    if not args.workflow_uid:
        print(
            "Warning: no --workflow-uid given, so this transfer uses the legacy per-subject "
            f"label {label} and cannot tell this run apart from an earlier one.",
            flush=True,
        )

    verify_dest_collection(transfer_client, args.dest_collection_id, args.dest_base_path)

    source_subject_root = f"{args.source_base_path.rstrip('/')}/{args.subject_id}"
    dest_subject_root = f"{args.dest_base_path.rstrip('/')}/{args.subject_id}"

    print(f"Discovering files for subject {args.subject_id}, scan types: {scan_types}", flush=True)
    discovery = discover_files(
        transfer_client,
        source_collection_id=args.source_collection_id,
        source_base_path=source_subject_root,
        scan_types=scan_types,
        subject_id=args.subject_id,
    )
    # Emitted before the zero-files check, so a failed step still says what it
    # looked for and found.
    emit_summary(discovery)

    if not discovery.rel_paths:
        raise RuntimeError(
            "No matching files found for any requested scan type — check source-base-path "
            "and that subject data exists on the source collection."
        )
    print(f"Discovered {len(discovery.rel_paths)} file(s) across all sessions.", flush=True)

    task_id = settle_existing_tasks(transfer_client, args.subject_id, label)
    if task_id is None:
        tdata = build_transfer_data(
            label=label,
            source_collection_id=args.source_collection_id,
            source_base_path=source_subject_root,
            dest_collection_id=args.dest_collection_id,
            dest_base_path=dest_subject_root,
            rel_paths=discovery.rel_paths,
        )
        task_id = submit_transfer(transfer_client, tdata, file_count=len(discovery.rel_paths))

    wait_for_transfer(transfer_client, task_id)
    print(f"Transfer complete for subject {args.subject_id}.", flush=True)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        transfer_client = build_transfer_client(
            os.environ["GLOBUS_NATIVE_APP_CLIENT_ID"], os.environ["GLOBUS_REFRESH_TOKEN"]
        )
        run(args, transfer_client)
    except CliError as err:
        report_error(err)
        sys.exit(1)
    except globus_sdk.TransferAPIError as e:
        report_error(globus_error(e))
        sys.exit(1)
    except (ValueError, RuntimeError) as e:
        # This module's own failures (no files, a failed or cancelled task, a
        # submission that never landed): a sentence, not a traceback. The cause
        # is kept when there is one, since "gave up after 10 attempts" is only
        # useful alongside what the 10th attempt said.
        print(f"error: {e}", file=sys.stderr, flush=True)
        if e.__cause__ is not None:
            print(f"  caused by: {e.__cause__}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
