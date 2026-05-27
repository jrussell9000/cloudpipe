# Globus

CloudPipe uses Globus Connect Server (GCS) v5 to transfer ABCD minimally-preprocessed data from the DAIRC MMPS endpoint to the cloudpipe S3 bucket. This document covers the architecture, credentials model, operational procedures, and recovery steps.

For the initial setup walkthrough (endpoint creation, storage gateway, IAM credentials, refresh token), see [globus-s3-gateway-config.md](globus-s3-gateway-config.md).

---

## Architecture

```
DAIRC MMPS Globus endpoint
  (source collection: 43583c7d-29c9-4d36-9cb5-c8a1641923cb)
        │
        │  GridFTP data channel (port 50000–51000)
        ▼
GCS v5 endpoint — EC2 c5n.xlarge, Elastic IP, Ubuntu 22.04
  S3 storage gateway (admin-managed IAM credentials)
        │
        │  S3 multipart PUT (no FUSE, no EBS staging)
        ▼
s3://abcd-v7/mmps_mproc/{subject}/{session}/...
```

The S3 storage gateway writes GridFTP data directly to S3 via multipart upload. No local staging volume or EFS is involved. The alternative POSIX+EBS staging approach is implemented but disabled (`globus-use-s3-gateway = true` in Terraform).

### Why S3 gateway and not mountpoint-s3

AWS Mountpoint for S3 only supports sequential writes from byte 0. Globus GridFTP uses Extended Block Mode, delivering data out-of-order and performing non-sequential writes. Any non-sequential write fails with `EINVAL` through the FUSE layer. The S3 gateway bypasses FUSE entirely.

---

## Current deployment values

| Resource | Value |
|---|---|
| EC2 instance type | `c5n.xlarge` |
| Endpoint ID | `8e6a5497-c260-4613-8698-e4fecd6365eb` |
| S3 storage gateway ID | `3cdb1567-f3fe-416c-a52b-ffaf46f9a2c9` |
| Destination collection ID | `00666689-6b52-444d-b6a8-57a3ce6ee97c` |
| Collection name | `cloudpipe-s3` |
| Source collection (DAIRC MMPS) | `43583c7d-29c9-4d36-9cb5-c8a1641923cb` |
| Source base path | `/abcd/derivatives/mmps_mproc` |
| Native app client ID | `e8f5215c-8d92-4899-b920-48ec9a412d28` |
| IAM credential identity | `<YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>` |
| UW-Madison HA subscription ID | `<YOUR_GLOBUS_SUBSCRIPTION_UUID>` |

---

## Infrastructure components

### EC2 instance

`c5n.xlarge` (4 vCPU, 10.5 GB RAM) in the public subnet (<YOUR_AWS_REGION>a) with an Elastic IP. The fixed IP is required for Globus endpoint registration and must not change between instance replacements.

The instance is **stopped when idle** and started automatically at the beginning of each cloudpipe workflow by `start-globus-instance-template`. A nightly EventBridge schedule (cron `0 0 * * ? *` UTC) stops it as a safety net in case a workflow completes without triggering an explicit stop.

Stop it manually after a run completes (if you don't want to wait for midnight):
```bash
INSTANCE_ID=$(aws ssm get-parameter \
  --name /cloudpipe/globus/instance-id \
  --query Parameter.Value --output text)
aws ec2 stop-instances --instance-ids "$INSTANCE_ID"
```

### SSM parameters

All workflow-accessible Globus config lives in SSM. Terraform creates these parameters; the `gcs-finalize-setup` script overwrites the placeholder values after the one-time interactive setup.

| Parameter | Set by | Content |
|---|---|---|
| `/cloudpipe/globus/instance-id` | Terraform | EC2 instance ID |
| `/cloudpipe/globus/collection-id` | `gcs-finalize-setup` | Destination GCS collection UUID (`ignore_changes` prevents Terraform from reverting it) |
| `/cloudpipe/globus/deployment-key` | `gcs-finalize-setup` | GCS deployment key JSON (SecureString; used for non-interactive node re-registration on instance replacement) |
| `/cloudpipe/globus/endpoint-id` | `gcs-finalize-setup` | GCS endpoint UUID (used alongside service credentials for non-interactive management commands) |
| `/cloudpipe/globus/gcs-client-id` | Manual (operator) | Globus Auth service account Client UUID (SecureString; see [Service credentials](#service-credentials)) |
| `/cloudpipe/globus/gcs-client-secret` | Manual (operator) | Globus Auth service account client secret (SecureString) |
| `/cloudpipe/globus/source-collection-id` | Terraform | Source collection UUID (DAIRC MMPS) |
| `/cloudpipe/globus/source-base-path` | Terraform | Root path on source collection |

### IAM roles

| Role | Used by | Permissions |
|---|---|---|
| `cloudpipe-globus-*` (instance profile) | GCS EC2 instance | S3 read/write on `abcd-v7`, SSM core, SSM write to collection-id + deployment-key + endpoint-id; SSM read for gcs-client-id + gcs-client-secret |
| `cloudpipe-argo-runner` | `argo-workflows-runner` SA | EC2 `StartInstances` (scoped to Globus instance ID), `DescribeInstanceStatus` + `DescribeInstances` (not resource-scoped), SSM `GetParameter` for instance-id + collection-id |

The runner role gets EC2 and SSM permissions from `terraform/modules/globus/main.tf` (`runner_globus_ec2` inline policy), not from the base runner IAM module.

SSM `SendCommand` and `GetCommandInvocation` permissions are only added when `globus_use_s3_gateway = false` — they are not needed in the current S3 gateway configuration.

### Credentials: two separate credential sets

Globus transfers require two independent credentials:

**1. IAM user access key (S3 gateway, admin-managed)**

A static IAM user key registered with the GCS S3 storage gateway via `globus-connect-server user-credentials s3-create`. This is the credential GCS uses to write to S3. It is stored in Globus's infrastructure, not on the instance or in SSM. The EC2 instance role is used for all other AWS API calls (SSM, EC2 metadata) — not for Globus S3 writes.

**2. Globus refresh token (transfer client)**

A long-lived OAuth2 refresh token tied to the `cloudpipe-transfer` native app. The `globus` container authenticates with this token to submit transfers on behalf of the registered identity. Stored in AWS Secrets Manager at `globus/refresh-token` and synced to the `globus-credentials` K8s Secret via ExternalSecret (refreshes every hour).

Argo workflow containers receive both credentials as environment variables:
```yaml
env:
  - name: GLOBUS_NATIVE_APP_CLIENT_ID   # from globus-credentials secret
  - name: GLOBUS_REFRESH_TOKEN          # from globus-credentials secret
```

---

## How transfer.py works

`images/globus/transfer.py` runs inside the `globus-transfer-template` pod:

1. **Authenticates** using `GLOBUS_NATIVE_APP_CLIENT_ID` + `GLOBUS_REFRESH_TOKEN` via `RefreshTokenAuthorizer`.
2. **Pre-flight check** — calls `operation_ls` on the destination collection to verify it is reachable and the S3 IAM credential is provisioned. A 404 is treated as a pass (path not yet created). Any other error fails fast with a message pointing to `user-credentials s3-create`.
3. **Discovers files** by walking the BIDS directory tree under `{source-base-path}/{subject-id}` on the source collection — lists sessions, then each `bids_dir` (anat/func/dwi) for each requested scan type.
4. **Task deduplication** — searches for existing tasks labelled `cloudpipe-{subject-id}`. Reuses an `ACTIVE` task (still transferring). Cancels any `INACTIVE` task (suspended/errored; will not self-recover) and resubmits fresh.
5. **Submits a transfer task** with `sync_level="checksum"` and `encrypt_data=True`. Handles `TooManyPendingJobs` with exponential backoff (10 attempts, 60s–600s).
6. **Polls** every 60 seconds until the task `SUCCEEDED`. Raises on `FAILED` or `CANCELLED`.

Transfer paths: each file is added as `{source-base-path}/{subject-id}/{session}/{bids_dir}/{filename}` → `{dest-base-path}/{session}/{bids_dir}/{filename}` (subject ID not repeated in dest path because the collection is already rooted at the bucket, and the dest path includes the subject segment).

---

## High Assurance

The ABCD source collection is Globus High Assurance (HA). The cloudpipe destination collection must also be HA for Globus to allow transfers between them. The cloudpipe endpoint is subscribed to the UW-Madison HA subscription (`<YOUR_GLOBUS_SUBSCRIPTION_UUID>`).

HA implications:
- `--authentication-timeout-mins` is set to 1 week (max 30 days for HA)
- `setup_auth.py` uses `prompt=login` to force a fresh auth event — reusing an existing browser session can produce a `No effective ACL rules` 403
- When the refresh token expires, transfers fail and `setup_auth.py` must be re-run

---

## Credential rotation

### Rotating the IAM access key

The IAM user key registered with the S3 gateway is a long-lived credential stored in Globus's system (not on the instance). Rotate it periodically:

1. Generate a new IAM access key in the AWS console for the IAM user associated with `<YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>`.
2. SSM into the GCS instance and load service credentials:
   ```bash
   aws ssm start-session \
     --target $(aws ssm get-parameter \
       --name /cloudpipe/globus/instance-id \
       --query Parameter.Value --output text) \
     --region <YOUR_AWS_REGION>
   sudo -i
   export GCS_CLI_CLIENT_ID=$(aws ssm get-parameter --region <YOUR_AWS_REGION> \
     --name /cloudpipe/globus/gcs-client-id --with-decryption --query Parameter.Value --output text)
   export GCS_CLI_CLIENT_SECRET=$(aws ssm get-parameter --region <YOUR_AWS_REGION> \
     --name /cloudpipe/globus/gcs-client-secret --with-decryption --query Parameter.Value --output text)
   export GCS_CLI_ENDPOINT_ID=$(aws ssm get-parameter --region <YOUR_AWS_REGION> \
     --name /cloudpipe/globus/endpoint-id --query Parameter.Value --output text)
   ```
3. Update the credential in Globus (enter the new key at the prompts):
   ```bash
   globus-connect-server user-credentials s3-create \
     3cdb1567-f3fe-416c-a52b-ffaf46f9a2c9 \
     --globus-identity <YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN> \
     --replace-existing
   ```
4. Verify S3 access is working (service credentials show an empty list — use `globus ls` instead):
   ```bash
   globus ls 00666689-6b52-444d-b6a8-57a3ce6ee97c:/
   ```
5. Delete the old IAM access key in the AWS console once the new key is confirmed working.

### Rotating the Globus refresh token

HA collections require periodic reauthentication (token expires after up to 30 days). When expired, `transfer.py` fails with an auth error.

```bash
export GLOBUS_NATIVE_APP_CLIENT_ID=e8f5215c-8d92-4899-b920-48ec9a412d28
python images/globus/setup_auth.py
```

`setup_auth.py` stores the new token in Secrets Manager and forces an immediate K8s secret sync by annotating the ExternalSecret. No `--dest-collection-id` is needed for HA collections.

To force an immediate K8s secret sync manually (without waiting for the hourly ESO refresh):
```bash
kubectl annotate externalsecret globus-credentials \
  -n argo-workflows \
  force-sync=$(date +%s) --overwrite
```

---

## Instance replacement

When the GCS EC2 instance is replaced (terminated and a new one launched):

1. Terraform creates a new instance with `user_data` that runs `gcs-auto-reregister` on first boot. This script reads the deployment key from SSM (`/cloudpipe/globus/deployment-key`) and re-registers the node non-interactively. The existing endpoint UUID and collection are preserved — no subscription re-request to UW-Madison needed.

2. The IAM credentials registered in Globus (`user-credentials s3-create`) are stored in Globus's infrastructure, not on the instance. They survive instance replacement automatically.

3. After the new instance is running, update SSM with the new instance ID:
   ```bash
   terraform apply   # automatically updates /cloudpipe/globus/instance-id SSM param
   ```

4. The Elastic IP is re-associated automatically by Terraform.

5. The collection UUID in `/cloudpipe/globus/collection-id` does not change — the `lifecycle { ignore_changes = [value] }` block prevents Terraform from reverting the value set by `gcs-finalize-setup`.

---

## Service credentials

All `globus-connect-server` management commands can authenticate non-interactively using a Globus Auth service account stored in SSM, eliminating the 30-day login session expiry problem. Load the three environment variables before any management command:

```bash
export GCS_CLI_CLIENT_ID=$(aws ssm get-parameter \
  --region <YOUR_AWS_REGION> --name /cloudpipe/globus/gcs-client-id \
  --with-decryption --query Parameter.Value --output text)
export GCS_CLI_CLIENT_SECRET=$(aws ssm get-parameter \
  --region <YOUR_AWS_REGION> --name /cloudpipe/globus/gcs-client-secret \
  --with-decryption --query Parameter.Value --output text)
export GCS_CLI_ENDPOINT_ID=$(aws ssm get-parameter \
  --region <YOUR_AWS_REGION> --name /cloudpipe/globus/endpoint-id \
  --query Parameter.Value --output text)
```

With these set, `globus-connect-server` commands authenticate as the service account without prompting. The client secret does not expire.

> **`user-credentials list` with service credentials**: The list only shows credentials owned by the service account identity — not the `<YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>` credential registered in step 5 of the setup guide. An empty list is expected; it does not mean the IAM credential is missing. Use `globus ls <collection-id>:/` to confirm S3 access is working.

---

## Operational checks

```bash
# SSM into the GCS instance
aws ssm start-session \
  --target $(aws ssm get-parameter \
    --name /cloudpipe/globus/instance-id \
    --query Parameter.Value --output text) \
  --region <YOUR_AWS_REGION>
sudo -i

# Load service credentials (no login session needed)
export GCS_CLI_CLIENT_ID=$(aws ssm get-parameter \
  --region <YOUR_AWS_REGION> --name /cloudpipe/globus/gcs-client-id \
  --with-decryption --query Parameter.Value --output text)
export GCS_CLI_CLIENT_SECRET=$(aws ssm get-parameter \
  --region <YOUR_AWS_REGION> --name /cloudpipe/globus/gcs-client-secret \
  --with-decryption --query Parameter.Value --output text)
export GCS_CLI_ENDPOINT_ID=$(aws ssm get-parameter \
  --region <YOUR_AWS_REGION> --name /cloudpipe/globus/endpoint-id \
  --query Parameter.Value --output text)

globus-connect-server endpoint show
globus-connect-server storage-gateway list
globus-connect-server collection list
globus-connect-server user-credentials list

# Check GCS services
systemctl status globus-gridftp-server
```

> **`user-credentials list` syntax**: Use `globus-connect-server user-credentials list` (no gateway ID argument). There is no `s3` subcommand for `list` or `delete` — only `s3-create` takes the gateway ID as a positional argument.

```bash
# Verify the K8s secret is populated
kubectl get secret globus-credentials -n argo-workflows -o json \
  | jq '.data | keys'

# Check ExternalSecret sync status
kubectl get externalsecret globus-credentials -n argo-workflows
```

---

## Concurrency limit

The `cloudpipe-semaphores` ConfigMap caps concurrent Globus transfers at 8:

```yaml
globus-transfer: "8"
```

The `c5n.xlarge` GCS instance runs one GridFTP process per active transfer. 8 is the safe ceiling for large BOLD file transfers on this instance type. Increase only after load testing — exceeding available memory causes GridFTP workers to be OOM-killed, which produces failed transfers that exhaust Argo retries.

To raise the limit, edit `argo/workflows/cloudpipe_minproc/cloudpipe-semaphores-configmap.yaml`, commit, and push.

---

## Troubleshooting

### "Bucket not allowed" on transfer

**Symptom**: Globus transfer fails with `550-Globus-S3-Error: Bucket not allowed`.

**Cause**: The S3 gateway was created with `s3_allow_multi_keys: true` (the default). In multi-key mode, the first component of every path is treated as a bucket name — so a path like `/mmps_mproc/sub-xxx` tells Globus to write to a bucket named `mmps_mproc`, not `abcd-v7`. The fix is `--no-allow-multiple-keys` at gateway creation time.

**Verify the flag**:
```bash
globus-connect-server storage-gateway list --format json \
  | python3 -c "import sys,json; d=json.load(sys.stdin)['DATA'][0]; print('s3_allow_multi_keys:', d.get('s3_allow_multi_keys'))"
```

If `s3_allow_multi_keys: True`, recreate the gateway (see below). An in-place update (`storage-gateway update s3`) technically works but does not reliably affect running transfers — recreation is more reliable.

**Another cause**: `transfer.py` checks for an existing active task (by label `cloudpipe-{subject-id}`) and reuses it if found. A stale task from before the fix can be reused, causing the old error to persist. Cancel stale tasks from the [Globus web app](https://app.globus.org) → Activity, then resubmit.

### "Your credential requires some initial setup" (invalid_credential)

**Symptom**: Transfer fails with `530-GridFTP-Message: Your credential requires some initial setup. code=invalid_credential`.

**Cause**: The IAM access key registered with the gateway is invalid — wrong key, rotated key, or key without S3 access to `abcd-v7`.

**Fix**:
```bash
# On GCS instance
globus-connect-server user-credentials list
globus-connect-server user-credentials delete <credential-id>
globus-connect-server user-credentials s3-create 3cdb1567-f3fe-416c-a52b-ffaf46f9a2c9 \
  --globus-identity <YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>
# Enter a valid IAM access key with permissions to s3://abcd-v7
```

### Gateway and collection recreation

If the gateway needs to be recreated (e.g., to change `s3_allow_multi_keys`), the full procedure:

```bash
# On the GCS instance, logged in via SSM (sudo -i)

# 1. Login if needed
ENDPOINT_ID=$(cat /etc/globus-connect-server/info.json | python3 -c "import sys,json; print(json.load(sys.stdin)['endpoint_id'])")
globus-connect-server login "$ENDPOINT_ID" --no-local-server

# 2. Delete user credentials (required before gateway deletion)
GATEWAY_ID="3cdb1567-f3fe-416c-a52b-ffaf46f9a2c9"
globus-connect-server user-credentials list
# For each credential ID in the output:
globus-connect-server user-credentials delete <credential-id>

# 3. Remove delete protection from collection, then delete it
COLLECTION_ID="562c9c0a-8e76-457b-9fdf-18a07ce55f80"
globus-connect-server collection update "$COLLECTION_ID" --no-delete-protected
globus-connect-server collection delete "$COLLECTION_ID"

# 4. Delete the gateway
globus-connect-server storage-gateway delete "$GATEWAY_ID"

# 5. Recreate gateway with --no-allow-multiple-keys
GATEWAY_ID=$(globus-connect-server storage-gateway create s3 "cloudpipe-s3" \
  --bucket abcd-v7 \
  --s3-endpoint https://s3.<YOUR_AWS_REGION>.amazonaws.com \
  --domain <YOUR_INSTITUTION_DOMAIN> \
  --high-assurance \
  --authentication-timeout-mins $((60 * 24 * 7)) \
  --s3-user-credential \
  --admin-managed-credentials \
  --no-allow-multiple-keys \
  --format json | python3 -c "import sys,json; d=json.load(sys.stdin); r=d[0] if isinstance(d,list) else d; print(r.get('id') or r.get('data',[{}])[0].get('id'))")
echo "New gateway: $GATEWAY_ID"

# 6. Create collection (rooted at /abcd-v7 — bucket name is always the first path component)
# Note: collection create returns the object directly; gateway create wraps in {"data":[...]}
COLLECTION_ID=$(globus-connect-server collection create \
  "$GATEWAY_ID" "/abcd-v7" "cloudpipe-s3" \
  --allow-guest-collections \
  --enable-https \
  --format json | python3 -c "import sys,json; d=json.load(sys.stdin); r=d[0] if isinstance(d,list) else d; print(r.get('id') or r.get('data',[{}])[0].get('id'))")
echo "New collection: $COLLECTION_ID"

# 7. Register IAM credentials
globus-connect-server user-credentials s3-create "$GATEWAY_ID" \
  --globus-identity <YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>
# Enter IAM access key with s3://abcd-v7 permissions at the prompts

# 8. Update SSM
aws ssm put-parameter --region <YOUR_AWS_REGION> \
  --name /cloudpipe/globus/collection-id \
  --type String --value "$COLLECTION_ID" --overwrite
```

### Gridftp readiness race

EC2 status checks pass before `gcs-auto-reregister` finishes starting `globus-gridftp-server`. The `start-globus-instance-template` Argo step polls port 443 after EC2 health checks to confirm gridftp is accepting connections. If this step times out, SSH/SSM in and verify:

```bash
systemctl status globus-gridftp-server
journalctl -u gcs-auto-reregister --no-pager -n 50
```

---

## POSIX staging alternative (disabled)

The POSIX staging approach (`globus_use_s3_gateway = false` in Terraform) uses a 500 Gi EBS volume attached at `/data/globus-staging` on the GCS instance. GridFTP writes to EBS, then `globus-s3-sync-template` runs `aws s3 sync` via SSM send-command. This approach works without a Globus subscription that includes the S3 add-on and does not require a static IAM user key, but is slower and more operationally complex.

The POSIX path is fully implemented in the WorkflowTemplate (the `globus-s3-sync-template` step) and is activated by setting `globus-use-s3-gateway = "false"` at workflow submission time. The Terraform EBS volume resource (`aws_ebs_volume.globus_staging`) is only provisioned when `globus_use_s3_gateway = false`.

See [globus-two-stage-transfer.md](globus-two-stage-transfer.md) for the POSIX setup guide.
