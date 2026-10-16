"""Stage one subject from the NBDC source collection over HTTPS (openspec add-https-ingress-mode).

Runs as the `https-transfer` step of `ingress-mode=https`, once per subject, on the
ingress node pool. It writes the same tree Globus ingress writes —
`{dest-base-path}/{subject}/{session}/{dir}/{file}` in the data bucket — so
`ingress-verify`, inventory and everything after cannot tell the modes apart.
Three phases, each with a failure behind it:

**1. Refuse a stale session before any request.** The source grants access to a
named person's Globus session, and this deployment's rule is that a session
older than 30 days is not used (operator decision, 2026-10-12). The source's own
timeout is a year, so nothing upstream would stop it. The pod reads the login
time `globus login` records and refuses — missing, unparseable, naive, in the
future or over 30 days — with exit code 78 (`EX_CONFIG`). The step's retry
expression excludes 78, because a refusal fails identically on every attempt.
The Prefect gate refuses a batch earlier; this is the control that also holds
for a hand `argo submit`.

**2. List with production's walk, copy with rclone.** The HTTPS server cannot list
directories, so the file set comes from the Transfer API through
`transfer.discover_files` — the same filter and the same `TRANSFER-SUMMARY` line
as Globus ingress. rclone then copies those exact paths. The access token reaches
it only through `RCLONE_HTTP_HEADERS`, never its command line, so it appears in
no process listing and no log. `no_check_bucket` stops rclone calling
`CreateBucket` before its first upload, which failed with
`IllegalLocationConstraintException` (plan 018, first T3 pod).

**3. Check every object's size against the listing.** The source cannot compute
checksums (`disable_verify: true`), and Globus never verified them either, so the
listed size is the integrity check. A missing or short object fails the step. A
copy killed midway leaves no partial object visible (plan 018 T3), and a retry
copies only what is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import boto3
import globus_sdk
import transfer

from globus_admin.errors import globus_error
from globus_admin.exits import CliError

#: Exit code for a refused session. Not in the retried set (the canonical retry
#: expressions admit 64/75/143), and excluded explicitly by the step's own.
EXIT_REFUSED = 78
MAX_SESSION_DAYS = 30
#: A login timestamp more than this far in the future is a clock or data error,
#: not a session to trust.
FUTURE_TOLERANCE = timedelta(minutes=5)
SESSION_PARAM = "/cloudpipe/globus/session-established-at"
LOGIN_COMMAND = "pixi run globus login"


class Refused(Exception):
    """A session this step must not use. Exits EXIT_REFUSED, never retried."""


def _log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# Phase 1: the 30-day rule
# ---------------------------------------------------------------------------


def check_session(
    established_at: str | None, *, now: datetime, max_days: int = MAX_SESSION_DAYS
) -> timedelta:
    """The session's age, or Refused. Fails closed on anything it cannot read."""
    remedy = f"Renew it with `{LOGIN_COMMAND}`."
    if not established_at or not established_at.strip():
        raise Refused(f"No Globus session time is recorded ({SESSION_PARAM} is empty). {remedy}")
    value = established_at.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Refused(
            f"{SESSION_PARAM} holds {value!r}, which is not a time — no login has been "
            f"recorded since it was created. {remedy}"
        ) from None
    if parsed.tzinfo is None:
        raise Refused(f"{SESSION_PARAM} holds {value!r}, a time with no zone. {remedy}")
    age = now - parsed
    if age < -FUTURE_TOLERANCE:
        raise Refused(f"{SESSION_PARAM} is in the future ({value}); refusing to trust it. {remedy}")
    if age > timedelta(days=max_days):
        raise Refused(
            f"The Globus session is {age.days} days old, past this deployment's "
            f"{max_days}-day rule (established {value}). {remedy}"
        )
    return age


# ---------------------------------------------------------------------------
# Phase 2: list, mint a token, copy
# ---------------------------------------------------------------------------


class RecordingTransferClient:
    """Passes `operation_ls` through and keeps every file's listed size.

    `transfer.discover_files` keeps paths only. Wrapping the client, rather than
    copying the walk, keeps the file set exactly the one Globus ingress transfers.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.sizes: dict[str, int] = {}

    def operation_ls(self, collection_id: str, path: str) -> list[dict[str, Any]]:
        entries = list(self.inner.operation_ls(collection_id, path=path))
        for entry in entries:
            if entry["type"] == "file":
                self.sizes[f"{path.rstrip('/')}/{entry['name']}"] = int(entry["size"])
        return entries

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def list_subject(
    transfer_client: Any, source_id: str, subject_root: str, scan_types: list[str], subject_id: str
) -> tuple[Any, dict[str, int]]:
    recorder = RecordingTransferClient(transfer_client)
    discovery = transfer.discover_files(
        recorder, source_id, subject_root, scan_types, subject_id=subject_id
    )
    sizes = {rel: recorder.sizes[f"{subject_root}/{rel}"] for rel in discovery.rel_paths}
    return discovery, sizes


def https_access_token(auth_client: Any, refresh_token: str, collection_id: str) -> str:
    """An access token for the source's `/https` scope, from the stored refresh token."""
    try:
        response = auth_client.oauth2_refresh_token(refresh_token)
    except globus_sdk.AuthAPIError as exc:
        raise Refused(
            f"Globus refused the HTTPS refresh token ({exc.http_status} {exc.code}). "
            f"Grant the scope again with `{LOGIN_COMMAND}`."
        ) from None
    tokens = response.by_resource_server.get(collection_id)
    if not tokens or not tokens.get("access_token"):
        raise Refused(
            "The HTTPS refresh token carries no grant for the source collection. "
            f"Grant the scope again with `{LOGIN_COMMAND}`."
        )
    return str(tokens["access_token"])


# What the source's operators see in their logs. Named, so the traffic is never an
# anonymous AWS address; the contact is the deployment's (cloudpipe-config
# `ingress_contact`), and is left off when unset or not a plain address.
USER_AGENT_PRODUCT = "cloudpipe-https-ingress/1"
_CONTACT_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$")


def user_agent(contact: str | None) -> str:
    contact = (contact or "").strip()
    if contact and _CONTACT_RE.match(contact):
        return f"{USER_AGENT_PRODUCT} (contact: {contact})"
    return USER_AGENT_PRODUCT


def rclone_command(
    *,
    https_server: str,
    subject_root: str,
    file_list: Path,
    bucket: str,
    dest_prefix: str,
    region: str,
    contact: str | None = None,
) -> list[str]:
    """The copy, with no secret in it: the token goes in rclone_env()."""
    dest = f":s3,env_auth,provider=AWS,region={region},no_check_bucket:{bucket}/{dest_prefix}/"
    return [
        "rclone",
        "copy",
        ":http:",
        dest,
        "--http-url",
        https_server.rstrip("/") + quote(subject_root.rstrip("/")) + "/",
        "--files-from",
        str(file_list),
        "--no-traverse",
        "--transfers",
        "8",
        "--multi-thread-streams",
        "0",
        "--s3-chunk-size",
        "16M",
        "--s3-upload-concurrency",
        "4",
        # Bounded, not 1 (2026-10-14): the source answers bursts of HTTP 500
        # on stat and open while all 8 ingress pods run (22 of 50 pilot
        # workflows, 20:38-20:55Z), and each failure used to cost the whole
        # pod plus a 1-2 min Argo backoff. Three passes 30 s apart retry only
        # the failed files, adding no load, and a refusal or throttle still
        # fails the step within about a minute (design.md, "One source IP").
        "--retries",
        "3",
        "--retries-sleep",
        "30s",
        "--low-level-retries",
        "10",
        "--stats",
        "30s",
        "--stats-one-line",
        "--user-agent",
        user_agent(contact),
        "-v",
    ]


def rclone_env(token: str, base: dict[str, str]) -> dict[str, str]:
    env = dict(base)
    env["RCLONE_HTTP_HEADERS"] = f"Authorization,Bearer {token}"
    # No config file is wanted; a path that does not exist keeps rclone from
    # looking in a home directory the pod's user does not have.
    env.setdefault("RCLONE_CONFIG", "/tmp/rclone-https-ingress.conf")
    return env


# ---------------------------------------------------------------------------
# Phase 3: every object, at its listed size
# ---------------------------------------------------------------------------


def verify_sizes(s3: Any, bucket: str, prefix: str, sizes: dict[str, int]) -> None:
    """Fail on any listed file missing from S3, or present at another size.

    Objects under the prefix that the listing does not name are left alone: a
    rerun with different scan types may have put them there.
    """
    staged: dict[str, int] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for obj in page.get("Contents", []):
            staged[obj["Key"][len(prefix) + 1 :]] = int(obj["Size"])
    missing = sorted(rel for rel in sizes if rel not in staged)
    wrong = sorted(rel for rel in sizes if rel in staged and staged[rel] != sizes[rel])
    if missing or wrong:
        lines = [f"  missing: {rel} (listed {sizes[rel]} bytes)" for rel in missing]
        lines += [f"  size: {rel} listed {sizes[rel]}, staged {staged[rel]}" for rel in wrong]
        raise RuntimeError(
            f"{len(missing)} file(s) missing and {len(wrong)} at the wrong size under "
            f"s3://{bucket}/{prefix}/:\n" + "\n".join(lines)
        )
    _log(f"Verified {len(sizes)} file(s) against the listed sizes.")


# ---------------------------------------------------------------------------
# The step
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--source-collection-id", required=True)
    parser.add_argument(
        "--source-base-path", required=True, help="Root on the source; the subject ID is appended"
    )
    parser.add_argument("--dest-bucket", required=True)
    parser.add_argument(
        "--dest-base-path",
        required=True,
        help="Prefix in the bucket, e.g. /mmps_mproc; the subject ID is appended",
    )
    parser.add_argument("--scan-types", required=True, help="JSON array, as for transfer.py")
    parser.add_argument("--session-param", default=SESSION_PARAM)
    parser.add_argument("--max-session-days", type=int, default=MAX_SESSION_DAYS)
    return parser.parse_args(argv)


def run(
    args: argparse.Namespace,
    *,
    env: dict[str, str],
    now: datetime,
    read_param: Callable[[str], str | None],
    transfer_client: Any,
    auth_client: Any,
    s3: Any,
    runner: Callable[..., Any] = subprocess.run,
    log: Callable[[str], None] = _log,
) -> None:
    scan_types = json.loads(args.scan_types)
    invalid = set(scan_types) - transfer.VALID_SCAN_TYPES
    if invalid:
        raise ValueError(f"Unknown scan type(s): {sorted(invalid)}")

    # Phase 1, before any request to Globus.
    age = check_session(read_param(args.session_param), now=now, max_days=args.max_session_days)
    log(f"Globus session is {age.days} day(s) old; the rule is {args.max_session_days}.")

    # Phase 2.
    source_root = f"{args.source_base_path.rstrip('/')}/{args.subject_id}"
    dest_prefix = f"{args.dest_base_path.strip('/')}/{args.subject_id}"
    discovery, sizes = list_subject(
        transfer_client, args.source_collection_id, source_root, scan_types, args.subject_id
    )
    transfer.emit_summary(discovery, log=log)
    if not sizes:
        raise RuntimeError(
            "No matching files found for any requested scan type — check source-base-path "
            "and that subject data exists on the source collection."
        )
    https_server = transfer_client.get_endpoint(args.source_collection_id)["https_server"]
    token = https_access_token(
        auth_client, env["GLOBUS_HTTPS_REFRESH_TOKEN"], args.source_collection_id
    )

    with tempfile.TemporaryDirectory() as scratch:
        file_list = Path(scratch) / "files.txt"
        file_list.write_text("".join(f"{rel}\n" for rel in sizes))
        command = rclone_command(
            https_server=https_server,
            subject_root=source_root,
            file_list=file_list,
            bucket=args.dest_bucket,
            dest_prefix=dest_prefix,
            region=env["AWS_DEFAULT_REGION"],
            contact=env.get("INGRESS_CONTACT"),
        )
        log(f"Copying {len(sizes)} file(s), {sum(sizes.values()) / 2**30:.2f} GiB, over HTTPS.")
        result = runner(command, env=rclone_env(token, env), check=False)
        if result.returncode != 0:
            raise RuntimeError(f"rclone exited {result.returncode}; see its log above")

    # Phase 3.
    verify_sizes(s3, args.dest_bucket, dest_prefix, sizes)
    log(f"HTTPS ingress complete for subject {args.subject_id}.")


def _read_param(name: str) -> str | None:
    """A missing parameter is "no session recorded" (refused), not a crash."""
    ssm = boto3.client("ssm")
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    env = dict(os.environ)
    try:
        for name in (
            "GLOBUS_NATIVE_APP_CLIENT_ID",
            "GLOBUS_REFRESH_TOKEN",
            "GLOBUS_HTTPS_REFRESH_TOKEN",
        ):
            if not env.get(name):
                raise Refused(
                    f"{name} is not set; the globus-credentials Secret lacks it. Run `{LOGIN_COMMAND}`."
                )
        run(
            args,
            env=env,
            now=datetime.now(UTC),
            read_param=_read_param,
            transfer_client=transfer.build_transfer_client(
                env["GLOBUS_NATIVE_APP_CLIENT_ID"], env["GLOBUS_REFRESH_TOKEN"]
            ),
            auth_client=globus_sdk.NativeAppAuthClient(env["GLOBUS_NATIVE_APP_CLIENT_ID"]),
            s3=boto3.client("s3"),
        )
    except Refused as err:
        print(f"error: {err}", file=sys.stderr, flush=True)
        sys.exit(EXIT_REFUSED)
    except CliError as err:
        transfer.report_error(err)
        sys.exit(1)
    except globus_sdk.TransferAPIError as exc:
        transfer.report_error(globus_error(exc))
        sys.exit(1)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
