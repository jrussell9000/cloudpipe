# Globus

CloudPipe uses Globus Connect Server (GCS) v5 to transfer ABCD minimally-preprocessed data from the DAIRC MMPS endpoint to the cloudpipe S3 bucket. This document covers the architecture, credentials model, operational procedures, and recovery steps.

For the initial setup walkthrough (endpoint creation, storage gateway, IAM credentials, refresh token), see [globus-setup.md](globus-setup.md).

---

## Architecture

```
DAIRC MMPS Globus endpoint
  (source collection: <YOUR_GLOBUS_SOURCE_COLLECTION_ID>)
        │
        │  GridFTP data channel (port 50000–51000)
        ▼
GCS v5 endpoint — EC2 c5n.xlarge, Elastic IP, Ubuntu 22.04
  S3 storage gateway (one registered key pair, admin-managed)
        │
        │  S3 multipart PUT (no FUSE, no EBS staging)
        ▼
  signing listener on 127.0.0.1 — one per gateway
    re-signs with a prefix-scoped role the instance assumes
        │
        ▼
s3://<YOUR_S3_BUCKET>/mmps_mproc/{subject}/{session}/...
```

The S3 storage gateway writes GridFTP data directly to S3 via multipart upload. No local staging volume or EFS is involved. The alternative POSIX+EBS staging approach was removed in 2026-09 ([ADR 001](decisions/001-s3-gateway-over-posix-staging.md)); the non-Globus ingress path is `presynced` ([data-ingress.md](data-ingress.md)).

The listener step is what removes the long-lived AWS access key from this path: the gateway's `s3_endpoint` is a loopback address, and the credentials that actually reach S3 are hourly role credentials the listener obtains on the instance ([ADR 019](decisions/019-signing-proxy-for-globus-ingress.md)). **Staging is cut over; production is not** — it still signs with its IAM user's key until `openspec/changes/retire-globus-s3-access-keys` section 8 ([globus-setup.md → Current deployment](globus-setup.md#current-deployment)).

### Why S3 gateway and not mountpoint-s3

AWS Mountpoint for S3 only supports sequential writes from byte 0. Globus GridFTP uses Extended Block Mode, delivering data out-of-order and performing non-sequential writes. Any non-sequential write fails with `EINVAL` through the FUSE layer. The S3 gateway bypasses FUSE entirely.

---

## Current deployment values

| Resource | Value |
|---|---|
| EC2 instance type | `c5n.xlarge` |
| Endpoint ID | `<YOUR_GLOBUS_ENDPOINT_ID>` |
| S3 storage gateway ID | `<YOUR_GLOBUS_S3_GATEWAY_ID>` |
| Destination collection ID | `<YOUR_GLOBUS_DEST_COLLECTION_ID>` |
| Collection name | `cloudpipe-s3` |
| Source collection (DAIRC MMPS) | `<YOUR_GLOBUS_SOURCE_COLLECTION_ID>` |
| Source base path | `/abcd/derivatives/mmps_mproc` |
| Native app client ID | `<YOUR_GLOBUS_NATIVE_APP_CLIENT_ID>` |
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

All workflow-accessible Globus config lives in SSM. Terraform creates these parameters with placeholder values and never reverts them afterwards (`ignore_changes`); the "Set by" column says what fills each one in. The endpoint in service today was built by hand before `bootstrap-endpoint` existed, with the older `gcs-finalize-setup` script, which wrote the same parameters.

| Parameter | Set by | Content |
|---|---|---|
| `/cloudpipe/globus/instance-id` | Terraform | EC2 instance ID |
| `/cloudpipe/globus/collection-id` | `globus configure` ([setup step 3.6b](globus-setup.md#36b-check-the-configuration-and-record-the-collection-id)), over the placeholder only — never over a different id | Destination GCS collection UUID |
| `/cloudpipe/globus/deployment-key` | `globus bootstrap-endpoint` | GCS deployment key JSON (SecureString; used for non-interactive node re-registration on instance replacement) |
| `/cloudpipe/globus/endpoint-id` | `globus bootstrap-endpoint`, written last | GCS endpoint UUID — the marker every tool reads as "this deployment has an endpoint" |
| `/cloudpipe/globus/gcs-client-id` | `globus register-service-client`, or by hand ([setup step 3.3](globus-setup.md#33-store-the-service-clients-id-and-secret)) | Globus Auth service client UUID (SecureString; see [Service credentials](#service-credentials)) |
| `/cloudpipe/globus/gcs-client-secret` | `globus register-service-client`, or by hand (setup step 3.3) | Globus Auth service client secret (SecureString). The command is the only place this value exists outside SSM — Globus discloses it once, at creation |
| `/cloudpipe/globus/source-collection-id` | Terraform | Source collection UUID (DAIRC MMPS) |
| `/cloudpipe/globus/source-base-path` | Terraform | Root path on source collection |
| `/cloudpipe/globus/node-report` | `cloudpipe-gcs-boot` (on the instance) | The endpoint's node records as of the last registration, for `globus doctor` check 13. Only the node can run `node list`, and it is stopped between batches, so this is the only way a workstation can see leftover records from a replaced host |
| `/cloudpipe/globus/reconcile-plan` | The reconcile (on the instance), on every `plan` and `apply` | Whether the endpoint matched its configuration, for `globus doctor` check 7 — same reason as `node-report`: the comparison needs listings only the node can make. The report records which gateway it examined, because `globus configure` scopes each run to one; check 7 refuses a plan written for the other environment rather than reading it as clean |
| `/cloudpipe/globus/listener-report` | The listener install document (on the instance), last step of every run | Whether each declared gateway's signing listener is serving, for `globus doctor` check 14. The listener binds `127.0.0.1` and nothing else — that is what makes an unauthenticated signing proxy safe — so this is the only evidence a workstation can ever have. Deployment-wide: one host runs every environment's listeners, and each environment picks out its own gateway |

### IAM roles

| Role | Used by | Permissions |
|---|---|---|
| `cloudpipe-globus-*` (instance profile) | GCS EC2 instance | S3 read/write on `<YOUR_S3_BUCKET>`, SSM core, SSM write to collection-id + deployment-key + endpoint-id + node-report + reconcile-plan + listener-report (a list of named ARNs, so a new host-written parameter needs its ARN added or the write is a silent AccessDenied); SSM read of everything under `/cloudpipe/globus/` and nothing outside it (a Deny overrides the managed policy's `Resource: "*"`) |
| `cloudpipe-argo-runner` | `argo-workflows-runner` SA | EC2 `StartInstances` (scoped to Globus instance ID), `DescribeInstanceStatus` + `DescribeInstances` (not resource-scoped), SSM `GetParameter` for instance-id + collection-id |

The runner role gets EC2 and SSM permissions from `terraform/modules/globus/main.tf` (`runner_globus_ec2` inline policy), not from the base runner IAM module.

The runner role has **no** SSM `SendCommand` / `GetCommandInvocation` permissions. It once granted them for the POSIX sync step; that step and its `runner_globus_ssm` policy were removed in 2026-09, and the S3 gateway needs neither.

### Credentials: two separate credential sets

Globus transfers require two independent credentials:

**1. The S3 gateway's registered key pair (admin-managed)**

Every S3 storage gateway needs one key pair registered against it with `globus-connect-server user-credentials s3-create`, because the GCS S3 connector's credential schema holds only a key id and a secret — it has no field for a session token, so it cannot hold role credentials ([ADR 019](decisions/019-signing-proxy-for-globus-ingress.md)). Whatever is registered is stored in Globus's infrastructure, not on the instance and not in SSM.

What that key pair *is* depends on whether the gateway has been cut over to its signing listener:

| | Staging `cloudpipe-s3-staging` | Production `cloudpipe-s3` |
|---|---|---|
| Registered | AWS's published example pair, `AKIAIOSFODNN7EXAMPLE` — **a placeholder, not a credential** | A real access key of IAM user `<YOUR_GLOBUS_S3_IAM_USER>` |
| Writes as | role `cloudpipe-globus-staging-writer`, hourly credentials, confined to `scratch/globus-staging/` | the IAM user, which can write to every bucket in the account |
| Rotation | nothing to rotate | [Rotating the IAM access key](#rotating-the-iam-access-key), until the cutover |

Production moves to the staging column in `openspec/changes/retire-globus-s3-access-keys` section 8. The EC2 instance role covers all other AWS API calls (SSM, EC2 metadata), and — on a cut-over gateway — is also what the listener uses to assume the writer role.

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
- `--authentication-timeout-mins` governs how often a human must re-authenticate.
  **There is no 30-day High Assurance ceiling** — that claim appeared in earlier
  revisions of this page and is wrong. Measured 2026-09-15 through the Transfer
  API's `get_endpoint`, the ABCD source collection is High Assurance *and* set to
  `525600` minutes (one year), which a 30-day ceiling would forbid.
  Our weekly re-login is therefore **self-imposed**: `cloudpipe-s3` was created
  with `10080` (7 days). Raising it is a one-line gateway update, not a
  negotiation with Globus.
- Until that raise is applied, reauthenticate weekly and never start a batch on a
  session older than 7 days.
- `pixi run globus login` uses `prompt=login` to force a fresh auth event — reusing an existing browser session can produce a `No effective ACL rules` 403
- When the session times out, transfers fail and the login must be re-run

---

## Day-to-day operations

Everything here goes through `pixi run globus`, from the repository root, logged
in with `aws sso login`. Every command takes `--json` for scripts and
`--non-interactive --yes` for unattended use; the published fields are in
[globus-contract.md](globus-contract.md).

| Question | Command |
|---|---|
| Where do things stand? Instance, session expiry, collections, active transfers | `pixi run globus status` |
| Is anything broken, and what fixes it? | `pixi run globus doctor` (add `--start-instance` if the instance is stopped) |
| Which transfers are running for a subject? | `pixi run globus tasks --subject <id>` |
| Cancel them | `pixi run globus tasks --subject <id> --cancel` |
| Renew the Globus session | `pixi run globus login` |
| Change the gateway or collection | edit the answers document, then `pixi run globus configure` |

`doctor` checks in dependency order and names the command or person that fixes
each failure; a check it could not run is reported as not run, never as passing.

### Changing the configuration

The answers document is the declaration; `configure` makes the endpoint match it.

```bash
pixi run globus init --force           # re-render from the edited answers
pixi run globus configure --plan-only  # read what would change
pixi run globus configure              # apply it, after asking
```

`configure` never deletes anything, never touches an object the declaration does
not name, and refuses — naming the field — rather than delete-and-recreate when a
field Globus cannot update in place differs. A plan you did not expect is a reason
to stop, not to apply.

### Staging gateway

A **staging gateway** is optional, and off unless `staging_enabled: true` is in
the answers document. It is a place to try a change to the Globus side before it
reaches production — a GCS upgrade, a rebuilt AMI, a change to the reconcile.

**A fresh deployment does not need one to set up.** Staging exists for a
situation only some deployments are in: a production endpoint configured by hand
before this tool, which the tool must be proved against before it may change
anything. This repository's own deployment is that case, so it keeps staging
permanently and holds production read-only (`production_managed: false`) until a
staging run has proved the reconcile. A deployment built by the tool from
nothing has nothing to protect: its production is managed from the start, and
staging is worth adding only if you want somewhere to try upgrades later.

| | Production | Staging |
|---|---|---|
| Gateway and collection | `<collection_name>` | `<collection_name>-staging` |
| Rooted at | `/<bucket>` | `/<bucket>/scratch/globus-staging` |
| SSM settings | `/<deployment>/globus/` | `/<deployment>/globus/staging/` |
| Pipeline login token | `globus/refresh-token` | `globus/refresh-token-staging` |
| Selected with | (default) | `--env staging` on any command |

What keeps it separate, and what does not:

- **It is the same endpoint and the same instance.** A separate staging endpoint
  would need its own subscription request. So run staging probes only when no
  batch is running — they share the production node.
- **`configure --env staging` cannot touch production.** The restriction to one
  gateway is applied on the instance, not only by the CLI.
- **Its data expires on its own.** It is written under `scratch/`, which the
  bucket's lifecycle rule clears after 7 days, and it stays inside the bucket
  whose CloudTrail data events are the audit record.
- **It costs almost nothing** — two Secrets Manager secrets and a few SSM
  parameters. Its login token needs renewing only when you use it.

Turning staging off later, after it has been applied, removes its AWS side on the
next `terraform apply`. The staging gateway and collection on the endpoint are
left alone — `configure` never deletes.

---

## Credential rotation

The Globus session needs a human every week, by default. The production S3 key
needs one whenever your security policy says — until the cutover removes it.

### Rotating the IAM access key

**Production only, and only until the cutover.** A cut-over gateway has no key to
rotate: its listener assumes a role and the credentials expire hourly on their own
([ADR 019](decisions/019-signing-proxy-for-globus-ingress.md)). Staging is already
in that state.

The IAM user key registered with the production S3 gateway is a long-lived
credential stored by Globus, not on the instance. There is no command for this;
it is done by hand:

1. Check the user has a free key slot — AWS allows two per user, and a new key
   must exist before the old one is removed:
   ```bash
   aws iam list-access-keys --user-name <iam-user>
   ```
   If both slots are taken, stop and decide which key may be deleted before
   going further. Do not delete a key you cannot account for.
2. Create the new key in the AWS console. Do not print it in a terminal.
3. Open a shell on the instance and load the service credentials
   ([Service credentials](#service-credentials)).
4. Replace the credential in Globus — it **prompts** for the new key:
   ```bash
   globus-connect-server user-credentials s3-create \
     <YOUR_GLOBUS_S3_GATEWAY_ID> \
     --globus-identity <YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN> \
     --replace-existing
   ```
5. Confirm with `pixi run globus doctor` — the destination listing check exercises
   the new key.
6. Deactivate, then delete, the old key in the AWS console.

### Recognising an expired session

The failure is **not** reported as an expired token. `transfer.py`'s destination pre-flight
`operation_ls` comes back `502 ExternalError.DirListingFailed.LoginFailed` wrapping a GridFTP
`530 LOGIN_DENIED`, and the payload reads as an authorization problem rather than a timeout:

```json
{"code": "permission_denied",
 "detail": {"DATA_TYPE": "not_from_allowed_domain#1.0.0", "allowed_domains": ["<YOUR_INSTITUTION_DOMAIN>"]},
 "authorization_parameters": {"session_message": "Session reauthentication required (Globus Transfer)",
                              "session_required_single_domain": ["<YOUR_INSTITUTION_DOMAIN>"]}}
```

`not_from_allowed_domain` invites the wrong diagnosis — it looks like the `--domain <YOUR_INSTITUTION_DOMAIN>`
gateway policy rejecting a non-<YOUR_INSTITUTION_DOMAIN> identity, i.e. a misconfiguration. It is not. The
identity is correct; its **session** has aged past `--authentication-timeout-mins`, so the HA
gateway stops counting it and is left with no <YOUR_INSTITUTION_DOMAIN> identity in the session. The
`session_required_single_domain` key in `authorization_parameters` is what distinguishes the two:
a genuine wrong-domain identity has a policy problem and no session requirement to satisfy.
The preceding `Token not valid for 'openid' scope` warning from `userinfo()` is unrelated
and benign — the token is deliberately transfer-scoped only.

A refresh token cannot fix this. Globus Auth sessions are extended by *authentication events*,
not by token refresh, so the only remedy is an interactive login — see below.

Observed 2026-08-17: token stored 2026-08-09T02:04Z, 1-week gateway timeout, so the session
lapsed 2026-08-16T02:04Z and every `globus-transfer` pod in the batch submitted at
2026-08-17T01:43Z failed pre-flight. The 2026-08-14 batch, day 5 of the same token, was clean.
**Check the session before submitting a batch** — it is one command and it is not
otherwise visible:

```bash
pixi run globus status
```

It reports when the session was established and when it expires; `doctor` fails
its session check once it has.

`doctor` measures that against the timeout **the endpoint enforces**, read from
the live gateway, not the one the configuration declares. The two differ whenever
the declaration has not been applied — production is `managed: false` until a
staging run has proved the reconcile — and trusting the declared value once
reported a session as lasting 30 days against a gateway enforcing 7. When they
disagree, the check says so and uses the live value.

### Rotating the Globus refresh token

HA collections require periodic reauthentication — the gateway's session timeout is 1 week
(see [High Assurance](#high-assurance)). When it lapses, `transfer.py` fails with the
misleading authorization error above.

From the repo root, run:
```bash
pixi run globus login
```

This opens a browser, requires a fresh authentication in the allowed identity domain,
stores the new token in Secrets Manager, records when the session was established, and
asks External Secrets to re-sync the Kubernetes Secret. The client ID comes from the
stored credential, so there is nothing to export. On a machine with no browser, add
`--no-browser` to get a URL to open elsewhere.

The re-sync needs Cloudflare WARP. Without it the login still succeeds and says so,
and the cluster picks the token up on External Secrets' hourly refresh instead.

**Stopgap while the session is lapsed:** subjects whose input is *already* in S3 can keep
processing with `ingress-mode=presynced`, which never touches Globus
([data-ingress.md](data-ingress.md#pre-staging-without-globus-ingress-modepresynced)). The
scope is narrower than it sounds. `mmps_mproc/` is transient staging: a successful run deletes
its subject's input, and a transfer that failed on the lapsed session staged nothing. So
presynced only helps subjects whose input survived an *unsuccessful* earlier run. Guessing
wrong is cheap — the staged-input validator fails in one cpu-light pod on an empty prefix — but
it is not a substitute for re-authenticating.

---

## Instance replacement

Replacing the GCS instance — for a new AMI, or because the old one is unhealthy —
keeps the endpoint, its UUID, the subscription, the collection and the registered
S3 credential. Only the node, the machine that serves the endpoint, changes.

With no workflows running (`pixi run globus tasks` lists none), from `terraform/`:

```bash
terraform plan -target=module.globus    # expect the instance to be replaced, and little else
terraform apply -target=module.globus
cd .. && pixi run globus doctor --start-instance
```

The new instance registers itself as a node on first boot, from the deployment key
in SSM — no browser, no subscription request. `doctor` confirms GridFTP is up, the
configuration matches, and both collections list. If it reports **stale node
records** left by the old instance, it names them; remove each with
`globus-connect-server node delete <node-id>` from a shell on the new instance.

What makes this safe:

- The registered S3 credential lives in Globus, not on the instance.
- The Elastic IP is re-associated by Terraform.
- The endpoint id, deployment key and collection id in SSM are ignored by
  Terraform (`lifecycle { ignore_changes = [value] }`), so replacing the instance
  cannot reset them.

The boot registration is `cloudpipe-gcs-boot`, a systemd unit baked into the
Packer AMI (`packer/globus-gcs/`). Its outcome is in
`systemctl status cloudpipe-gcs-boot` and `journalctl -u cloudpipe-gcs-boot`: it
exits 0 without registering when no endpoint exists yet, fails when it cannot
*read* the deployment key, and starts GridFTP when it registers. An instance built
before the Packer AMI registers through `gcs-auto-reregister` instead, logging to
`/var/log/gcs-auto-reregister.log`.

---

## Under the hood

The commands above are the supported interface. What follows is what they wrap, for
debugging on the instance.

### Opening a shell on the instance

Needs the AWS
[Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html):

```bash
aws ssm start-session \
  --target $(aws ssm get-parameter \
    --name /cloudpipe/globus/instance-id \
    --query Parameter.Value --output text) \
  --region <YOUR_AWS_REGION>
sudo -i
```

### Service credentials

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

> **`sudo -i` discards these three variables**, because it starts a login shell with a
> clean environment. Export them *after* `sudo -i`, not before. A management command run
> without them fails with **"You must log in"** — which reads like an expired session and
> is not one. There is no session to expire here: the service client authenticates per
> command. If you see it, re-export the three variables in the shell you are actually in
> and run the command again. The message means the same thing in every other context too,
> including [Endpoint recovery](#endpoint-recovery-after-accidental-deletion), where it is
> only evidence of a deleted endpoint when the variables *are* loaded.

> **`user-credentials list` with service credentials**: The list only shows credentials owned by the service account identity — not the `<YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>` credential registered in [setup step 3.6a](globus-setup.md#36a-register-the-gateways-placeholder-key-pair). An empty list is expected; it does not mean the registration is missing. Use `globus ls <collection-id>:/` to confirm S3 access is working.

### Checks by hand

What `doctor` looks at, from a shell on the instance with the service credentials
loaded:

```bash
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

# Force an immediate sync — what `globus login` does after storing the token
kubectl annotate externalsecret globus-credentials \
  -n argo-workflows \
  force-sync=$(date +%s) --overwrite
```

The `kubectl` commands need Cloudflare WARP.

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

### `Warning: could not retrieve userinfo` — expected, ignore it

**Symptom**: every `globus-transfer` pod logs, before it does anything useful:

```
Warning: could not retrieve userinfo: ... Token not valid for 'openid' scope
```

**Cause**: none. The token is deliberately scoped to Transfer only, so the optional
`auth_client.userinfo()` call in `images/globus/transfer.py` — which exists to print
*Authenticated as: …* for the log — is refused. The `except` around it prints this
warning and the transfer proceeds normally.

**It prints on every run, including every successful one.** It is not a symptom of an
expired session, a missing credential or a misconfigured client, and it does not need
fixing. Do not spend a triage on it; the same string appears alongside real failures and
means nothing there either (see [Recognising an expired session](#recognising-an-expired-session)).

### "Bucket not allowed" on transfer

**Symptom**: Globus transfer fails with `550-Globus-S3-Error: Bucket not allowed`.

**Cause**: The S3 gateway was created with `s3_allow_multi_keys: true` (the default). In multi-key mode, the first component of every path is treated as a bucket name — so a path like `/mmps_mproc/sub-xxx` tells Globus to write to a bucket named `mmps_mproc`, not `<YOUR_S3_BUCKET>`. The fix is `--no-allow-multiple-keys` at gateway creation time.

**Verify the flag**:
```bash
globus-connect-server storage-gateway list --format json \
  | python3 -c "import sys,json; d=json.load(sys.stdin)['DATA'][0]; print('s3_allow_multi_keys:', d.get('s3_allow_multi_keys'))"
```

If `s3_allow_multi_keys: True`, recreate the gateway (see below). An in-place update (`storage-gateway update s3`) technically works but does not reliably affect running transfers — recreation is more reliable.

**Another cause**: `transfer.py` checks for an existing active task (by label `cloudpipe-{subject-id}`) and reuses it if found. A stale task from before the fix can be reused, causing the old error to persist. Cancel stale tasks from the [Globus web app](https://app.globus.org) → Activity, then resubmit.

### "Your credential requires some initial setup" (invalid_credential)

**Symptom**: Transfer fails with `530-GridFTP-Message: Your credential requires some initial setup. code=invalid_credential`.

**Cause**: The IAM access key registered with the gateway is invalid — wrong key, rotated key, or key without S3 access to `<YOUR_S3_BUCKET>`.

**Fix**:
```bash
# On GCS instance
globus-connect-server user-credentials list
globus-connect-server user-credentials delete <credential-id>
globus-connect-server user-credentials s3-create <YOUR_GLOBUS_S3_GATEWAY_ID> \
  --globus-identity <YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>
# Enter a valid IAM access key with permissions to s3://<YOUR_S3_BUCKET>
```

### Gateway and collection recreation

If the gateway needs to be recreated (e.g., to change `s3_allow_multi_keys`), the full procedure:

```bash
# On the GCS instance, logged in via SSM (sudo -i)

# 1. Load the service credentials — see "Service credentials"; no browser login needed

# 2. Delete user credentials (required before gateway deletion)
GATEWAY_ID="<YOUR_GLOBUS_S3_GATEWAY_ID>"
globus-connect-server user-credentials list
# For each credential ID in the output:
globus-connect-server user-credentials delete <credential-id>

# 3. Remove delete protection from collection, then delete it
COLLECTION_ID="<YOUR_GLOBUS_COLLECTION_ID>"
globus-connect-server collection update "$COLLECTION_ID" --no-delete-protected
globus-connect-server collection delete "$COLLECTION_ID"

# 4. Delete the gateway
globus-connect-server storage-gateway delete "$GATEWAY_ID"

# 5. Recreate gateway with --no-allow-multiple-keys
GATEWAY_ID=$(globus-connect-server storage-gateway create s3 "cloudpipe-s3" \
  --bucket <YOUR_S3_BUCKET> \
  --s3-endpoint https://s3.<YOUR_AWS_REGION>.amazonaws.com \
  --domain <YOUR_INSTITUTION_DOMAIN> \
  --high-assurance \
  --authentication-timeout-mins $((60 * 24 * 7)) \
  --s3-user-credential \
  --admin-managed-credentials \
  --no-allow-multiple-keys \
  --format json | python3 -c "import sys,json; d=json.load(sys.stdin); r=d[0] if isinstance(d,list) else d; print(r.get('id') or r.get('data',[{}])[0].get('id'))")
echo "New gateway: $GATEWAY_ID"

# 6. Create collection (rooted at /<YOUR_S3_BUCKET> — bucket name is always the first path component)
# Note: collection create returns the object directly; gateway create wraps in {"data":[...]}
COLLECTION_ID=$(globus-connect-server collection create \
  "$GATEWAY_ID" "/<YOUR_S3_BUCKET>" "cloudpipe-s3" \
  --allow-guest-collections \
  --enable-https \
  --format json | python3 -c "import sys,json; d=json.load(sys.stdin); r=d[0] if isinstance(d,list) else d; print(r.get('id') or r.get('data',[{}])[0].get('id'))")
echo "New collection: $COLLECTION_ID"

# 7. Register IAM credentials
globus-connect-server user-credentials s3-create "$GATEWAY_ID" \
  --globus-identity <YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>
# Enter IAM access key with s3://<YOUR_S3_BUCKET> permissions at the prompts

# 8. Update SSM — by hand: `globus configure` records a collection id only over the
#    placeholder, and never replaces the old collection's id with a new one
aws ssm put-parameter --region <YOUR_AWS_REGION> \
  --name /cloudpipe/globus/collection-id \
  --type String --value "$COLLECTION_ID" --overwrite
```

Then run `pixi run globus configure` to confirm the recreated objects match the
declaration.

### Gridftp readiness race

EC2 status checks pass before the boot registration finishes starting `globus-gridftp-server`. The `start-globus-instance-template` Argo step polls port 443 after EC2 health checks to confirm gridftp is accepting connections. If this step times out, run `pixi run globus doctor` — its GridFTP check reports the same thing — or open a shell on the instance and look:

```bash
systemctl status globus-gridftp-server
journalctl -u cloudpipe-gcs-boot --no-pager -n 50     # instances built from the Packer AMI
journalctl -u gcs-auto-reregister --no-pager -n 50    # instances built before it
```

### "A transfer with identical paths has not yet completed" (409 Conflict)

**Symptom**: resubmitting a workflow before Globus clears the prior task:

```
409 Conflict: A transfer with identical paths has not yet completed
```

`transfer.py` deduplicates by label (`cloudpipe-{subject-id}`) — reusing `ACTIVE` tasks
and cancelling `INACTIVE` ones — but a manual resubmit outside that flow can still
collide. Clear the stale tasks before retrying (or from the
[Globus web app](https://app.globus.org) → Activity).

Terminating an Argo workflow does **not** cancel the Globus task it submitted, and a
surviving `ACTIVE` task with a matching label gets *adopted* by the next run — so a
resubmit can re-surface the original failure even after the underlying cause is fixed.
Check for stale tasks after any terminated batch.

List first — this reaches every task the token owns, not just the colliding one:

```python
pixi run python - <<'EOF'
import boto3, json, globus_sdk
secret = json.loads(
    boto3.client("secretsmanager", region_name="<YOUR_AWS_REGION>")
    .get_secret_value(SecretId="globus/refresh-token")["SecretString"]
)
client = globus_sdk.NativeAppAuthClient(secret["native-app-client-id"])
authorizer = globus_sdk.RefreshTokenAuthorizer(secret["refresh-token"], client)
tc = globus_sdk.TransferClient(authorizer=authorizer)
# globus-sdk 4.x: filter= clause dict. The pre-4.x `filter_status="ACTIVE,INACTIVE"`
# raises TypeError on the pinned SDK (4.8.1).
for task in tc.task_list(filter={"status": ["ACTIVE", "INACTIVE"]}):
    print(f"{task['task_id']}  {task['status']}  {task['label']}  {task['request_time']}")
    # To cancel, uncomment — verify the labels above are yours to cancel first:
    # print(tc.cancel_task(task["task_id"])["code"])
EOF
```

Run it from the repo root: `globus-sdk` is a pixi dependency, so bare `python3` will
fail on the import.

### Endpoint recovery (after accidental deletion)

If the GCS endpoint is deleted from the Globus web UI, transfers fail with:

```
530-Login incorrect. : GlobusError: v=1 c=ENDPOINT_ERROR
530-Failure while contacting GCS Manager API.
```

The boot registration cannot fix this — it only registers a node against an *existing*
endpoint. The endpoint has to be created again, which means a new endpoint UUID, a new
subscription request, and a new service client.

**Diagnosis** (SSM in, `sudo -i`):

```bash
systemctl status globus-gridftp-server        # running but unable to validate logins
systemctl status apache2                       # running but proxying to a dead backend
curl -k -s --max-time 5 https://localhost/api/v1/endpoint  # hangs = GCS Manager down
globus-connect-server endpoint show            # read the note below before judging this one
```

**"You must log in" on its own proves nothing.** The CLI prints it whenever the
`GCS_CLI_*` variables are absent from the environment — the ordinary state of a fresh
shell, and `sudo -i` discards them, so it is the *expected* reply to running the command
before loading [Service credentials](#service-credentials). It does not mean a Globus
session lapsed, and it does not mean the endpoint is gone.

The diagnosis is the **pair**: `curl` hangs *and* `endpoint show` still says "You must log
in" with the `GCS_CLI_*` variables loaded. Only then has the endpoint been deleted.

**Recovery.** Stop anything that submits transfers first; every transfer fails until
the end of step 6 anyway.

1. **Register a new service client.** Globus will not let the client that created
   the old endpoint create another one. Register a confidential client as in
   [globus-prerequisites.md → Gate 4](globus-prerequisites.md#gate-4--the-build-itself),
   put its UUID in the answers document's `service_client_id`, and store its id and
   secret as in [setup step 3.3](globus-setup.md#33-store-the-service-clients-id-and-secret).

2. **Tell the deployment it has no endpoint.** SSM still names the deleted one, so
   `bootstrap-endpoint` would refuse, and it still names the old collection, so
   `configure` would report a conflict rather than record the new one.
   `cleanup-endpoint` cannot do this — it refuses while a collection is recorded, and
   there is no endpoint left to delete. Reset the endpoint id **first**, because it is
   the marker every tool reads:
   ```bash
   DEPLOYMENT=cloudpipe
   aws ssm put-parameter --name "/$DEPLOYMENT/globus/endpoint-id" \
     --type String --overwrite --value REPLACE_AFTER_GCS_SETUP
   aws ssm put-parameter --name "/$DEPLOYMENT/globus/collection-id" \
     --type String --overwrite --value REPLACE_AFTER_GCS_SETUP
   ```
   (and `/staging/collection-id` too, if staging had one).

3. **Clear the old endpoint's state on the instance.** In a shell on the instance
   (`sudo -i`):
   ```bash
   systemctl stop globus-gridftp-server apache2
   rm /var/lib/globus-connect-server/info.json
   rm /var/lib/globus-connect-server/gcs-manager/gcs54.db
   rm /var/lib/globus-connect-server/gcs-manager/gridftp-key
   rm /var/lib/globus-connect-server/gcs-manager/gridftp-key.old
   ```
   The GCS tooling drops to the `gcsweb` user to write `info.json`, and the directory
   is root-owned, so pre-create it for the node setup that follows:
   ```bash
   touch /var/lib/globus-connect-server/info.json
   chown gcsweb:gcsweb /var/lib/globus-connect-server/info.json
   ```

4. **Create the endpoint:** `pixi run globus bootstrap-endpoint`
   ([setup step 3.4](globus-setup.md#34-create-the-globus-endpoint)). It records the
   new UUID and deployment key, and prints the subscription request.

5. **Re-subscribe.** The HA subscription is tied to the endpoint UUID, so send the
   request text to `<YOUR_GLOBUS_SUBSCRIPTION_ADMIN_EMAIL>`
   ([setup step 3.5](globus-setup.md#35-ask-for-the-subscription)).

6. **Rebuild the rest:** [setup steps 3.6–3.9](globus-setup.md#36-create-the-storage-gateway-and-collection)
   — gateway, collection, IAM credential, `configure`, then `globus login`, because
   the existing token was granted against the old collection.

> **Cleanup**: orphaned `cloudpipe-s3` collections from the deleted endpoint that appear
> in the Globus web UI can be safely deleted.

---

## POSIX staging alternative (removed)

**Removed in 2026-09.** The POSIX staging approach used a 500 Gi EBS volume at
`/data/globus-staging`: GridFTP wrote to EBS, then `globus-s3-sync-template` ran
`aws s3 sync` via SSM send-command. It needed no S3 add-on and no static IAM key,
but it was slower, left a window where data existed only on instance storage, and
had never actually been exercised.

There is no longer a variable to flip. The Argo template, the EBS volume and
attachment, the `runner_globus_ssm` policy, the `globus_use_s3_gateway` variable
and the `user_data` branches are all deleted —
[ADR 001's Update](decisions/001-s3-gateway-over-posix-staging.md) records what
went and why the removal was safe.

**If you need ingress without the S3 add-on**, stage the data into the bucket by
whatever means you have and submit with `ingress-mode=presynced`; the
`ingress-verify` step checks it against the ingress contract before anything
consumes it. See [data-ingress.md](data-ingress.md).

[globus-setup.md → Appendix](globus-setup.md#appendix-posixebs-staging-removed-2026-09--historical-only)
keeps the historical setup detail for reference only.
