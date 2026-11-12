"""Stage one subject from the NBDC source collection over HTTPS (openspec add-https-ingress-mode).

Runs as the `https-transfer` step of `ingress-mode=https`, once per subject, on the
ingress node pool. It writes the tree Globus ingress wrote —
`{dest-base-path}/{subject}/{session}/{dir}/{file}` in the data bucket — so
`ingress-verify`, inventory and everything after see the layout they always have.
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

**2. List with production's walk, then copy.** The HTTPS server cannot list
directories, so the file set comes from the Transfer API through
`listing.discover_files` — the filter Globus ingress used, and the same
`TRANSFER-SUMMARY` line it printed. `https_copy.Copier` then copies those exact
paths, one GET per file streamed into an S3 multipart upload, with the request
limit on the source only (its docstring says why that replaced rclone). The access
token is held in a request header in this process, never in a command line or a
log.

**3. Check every object's size against the listing.** The source cannot compute
checksums (`disable_verify: true`), and Globus never verified them either, so the
listed size is the integrity check. A missing or short object fails the step. A
copy killed midway leaves no partial object visible (plan 018 T3), and a retry
copies only what is missing.

**Passes copy only what is missing.** The source fails individual requests with
HTTP 500, intermittently and all the time (wave 1, 2026-10-21: in 99 of 153
minutes with 500s, other pods were copying fine). With rclone, a whole-copy retry
sent a HEAD for every listed file again: a pass that lost 3 files sent 113 HEADs into the
struggling server and, in a bad minute, failed them all. So this step makes up to
four passes, each against a file list rebuilt from what S3 actually holds, and the
copier retries a file only a few times within a pass. A pod retried by Argo starts
the same way, so it never re-requests a file an earlier attempt staged. A file
staged at the wrong size is deleted before the pass. The copier logs each failed
file's status and JSON error body (it carries the request ID the source's operators
need); after a pass that fails, one file still missing is also probed directly
(HEAD, then a 1-byte GET), with timing and safe headers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import boto3
import globus_sdk
import https_copy
import listing
import requests
from botocore.config import Config

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
#: Waits before each further pass. Each pass requests only the files still
#: missing, so a wait costs time, never load. Short, because the source's 500s
#: are fast per-request rejections that fail independently (the #846 probes:
#: ~220 ms, a HEAD and a GET on one file failing apart), not minutes-long
#: outages: at ~14% of requests failing, two quick retries leave ~0.3%.
PASS_SLEEPS_SECS = (15, 30, 60)
PROBE_TIMEOUT_SECS = 90
#: Response headers worth logging from a probe. Others are logged by name only,
#: so a cookie or session value never reaches a log.
PROBE_HEADERS_SHOWN = {
    "content-type",
    "content-length",
    "content-range",
    "retry-after",
    "server",
    "date",
    "x-request-id",
    "x-amzn-requestid",
    "www-authenticate",
    "location",
}


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

    `listing.discover_files` keeps paths only. Wrapping the client, rather than
    copying the walk, keeps the file set exactly the one the listing decides.
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
    discovery = listing.discover_files(
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


#: Our load on the NBDC source per copy lives in https_copy (SOURCE_CONCURRENCY,
#: SOURCE_REQUESTS_PER_SEC). tests/argo/test_nbdc_concurrency.py holds each, times
#: the https-transfer slots, to the operator's ceiling of 8.

#: Connections to S3 per process: SOURCE_CONCURRENCY files x their part uploads,
#: plus the listing and size check. botocore's default pool of 10 would make part
#: uploads wait on each other.
S3_MAX_POOL_CONNECTIONS = 32


def probe_source(
    url: str,
    token: str,
    *,
    agent: str,
    http: Any = requests,
    acquire: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    """What the source answers for one file, with timing: a HEAD, then a 1-byte GET.

    `X-Requested-With` makes the server answer an error as JSON rather than a
    browser redirect. A body is read only when it is JSON (Globus's error shape),
    so a GET that unexpectedly succeeds brings at most one byte of file content.
    The token is sent, never returned. `acquire` runs before each request: the step
    passes the copier's rate limiter, so the probe counts against the same limit.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": agent,
    }
    out: dict[str, Any] = {}
    for method, extra in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        acquire()
        started = datetime.now(UTC)
        try:
            response = http.request(
                method,
                url,
                headers={**headers, **extra},
                allow_redirects=False,
                timeout=PROBE_TIMEOUT_SECS,
                stream=True,
            )
        except requests.RequestException as exc:
            out[method] = {
                "error": type(exc).__name__,
                "elapsed_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
            }
            continue
        with response:
            elapsed_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
            result: dict[str, Any] = {
                "status": response.status_code,
                "elapsed_ms": elapsed_ms,
                "headers": {
                    k: v for k, v in response.headers.items() if k.lower() in PROBE_HEADERS_SHOWN
                },
                "other_header_names": sorted(
                    k for k in response.headers if k.lower() not in PROBE_HEADERS_SHOWN
                ),
            }
            if method == "GET" and "json" in response.headers.get("Content-Type", ""):
                result["body"] = response.text[:2000]
            out[method] = result
    return out


def copy_missing(
    *,
    sizes: dict[str, int],
    s3: Any,
    bucket: str,
    dest_prefix: str,
    copy: Callable[[list[str]], list[https_copy.Failure]],
    probe: Callable[[str], dict[str, Any]],
    sleeper: Callable[[float], None],
    log: Callable[[str], None],
) -> int:
    """Up to four passes, each over only the files S3 does not yet hold.

    Returns how many files the last pass failed (0 when nothing was left to copy).
    The size check that follows is what decides the step.
    """
    failed = 0
    for attempt in range(len(PASS_SLEEPS_SECS) + 1):
        staged = staged_sizes(s3, bucket, dest_prefix)
        todo = still_missing(sizes, staged)
        if not todo:
            if attempt:
                log(f"Pass {attempt} left nothing missing.")
            return 0
        if attempt:
            wait = PASS_SLEEPS_SECS[attempt - 1]
            log(f"{len(todo)} file(s) still missing; pass {attempt + 1} in {wait} s.")
            sleeper(wait)
        elif len(todo) < len(sizes):
            log(f"{len(sizes) - len(todo)} of {len(sizes)} file(s) already staged.")
        # A file staged at the wrong size is a stream the source cut short. rclone
        # (before the copier) uploaded such a stream as complete and then skipped it
        # as present on every pass and every Argo attempt (wave 2, 2026-10-24: 3
        # subjects). The copier deletes its own short uploads, but an object can
        # still be left short by an earlier image or a killed pod, so deleting it
        # here makes it missing again and the pass fetches it whole.
        short = [rel for rel in todo if rel in staged]
        if short:
            log(
                f"{len(short)} file(s) staged short; deleting them before the copy: "
                + ", ".join(f"{rel} ({staged[rel]} of {sizes[rel]} bytes)" for rel in short)
            )
            delete_staged(s3, bucket, dest_prefix, short)
        log(
            f"Pass {attempt + 1}: copying {len(todo)} file(s), "
            f"{sum(sizes[rel] for rel in todo) / 2**30:.2f} GiB, over HTTPS."
        )
        failed = len(copy(todo))
        if failed:
            left = still_missing(sizes, staged_sizes(s3, bucket, dest_prefix))
            log(f"{failed} file(s) failed this pass; {len(left)} file(s) still missing.")
            if left:
                log(f"Source probe for {left[0]}: {json.dumps(probe(left[0]))}")
    return failed


# ---------------------------------------------------------------------------
# Phase 3: every object, at its listed size
# ---------------------------------------------------------------------------


def staged_sizes(s3: Any, bucket: str, prefix: str) -> dict[str, int]:
    """Every object under the subject's prefix, by path relative to it."""
    staged: dict[str, int] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for obj in page.get("Contents", []):
            staged[obj["Key"][len(prefix) + 1 :]] = int(obj["Size"])
    return staged


def still_missing(sizes: dict[str, int], staged: dict[str, int]) -> list[str]:
    """Listed files not yet in S3 at their listed size, in listing order."""
    return [rel for rel, size in sizes.items() if staged.get(rel) != size]


def delete_staged(s3: Any, bucket: str, prefix: str, rels: list[str]) -> None:
    """Delete these listed files' staged copies, at most 1,000 keys per request."""
    keys = [f"{prefix}/{rel}" for rel in rels]
    for start in range(0, len(keys), 1000):
        response = s3.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in keys[start : start + 1000]], "Quiet": True},
        )
        errors = response.get("Errors") or []
        if errors:
            first = errors[0]
            raise RuntimeError(
                f"could not delete {len(errors)} short object(s), e.g. {first.get('Key')}: "
                f"{first.get('Code')} {first.get('Message')}"
            )


def verify_sizes(s3: Any, bucket: str, prefix: str, sizes: dict[str, int]) -> None:
    """Fail on any listed file missing from S3, or present at another size.

    Objects under the prefix that the listing does not name are left alone: a
    rerun with different scan types may have put them there.
    """
    staged = staged_sizes(s3, bucket, prefix)
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
    parser.add_argument(
        "--scan-types",
        required=True,
        help=f"JSON array of scan types; valid: {sorted(listing.VALID_SCAN_TYPES)}",
    )
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
    make_copier: Callable[..., Any] = https_copy.Copier,
    log: Callable[[str], None] = _log,
    sleeper: Callable[[float], None] = time.sleep,
    prober: Callable[..., dict[str, Any]] = probe_source,
) -> None:
    scan_types = json.loads(args.scan_types)
    invalid = set(scan_types) - listing.VALID_SCAN_TYPES
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
    listing.emit_summary(discovery, log=log)
    if not sizes:
        raise RuntimeError(
            "No matching files found for any requested scan type — check source-base-path "
            "and that subject data exists on the source collection."
        )
    https_server = transfer_client.get_endpoint(args.source_collection_id)["https_server"]
    token = https_access_token(
        auth_client, env["GLOBUS_HTTPS_REFRESH_TOKEN"], args.source_collection_id
    )

    agent = user_agent(env.get("INGRESS_CONTACT"))
    file_url = https_server.rstrip("/") + quote(source_root.rstrip("/")) + "/"
    # One copier for every pass, so one rate limiter covers every request this pod
    # sends the source, the probe's included.
    copier = make_copier(
        base_url=file_url,
        token=token,
        user_agent=agent,
        s3=s3,
        bucket=args.dest_bucket,
        dest_prefix=dest_prefix,
        log=log,
    )
    failed = copy_missing(
        sizes=sizes,
        s3=s3,
        bucket=args.dest_bucket,
        dest_prefix=dest_prefix,
        copy=lambda todo: copier.copy(todo, sizes),
        probe=lambda rel: prober(
            file_url + quote(rel), token, agent=agent, acquire=copier.limiter.acquire
        ),
        sleeper=sleeper,
        log=log,
    )

    # Phase 3. The sizes decide the step; the copier's failures only explain it.
    try:
        verify_sizes(s3, args.dest_bucket, dest_prefix, sizes)
    except RuntimeError as exc:
        if failed:
            raise RuntimeError(f"{failed} file(s) failed on the last pass; {exc}") from None
        raise
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
            transfer_client=listing.build_transfer_client(
                env["GLOBUS_NATIVE_APP_CLIENT_ID"], env["GLOBUS_REFRESH_TOKEN"]
            ),
            auth_client=globus_sdk.NativeAppAuthClient(env["GLOBUS_NATIVE_APP_CLIENT_ID"]),
            s3=boto3.client("s3", config=Config(max_pool_connections=S3_MAX_POOL_CONNECTIONS)),
        )
    except Refused as err:
        print(f"error: {err}", file=sys.stderr, flush=True)
        sys.exit(EXIT_REFUSED)
    except CliError as err:
        listing.report_error(err)
        sys.exit(1)
    except globus_sdk.TransferAPIError as exc:
        listing.report_error(globus_error(exc))
        sys.exit(1)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
