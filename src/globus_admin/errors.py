"""Translate known failure signatures into a next step.

Each of these cost someone real time at least once, and every one of them reads
as something other than what it is:

* A lapsed Globus session arrives as `not_from_allowed_domain`, which reads as a
  gateway misconfiguration. It sent people to inspect `--domain` and identity
  mapping while a whole 300-subject batch died at pre-flight (2026-08-17). The
  discriminator is `authorization_parameters.session_required_single_domain`: a
  genuinely wrong-domain identity has no session requirement to satisfy.
* A lapsed Cloudflare WARP session arrives as a TLS handshake timeout, which
  reads as a cluster outage.
* An expired AWS SSO login arrives as a botocore token error deep in a traceback.

Translation never replaces the raw error — `CliError.raw` keeps it, and the
Emitter always prints it. A novel failure must stay debuggable.
"""

from __future__ import annotations

import json
from typing import Any

from .exits import CliError, ExitCode, Remedy

LOGIN_COMMAND = "pixi run globus login"
WARP_REAUTH_COMMAND = "warp-cli debug access-reauth"


def globus_error(exc: Exception, *, env: str = "production", context: str = "") -> CliError:
    """Translate a Globus SDK error. Unrecognized errors keep their text and exit 3."""
    payload = _globus_payload(exc)
    code = str(payload.get("code") or getattr(exc, "code", "") or "")
    message = str(payload.get("message") or getattr(exc, "message", "") or str(exc))
    raw = _raw_text(exc)
    auth_params = payload.get("authorization_parameters") or {}
    detail = payload.get("detail") or {}
    detail_type = detail.get("DATA_TYPE", "") if isinstance(detail, dict) else ""
    env_flag = "" if env == "production" else f" --env {env}"

    wrong_domain = "not_from_allowed_domain" in f"{detail_type} {code} {message}"
    session_required = bool(
        auth_params.get("session_required_single_domain")
        or auth_params.get("session_required_identities")
        or auth_params.get("session_required_policies")
    )

    if session_required or (wrong_domain and session_required):
        domains = auth_params.get("session_required_single_domain") or []
        domain_text = f" ({', '.join(domains)})" if domains else ""
        return CliError(
            "globus.session_expired",
            "The Globus session behind the stored refresh token has expired"
            f"{domain_text}. Refreshing the token cannot fix this — Globus sessions are "
            "extended by an interactive login, not by a token refresh.",
            exit_code=ExitCode.BLOCKED,
            remedy=Remedy("command", f"{LOGIN_COMMAND}{env_flag}"),
            gate="globus_login",
            raw=raw,
            detail={"allowed_domains": list(domains)},
        )

    if wrong_domain:
        return CliError(
            "globus.identity_wrong_domain",
            "The identity used is outside the domains this storage gateway allows. "
            "This is an identity problem, not an expired session — there is no session "
            "requirement in the response to satisfy.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("human", "log in with an identity in the gateway's allowed domain"),
            raw=raw,
        )

    # The code is kept; the remedy is not. It used to name `rotate-s3-key`, which
    # was never built (`simplify-globus-ingress` 9.2, superseded) — and this is the
    # path a transfer pod takes, so the pod log was telling its reader to run a
    # command that does not exist. Behind a signing listener the registered key
    # authenticates nothing, so the cause is one of several that only `doctor` can
    # tell apart (check 12 for the role); guessing between them here would be wrong
    # as often as right.
    if "invalid_credential" in f"{code} {message}" or "requires some initial setup" in message:
        return CliError(
            "globus.s3_credential_invalid",
            "The storage gateway could not authenticate to S3. Behind a signing listener "
            "that means the listener is down, its role cannot be assumed, or no key pair is "
            "registered for this identity; on a gateway still using a static key, the key "
            "itself may be missing or invalid.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("command", f"pixi run globus doctor{env_flag}"),
            raw=raw,
        )

    if "Bucket not allowed" in message:
        return CliError(
            "globus.gateway_path_misconfigured",
            "The gateway rejected the path's first component as a bucket name. The "
            "collection must be rooted at /<bucket>, and the gateway created with "
            "--no-allow-multiple-keys.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("command", f"pixi run globus configure{env_flag}"),
            raw=raw,
        )

    if code.startswith("ClientError.NotFound") or getattr(exc, "http_status", None) == 404:
        subject_hint = f" ({context})" if context else ""
        return CliError(
            "globus.not_found_at_source",
            f"The path was not found on the source collection{subject_hint}. For a subject "
            "root this means the subject is not present at the source, not that the "
            "transfer is broken.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("human", "check the subject id and the source base path"),
            raw=raw,
        )

    # Checked last, so every classification above wins: a lapsed session and a
    # rejected credential are both more specific readings of a failed login than
    # "the connection ended", and a response can carry both.
    if _is_transport_eof(f"{code} {message} {raw}"):
        return CliError(
            GRIDFTP_UNAVAILABLE,
            "The collection's GridFTP server closed the connection during login. That is "
            "the server going away mid-conversation, not a credential being refused — a "
            "refusal names the identity or the domain. On an instance that has just been "
            "started, expect this for a minute or so: GridFTP restarts several times while "
            "the node re-registers, and it accepts TCP connections throughout.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("human", "if the instance has just started, wait a minute and retry"),
            raw=raw,
        )

    return CliError(
        "globus.error",
        message or "Globus returned an error.",
        exit_code=ExitCode.CHECK_FAILED,
        raw=raw,
    )


#: A login that ended rather than answered. `doctor` waits on this code, which is
#: why it is a named constant and why the markers below are narrow: anything folded
#: in here becomes something the checklist sits and waits on, and waiting fixes only
#: a server that is still coming up.
GRIDFTP_UNAVAILABLE = "globus.gridftp_unavailable"

#: Two spellings because two layers produce them: the GridFTP control channel says
#: "an end-of-file was reached", and globus_xio underneath it says "An end of file
#: occurred". A real failure has shown both in one message, but neither alone can be
#: assumed — matching only one would leave the other reading as an ordinary error.
_TRANSPORT_EOF_MARKERS = (
    "an end-of-file was reached",
    "an end of file occurred",
)


def _is_transport_eof(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _TRANSPORT_EOF_MARKERS)


def aws_error(exc: Exception, *, profile: str | None = None) -> CliError:
    """Translate an AWS credential failure. An expired SSO login is a human gate, not an error."""
    name = type(exc).__name__
    text = str(exc)
    profile_flag = f" --profile {profile}" if profile else ""

    if name in ("UnauthorizedSSOTokenError", "SSOTokenLoadError") or "sso" in text.lower():
        return CliError(
            "aws.sso_login_expired",
            "The AWS SSO login for this profile has expired.",
            exit_code=ExitCode.BLOCKED,
            remedy=Remedy("command", f"aws sso login{profile_flag}"),
            gate="aws_sso_login",
            raw=text,
        )

    if name in ("NoCredentialsError", "ProfileNotFound", "NoRegionError"):
        return CliError(
            "aws.credentials_missing",
            "No usable AWS credentials or region were found in this environment.",
            exit_code=ExitCode.BLOCKED,
            remedy=Remedy("command", "aws configure sso"),
            gate="aws_sso_login",
            raw=text,
        )

    return CliError("aws.error", text or name, exit_code=ExitCode.CHECK_FAILED, raw=text)


def cluster_error(detail: str) -> CliError:
    """Translate a failure to reach the EKS API.

    The private endpoint is reached over Cloudflare WARP, and a lapsed WARP
    session (24 hours) surfaces as a TLS handshake timeout rather than an
    authorization error. Nothing here reasons about the AWS Client VPN: it is a
    fallback pending removal (#359), and code that knows about it would have to
    be unpicked when it goes.
    """
    lowered = detail.lower()
    handshake = "handshake timeout" in lowered or "tls handshake" in lowered
    refused = "i/o timeout" in lowered or "connection refused" in lowered or "timed out" in lowered

    if handshake or refused:
        return CliError(
            "cluster.warp_not_connected",
            "The EKS API did not answer. Its endpoint is private and reached over "
            "Cloudflare WARP, whose session lasts 24 hours; a lapsed session looks "
            "exactly like this, so it is far more likely than a cluster outage.",
            exit_code=ExitCode.BLOCKED,
            remedy=Remedy("command", WARP_REAUTH_COMMAND),
            gate="cluster_access",
            raw=detail,
        )

    return CliError(
        "cluster.unreachable",
        "Could not reach the Kubernetes API.",
        exit_code=ExitCode.BLOCKED,
        remedy=Remedy("command", WARP_REAUTH_COMMAND),
        gate="cluster_access",
        raw=detail,
    )


def _globus_payload(exc: Exception) -> dict[str, Any]:
    """Best-effort extraction of a Globus error body from an SDK exception.

    Duck-typed rather than isinstance'd against `globus_sdk.TransferAPIError`, so
    the table can be unit-tested with recorded payloads and no SDK objects.
    """
    for attr in ("raw_json", "_raw_json"):
        value = getattr(exc, attr, None)
        if isinstance(value, dict):
            return value
    text = getattr(exc, "text", None) or getattr(exc, "raw_text", None)
    if isinstance(text, str) and text.strip().startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _raw_text(exc: Exception) -> str:
    text = getattr(exc, "text", None) or getattr(exc, "raw_text", None)
    if isinstance(text, str) and text:
        return text
    return str(exc)
