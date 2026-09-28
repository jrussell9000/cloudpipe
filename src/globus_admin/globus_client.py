"""Talking to Globus with the token this deployment stores.

Task listing is label-driven: every task the pipeline submits is labelled
`cloudpipe-…`, and `cancel_transfers.py` matched that prefix. Keeping the prefix
match (rather than an exact label) is what lets one command clean up both the
legacy `cloudpipe-{subject}` labels and the run-scoped ones that replace them.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from . import errors
from .exits import CliError, ExitCode, Remedy

LABEL_PREFIX = "cloudpipe-"
ACTIVE_STATUSES = ("ACTIVE", "INACTIVE")


@dataclass(frozen=True)
class TransferTask:
    task_id: str
    label: str
    status: str
    request_time: str
    subject: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "label": self.label,
            "status": self.status,
            "request_time": self.request_time,
            "subject": self.subject,
        }


def build_transfer_client(secret: dict[str, Any] | None, *, env: str = "production") -> Any:
    """Build a TransferClient from the stored `{native-app-client-id, refresh-token}`."""
    if not secret:
        raise CliError(
            "globus.token_missing",
            "No Globus refresh token is stored for this environment.",
            exit_code=ExitCode.BLOCKED,
            remedy=Remedy(
                "command",
                "pixi run globus login" + ("" if env == "production" else f" --env {env}"),
            ),
            gate="globus_login",
        )

    client_id = secret.get("native-app-client-id")
    refresh_token = secret.get("refresh-token")
    if not client_id or not refresh_token:
        raise CliError(
            "globus.token_incomplete",
            "The stored Globus credential is missing native-app-client-id or refresh-token.",
            exit_code=ExitCode.CHECK_FAILED,
            remedy=Remedy(
                "command",
                "pixi run globus login" + ("" if env == "production" else f" --env {env}"),
            ),
        )

    import globus_sdk

    native_client = globus_sdk.NativeAppAuthClient(str(client_id))
    authorizer = globus_sdk.RefreshTokenAuthorizer(str(refresh_token), native_client)
    return globus_sdk.TransferClient(authorizer=authorizer)


def iter_pipeline_tasks(
    transfer_client: Any,
    *,
    statuses: tuple[str, ...] = ACTIVE_STATUSES,
    prefix: str = LABEL_PREFIX,
) -> Iterator[TransferTask]:
    """Every pipeline-labelled task in the given statuses, across all pages.

    Paginated deliberately: `transfer.py`'s single `task_list` call sees only the
    first page, so a large backlog can hide the very task being hunted.
    """
    for status in statuses:
        try:
            paginator = transfer_client.paginated.task_list(filter=f"status:{status}")
            pages = paginator.items() if hasattr(paginator, "items") else paginator
        except Exception as exc:
            raise errors.globus_error(exc) from exc
        for entry in _flatten(pages):
            label = str(entry.get("label") or "")
            if not label.startswith(prefix):
                continue
            yield TransferTask(
                task_id=str(entry.get("task_id", "")),
                label=label,
                status=str(entry.get("status", status)),
                request_time=str(entry.get("request_time", "")),
                subject=_subject_from_label(label, prefix),
            )


def cancel(transfer_client: Any, task_id: str) -> str:
    try:
        response = transfer_client.cancel_task(task_id)
    except Exception as exc:
        raise errors.globus_error(exc) from exc
    return str(response.get("code", "Cancelled"))


def collection_reachable(transfer_client: Any, collection_id: str, path: str) -> None:
    """One listing against a collection. A 404 passes: the path may not exist yet.

    This is the same pre-flight `transfer.py` does, and the check that turns a
    lapsed session into a clear message instead of 300 failed workflows.
    """
    try:
        transfer_client.operation_ls(collection_id, path=path)
    except Exception as exc:
        if getattr(exc, "http_status", None) == 404 or str(getattr(exc, "code", "")).startswith(
            "ClientError.NotFound"
        ):
            return
        raise errors.globus_error(exc) from exc


def live_timeout_minutes(transfer_client: Any, collection_id: str) -> int | None:
    """What the endpoint ACTUALLY enforces for this collection, or None.

    `get_endpoint` is how the 2026-09-15 measurement that started this work was
    taken, and it is the only reading that counts: the declared timeout is what
    the endpoint has been *asked* for, which is a different thing while
    production is `managed: false` or while the declaration names a gateway that
    does not exist.

    None on any failure, deliberately. This feeds a check that already has a
    fallback, and a session check that fails because a metadata call failed
    would be worse than one that says which number it used.
    """
    try:
        response = transfer_client.get_endpoint(collection_id)
    except Exception:
        return None
    value = response.get("authentication_timeout_mins") if hasattr(response, "get") else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _flatten(pages: Any) -> Iterator[dict[str, Any]]:
    for page in pages:
        if isinstance(page, dict) and "DATA" in page:
            yield from page["DATA"]
        elif isinstance(page, dict):
            yield page
        else:
            yield from page


def _subject_from_label(label: str, prefix: str) -> str | None:
    """Pull the subject out of `cloudpipe-sub-XXXX[-<run>]`.

    Subject ids contain a hyphen themselves, so this cannot just split on the
    first one — `cloudpipe-sub-ABC123-7f3e` must give `sub-ABC123`, not `sub`.
    """
    remainder = label[len(prefix) :]
    if not remainder:
        return None
    parts = remainder.split("-")
    if parts[0] == "sub" and len(parts) > 1:
        return f"sub-{parts[1]}"
    return parts[0]
