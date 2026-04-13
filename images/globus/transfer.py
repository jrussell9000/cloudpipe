import argparse
import json
import os
import sys
import time

import globus_sdk


POLL_INTERVAL_SECS = 60

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


def build_transfer_client(native_app_client_id: str, refresh_token: str) -> globus_sdk.TransferClient:
    native_client = globus_sdk.NativeAppAuthClient(native_app_client_id)
    authorizer = globus_sdk.RefreshTokenAuthorizer(refresh_token, native_client)

    auth_client = globus_sdk.AuthClient(authorizer=authorizer)
    try:
        userinfo = auth_client.userinfo()
        print(f"Authenticated as: {userinfo.get('email') or userinfo.get('sub')}", flush=True)
    except Exception as e:
        print(f"Warning: could not retrieve userinfo: {e}", flush=True)

    return globus_sdk.TransferClient(authorizer=authorizer)


def ls(transfer_client: globus_sdk.TransferClient, collection_id: str, path: str) -> list:
    """Return a list of items from a single Globus ls call, handling pagination."""
    items = []
    for entry in transfer_client.operation_ls(collection_id, path=path):
        items.append(entry)
    return items


def discover_files(
    transfer_client: globus_sdk.TransferClient,
    source_collection_id: str,
    source_base_path: str,
    scan_types: list[str],
) -> list[str]:
    """
    Walk the BIDS directory tree under source_base_path and return a list of
    relative paths for all files matching the requested scan types across every
    available session.

    Expected source layout (source_base_path is the subject root):
        ses-<session>/
            anat/   ← T1w, T2w
            func/   ← rest, nback, sst, mid
            dwi/    ← dwi
    """
    base = source_base_path.rstrip("/")
    rel_paths = []

    # Build a lookup: bids_dir → [filename_substrings]
    # e.g. {"anat": ["_T1w", "_T2w"], "func": ["_task-rest"], "dwi": ["_dwi"]}
    dir_patterns: dict[str, list[str]] = {}
    for scan_type in scan_types:
        bids_dir, pattern = SCAN_TYPE_MAP[scan_type]
        dir_patterns.setdefault(bids_dir, []).append(pattern)

    # List sessions (top-level directories under the subject root)
    try:
        top_entries = ls(transfer_client, source_collection_id, base)
    except globus_sdk.TransferAPIError as e:
        raise RuntimeError(f"Failed to list {base}: {e}") from e

    sessions = [e["name"] for e in top_entries if e["type"] == "dir" and e["name"].startswith("ses-")]
    if not sessions:
        raise RuntimeError(f"No session directories found under {base}. Check source-base-path.")
    print(f"Found {len(sessions)} session(s): {sessions}", flush=True)

    for session in sessions:
        for bids_dir, patterns in dir_patterns.items():
            dir_path = f"{base}/{session}/{bids_dir}"
            try:
                entries = ls(transfer_client, source_collection_id, dir_path)
            except globus_sdk.TransferAPIError as e:
                if e.code == "ClientError.NotFound" or e.http_status == 404:  # Directory may not exist for this session/type combination — skip silently
                    continue
                else:
                    raise e

            for entry in entries:
                if entry["type"] != "file":
                    continue
                name = entry["name"]
                if any(p in name for p in patterns):
                    rel_paths.append(f"{session}/{bids_dir}/{name}")
                elif bids_dir == "func" and name.endswith(".tsv"):
                    rel_paths.append(f"{session}/{bids_dir}/{name}")

    return rel_paths


def submit_transfer(
    transfer_client: globus_sdk.TransferClient,
    subject_id: str,
    source_collection_id: str,
    source_base_path: str,
    dest_collection_id: str,
    dest_base_path: str,
    rel_paths: list[str],
) -> str:
    label = f"cloudpipe-{subject_id}"

    # If a task with this label is already active (e.g. from a prior retry),
    # reuse it rather than submitting a duplicate that will conflict.
    for task in transfer_client.task_list(filter="status:ACTIVE,INACTIVE"):
        if task["label"] == label:
            task_id = task["task_id"]
            print(f"Reusing existing transfer task {task_id} ({task['status']})", flush=True)
            return task_id

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

    result = transfer_client.submit_transfer(tdata)
    task_id = result["task_id"]
    print(f"Submitted transfer task {task_id} ({len(rel_paths)} files)", flush=True)
    return task_id


def wait_for_transfer(transfer_client: globus_sdk.TransferClient, task_id: str) -> None:
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

        time.sleep(POLL_INTERVAL_SECS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover and transfer BIDS scan files for one subject via Globus."
    )
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--source-collection-id", required=True)
    parser.add_argument(
        "--source-base-path",
        required=True,
        help="Path to the subject root on the source collection, e.g. /abcd/raw/sub-NDARXXX",
    )
    parser.add_argument("--dest-collection-id", required=True)
    parser.add_argument(
        "--dest-base-path",
        required=True,
        help="Path to the subject root on the destination collection, e.g. /mmps_mproc/sub-NDARXXX",
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    native_app_client_id = os.environ["GLOBUS_NATIVE_APP_CLIENT_ID"]
    refresh_token = os.environ["GLOBUS_REFRESH_TOKEN"]

    scan_types = json.loads(args.scan_types)
    invalid = set(scan_types) - VALID_SCAN_TYPES
    if invalid:
        print(f"Unknown scan type(s): {invalid}. Valid: {sorted(VALID_SCAN_TYPES)}", file=sys.stderr)
        sys.exit(1)

    transfer_client = build_transfer_client(native_app_client_id, refresh_token)

    source_subject_root = f"{args.source_base_path.rstrip('/')}/{args.subject_id}"
    dest_subject_root   = f"{args.dest_base_path.rstrip('/')}/{args.subject_id}"

    print(f"Discovering files for subject {args.subject_id}, scan types: {scan_types}", flush=True)
    rel_paths = discover_files(
        transfer_client,
        source_collection_id=args.source_collection_id,
        source_base_path=source_subject_root,
        scan_types=scan_types,
    )

    if not rel_paths:
        print("No matching files found — nothing to transfer.", flush=True)
        sys.exit(0)

    print(f"Discovered {len(rel_paths)} file(s) across all sessions.", flush=True)

    task_id = submit_transfer(
        transfer_client,
        subject_id=args.subject_id,
        source_collection_id=args.source_collection_id,
        source_base_path=source_subject_root,
        dest_collection_id=args.dest_collection_id,
        dest_base_path=dest_subject_root,
        rel_paths=rel_paths,
    )

    wait_for_transfer(transfer_client, task_id)
    print(f"Transfer complete for subject {args.subject_id}.", flush=True)


if __name__ == "__main__":
    main()
