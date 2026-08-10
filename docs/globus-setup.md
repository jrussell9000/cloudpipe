# Globus Connect Server — Setup Guide

One-time setup for a Globus Connect Server (GCS) v5 endpoint that writes ABCD data
directly to S3 via the **GCS native S3 storage gateway** (`--s3-user-credential
--admin-managed-credentials`). In this mode the gateway authenticates to S3 with a
static IAM user access key managed by the GCS admin; no FUSE mount or EBS staging
volume is required. GridFTP data channels write directly to the S3 bucket.

For architecture, the two-credential model, High Assurance background, credential
rotation, instance replacement, and day-to-day operations, see
[globus.md](globus.md) — this guide covers only the ordered build-from-scratch steps
and does not repeat that reference material.

> The legacy POSIX+EBS staging approach is retained as a historical alternative in the
> [appendix](#appendix-posixebs-staging-historical); it is not the current deployment.

---

## Current deployment

| Resource | Value |
|---|---|
| Endpoint ID | `<YOUR_GLOBUS_ENDPOINT_ID>` |
| S3 gateway ID | `<YOUR_GLOBUS_S3_GATEWAY_ID>` |
| Collection ID | `<YOUR_GLOBUS_DEST_COLLECTION_ID>` |
| Collection name | `cloudpipe-s3` |
| SSM parameter | `/cloudpipe/globus/collection-id` |
| Registered IAM credential identity | `<YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>` |
| IAM user | `programmatic-access-smph_psy_neuro_globus` |
| Native app client ID | `<YOUR_GLOBUS_NATIVE_APP_CLIENT_ID>` |

---

## Why admin-managed S3 credentials (not mountpoint-s3, not IMDS)

AWS Mountpoint for S3 only supports sequential writes starting from byte 0 of a new
file. Globus GridFTP uses Extended Block Mode, which delivers data out-of-order and
performs non-sequential writes even when `max_parallelism` is set to 1. Any
non-sequential write is rejected by the Mountpoint FUSE layer with `EINVAL`, causing
transfers to fail with `500 globus_xio: System error in write: Invalid argument`. The
S3 gateway bypasses FUSE entirely.

The GCS S3 connector supports exactly three credential modes:

| Mode | Credential mechanism | Suitable for automation |
|---|---|---|
| `--s3-unauthenticated` | Anonymous (no credentials) | Only for public buckets — not applicable |
| `--s3-user-credential` (alone) | Per-user: each Globus identity registers their own AWS key in the Globus web UI | No — requires manual web UI step per identity; breaks fully automated pipelines |
| `--s3-user-credential` + `--admin-managed-credentials` | Single IAM key registered by admin via CLI; applies to all mapped identities | Yes — single key, rotated by admin |

There is **no IMDS / EC2 instance role support** in the S3 connector. `--admin-managed-credentials`
is the closest equivalent to a service-account credential: the admin registers one IAM
user key that all authenticated Globus identities use for S3 access. The tradeoff is
that this static key must be rotated periodically (see
[globus.md → Credential rotation](globus.md#credential-rotation)).

---

## Prerequisites

### Globus subscription

The `--high-assurance` flag requires a **Globus Standard subscription**. The ABCD source
collection is High Assurance, and Globus requires both sides of a transfer to be HA, so
the cloudpipe destination collection must be HA too. Background:
[globus.md → High Assurance](globus.md#high-assurance).

**For UW-Madison deployments**, request endpoint membership under the UW-Madison HA
subscription (Step 7):

```
Subscription ID: <YOUR_GLOBUS_SUBSCRIPTION_UUID>
Contact: <YOUR_GLOBUS_SUBSCRIPTION_ADMIN_EMAIL>
```

### AWS infrastructure

The cloudpipe Terraform module (`terraform/modules/globus`) provisions:

- EC2 instance (`c5n.xlarge`, Ubuntu 22.04) with Elastic IP
- Security group (ports 443 and 50000–51000 open to `0.0.0.0/0`, SSH restricted to admin prefix list)
- IAM role with S3 read/write and SSM access (used by the instance for SSM and non-Globus S3 operations)
- SSM parameters for instance ID, collection ID, and deployment key
- EventBridge nightly stop scheduler

Run `terraform apply` from `terraform/` before proceeding.

### IAM user for the S3 gateway

The S3 gateway requires a dedicated **IAM user** (not a role) with programmatic access
to the S3 bucket. Create one with a policy granting `s3:GetObject`, `s3:PutObject`,
`s3:DeleteObject`, `s3:ListBucket` on the target bucket. Generate an access key pair —
you register it with the gateway in Step 5.

> **Do not pass the key on the command line.** `user-credentials s3-create` prompts for
> it interactively to avoid exposing it in shell history.

### Globus service account

Register a **service account** at the [Globus Developers Portal](https://developers.globus.org)
to own the endpoint, rather than a personal NetID:

1. Log in with an institutional identity.
2. Select **Register a thick client or script**.
3. Name it (e.g., `cloudpipe-gcs`).
4. Copy the **Client UUID** — used as `--client-id` in endpoint setup and as
   `globus_client_id` in `terraform.tfvars`.

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

**Ports 443 and 50000–51000 must be open to `0.0.0.0/0`.** GridFTP data channels are
peer-to-peer between endpoints at other institutions worldwide. Access control is
enforced by Globus OAuth2/OIDC, not by network-layer filtering.

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

The equivalent manual steps are below.

### 2a. Create the endpoint

```bash
GCS_CLIENT_ID="<your-globus-thick-client-uuid>"

globus-connect-server endpoint setup "cloudpipe" \
  --organization "BRAVE Research Collaborative" \
  --contact-email "<YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>" \
  --owner "<YOUR_GLOBUS_SUBSCRIPTION_ADMIN_EMAIL>" \
  --client-id "$GCS_CLIENT_ID" \
  --public \
  --agree-to-letsencrypt-tos
```

Authenticate with your institutional (<YOUR_INSTITUTION_DOMAIN>) identity when prompted.

### 2b. Save the deployment key

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

### 2e. Create a service account for non-interactive management

The `globus-connect-server login` session from Step 2d expires after ~30 days; any
management command run after expiry fails with an authentication error. To eliminate
this, register a **confidential client** and add it as an endpoint administrator. All
subsequent `globus-connect-server` commands then authenticate non-interactively via
three environment variables. (Day-to-day usage of these credentials is documented in
[globus.md → Service credentials](globus.md#service-credentials).)

#### Create the service account client

1. Go to [app.globus.org/settings/developers](https://app.globus.org/settings/developers) and log in.
2. Select **Register a service account or application credential for automation**, then create or select a project.
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

> The endpoint ID (`/cloudpipe/globus/endpoint-id`) is written automatically by
> `gcs-finalize-setup` — no manual action needed for that parameter.
>
> **On instance replacement**: `gcs-auto-reregister` reads these SSM parameters on every
> boot and runs an S3 credential health check automatically — no manual re-login is
> needed after routine instance replacement.

---

## Step 3: S3 storage gateway

`--s3-user-credential` enables per-identity credential slots; `--admin-managed-credentials`
allows the admin to register a single IAM key that applies to all identities (Step 5).

```bash
S3_BUCKET="<YOUR_S3_BUCKET>"

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

Create a mapped collection rooted at the S3 bucket root. Transfer destination paths are
relative to the bucket (e.g., `/mmps_mproc/sub-xxx` maps to `s3://<YOUR_S3_BUCKET>/mmps_mproc/sub-xxx`).

```bash
S3_BUCKET="<YOUR_S3_BUCKET>"

COLLECTION_ID=$(globus-connect-server collection create \
  "${GATEWAY_ID}" "/${S3_BUCKET}" "cloudpipe-s3" \
  --allow-guest-collections \
  --enable-https \
  --format json | python3 -c "import sys,json; d=json.load(sys.stdin); r=d[0] if isinstance(d,list) else d; print(r.get('id') or r.get('data',[{}])[0].get('id'))")

echo "Collection ID: $COLLECTION_ID"
```

> **JSON extraction note**: `collection create --format json` returns the collection
> object directly (`{"id": "..."}`) while `storage-gateway create` wraps it in
> `{"data": [...]}`. The Python extractor
> `r=d[0] if isinstance(d,list) else d; print(r.get('id') or r.get('data',[{}])[0].get('id'))`
> handles both. Do not use `d['data'][0]['id']` for collection create — it raises
> `KeyError: 'data'`.

> **Path semantics**: The GCS S3 connector always treats the first path component as the
> bucket name, regardless of whether `--no-allow-multiple-keys` is set. Root the
> collection at `/<bucket-name>` (e.g., `/<YOUR_S3_BUCKET>`). With the collection rooted at
> `/<YOUR_S3_BUCKET>`, a collection path of `/mmps_mproc/sub-xxx` maps to
> `s3://<YOUR_S3_BUCKET>/mmps_mproc/sub-xxx`. Set `globus_dest_base_path = "/mmps_mproc"` in
> `terraform.tfvars`.
>
> **Common mistake**: Rooting the collection at `/` causes every transfer path's first
> component (e.g., `mmps_mproc`) to be interpreted as a bucket name, resulting in
> `550-Globus-S3-Error: Bucket not allowed`.

> **After collection creation**: allow 2–3 minutes before submitting a transfer
> workflow. Globus takes a short time to propagate a newly created collection through
> its relay infrastructure. Transfers submitted immediately may fail transiently and
> exhaust retries before the collection is reachable.

---

## Step 5: Register IAM credentials

Register the IAM user access key for the pipeline's Globus identity. The command prompts
for the access key ID and secret key interactively — do not pass them as arguments.

```bash
globus-connect-server user-credentials s3-create \
  "${GATEWAY_ID}" \
  --globus-identity <YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>
# Prompts: AWS Access Key ID, then AWS Secret Access Key
```

Verify the credential was registered:

```bash
globus-connect-server user-credentials list
```

> **Note**: If running with `GCS_CLI_*` service credentials set, `user-credentials list`
> returns an empty table — the service account identity has no credentials of its own.
> This is expected. Use `globus ls <YOUR_GLOBUS_DEST_COLLECTION_ID>:/` to confirm S3
> access is working.

(To rotate this key later, see [globus.md → Credential rotation](globus.md#credential-rotation).)

---

## Step 6: Store the collection ID in SSM

```bash
aws ssm put-parameter \
  --region <YOUR_AWS_REGION> \
  --name /cloudpipe/globus/collection-id \
  --type String \
  --value "$COLLECTION_ID" \
  --overwrite
```

> **SSM staleness**: The Prefect queue manager reads SSM once at startup and caches the
> collection ID for the entire flow run. If SSM is updated while a flow run is active,
> cancel and restart the flow run.

---

## Step 7: Subscribe the endpoint to the UW-Madison HA subscription

Email `<YOUR_GLOBUS_SUBSCRIPTION_ADMIN_EMAIL>` with the endpoint UUID and request it be added to the
UW-Madison Globus High Assurance Subscription (`<YOUR_GLOBUS_SUBSCRIPTION_UUID>`).

Until the subscription is activated, `storage-gateway create s3 --high-assurance` fails
with a subscription error.

> **Subscription is tied to the endpoint UUID.** If the instance is replaced and a new
> endpoint is created, submit the new UUID to `<YOUR_GLOBUS_SUBSCRIPTION_ADMIN_EMAIL>` again. Use
> `gcs-auto-reregister` for routine instance replacements — it preserves the existing
> endpoint UUID and re-registers the node non-interactively.

---

## Step 8: Register a Globus native app and obtain a refresh token

The Argo transfer step authenticates using a **native app client ID** and a **refresh
token** stored in AWS Secrets Manager.

This is a **separate registration** from the service account used in Step 2 — they serve
different purposes and must not be confused:

| App | Registration type | Used by |
|---|---|---|
| `cloudpipe-gcs` (service account) | Thick client / script | `gcs-finalize-setup` — owns the GCS endpoint; `globus_client_id` in `terraform.tfvars` |
| `cloudpipe-transfer` (native app) | Native app | `setup_auth.py` and Argo workflows at runtime — generates the refresh token |

Using the service account UUID as `GLOBUS_NATIVE_APP_CLIENT_ID` fails with "Only native
app clients can access this URL".

### Create the native app

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

Do **not** pass `--dest-collection-id`. HA collections do not expose a `data_access`
scope to external clients (attempting it returns "requested unknown scopes"). The HA
session policy is enforced by Globus at transfer time instead.

The script prints a URL. Open it, log in with your institutional identity — a fresh
login is required (`prompt=login`) to satisfy the HA session policy, otherwise reusing an
existing browser session can produce a `No effective ACL rules` 403. Paste the auth code
back. The script stores the credentials in Secrets Manager under `globus/refresh-token`
and triggers a K8s secret sync.

> **Per-lab tokens**: Each lab manages its own `globus/refresh-token` secret. The
> workflow template and External Secret are shared infrastructure — only the secret value
> changes per deployment.
>
> **Token expiry**: HA collections require periodic reauthentication (30-day max). When
> the token expires, re-run `setup_auth.py`
> ([globus.md → Credential rotation](globus.md#credential-rotation)).

---

## Step 9: Apply Kubernetes manifests

```bash
kubectl apply -f argo/workflows/cloudpipe_minproc/globus-credentials-external-secret.yaml

# Verify sync (External Secrets Operator refreshes hourly)
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

Check the collection from the [Globus web app](https://app.globus.org): search for the
endpoint by name or UUID, confirm it is active, and browse the collection root to verify
S3 bucket contents appear.

For ongoing operational checks, troubleshooting, and recovery procedures, see
[globus.md](globus.md).

---

## Appendix: POSIX+EBS staging (historical)

> **Not the current deployment.** The running system uses the S3 storage gateway above
> (`globus_use_s3_gateway = true`). The POSIX+EBS staging approach described here is
> retained for reference; it does not require a static IAM user key or a Globus S3
> subscription add-on, but adds an EBS staging volume and an extra `aws s3 sync` step and
> is slower and more operationally complex.

In this mode (`globus_use_s3_gateway = false` in `terraform/terraform.tfvars`), a GCS
**POSIX storage gateway** presents a local directory backed by a 500 GB gp3 EBS volume as
the Globus collection. GridFTP writes land on EBS at `/data/globus-staging`; the
`globus-s3-sync-template` Argo step then runs `aws s3 sync` via SSM to push files to S3
and clean up the staging area. Because it uses the EC2 instance IAM role via the OS
credential chain (no static key) and maps any authenticated identity to the local
`cloudpipe` service account via `gcs-identity-map.py`, no per-user credential
registration is required.

Activate by setting `globus-use-s3-gateway: "false"` in the Argo workflow parameters; the
Prefect queue manager passes this to the master workflow. The Terraform EBS volume
(`aws_ebs_volume.globus_staging`) is only provisioned when `globus_use_s3_gateway = false`.

### POSIX gateway and collection creation

The `gcs-finalize-setup` script, in POSIX mode, creates the gateway against the local
staging mount and roots the collection there:

```bash
# POSIX storage gateway (maps any authenticated identity to the local cloudpipe user)
GATEWAY_ID=$(globus-connect-server storage-gateway create posix "cloudpipe-s3" \
  --domain <YOUR_INSTITUTION_DOMAIN> \
  --high-assurance \
  --authentication-timeout-mins $((60 * 24 * 7)) \
  --identity-mapping "external:/usr/local/bin/gcs-identity-map.py" \
  --format json | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Mapped collection rooted at the staging directory
COLLECTION_ID=$(globus-connect-server collection create \
  "$GATEWAY_ID" "/data/globus-staging" "cloudpipe-s3" \
  --allow-guest-collections \
  --enable-https \
  --format json | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
```

Transfer destination paths are relative to the staging root (e.g. `/mmps_mproc/sub-xxx`)
— no bucket-name prefix needed, since `aws s3 sync` prepends the bucket.

### gcs-auto-reregister boot logic (both modes)

When the EC2 instance is replaced, the `gcs-auto-reregister` systemd service re-registers
the node non-interactively on first boot — the endpoint, collection, and refresh token
all stay the same; only the node IP changes. Progress is logged to
`/var/log/gcs-auto-reregister.log`.

| Condition | Action |
|---|---|
| `globus-gridftp-server` already active | No-op — skip |
| `/etc/globus-connect-server/deployment-key.json` exists (stop/start of same instance) | Re-run `node setup` with new IP, restart gridftp |
| No local key, SSM has a real deployment key | Pull key from SSM, run `node setup`, start gridftp |
| No local key, SSM placeholder not yet replaced | Print instructions to run `gcs-finalize-setup` manually, exit |

`gcs-auto-reregister` only re-registers a node against an existing endpoint. If the
endpoint itself is deleted, see
[globus.md → Endpoint recovery](globus.md#endpoint-recovery-after-accidental-deletion).
