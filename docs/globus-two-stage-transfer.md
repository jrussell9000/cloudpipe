# Globus Two-Stage Transfer Setup (POSIX+EBS)

This document describes the POSIX+EBS staging approach for the Globus transfer workflow: Globus deposits files onto an EBS staging volume on the GCS EC2 instance, then `globus-s3-sync-template` syncs them to S3.

> **Current deployment** uses the GCS native S3 storage gateway (`--admin-managed-credentials`), which writes directly to S3 without EBS staging. See [globus-s3-gateway-config.md](globus-s3-gateway-config.md) for that setup. The POSIX+EBS approach documented here is an alternative — use it if static IAM key management is undesirable or if the S3 gateway encounters issues.

## Storage gateway approach

Two approaches are supported, controlled by `globus_use_s3_gateway` in `terraform/terraform.tfvars`:

| Variable value | Gateway type | EBS staging volume | Globus subscription required |
|---|---|---|---|
| `false` | POSIX (EBS staging) — GridFTP writes to local EBS, `globus-s3-sync-template` syncs to S3 | Yes (500 GB gp3) | No |
| `true` | POSIX (mountpoint-s3) — **incompatible with Globus; do not use** (see below) | No | Yes — HA subscription |

Set `globus-use-s3-gateway: "false"` in the Argo workflow parameters to activate EBS staging. The Prefect queue manager passes this parameter to the master workflow.

---

## Architecture overview

```
Remote collection          Globus network          cloudpipe AWS
(source institution) ───────────────────────── GCS EC2 ──── S3 bucket
```

The destination side is a **Globus Connect Server v5** (GCS) EC2 instance (`c5n.xlarge`, Ubuntu 22.04). A GCS **POSIX storage gateway** backed by an EBS staging volume presents a local directory as a Globus collection. GridFTP writes land on EBS at `/data/globus-staging`; `globus-s3-sync-template` then runs `aws s3 sync` via SSM to push files to S3 and clean up the staging area.

> **Why not mountpoint-s3?** AWS Mountpoint for S3 only supports sequential writes starting from byte 0 of a new file. Globus GridFTP uses Extended Block Mode, which delivers data out-of-order and performs non-sequential writes even when `max_parallelism` is set to 1. Any non-sequential write is rejected by the Mountpoint FUSE layer with `EINVAL`, causing transfers to fail with `500 globus_xio: System error in write: Invalid argument`. EBS staging does not have this constraint.
>
> **Tradeoff vs. the GCS native S3 gateway**: POSIX+EBS uses the EC2 instance IAM role (no static credentials), and any Globus identity maps to the local `cloudpipe` service account via `gcs-identity-map.py` — no per-user credential registration required. The cost is an extra `aws s3 sync` step and the EBS staging volume. The S3 gateway avoids EBS but requires an IAM user key to be generated and rotated. See [globus-s3-gateway-config.md](globus-s3-gateway-config.md).

AWS resources created by the `terraform/modules/globus` module:

| Resource | Purpose |
|---|---|
| EC2 instance + Elastic IP | Globus Connect Server |
| Security group | Ports 443, 50000–51000 open to `0.0.0.0/0` (required by GCS v5), SSH restricted to admin prefix list |
| IAM role + instance profile | Grants the instance S3 read/write, SSM write (collection ID + deployment key), and SSM read (deployment key) |
| `/{name}/globus/instance-id` (SSM) | Instance ID — read by Argo's start step |
| `/{name}/globus/collection-id` (SSM) | Collection UUID — written by `gcs-finalize-setup` after initial setup; read by Terraform as a live data source so `terraform apply` always picks up the current value |
| `/{name}/globus/deployment-key` (SSM, SecureString) | GCS deployment key — written by `gcs-finalize-setup`; read by `gcs-auto-reregister` on future instance replacements to re-register non-interactively |
| EventBridge Scheduler | Stops (not terminates) the instance at midnight UTC as a safety net — the next workflow run starts it again automatically |
| Argo runner IAM policy | Grants Argo permission to start the instance and read the instance ID and collection ID SSM parameters |

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
globus_client_id               = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"  # Globus service account app — see Step 2
```

`globus_org_name` and `globus_contact_email` have no defaults and must be present — `terraform apply` will fail without them.

The `globus_dest_collection_id` is **not** a Terraform variable. The Prefect queue manager flow reads it directly from SSM at runtime (`/cloudpipe/globus/collection-id`), so it picks up the correct value after any instance replacement without requiring a `terraform apply` or flow redeployment.

After the cluster has been deployed, retrieve the Globus outputs:

```bash
terraform output globus_public_ip
terraform output globus_collection_id_ssm_parameter
```

The EC2 instance is running and the `user_data` first-boot script is executing (or has finished). Terraform stores a placeholder value (`REPLACE_AFTER_GCS_SETUP`) in the `collection-id` and `deployment-key` SSM parameters — both get overwritten in Step 2.

---

## Step 2: One-time GCS setup (`gcs-finalize-setup`)

This step is interactive and requires two browser authentication flows. It only needs to be run once per deployment. For subsequent instance replacements, see [Instance replacement (automated)](#instance-replacement-automated) below.

### Prerequisite: Register a Globus service account app

The `gcs-finalize-setup` script creates the endpoint under a **Globus service account app** rather than a personal NetID identity. This is required for UW-Madison subscription association — the endpoint must be owned by `globusadmins@<YOUR_INSTITUTION_DOMAIN>` (the UW subscription manager) at creation time.

1. Go to [developers.globus.org](https://developers.globus.org) and log in with your institutional identity.
2. Under **Projects**, select an existing project or create a new one.
3. Click **Add** → **Add a New App** → **Register a thick client or script that will be installed and run by users**.
4. Name it something descriptive (e.g., `cloudpipe-gcs`).
5. Copy the **Client UUID** — this is your `globus_client_id`.

Add it to `terraform/terraform.tfvars` and run `terraform apply`:

```hcl
globus_client_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

> **Note**: Because `user_data_replace_on_change = false`, running `terraform apply` alone does not push the updated script to the existing instance. If the instance is already running, update the script in place before proceeding:
> ```bash
> sudo sed -i 's/GCS_CLIENT_ID=.*/GCS_CLIENT_ID="<your-client-id>"/' /usr/local/bin/gcs-finalize-setup
> ```

### Wait for user data to finish

The first-boot `user_data` script installs GCS v5 and writes the helper scripts. It can take a few minutes. Check progress via EC2 console output or SSM Session Manager:

```bash
aws ssm start-session \
  --target $(aws ssm get-parameter --name /cloudpipe/globus/instance-id --query Parameter.Value --output text) \
  --region <YOUR_AWS_REGION>

sudo -i
tail -f /var/log/gcs-user-data.log
```

When user data completes, you will see (S3 gateway mode):

```
Globus Connect Server installed (S3 gateway mode).
GridFTP writes directly to S3: <bucket-name>
Requires Globus subscription with S3 add-on (standard tier).
First-time setup: SSH/SSM in and run:
  sudo /usr/local/bin/gcs-finalize-setup
Future replacements: gcs-auto-reregister runs automatically on boot.
```

Or for POSIX staging mode (`globus_use_s3_gateway = false`):

```
Globus Connect Server installed (POSIX staging mode).
GridFTP writes to EBS staging volume at: /data/globus-staging
Run globus-s3-sync after transfer to push files to S3.
First-time setup: SSH/SSM in and run:
  sudo /usr/local/bin/gcs-finalize-setup
Future replacements: gcs-auto-reregister runs automatically on boot.
```

### Run the finalization script

SSH (or use Session Manager) and run:

```bash
sudo /usr/local/bin/gcs-finalize-setup
```

The script performs these steps:

1. **Endpoint setup** — `globus-connect-server endpoint setup` — authenticates as the registered service account app (`--client-id $GCS_CLIENT_ID`), sets the endpoint owner to `globusadmins@<YOUR_INSTITUTION_DOMAIN>` (the UW-Madison subscription manager), marks the endpoint `--public`, and runs a browser auth flow. Records the endpoint UUID.
2. **Join the UW-Madison Globus subscription group** — the script prints the endpoint UUID and prompts you to email `globusadmins@<YOUR_INSTITUTION_DOMAIN>` with your endpoint ID. Per UW-Madison's instructions, this is how you join the UW-Madison Globus subscription group (`<YOUR_GLOBUS_SUBSCRIPTION_UUID>`). Group Administrators and Managers can subscribe Globus resources to the subscription regardless of resource ownership. You can continue with the remaining setup steps while waiting for confirmation.
3. **Deployment key saved** — copies `deployment-key.json` to `/etc/globus-connect-server/deployment-key.json` (canonical path) and saves it to SSM at `/{name}/globus/deployment-key` as a SecureString.
4. **Node setup** — configures the node with the instance's public IP (fetched from `checkip.amazonaws.com`).
5. **Service start** — starts and enables `globus-gridftp-server`.
6. **Endpoint login** — `globus-connect-server login` — second browser auth flow, authorizes the GCS Manager API.
7. **Verify mountpoint-s3 mount** — confirms `/data/s3-mount` is active before creating the gateway.
8. **POSIX storage gateway** — creates a POSIX gateway at `/data/s3-mount` with `--high-assurance`, 7-day authentication timeout, and an identity mapping script that maps any authenticated Globus identity to the local `cloudpipe` user (uid 997), which owns the FUSE mount.
9. **Mapped collection** — creates the collection rooted at `/data/s3-mount` (the S3 bucket root) and prints its UUID. Transfer destination paths are relative to the bucket (e.g. `/mmps_mproc/sub-xxx`) — no bucket-name prefix needed.
10. **SSM write** — stores the collection UUID in `/{name}/globus/collection-id`.

At the end you will see:

```
=== Setup complete ===
Destination collection UUID: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
Use this as globus-dest-collection-id when submitting workflows.
```

Both SSM parameters are now populated. Terraform has `ignore_changes = [value]` on both, so subsequent `terraform apply` runs will not overwrite them.

---

## Instance replacement (automated)

When the Globus EC2 instance is replaced (e.g., after `terraform taint` or a forced instance refresh), the `gcs-auto-reregister` systemd service runs on first boot and handles re-registration non-interactively — no browser required, because the endpoint, collection, and refresh token all remain the same; only the node IP changes.

**Boot logic:**

| Condition | Action |
|---|---|
| `globus-gridftp-server` already active | No-op — skip |
| `/etc/globus-connect-server/deployment-key.json` exists (stop/start of same instance) | Re-run `node setup` with new IP, restart gridftp |
| No local key, SSM has a real deployment key | Pull key from SSM, run `node setup`, start gridftp |
| No local key, SSM placeholder not yet replaced | Print instructions to run `gcs-finalize-setup` manually, exit |

Progress is logged to `/var/log/gcs-auto-reregister.log`.

After a replacement, `terraform apply` automatically picks up the new collection ID from SSM — no variable changes needed.

> **Limitation**: `gcs-auto-reregister` only re-registers the node against an existing endpoint. If the endpoint itself has been deleted from the Globus web UI, automated recovery will not work — see [Endpoint recovery (after accidental deletion)](#endpoint-recovery-after-accidental-deletion).

> **Subscription warning**: `gcs-auto-reregister` preserves the existing endpoint UUID — the UW-Madison HA subscription association carries over automatically. However, if `gcs-finalize-setup` is ever run on a replacement instance (which creates a brand-new endpoint with a new UUID), the new endpoint must be subscribed to the HA subscription. Group Administrators and Managers can do this directly in the Globus web UI; Members should email `globusadmins@<YOUR_INSTITUTION_DOMAIN>` with the new endpoint UUID. The subscription is tied to the endpoint UUID, not the instance or IP address.

---

## Endpoint recovery (after accidental deletion)

If the Globus endpoint is accidentally deleted from the Globus web UI, transfers will fail with:

```
530-Login incorrect. : GlobusError: v=1 c=ENDPOINT_ERROR
530-Failure while contacting GCS Manager API.
```

The GCS Manager backend (proxied by Apache2) loses its registration and stops responding. `gcs-auto-reregister` cannot fix this — a full endpoint re-creation is required.

### Diagnosis

SSM into the instance:

```bash
aws ssm start-session \
  --target $(aws ssm get-parameter --name /cloudpipe/globus/instance-id --query Parameter.Value --output text) \
  --region <YOUR_AWS_REGION>
sudo -i
```

Then check:

```bash
systemctl status globus-gridftp-server        # running but unable to validate logins
systemctl status apache2                       # running but proxying to a dead backend
curl -k -s --max-time 5 https://localhost/api/v1/endpoint  # hangs = GCS Manager down
globus-connect-server endpoint show            # "You must log in" = endpoint gone
```

If curl hangs and `endpoint show` returns "You must log in", the endpoint has been deleted.

### Recovery procedure

**On the instance (via SSM):**

**1. Stop services and clear stale GCS state:**

```bash
systemctl stop globus-gridftp-server apache2

rm /var/lib/globus-connect-server/info.json
rm /var/lib/globus-connect-server/gcs-manager/gcs54.db
rm /var/lib/globus-connect-server/gcs-manager/gridftp-key
rm /var/lib/globus-connect-server/gcs-manager/gridftp-key.old
```

**2. Create a new endpoint:**

> Do not use `gcs-finalize-setup` here — it captures stdout from `globus-connect-server endpoint setup` via command substitution, which swallows the interactive auth URL on a headless terminal and causes the script to hang silently. Run the command directly instead.

```bash
GCS_CLIENT_ID="<your-globus-client-id>"  # from terraform.tfvars

globus-connect-server endpoint setup cloudpipe \
  --organization "BRAVE Research Collaborative" \
  --contact-email "<YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>" \
  --owner "globusadmins@<YOUR_INSTITUTION_DOMAIN>" \
  --client-id "$GCS_CLIENT_ID" \
  --public \
  --agree-to-letsencrypt-tos
```

Open the printed URL in a browser on your local machine, authenticate, and paste the auth code back. Note the printed endpoint UUID.

> **If you are already a member of the UW-Madison Globus subscription group**: Group Administrators and Managers can subscribe Globus resources to the subscription regardless of ownership. If you have Admin or Manager role, you can subscribe the new endpoint directly in the [Globus web UI](https://app.globus.org) under your subscription group rather than emailing globusadmins@<YOUR_INSTITUTION_DOMAIN>. Members (non-admin) still need to contact a group Admin or Manager.

**2b. Subscribe the new endpoint (if not already a group Admin/Manager):**

Email `globusadmins@<YOUR_INSTITUTION_DOMAIN>` with the new endpoint UUID so a group Admin or Manager can subscribe it to the UW-Madison HA subscription. If you already have Admin or Manager role in the group, you can subscribe the endpoint directly in the [Globus web UI](https://app.globus.org) without emailing.

> **Subscription warning**: The HA subscription is tied to the endpoint UUID. A new endpoint always requires re-subscribing. See [Instance replacement (automated)](#instance-replacement-automated) for why you should prefer `gcs-auto-reregister` (which preserves the endpoint UUID) over full endpoint re-creation.

**3. Save the new deployment key to SSM and canonical path:**

```bash
cp ~/deployment-key.json /etc/globus-connect-server/deployment-key.json
aws ssm put-parameter \
  --region <YOUR_AWS_REGION> \
  --name /cloudpipe/globus/deployment-key \
  --type SecureString \
  --value "$(cat /etc/globus-connect-server/deployment-key.json)" \
  --overwrite
```

**4. Run node setup:**

The GCS tooling drops to the `gcsweb` user internally to write `/var/lib/globus-connect-server/info.json`. Since we deleted that file and the directory is owned by root, pre-create it with the right ownership first:

```bash
touch /var/lib/globus-connect-server/info.json
chown gcsweb:gcsweb /var/lib/globus-connect-server/info.json
globus-connect-server node setup \
  -d /etc/globus-connect-server/deployment-key.json \
  --ip-address "$(curl -s https://checkip.amazonaws.com)"
```

**5. Start services:**

```bash
systemctl start apache2
systemctl start globus-gridftp-server
```

**6. Login to the new endpoint** (second browser auth flow):

```bash
globus-connect-server login <new-endpoint-uuid>
```

**7. Ensure mountpoint-s3 is running:**

```bash
systemctl start s3-mount
mountpoint -q /data/s3-mount || { echo "ERROR: /data/s3-mount not mounted"; exit 1; }
```

**8. Create POSIX storage gateway:**

```bash
GATEWAY_ID=$(globus-connect-server storage-gateway create posix "cloudpipe-s3" \
  --domain <YOUR_INSTITUTION_DOMAIN> \
  --high-assurance \
  --authentication-timeout-mins $((60 * 24 * 7)) \
  --identity-mapping "external:/usr/local/bin/gcs-identity-map.py" \
  --format json | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Gateway ID: $GATEWAY_ID"
```

**9. Create mapped collection:**

```bash
COLLECTION_ID=$(globus-connect-server collection create \
  "$GATEWAY_ID" "/data/s3-mount" "cloudpipe-s3" \
  --allow-guest-collections \
  --enable-https \
  --format json | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Collection ID: $COLLECTION_ID"
```

**9. Save new collection UUID to SSM:**

```bash
aws ssm put-parameter \
  --region <YOUR_AWS_REGION> \
  --name /cloudpipe/globus/collection-id \
  --type String \
  --value "$COLLECTION_ID" \
  --overwrite
```

**Back on your local machine:**

```bash
# Pick up the new collection ID in Terraform state
cd terraform && terraform apply

# Re-scope the refresh token to the new collection
export GLOBUS_NATIVE_APP_CLIENT_ID=<your-native-app-client-id>
python images/globus/setup_auth.py
```

> **Why `setup_auth.py` is required**: The existing refresh token in Secrets Manager is scoped to the old collection UUID. Without re-running this step, the Argo transfer workflow will authenticate but fail when accessing the new collection.

> **Cleanup**: Any `cloudpipe-s3` collections from the deleted endpoint that appear in the Globus web UI are orphaned and can be safely deleted.

---

## Step 3: Create a Globus Native App and obtain a refresh token

The Argo transfer step authenticates to Globus using a **native app client ID** and a **refresh token**. These are stored per-lab in AWS Secrets Manager.

> **Two separate Globus app registrations are required** — they serve different purposes and must not be confused:
> | App | Registration type | Used by |
> |---|---|---|
> | `cloudpipe-gcs` (service account) | Thick client / script | `gcs-finalize-setup` — owns the GCS endpoint, set via `globus_client_id` in `terraform.tfvars` |
> | `cloudpipe-transfer` (native app) | Native app | `setup_auth.py` and Argo workflows at runtime — generates the refresh token |
>
> Using the service account UUID as `GLOBUS_NATIVE_APP_CLIENT_ID` will fail with "Only native app clients can access this URL".

### Create a Globus Native App

1. Go to [developers.globus.org](https://developers.globus.org) and log in.
2. Select **Register a native app**.
3. Give it a name (e.g., `cloudpipe-transfer`).
4. Add `https://auth.globus.org/v2/web/auth-code` as a redirect URI.
5. Copy the **Client UUID** — this is your `native-app-client-id`.

### Obtain a refresh token and store credentials

Use `images/globus/setup_auth.py`, which requires `globus-sdk` and `boto3`. It obtains a refresh token, writes it to AWS Secrets Manager, and forces an immediate sync of the Kubernetes secret — all in one step.

```bash
export GLOBUS_NATIVE_APP_CLIENT_ID=<native-app-client-id>
python images/globus/setup_auth.py
```

Do **not** pass `--dest-collection-id`. The destination collection is a High Assurance mapped collection; HA collections do not expose a `data_access` scope to external clients (attempting it returns "requested unknown scopes"). The HA session policy is enforced by Globus at transfer time instead.

The script will print a URL. Open it, log in with your institutional identity (e.g. UW-Madison) — a fresh login is required (`prompt=login`) to satisfy the HA session policy. Paste the auth code back. The script stores the credentials in Secrets Manager under `globus/refresh-token` and triggers a K8s secret sync.

> **Per-lab tokens**: Each lab or research group manages their own `globus/refresh-token` secret. The workflow template and External Secret are shared infrastructure — only the secret value changes per deployment.

> **Token expiry**: The ABCD collection is a High Assurance collection. Its session policy requires periodic reauthentication (30-day max). When the token expires, re-run `setup_auth.py` to obtain a fresh one.

> **After instance replacement**: When the Globus instance is replaced, `gcs-auto-reregister` preserves the existing endpoint and collection UUIDs — no token refresh is needed. If `gcs-finalize-setup` was run (creating a new endpoint and collection), re-run `setup_auth.py` to generate a fresh token for the new collection.

### Cancel pending tasks before retrying

If a transfer fails mid-way and you resubmit the workflow before Globus clears the previous task, you will see:

```
409 Conflict: A transfer with identical paths has not yet completed
```

Cancel all active tasks before retrying:

```python
python3 - <<'EOF'
import boto3, json, globus_sdk
secret = json.loads(
    boto3.client("secretsmanager").get_secret_value(SecretId="globus/refresh-token")["SecretString"]
)
client = globus_sdk.NativeAppAuthClient(secret["native-app-client-id"])
authorizer = globus_sdk.RefreshTokenAuthorizer(secret["refresh-token"], client)
tc = globus_sdk.TransferClient(authorizer=authorizer)
for task in tc.task_list(filter_status="ACTIVE,INACTIVE"):
    print(f"Cancelling {task['task_id']} ({task['label']})")
    tc.cancel_task(task["task_id"])
EOF
```

---

## Step 4: Apply Kubernetes manifests

Apply the External Secret, which syncs the Secrets Manager secret into a Kubernetes Secret in the `argo-workflows` namespace:

```bash
kubectl apply -f argo/workflows/cloudpipe_minproc/globus-credentials-external-secret.yaml
```

Verify the sync (External Secrets Operator refreshes every hour, but you can trigger it manually):

```bash
kubectl get externalsecret globus-credentials -n argo-workflows
kubectl get secret globus-credentials -n argo-workflows
```

Apply the workflow template:

```bash
kubectl apply -f argo/workflows/cloudpipe_minproc/globus-transfer-workflow-template.yaml
```

---

## Step 5: Submit a workflow

The `globus-transfer` WorkflowTemplate exposes two templates consumed by the master DAG:

| Template | What it does |
|---|---|
| `start-globus-instance-template` | Reads the instance ID from SSM, starts the EC2 instance if not running, waits for status checks to pass |
| `globus-transfer-template` | Runs `transfer.py` in the `cloudpipe/globus` container; submits a Globus transfer task and polls until completion. Files land directly in S3 via the GCS S3 gateway. |

Parameters required by `globus-transfer-template`:

| Parameter | Example | Description |
|---|---|---|
| `source-collection-id` | `xxxxxxxx-...` | UUID of the remote Globus collection |
| `source-base-path` | `/abcd/raw` | Base path on the source; subject ID is appended |
| `dest-collection-id` | `yyyyyyyy-...` | UUID of the S3-backed GCS collection (from SSM) |
| `dest-base-path` | `/mmps_mproc` | Base path on the destination; subject ID is appended |
| `scan-types` | `'["T1w","T2w","rest"]'` | JSON array of BIDS scan types to transfer |

All Globus parameters are passed by the Prefect `cloudpipe-queue-manager` flow at submission time. The `dest-collection-id` is read live from SSM (`/cloudpipe/globus/collection-id`) each time the flow **starts**, so it remains correct after instance replacement without any redeployment.

> **SSM staleness warning**: The queue manager reads SSM once at startup and caches the collection ID for the entire flow run. If an instance replacement occurs (updating SSM) while the flow is already running, that flow run will continue submitting workflows with the old collection UUID — resulting in Globus transfer errors against the decommissioned endpoint. If you replace the instance while the queue manager is running, cancel the Prefect flow run and restart it after SSM has been updated with the new collection ID.

The transfer uses `sync_level=checksum`, so re-runs skip files that already exist at the destination with a matching checksum.

---

## Credential rotation

To rotate the refresh token (e.g., after it expires or a team member leaves), re-run `setup_auth.py` (Step 3). It will update the Secrets Manager secret and force an immediate K8s secret sync automatically.

To force an immediate sync without waiting for the hourly ESO refresh:

```bash
kubectl annotate externalsecret globus-credentials \
  -n argo-workflows \
  force-sync=$(date +%s) --overwrite
```

The EC2 instance itself authenticates to Globus as the endpoint operator via credentials stored on disk by `gcs-finalize-setup`; those are separate from the refresh token used by the workflow container.
