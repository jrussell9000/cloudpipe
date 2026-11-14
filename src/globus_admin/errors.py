"""Translate known failure signatures into a next step.

Each of these cost someone real time at least once, and every one of them reads
as something other than what it is:

* A lapsed Globus session arrives as `not_from_allowed_domain`, which reads as a
  domain misconfiguration. It sent people to inspect `--domain` and identity
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


def globus_error(exc: Exception, *, context: str = "") -> CliError:
    """Translate a Globus SDK error. Unrecognized errors keep their text and exit 3."""
    payload = _globus_payload(exc)
    code = str(payload.get("code") or getattr(exc, "code", "") or "")
    message = str(payload.get("message") or getattr(exc, "message", "") or str(exc))
    raw = _raw_text(exc)
    auth_params = payload.get("authorization_parameters") or {}
    detail = payload.get("detail") or {}
    detail_type = detail.get("DATA_TYPE", "") if isinstance(detail, dict) else ""

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
            remedy=Remedy("command", LOGIN_COMMAND),
            gate="globus_login",
            raw=raw,
            detail={"allowed_domains": list(domains)},
        )

    if wrong_domain:
        return CliError(
            "globus.identity_wrong_domain",
            "The identity used is outside the domains the source collection's High "
            "Assurance policy allows. "
            "This is an identity problem, not an expired session — there is no session "
            "requirement in the response to satisfy.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("human", "log in with an identity in the collection's allowed domain"),
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

    return CliError(
        "globus.error",
        message or "Globus returned an error.",
        exit_code=ExitCode.CHECK_FAILED,
        raw=raw,
    )


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
    authorization error. Nothing here reasons about the AWS Client VPN: it was
    removed on 2026-11-06 (#359), and WARP is the only path.
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
