"""Talking to Globus with the tokens this deployment stores.

Two calls, the same two the Prefect batch gate makes before it submits anything
(`prefect/flows/lib/globus_session.py`): a listing of the source base path with
the transfer token, and one refresh of the HTTPS token for the source
collection's scope. `doctor` makes them so an operator sees what the gate will
see. The task listing and cancelling went with the Globus Transfer mode
(openspec retire-globus-host).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from . import errors
from .exits import CliError, ExitCode, Remedy

#: Globus picks one of the source collection's data nodes for each `ls`, and with
#: one node down about half of all listings fail with a 502
#: `ExternalError.DirListingFailed` (2026-11-07..09). The same retry as the
#: ingress pods' listing and the batch gate: that one error, six attempts, 2 s apart.
LS_ATTEMPTS = 6
LS_RETRY_SECS = 2.0

LOGIN = "pixi run globus login"


def build_transfer_client(secret: dict[str, Any] | None) -> Any:
    """Build a TransferClient from the stored `{native-app-client-id, refresh-token}`."""
    if not secret:
        raise CliError(
            "globus.token_missing",
            "No Globus refresh token is stored for this deployment.",
            exit_code=ExitCode.BLOCKED,
            remedy=Remedy("command", LOGIN),
            gate="globus_login",
        )

    client_id = secret.get("native-app-client-id")
    refresh_token = secret.get("refresh-token")
    if not client_id or not refresh_token:
        raise CliError(
            "globus.token_incomplete",
            "The stored Globus credential is missing native-app-client-id or refresh-token.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("command", LOGIN),
        )

    import globus_sdk

    native_client = globus_sdk.NativeAppAuthClient(str(client_id))
    authorizer = globus_sdk.RefreshTokenAuthorizer(str(refresh_token), native_client)
    return globus_sdk.TransferClient(authorizer=authorizer)


def _is_listing_node_failure(exc: Exception) -> bool:
    return getattr(exc, "http_status", None) == 502 and str(
        getattr(exc, "code", "") or ""
    ).startswith("ExternalError.DirListingFailed")


def source_listable(
    transfer_client: Any,
    collection_id: str,
    path: str,
    *,
    attempts: int = LS_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """List the source base path, as the batch gate does. Returns the attempts taken.

    A 404 is a failure, not a pass: the base path is where every subject is read
    from, so its absence fails every workflow. A listing that lands on a failed
    source node is retried; any other error is raised at once, translated.
    """
    for attempt in range(1, attempts + 1):
        try:
            transfer_client.operation_ls(collection_id, path=path)
        except Exception as exc:
            if _is_listing_node_failure(exc) and attempt < attempts:
                sleep(LS_RETRY_SECS)
                continue
            if getattr(exc, "http_status", None) == 404:
                raise CliError(
                    "globus.source_path_missing",
                    f"The source base path {path} does not exist on collection {collection_id}, "
                    "so no subject could be read.",
                    exit_code=ExitCode.CHECK_FAILED,
                    remedy=Remedy(
                        "human",
                        "check the source-base-path SSM parameter (Terraform's "
                        "globus_source_base_path) against the data provider's layout",
                    ),
                    raw=str(exc),
                ) from exc
            raise errors.globus_error(exc) from exc
        return attempt
    raise AssertionError("unreachable")


def https_token_refreshes(
    secret: dict[str, Any] | None, collection_id: str, *, auth_client: Any = None
) -> None:
    """One refresh of the stored HTTPS token, for the source collection's scope.

    The same check the batch gate makes and the same thing every ingress pod
    needs: an access token for the source collection's resource server. Raises a
    translated CliError on any failure; returns nothing on success, and never
    returns or logs a token.
    """
    refresh_token = (secret or {}).get("https-refresh-token")
    if not refresh_token:
        raise CliError(
            "globus.https_token_missing",
            "The stored Globus credential has no https-refresh-token, so no ingress pod "
            "could read the source over HTTPS.",
            exit_code=ExitCode.BLOCKED,
            remedy=Remedy("command", LOGIN),
            gate="globus_login",
        )
    if auth_client is None:
        import globus_sdk

        client_id = (secret or {}).get("native-app-client-id")
        if not client_id:
            raise CliError(
                "globus.token_incomplete",
                "The stored Globus credential is missing native-app-client-id.",
                exit_code=ExitCode.CHECK_FAILED,
                remedy=Remedy("command", LOGIN),
            )
        auth_client = globus_sdk.NativeAppAuthClient(str(client_id))
    try:
        response = auth_client.oauth2_refresh_token(str(refresh_token))
    except Exception as exc:
        raise CliError(
            "globus.https_refresh_refused",
            "Globus refused the stored HTTPS refresh token, so every ingress pod would be "
            "refused too.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("command", LOGIN),
            raw=str(exc),
        ) from exc
    tokens = response.by_resource_server.get(collection_id) or {}
    if not tokens.get("access_token"):
        raise CliError(
            "globus.https_no_grant",
            f"The HTTPS refresh token carries no grant for source collection {collection_id}.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy("command", LOGIN),
        )
