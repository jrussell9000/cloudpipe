# Globus Transfer Setup

This document describes the end-to-end process for deploying and configuring the Globus transfer workflow in cloudpipe. The workflow pulls BIDS data from a remote Globus collection (e.g., an ABCD data partner) into the cloudpipe S3 bucket.

## Architecture overview

```
Remote collection          Globus network          cloudpipe AWS
(source institution) ───────────────────────── GCS EC2 ──── S3 bucket
                                                  │
                                            mountpoint-s3
```

The destination side is a **Globus Connect Server v5** (GCS) EC2 instance (`c5n.xlarge`, Ubuntu 22.04) that exposes the cloudpipe S3 bucket as a Globus mapped collection via `mountpoint-s3`. The instance is stopped when idle and started on demand by the Argo workflow.

AWS resources created by the `terraform/modules/globus` module:

| Resource | Purpose |
|---|---|
| EC2 instance + Elastic IP | Globus Connect Server |
| Security group | Ports 443, 50000–51000 open to `0.0.0.0/0` (required by GCS v5), SSH restricted to admin prefix list |
| IAM role + instance profile | Grants the instance S3 read/write and SSM write access |
| `/{name}/globus/instance-id` (SSM) | Instance ID — read by Argo's start step |
| `/{name}/globus/collection-id` (SSM) | Collection UUID — written by `gcs-finalize-setup` after initial setup |
| EventBridge Scheduler | Stops (not terminates) the instance at midnight UTC as a safety net — the next workflow run starts it again automatically |
| Argo runner IAM policy | Grants Argo permission to start the instance and read both SSM parameters |

---

## Step 1: Set Terraform variables

The Globus module is part of the main cluster Terraform configuration — the EC2 instance is provisioned alongside the rest of the infrastructure on the initial `terraform apply`. There is no separate Globus-specific apply step.

Before the initial cluster deployment, add the following to `terraform/terraform.tfvars`:

```hcl
globus_s3_destination_bucket   = "your-s3-bucket-name"
globus_admin_prefix_list_id    = "pl-xxxxxxxxx"   # prefix list of IPs allowed SSH
globus_org_name                = "Your Organization"
globus_contact_email           = "admin@example.com"
globus_collection_name         = "cloudpipe-s3"   # display name in Globus web UI
```

`globus_org_name` and `globus_contact_email` have no defaults and must be present — `terraform apply` will fail without them.

After the cluster has been deployed, retrieve the Globus outputs:

```bash
terraform -chdir=terraform output globus_public_ip
terraform -chdir=terraform output globus_collection_id_ssm_parameter
```

The EC2 instance is running and the `user_data` first-boot script is executing (or has finished). Terraform stores a placeholder value (`REPLACE_AFTER_GCS_SETUP`) in the `collection-id` SSM parameter — it gets overwritten in Step 2.

---

## Step 2: One-time GCS setup (`gcs-finalize-setup`)

This step is interactive and requires two browser authentication flows. It only needs to be run once per deployment.

### Wait for user data to finish

The first-boot `user_data` script installs GCS v5 and `mountpoint-s3` and mounts the S3 bucket. It can take a few minutes. Check progress via EC2 console output or SSM Session Manager:

```bash
aws ssm start-session --target <instance-id>
sudo tail -f /var/log/gcs-user-data.log
```

When user data completes, you will see:

```
Globus Connect Server and mountpoint-s3 installed.
S3 bucket mounted at: /mnt/s3/<bucket-name>
To complete Globus setup, SSH/SSM in and run:
  sudo /usr/local/bin/gcs-finalize-setup
```

### Run the finalization script

SSH (or use Session Manager) and run:

```bash
sudo /usr/local/bin/gcs-finalize-setup
```

The script performs these steps:

1. **Endpoint setup** — `globus-connect-server endpoint setup` — prompts you to open a URL and authenticate with your Globus account. Records the endpoint UUID.
2. **Node setup** — configures the node with the instance's public IP (fetched from `checkip.amazonaws.com`).
3. **Service start** — starts and enables `globus-gridftp-server`.
4. **Endpoint login** — `globus-connect-server login` — second browser auth flow, authorizes the GCS Manager API.
5. **Storage gateway** — creates a POSIX gateway over `/mnt/s3/<bucket>`.
6. **Mapped collection** — creates the collection and prints its UUID.
7. **SSM write** — stores the collection UUID in `/{name}/globus/collection-id`.

At the end you will see:

```
=== Setup complete ===
Destination collection UUID: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
Use this as globus-dest-collection-id when submitting workflows.
```

The SSM parameter is now populated. Terraform has `ignore_changes = [value]` on this parameter, so subsequent `terraform apply` runs will not overwrite it.

---

## Step 3: Create a Globus Native App and obtain a refresh token

The Argo transfer step authenticates to Globus using a **native app client ID** and a **refresh token**. These are stored per-lab in AWS Secrets Manager.

### Create a Globus Native App

1. Go to [developers.globus.org](https://developers.globus.org) and log in.
2. Select **Register a thick client or script that will be installed and run by users**.
3. Give it a name (e.g., `cloudpipe-transfer`).
4. Add `https://auth.globus.org/v2/web/auth-code` as a redirect URI.
5. Under app settings, check **Use effective identity (ID token + userinfo)**. Without this, the token exchange will fail with a 500 error.
6. Copy the **Client UUID** — this is your `native-app-client-id`.

### Obtain a refresh token and store credentials

Use `images/globus/setup_auth.py`, which requires `globus-sdk` and `boto3`. It obtains a refresh token, writes it to AWS Secrets Manager, and forces an immediate sync of the Kubernetes secret — all in one step.

```bash
export GLOBUS_NATIVE_APP_CLIENT_ID=<native-app-client-id>
python images/globus/setup_auth.py
```

The script will print a URL. Open it, log in with your institutional identity (e.g. UW-Madison), and paste the auth code back. The script then stores the credentials in Secrets Manager under `globus/refresh-token` and triggers a K8s secret sync.

> **Per-lab tokens**: Each lab or research group manages their own `globus/refresh-token` secret. The workflow template and External Secret are shared infrastructure — only the secret value changes per deployment.

> **Token expiry**: The ABCD collection is a High Assurance guest collection. Its session policy requires periodic reauthentication (typically 30 days or less). When the token expires, re-run `setup_auth.py` to obtain a fresh one.

---

## Step 4: Apply Kubernetes manifests

Apply the External Secret, which syncs the Secrets Manager secret into a Kubernetes Secret in the `argo-workflows` namespace:

```bash
kubectl apply -f argo/workflows/cloudpipe_v2/globus-credentials-external-secret.yaml
```

Verify the sync (External Secrets Operator refreshes every hour, but you can trigger it manually):

```bash
kubectl get externalsecret globus-credentials -n argo-workflows
kubectl get secret globus-credentials -n argo-workflows
```

Apply the workflow template:

```bash
kubectl apply -f argo/workflows/cloudpipe_v2/globus-transfer-workflow-template.yaml
```

---

## Step 5: Submit a workflow

The `globus-transfer` WorkflowTemplate exposes two templates consumed by the master DAG:

| Template | What it does |
|---|---|
| `start-globus-instance-template` | Reads the instance ID from SSM, starts the EC2 instance if not running, waits for status checks to pass |
| `globus-transfer-template` | Runs `transfer.py` in the `cloudpipe/globus` container; submits a Globus transfer task and polls until completion |

Parameters required by `globus-transfer-template`:

| Parameter | Example | Description |
|---|---|---|
| `source-collection-id` | `xxxxxxxx-...` | UUID of the remote Globus collection |
| `source-base-path` | `/abcd/raw` | Base path on the source; subject ID is appended |
| `dest-collection-id` | `yyyyyyyy-...` | UUID of the S3-backed GCS collection (from SSM) |
| `dest-base-path` | `/mmps_mproc` | Base path on the destination; subject ID is appended |
| `scan-types` | `'["T1w","T2w","rest"]'` | JSON array of BIDS scan types to transfer |

The transfer uses `sync_level=checksum`, so re-runs skip files that already exist at the destination with a matching checksum.

---

## Credential rotation

To rotate the refresh token (e.g., after it expires or a team member leaves), re-run `setup_auth.py` (Step 3). It will update the Secrets Manager secret and force an immediate K8s secret sync automatically.

The EC2 instance itself authenticates to Globus as the endpoint operator via credentials stored on disk by `gcs-finalize-setup`; those are separate from the refresh token used by the workflow container.
