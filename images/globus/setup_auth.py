"""
One-time interactive script to generate a Globus refresh token and store it in
AWS Secrets Manager for use by cloudpipe workflows.

Run this locally (not in a container) before the workflow is used for the first time:

    export GLOBUS_NATIVE_APP_CLIENT_ID=<your-native-app-client-id>
    python setup_auth.py

Requires boto3 and globus-sdk. AWS credentials must be configured (e.g. via
~/.aws/credentials or environment variables) with secretsmanager write access.

After running, force an immediate sync of the Kubernetes secret:

    kubectl delete secret globus-credentials -n argo-workflows
    kubectl annotate externalsecret globus-credentials -n argo-workflows \
      force-sync=$(date +%s) --overwrite
"""

import argparse
import json
import os
import subprocess
import time

import boto3
import globus_sdk
from globus_sdk.scopes import GCSCollectionScopes


SECRET_ID = "globus/refresh-token"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a Globus refresh token and store it in AWS Secrets Manager."
    )
    parser.add_argument(
        "--client-id",
        metavar="UUID",
        help="Globus native app client UUID. Defaults to GLOBUS_NATIVE_APP_CLIENT_ID env var.",
    )
    parser.add_argument(
        "--dest-collection-id",
        metavar="UUID",
        help=(
            "UUID of the destination GCS mapped collection. "
            "Required when the destination is a non-High-Assurance GCS v5 mapped collection, "
            "which needs an explicit data_access scope in the token."
        ),
    )
    args = parser.parse_args()

    native_app_client_id = (
        args.client_id or os.environ.get("GLOBUS_NATIVE_APP_CLIENT_ID", "")
    ).strip()
    if not native_app_client_id:
        raise SystemExit(
            "Provide a client ID via --client-id or set GLOBUS_NATIVE_APP_CLIENT_ID."
        )

    if args.dest_collection_id:
        dest_collection_scopes = GCSCollectionScopes(args.dest_collection_id)
        transfer_scope = globus_sdk.Scope(
            str(globus_sdk.TransferClient.scopes.all)
        ).with_dependency(dest_collection_scopes.data_access)
        scopes = [transfer_scope, "openid profile email offline_access"]
    else:
        scopes = [globus_sdk.TransferClient.scopes.all, "openid profile email offline_access"]

    client = globus_sdk.NativeAppAuthClient(native_app_client_id)
    client.oauth2_start_flow(requested_scopes=scopes, refresh_tokens=True)

    # prompt=login forces a fresh authentication event, which is required for
    # High Assurance (HA) collections. Without it, Globus may reuse an existing
    # browser session that doesn't satisfy the HA session policy, resulting in a
    # "No effective ACL rules" 403 error when the token is used for transfer.
    authorize_url = client.oauth2_get_authorize_url(query_params={"prompt": "login"})
    print(f"\nAuthenticate at:\n\n  {authorize_url}\n")
    auth_code = input("Paste the auth code: ").strip()

    tokens = client.oauth2_exchange_code_for_tokens(auth_code)
    refresh_token = tokens.by_resource_server["transfer.api.globus.org"]["refresh_token"]

    secret_value = json.dumps({
        "native-app-client-id": native_app_client_id,
        "refresh-token": refresh_token,
    })

    sm = boto3.client("secretsmanager")
    try:
        sm.put_secret_value(SecretId=SECRET_ID, SecretString=secret_value)
        print(f"\nStored refresh token in Secrets Manager: {SECRET_ID}")
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(Name=SECRET_ID, SecretString=secret_value)
        print(f"\nCreated secret in Secrets Manager: {SECRET_ID}")

    print("\nForcing immediate sync of the Kubernetes secret...")
    subprocess.run(
        ["kubectl", "delete", "secret", "globus-credentials", "-n", "argo-workflows"],
        check=False,  # tolerate "not found" if secret doesn't exist yet
    )
    subprocess.run(
        [
            "kubectl", "annotate", "externalsecret", "globus-credentials",
            "-n", "argo-workflows",
            f"force-sync={int(time.time())}",
            "--overwrite",
        ],
        check=True,
    )
    print("Done. The globus-credentials secret will be recreated from Secrets Manager.")


if __name__ == "__main__":
    main()
