# Globus Connect Server — S3 Storage Gateway Configuration Guide

This guide covers deploying a Globus Connect Server (GCS) v5 endpoint backed by the **GCS native S3 storage gateway** (`--s3-user-credential --admin-managed-credentials`). In this mode the gateway authenticates to S3 with a static IAM user access key managed by the GCS admin; no FUSE mount or EBS staging volume is required. GridFTP data channels write directly to the S3 bucket via the S3 connector.

It integrates guidance from the UW-Madison High Assurance GCS configuration guide for teams operating under a HIPAA Business Associate Agreement (BAA) with Globus, which is required for transferring ABCD Study data.

> For the alternative POSIX+EBS staging approach (GridFTP writes to EBS, then `aws s3 sync`), see [globus-two-stage-transfer.md](globus-two-stage-transfer.md).

---

## Architecture overview

```
Globus source endpoint (DAIRC)
        │  GridFTP data channel
        ▼
GCS v5 endpoint (EC2, Elastic IP)
        │  S3 storage gateway → IAM user credentials (admin-managed)
        ▼
s3://abcd-v7/...   (S3 bucket, written directly — no FUSE mount)
```

### Why not mountpoint-s3

AWS Mountpoint for S3 only supports sequential writes starting from byte 0 of a new file. Globus GridFTP uses Extended Block Mode, which delivers data out-of-order and performs non-sequential writes even when `max_parallelism` is set to 1. Any non-sequential write is rejected by the Mountpoint FUSE layer with `EINVAL`, causing transfers to fail with `500 globus_xio: System error in write: Invalid argument`. The S3 gateway bypasses FUSE entirely and is not affected by this constraint.

### Credential modes — why admin-managed-credentials

The GCS S3 connector supports exactly three credential modes:

| Mode | Credential mechanism | Suitable for automation |
|---|---|---|
| `--s3-unauthenticated` | Anonymous (no credentials) | Only for public buckets — not applicable |
| `--s3-user-credential` (alone) | Per-user: each Globus identity registers their own AWS key in the Globus web UI | No — requires manual web UI step per identity; breaks fully automated pipelines |
| `--s3-user-credential` + `--admin-managed-credentials` | Single IAM key registered by admin via CLI; applies to all mapped identities | Yes — single key, rotated by admin |

There is **no IMDS / EC2 instance role support** in the S3 connector. The `--admin-managed-credentials` flag is the closest equivalent to a service-account credential for automated use: the admin registers one IAM user key that all authenticated Globus identities use for S3 access.

> **Tradeoff**: Unlike the POSIX+EBS approach (which uses the instance IAM role via the OS credential chain), the S3 gateway requires maintaining a static IAM user key and rotating it periodically. See [Credential rotation](#credential-rotation) below.

---

## Current deployment

| Resource | Value |
|---|---|
| Endpoint ID | `8e6a5497-c260-4613-8698-e4fecd6365eb` |
| S3 gateway ID | `3cdb1567-f3fe-416c-a52b-ffaf46f9a2c9` |
| Collection ID | `00666689-6b52-444d-b6a8-57a3ce6ee97c` |
| Collection name | `cloudpipe-s3` |
| SSM parameter | `/cloudpipe/globus/collection-id` |
| Registered IAM credential identity | `<YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>` |
| IAM user | `programmatic-access-smph_psy_neuro_globus` |
| Native app client ID | `e8f5215c-8d92-4899-b920-48ec9a412d28` |

---

## High Assurance overview

[Globus High Assurance (HA)](https://www.globus.org/high-assurance) adds security controls required for restricted data (HIPAA, CUI):

| Feature | Purpose |
|---|---|
| Higher authentication assurance | Requires session reauthentication at a policy-defined interval |
| Device isolation | Restricts which clients and endpoints can participate in transfers |
| Forced transit encryption | All data channels are encrypted; transfers are rejected if encryption cannot be negotiated |
| Audit logging | Full transfer audit trail available to endpoint administrators |
| Mapped Collections | Per-user access control enforced at the collection layer |

UW-Madison operates under a **Globus HA subscription with a HIPAA BAA**, allowing storage of PHI and restricted research data. The ABCD source collection is HA — Globus requires both sides of a transfer to be HA, so the cloudpipe destination collection must also be HA.

---

## Prerequisites

### Globus subscription

The `--high-assurance` flag requires a **Globus Standard subscription**.

**For UW-Madison deployments**, request endpoint membership under the UW-Madison HA subscription:

```
Subscription ID: <YOUR_GLOBUS_SUBSCRIPTION_UUID>
Contact: globusadmins@<YOUR_INSTITUTION_DOMAIN>
```

Email `globusadmins@<YOUR_INSTITUTION_DOMAIN>` after endpoint creation and include the endpoint UUID.

### AWS infrastructure

The cloudpipe Terraform module (`terraform/modules/globus`) provisions:

- EC2 instance (`c5n.xlarge`, Ubuntu 22.04) with Elastic IP
- Security group (ports 443 and 50000–51000 open to `0.0.0.0/0`, SSH restricted to admin prefix list)
- IAM role with S3 read/write and SSM access (used by the instance for SSM and non-Globus S3 operations)
- SSM parameters for instance ID, collection ID, and deployment key
- EventBridge nightly stop scheduler

Run `terraform apply` from `terraform/` before proceeding.

### IAM user for S3 gateway

The S3 gateway requires a dedicated **IAM user** (not a role) with programmatic access to the S3 bucket. Create one in AWS IAM with a policy granting `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket` on the target bucket. Generate an access key pair — you will register it with the gateway in Step 5.

> **Do not pass the key on the command line.** The `user-credentials s3-create` command prompts for the key interactively to avoid exposing it in shell history.

### Globus Developer account

Register a **service account** at the [Globus Developers Portal](https://developers.globus.org) to own the endpoint, rather than using a personal NetID.

1. Log in with an institutional identity.
2. Select **Register a thick client or script**.
3. Name it (e.g., `cloudpipe-gcs-service-account`).
4. Copy the **Client UUID** — used as `--client-id` in endpoint setup.

---

## Network requirements

> Reference: [Globus Connect Server Network Requirements](https://docs.globus.org/globus-connect-server/v5/#open-tcp-ports_section)

| Direction | Port(s) | Protocol | Source/Destination | Purpose |
|---|---|---|---|---|
| Inbound | 443 | TCP | `0.0.0.0/0` | GCS Manager API + HTTPS collection access |
| Inbound | 50000–51000 | TCP | `0.0.0.0/0` | GridFTP data channels |
| Inbound | 22 | TCP | Admin prefix list | SSH access |
| Outbound | 443 | TCP | `0.0.0.0/0` | Globus REST API, S3 API |
| Outbound | 50000–51000 | TCP | `0.0.0.0/0` | GridFTP data channels |

**Ports 443 and 50000–51000 must be open to `0.0.0.0/0`.** GridFTP data channels are peer-to-peer between endpoints at other institutions worldwide. Access control is enforced by Globus OAuth2/OIDC, not by network-layer filtering.

---

## Step 1: Wait for user_data to complete

```bash
aws ssm start-session \
  --target $(aws ssm get-parameter --name /cloudpipe/globus/instance-id \
    --query Parameter.Value --output text) \
  --region <YOUR_AWS_REGION>

sudo -i
tail -f /var/log/gcs-user-data.log
```

Wait for the completion banner, then verify GCS is running:

```bash
systemctl status globus-gridftp-server
```

---

## Step 2: Endpoint setup

Run the finalization script (requires two browser authentication flows):

```bash
sudo /usr/local/bin/gcs-finalize-setup
```

### 2a. Create the endpoint

```bash
GCS_CLIENT_ID="<your-globus-thick-client-uuid>"

globus-connect-server endpoint setup "cloudpipe" \
  --organization "BRAVE Research Collaborative" \
  --contact-email "<YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>" \
  --owner "globusadmins@<YOUR_INSTITUTION_DOMAIN>" \
  --client-id "$GCS_CLIENT_ID" \
  --public \
  --agree-to-letsencrypt-tos
```

Authenticate with your institutional (<YOUR_INSTITUTION_DOMAIN>) identity when prompted.

### 2b. Save deployment key

```bash
mkdir -p /etc/globus-connect-server
cp ~/deployment-key.json /etc/globus-connect-server/deployment-key.json

aws ssm put-parameter \
  --region <YOUR_AWS_REGION> \
  --name /cloudpipe/globus/deployment-key \
  --type SecureString \
  --value "$(cat /etc/globus-connect-server/deployment-key.json)" \
  --overwrite
```

### 2c. Node setup and service start

```bash
globus-connect-server node setup \
  -d /etc/globus-connect-server/deployment-key.json \
  --ip-address "$(curl -s https://checkip.amazonaws.com)"

systemctl start globus-gridftp-server
systemctl enable globus-gridftp-server
```

### 2d. Endpoint login (second browser flow)

```bash
globus-connect-server login localhost
```

### 2e. Create service account for non-interactive management

The `globus-connect-server login` session from Step 2d expires after approximately 30 days. Any management command run after expiry (storage gateway, collection, user-credentials) fails with an authentication error. To eliminate this failure mode, register a **confidential client** in the Globus Developers Portal and add it as an endpoint administrator. All subsequent `globus-connect-server` commands can then authenticate non-interactively via three environment variables — no periodic re-login required.

#### Create the service account client

1. Go to [app.globus.org/settings/developers](https://app.globus.org/settings/developers) and log in.
2. Select **Register a service account or application credential for automation**, then create a new project or select an existing one.
3. Name it `cloudpipe-gcs-mgmt`.
4. Click **Register App**.
5. Copy the **Client UUID** from the app detail page.
6. Click **Generate New Client Secret**, give it a label (e.g. `cloudpipe-production-YYYY-MM`), and copy the secret immediately — it is only shown once.

#### Add as endpoint administrator

Run from an SSM session on the GCS instance while the Step 2d login session is still active:

```bash
GCS_MGMT_CLIENT_ID="<client-uuid-from-previous-step>"
globus-connect-server endpoint role create administrator \
  "${GCS_MGMT_CLIENT_ID}@clients.auth.globus.org"
```

#### Store credentials in SSM

```bash
aws ssm put-parameter \
  --region <YOUR_AWS_REGION> \
  --name /cloudpipe/globus/gcs-client-id \
  --type SecureString \
  --value "<client-uuid>" \
  --overwrite

aws ssm put-parameter \
  --region <YOUR_AWS_REGION> \
  --name /cloudpipe/globus/gcs-client-secret \
  --type SecureString \
  --value "<client-secret>" \
  --overwrite
```

> The endpoint ID (`/cloudpipe/globus/endpoint-id`) is written automatically by `gcs-finalize-setup` — no manual action needed for that parameter.

#### Using service credentials for management commands

Once stored, export the three environment variables before any `globus-connect-server` command:

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

# All management commands now run without interactive login:
globus-connect-server endpoint show
globus-connect-server storage-gateway list
globus-connect-server collection list
globus-connect-server user-credentials list
```

Add these exports to your shell session or a helper script. The client secret does not expire, so no periodic reauthentication is needed.

> **On instance replacement**: `gcs-auto-reregister` reads these SSM parameters on every boot and runs an S3 credential health check automatically — no manual re-login is needed after routine instance replacement.

---

## Step 3: S3 storage gateway

Create the S3 storage gateway. `--s3-user-credential` enables per-identity credential slots; `--admin-managed-credentials` allows the admin to register a single IAM key that applies to all identities (Step 5).

```bash
S3_BUCKET="abcd-v7"

GATEWAY_ID=$(globus-connect-server storage-gateway create s3 "cloudpipe-s3" \
  --bucket "$S3_BUCKET" \
  --domain <YOUR_INSTITUTION_DOMAIN> \
  --s3-endpoint https://s3.<YOUR_AWS_REGION>.amazonaws.com \
  --s3-user-credential \
  --admin-managed-credentials \
  --no-allow-multiple-keys \
  --high-assurance \
  --authentication-timeout-mins $((60 * 24 * 7)) \
  --format json | python3 -c "import sys,json; d=json.load(sys.stdin); r=d[0] if isinstance(d,list) else d; print(r.get('id') or r.get('data',[{}])[0].get('id'))")

echo "S3 gateway ID: $GATEWAY_ID"
```

| Flag | Purpose |
|---|---|
| `--bucket` | Locks the gateway to a single S3 bucket — required with `--no-allow-multiple-keys` |
| `--domain <YOUR_INSTITUTION_DOMAIN>` | Restricts access to <YOUR_INSTITUTION_DOMAIN> Globus identities |
| `--s3-endpoint` | AWS S3 regional endpoint for the target bucket's region |
| `--s3-user-credential` | Enables per-identity credential slots |
| `--admin-managed-credentials` | Allows admin to register a single IAM key for all identities via CLI |
| `--no-allow-multiple-keys` | **Critical.** Without this flag (`s3_allow_multi_keys: true` is the default), Globus treats the first component of every path as a bucket name. Transferring to `/mmps_mproc/sub-xxx` would try to write to a bucket named `mmps_mproc` and fail with `550-Globus-S3-Error: Bucket not allowed`. With this flag, the bucket is fixed by `--bucket` and collection paths are relative to the bucket root. |
| `--high-assurance` | Required because the ABCD source collection is HA |
| `--authentication-timeout-mins` | Session expiry before reauthentication (1 week; 30-day max for HA) |

---

## Step 4: Collection

Create a mapped collection rooted at the S3 bucket root. Transfer destination paths are relative to the bucket (e.g., `/mmps_mproc/sub-xxx` maps to `s3://<YOUR_TEMP_S3_BUCKET>/mmps_mproc/sub-xxx`).

```bash
S3_BUCKET="abcd-v7"

COLLECTION_ID=$(globus-connect-server collection create \
  "${GATEWAY_ID}" "/${S3_BUCKET}" "cloudpipe-s3" \
  --allow-guest-collections \
  --enable-https \
  --format json | python3 -c "import sys,json; d=json.load(sys.stdin); r=d[0] if isinstance(d,list) else d; print(r.get('id') or r.get('data',[{}])[0].get('id'))")

echo "Collection ID: $COLLECTION_ID"
```

> **JSON extraction note**: `collection create --format json` returns the collection object directly (`{"id": "..."}`) while `storage-gateway create` wraps it in `{"data": [...]}`. The Python extractor `r=d[0] if isinstance(d,list) else d; print(r.get('id') or r.get('data',[{}])[0].get('id'))` handles both formats. Do not use `d['data'][0]['id']` for collection create — it will raise `KeyError: 'data'`.

> **Path semantics**: The GCS S3 connector always treats the first path component as the bucket name, regardless of whether `--no-allow-multiple-keys` is set. Root the collection at `/<bucket-name>` (e.g., `/abcd-v7`). The `--no-allow-multiple-keys` flag restricts access to that one bucket but does not change the path format. With the collection rooted at `/abcd-v7`, a collection path of `/mmps_mproc/sub-xxx` maps to `s3://abcd-v7/mmps_mproc/sub-xxx`. Set `globus_dest_base_path = "/mmps_mproc"` in `terraform.tfvars`.
>
> **Common mistake**: Rooting the collection at `/` causes every transfer path's first component (e.g., `mmps_mproc`) to be interpreted as a bucket name, resulting in `550-Globus-S3-Error: Bucket not allowed`.

---

## Step 5: Register IAM credentials

Register the IAM user access key for the pipeline's Globus identity. The command prompts for the access key ID and secret key interactively — do not pass them as arguments.

```bash
globus-connect-server user-credentials s3-create \
  "${GATEWAY_ID}" \
  --globus-identity <YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>
# Prompts: AWS Access Key ID, then AWS Secret Access Key
```

To update an existing credential (e.g., after rotating the IAM key):

```bash
globus-connect-server user-credentials s3-create \
  "${GATEWAY_ID}" \
  --globus-identity <YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN> \
  --replace-existing
```

Verify the credential was registered:

```bash
globus-connect-server user-credentials list
```

> **Note**: If running with `GCS_CLI_*` service credentials set, `user-credentials list` will return an empty table — the service account identity has no credentials of its own. This is expected. Use `globus ls 00666689-6b52-444d-b6a8-57a3ce6ee97c:/` to confirm S3 access is working.

---

> **After collection creation**: allow 2–3 minutes before submitting a transfer workflow. Globus takes a short time to propagate a newly created collection through its relay infrastructure. Transfers submitted immediately may fail transiently and exhaust retries before the collection is reachable.

## Step 6: Store collection ID in SSM

```bash
aws ssm put-parameter \
  --region <YOUR_AWS_REGION> \
  --name /cloudpipe/globus/collection-id \
  --type String \
  --value "$COLLECTION_ID" \
  --overwrite
```

> **SSM staleness**: The Prefect queue manager reads SSM once at startup and caches the collection ID for the entire flow run. If SSM is updated while a flow run is active, cancel and restart the flow run.

---

## Step 7: Subscribe the endpoint to the UW-Madison HA subscription

Email `globusadmins@<YOUR_INSTITUTION_DOMAIN>` with the endpoint UUID and request it be added to the UW-Madison Globus High Assurance Subscription (`<YOUR_GLOBUS_SUBSCRIPTION_UUID>`).

Until the subscription is activated, `storage-gateway create s3 --high-assurance` will fail with a subscription error.

> **Subscription is tied to the endpoint UUID.** If the instance is replaced and a new endpoint is created, submit the new UUID to `globusadmins@<YOUR_INSTITUTION_DOMAIN>` again. Use `gcs-auto-reregister` for routine instance replacements — it preserves the existing endpoint UUID and re-registers the node non-interactively.

---

## Step 8: Register a Globus native app and obtain a refresh token

The Argo transfer step authenticates using a **native app client ID** and a **refresh token** stored in AWS Secrets Manager.

### Create the native app

This is a **separate registration** from the service account used in Step 2. Using the service account UUID here will fail with "Only native app clients can access this URL".

1. Go to [developers.globus.org](https://developers.globus.org) and log in.
2. Select **Register a native app**.
3. Name it `cloudpipe-transfer`.
4. Add `https://auth.globus.org/v2/web/auth-code` as a redirect URI.
5. Copy the **Client UUID**.

### Generate and store the refresh token

```bash
export GLOBUS_NATIVE_APP_CLIENT_ID=<native-app-client-uuid>
python images/globus/setup_auth.py
```

Do **not** pass `--dest-collection-id`. HA collections do not expose a `data_access` scope to external clients.

> **High Assurance requirement**: `setup_auth.py` uses `prompt=login` to force a fresh authentication event. Reusing an existing browser session can result in a `No effective ACL rules` 403 error.

> **Token expiry**: HA collections require periodic reauthentication (30-day max). When the token expires, re-run `setup_auth.py`.

---

## Step 9: Apply Kubernetes manifests

```bash
kubectl apply -f argo/workflows/cloudpipe_minproc/globus-credentials-external-secret.yaml

# Verify sync
kubectl get externalsecret globus-credentials -n argo-workflows
kubectl get secret globus-credentials -n argo-workflows

kubectl apply -f argo/workflows/cloudpipe_minproc/globus-transfer-workflow-template.yaml
```

---

## Verification

```bash
globus-connect-server endpoint show
globus-connect-server storage-gateway list
globus-connect-server collection list
globus-connect-server user-credentials list
```

Check the collection is accessible from the [Globus web app](https://app.globus.org): search for the endpoint by name or UUID, confirm it is active, and browse the collection root to verify S3 bucket contents appear.

---

## Instance replacement (automated)

When the GCS EC2 instance is replaced, the `gcs-auto-reregister` systemd service re-registers the node non-interactively on first boot using the deployment key stored in SSM. The existing endpoint UUID is preserved.

After instance replacement, the **IAM credentials registered in Step 5 are preserved in Globus** — they are stored in Globus's system, not on the instance. No re-registration is needed unless the IAM key is rotated.

---

## Credential rotation

### Rotating the IAM access key

The IAM user key registered in Step 5 is a long-lived credential and should be rotated periodically. When you rotate it in AWS, you must update Globus's copy before the old key is deleted.

1. **Generate a new IAM access key** in the AWS console or CLI for the IAM user.

2. **SSM into the GCS instance**:

   ```bash
   aws ssm start-session \
     --target $(aws ssm get-parameter --name /cloudpipe/globus/instance-id \
       --query Parameter.Value --output text) \
     --region <YOUR_AWS_REGION>
   sudo -i
   ```

3. **Update the credential in Globus** (prompts interactively — enter the new key when asked):

   ```bash
   globus-connect-server user-credentials s3-create \
     3cdb1567-f3fe-416c-a52b-ffaf46f9a2c9 \
     --globus-identity <YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN> \
     --replace-existing
   # Enter new Access Key ID and Secret Access Key at the prompts
   ```

4. **Verify** the credential was updated:

   ```bash
   globus-connect-server user-credentials list
   # Verify the updated timestamp changed; check the correct gateway ID appears
   ```

5. **Delete the old IAM access key** in the AWS console once the new key is confirmed working.

### Rotating the Globus refresh token

To rotate the refresh token (expiry or team change):

```bash
export GLOBUS_NATIVE_APP_CLIENT_ID=e8f5215c-8d92-4899-b920-48ec9a412d28
python images/globus/setup_auth.py
```

To force an immediate K8s secret sync without waiting for the hourly ESO refresh:

```bash
kubectl annotate externalsecret globus-credentials \
  -n argo-workflows \
  force-sync=$(date +%s) --overwrite
```
