"""List one subject's BIDS files on the source collection through the Transfer API.

This is the listing HTTPS ingress (`https_ingress.py`) uses to decide what to
copy: the HTTPS server cannot list directories, so the file set comes from a
Transfer API walk filtered by scan type. It is the same walk and the same filter
Globus ingress used, moved here when Globus ingress was retired (openspec
retire-globus-host). Two behaviours here are deliberate and each has a failure
behind it:

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

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import globus_sdk

from globus_admin.errors import globus_error
from globus_admin.exits import CliError

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
    `func/` that names no task at all still travels (events files for the
    requested runs name their task; this keeps any that do not). One that names
    another task does not: every subject carries motion and events files for
    `task-mid` and `task-sst`, which nothing here processes, and each is a request
    on the source (28 of 113 files for a typical subject; 2026-10-22, when the
    source was rejecting requests under load).
    """
    matched = 0
    for entry in entries:
        if entry["type"] != "file":
            continue
        name = entry["name"]
        types_here = [scan_type for scan_type, pattern in patterns if pattern in name]
        taskless_tsv = bids_dir == FUNCTIONAL_DIR and name.endswith(".tsv") and "_task-" not in name
        if not types_here and not taskless_tsv:
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
# The client and the error reporter
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
