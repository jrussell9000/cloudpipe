"""Cancel all active/inactive Globus transfer tasks whose label starts with 'cloudpipe-'."""
import os
import sys

import globus_sdk


def build_transfer_client(native_app_client_id: str, refresh_token: str) -> globus_sdk.TransferClient:
    native_client = globus_sdk.NativeAppAuthClient(native_app_client_id)
    authorizer = globus_sdk.RefreshTokenAuthorizer(refresh_token, native_client)
    return globus_sdk.TransferClient(authorizer=authorizer)


def main() -> None:
    native_app_client_id = os.environ["GLOBUS_NATIVE_APP_CLIENT_ID"]
    refresh_token = os.environ["GLOBUS_REFRESH_TOKEN"]

    tc = build_transfer_client(native_app_client_id, refresh_token)

    # Collect all pages of ACTIVE and INACTIVE tasks.
    targets = []
    for status in ("ACTIVE", "INACTIVE"):
        paginator = tc.paginated.task_list(filter=f"status:{status}")
        for page in paginator:
            for task in page:
                if (task.get("label") or "").startswith("cloudpipe-"):
                    targets.append(task)

    if not targets:
        print("No active/inactive cloudpipe-* transfer tasks found.")
        sys.exit(0)

    print(f"Found {len(targets)} cloudpipe-* task(s) to cancel:")
    for t in targets:
        print(f"  {t['task_id']}  {t['label']}  status={t['status']}", flush=True)

    cancelled = 0
    for t in targets:
        try:
            tc.cancel_task(t["task_id"])
            print(f"  Cancelled {t['task_id']} ({t['label']})", flush=True)
            cancelled += 1
        except globus_sdk.TransferAPIError as e:
            print(f"  Failed to cancel {t['task_id']}: {e}", flush=True)

    print(f"\nCancelled {cancelled}/{len(targets)} tasks.")


if __name__ == "__main__":
    main()
