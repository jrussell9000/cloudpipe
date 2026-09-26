# Globus Connect Server — Setup Guide

How to build this deployment's Globus ingress from nothing: a Globus Connect Server
(GCS) v5 endpoint on EC2 whose **S3 storage gateway** writes transferred data
straight into the pipeline's bucket.

You do it with one tool, `pixi run globus`, and one file you fill in, the
**answers document**. The tool tells you what to do next at every point, so this
guide is mostly about the parts it cannot do for you: installing things on your
machine and asking other people for access.

**Lost? Run `pixi run globus setup-status`.** It lists every setup step in order,
says which are done, which one is next, and which are waiting on someone else.
It changes nothing, so run it as often as you like.

For architecture, the two-credential model, High Assurance background and
day-to-day operations, see [globus.md](globus.md). For the steps that need other
people — the data use agreement, the Globus app registrations, the subscription —
see [globus-prerequisites.md](globus-prerequisites.md). The raw commands the tool
runs for you are in the [appendix](#appendix-under-the-hood).

---

## 1. Prerequisites on your machine

Four programs, installed once. `setup-status` checks all four together and lists
every one that is missing, so you can fix your machine in one pass.

| Program | Why | Install |
|---|---|---|
| AWS CLI **v2** | Every command reaches AWS through your AWS login. v1 has no `aws sso login`. | [AWS CLI install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| Terraform **1.10 or newer** | Creates the AWS side: the instance, parameters and permissions. The floor is `required_version` in `terraform/versions.tf`. | [Terraform install](https://developer.hashicorp.com/terraform/install) |
| pixi | Runs `globus` and everything else in this repository with the right Python packages. | [pixi.sh](https://pixi.sh) |
| A web browser | Globus and AWS both log you in through one. | — |

### Log in to AWS with single sign-on

Set up an AWS profile once:

```bash
aws configure sso
```

It asks four things you need to have ready:

| Prompt | What to enter | Where to get it |
|---|---|---|
| `SSO session name` | Any short label, for example `cloudpipe` | You choose it |
| `SSO start URL` | Your organization's **AWS access portal URL**, which looks like `https://<something>.awsapps.com/start` | Whoever administers your AWS account. It is also the address you use to log in to AWS in a browser |
| `SSO region` | The region your organization's IAM Identity Center lives in | Same person. It is often, but not always, the region you deploy to |
| `SSO registration scopes` | Press Enter to accept the default | — |

A browser opens; approve the request. Then pick the account and role you were
given, set the **default region to the region you are deploying to** (this
deployment uses `<YOUR_AWS_REGION>`), and accept the default profile name or choose one.
If you named a profile, make it the one commands use:

```bash
export AWS_PROFILE=<the profile name you chose>
```

From then on, log in at the start of each working day — the login lasts hours,
not weeks:

```bash
aws sso login
```

> **Static AWS keys are not supported.** The `globus` tool never asks for an
> access key, never reads one from a file, and its SSO check **fails** if your
> credentials come from anything other than an SSO login. Use the steps above.

### Confirm it all worked

```bash
pixi run globus doctor
```

The first check is your machine: it names anything missing and what to do about
it. The later checks will fail until you have built something — that is expected
on a first run. `setup-status` shows the same prerequisites as its first four
steps.

### Cloudflare WARP: needed only for the cluster steps

The Kubernetes cluster is private and reached through Cloudflare WARP. **Setting up
Globus itself does not need WARP** — every setup command works through AWS. To
enroll, follow [infrastructure.md → Remote access](infrastructure.md#remote-access-cloudflare-warp)
rather than instructions repeated here.

Without WARP, exactly these things are limited:

- **Immediate secret sync after `globus login`.** The new credential still reaches
  the cluster, but on External Secrets' own schedule (within the hour) rather
  than at once.
- **The running-workflow guard.** Commands that could disturb running transfers
  check for them first. Without WARP they cannot look, so they ask you to
  confirm instead.
- **The Kubernetes check in `doctor`.** It is reported as not run, never as
  passing.
- **Probe and canary workflow submissions**, which need the cluster.
- **`prefect deploy`**, which reaches the Prefect server inside the cluster.

If a command reports a TLS handshake timeout while WARP says it is connected,
your WARP session has lapsed (it lasts 24 hours); run
`warp-cli debug access-reauth`.

---

## 2. Things other people have to grant

Start these on day one — each can take days or weeks, and none depends on your
machine being ready. [globus-prerequisites.md](globus-prerequisites.md) walks
through each one; `setup-status` shows each as waiting on the named person and,
where there is something to send, attaches the text to send.

| Gate | What you need | Recorded in the answers document as |
|---|---|---|
| `nda_duc` | A data use agreement granting access to the source data | `source_collection_id`, `source_base_path` |
| `globus_app_registration` | Two Globus applications: a **service client** that will own the endpoint (registered by `globus register-service-client`, or in a browser), and a **native app** used to log in (a browser, and only a browser) | `service_client_id`, `service_client_secret_ref`, `native_app_client_id` |
| `globus_subscription` | An institutional Globus subscription that includes High Assurance | `subscription_id` (optional) |
| `endpoint_subscription` | The subscription holder adding *your* endpoint — requested after step 3.4 | nothing: `doctor` observes it |

**Nothing in this table is an AWS credential.** Earlier versions of this guide asked
you to obtain an IAM user and an access key for the storage gateway. That is no
longer required — see [how the gateway reaches S3](#how-the-gateway-reaches-s3-and-why-there-is-no-aws-key)
below, and [ADR 019](decisions/019-signing-proxy-for-globus-ingress.md).

---

## 3. The setup path

Each step names the `setup-status` step it completes, so you can match what the
tool says to where you are here.

### 3.1 Fill in the answers document

*`setup-status` step: `setup.answers`*

Create `globus-answers.yaml` in the repository root (or anywhere, and point at it
with `--answers PATH` or `GLOBUS_ANSWERS`). The required fields:

```yaml
deployment_name: cloudpipe          # short, lowercase; most names derive from it
aws_account_id: "123456789012"      # the account you logged in to — a safety check
aws_region: <YOUR_AWS_REGION>
bucket: my-transfer-bucket          # where transferred data lands
contact_email: lab-admin@example.edu
owner_email: globus-admin@example.edu
service_client_id: <UUID of the service client>
service_client_secret_ref: ssm:/cloudpipe/globus/gcs-client-secret
native_app_client_id: <UUID of the native app>
source_collection_id: <UUID from your data use agreement>
source_base_path: /path/on/the/source/collection
```

`service_client_secret_ref` says **where** the secret lives, never the secret
itself — the schema rejects a literal. It must be `ssm:` followed by
`/<deployment_name>/globus/gcs-client-secret`, because the endpoint is created on
the instance, which can read only its own deployment's SSM parameters. You put
the secret there in step 3.3.

The optional fields (`identity_domain`, `subscription_id`, `session_timeout_days`,
`gateway_name`, `staging_enabled`, …) are described in
`src/globus_admin/schemas/answers.schema.json`. On a fresh deployment, leave
`staging_enabled` and `production_managed` out: you get no staging gateway, and
`configure` may change production to match this document. Both exist for a
deployment whose production predates this tool
([globus.md → Staging gateway](globus.md#staging-gateway)). Then:

```bash
pixi run globus init
```

`init` validates the document and writes two files into `terraform/`:
`globus.auto.tfvars`, which Terraform reads, and `globus-config.json`, a preview
of the gateway and collection that step 3.6 will create. It refuses to overwrite
either without `--force`. If the document has a problem, `init` names the field.

### 3.2 Create the AWS infrastructure

*`setup-status` step: `setup.infrastructure`*

From the `terraform/` directory:

```bash
cd terraform
terraform init
terraform plan -target=module.globus
terraform apply -target=module.globus
cd ..
```

Read the plan before typing `yes`. On a first build it only adds resources; a
plan that **destroys or replaces** anything on an existing deployment should be
stopped and asked about. This creates the EC2 instance, its SSM parameters (with
placeholder values), the permissions, and the SSM documents the next steps run.

### 3.3 Store the service client's id and secret

The two values from the service-client registration go into SSM, where the
instance reads them.

If you have not registered the client yet, one command does both halves:

```bash
pixi run globus register-service-client
```

It registers the confidential client through the Globus Auth API and stores the
id and secret in the two parameters below, so neither value is ever typed or
pasted. It logs in for the `manage_projects` scope — a separate consent from
`globus login`, held in memory and never stored — and refuses if either parameter
already holds a value. Its opposite is `globus delete-service-client --client-id`.

As its last step it makes the new client an **administrator of its Auth project**,
which is not a convenience: `globus-connect-server endpoint setup` refuses to
create an endpoint as an identity that administers no project, and passing it a
project id does not substitute for the role. The grant runs after the credentials
are stored, so if it fails the client is still recorded and one command finishes
the job:

```bash
pixi run globus grant-project-admin --project-id <the project's uuid>
```

That command is also what to run for a client registered in the Globus Developers
Portal before this tooling existed — the portal does not add the client as an
administrator either. It is idempotent, and it preserves the project's existing
administrators.

Note the ordering: it writes parameters Terraform created in step 3.2, so it
cannot run before that, while step 3.1 already needs `service_client_id` in the
answers document. On a first build that means registering in the portal at 3.1, or
running this command and then re-running `init --force` and `terraform apply`
before step 3.4.

If the client is already registered, put the two values in by hand instead. The
secret is typed at a hidden prompt, so it never appears on screen or in your shell
history:

```bash
DEPLOYMENT=cloudpipe   # your deployment_name

read -rp 'Service client id: ' v && printf '%s' "$v" | aws ssm put-parameter \
  --name "/$DEPLOYMENT/globus/gcs-client-id" --type SecureString --overwrite \
  --value file:///dev/stdin; unset v

read -rsp 'Service client secret: ' v && echo && printf '%s' "$v" | aws ssm put-parameter \
  --name "/$DEPLOYMENT/globus/gcs-client-secret" --type SecureString --overwrite \
  --value file:///dev/stdin; unset v
```

Each prints a version number on success.

### 3.4 Create the Globus endpoint

*`setup-status` step: `setup.endpoint`*

```bash
pixi run globus bootstrap-endpoint
```

This is done **once per deployment**. It starts the instance if it is stopped,
creates the endpoint under the service client, records the endpoint's id and
deployment key in SSM, and offers to stop the instance again. Allow up to 45
minutes; it gives up after that.

It refuses if the deployment already records an endpoint. It ends by printing the
**subscription request** — the text for the next step.

> **The service client is used up by this.** Globus will not let the client that
> created an endpoint create another one. If the endpoint is ever deleted, a new
> service client has to be registered before running this again — `globus
> delete-service-client --client-id <recorded id>`, then
> `globus register-service-client`. Only
> ever delete an endpoint with `globus cleanup-endpoint`, which refuses one that
> is in service — see [globus-contract.md](globus-contract.md#bootstrap-endpoint-and-cleanup-endpoint).

### 3.5 Ask for the subscription

*`setup-status` step: `setup.subscription` · gate: `endpoint_subscription`*

Send the request text from step 3.4 to your institution's Globus subscription
manager. If you have lost it, `pixi run globus setup-status` attaches it to this
step. For UW–Madison the address is `<YOUR_GLOBUS_SUBSCRIPTION_ADMIN_EMAIL>` and the subscription
is `<YOUR_GLOBUS_SUBSCRIPTION_UUID>`.

Nothing else can proceed until they reply: a High Assurance storage gateway
cannot be created on an unsubscribed endpoint. `setup-status` shows this step as
done once `doctor` sees the subscription.

### 3.6 Create the storage gateway and collection

*`setup-status` step: `setup.configure` (done once 3.6b has run)*

> **`configure` creates both now.** It plans `create storage_gateway …` and
> `create collection …` against an endpoint that has neither, and applying that
> plan runs them — gateway first, because the collection is created against the
> gateway's id. The hand commands below are kept as the reference for what it
> runs and for recovering one object without the other.
>
> Two things it still does not do. It **registers no credential**: the gateway it
> creates has none until `user-credentials s3-create` is run by hand (step 3.6a),
> and the apply says so rather than reporting a finished setup. And it needs the
> AMI rebuilt to reach the instance, since the reconcile ships in the image.

This needs the AWS
[Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
installed. Open a shell on the instance:

```bash
DEPLOYMENT=cloudpipe   # your deployment_name
aws ssm start-session --target "$(aws ssm get-parameter \
  --name "/$DEPLOYMENT/globus/instance-id" --query Parameter.Value --output text)"
```

On the instance, run `sudo -i`, load the service credentials as
[globus.md → Service credentials](globus.md#service-credentials) shows, and run
the [storage gateway](#s3-storage-gateway) and
[collection](#collection) commands from the appendix. Use **exactly** the names,
bucket, domain and timeout that `terraform/globus-config.json` (written by
`init`) declares — `configure` matches objects by display name, and compares the
rest.

The gateway is created with `--no-allow-multiple-keys` and the collection is
rooted at `/<bucket>`, which is what makes a destination path like
`/mmps_mproc/sub-xxx` land at `s3://<bucket>/mmps_mproc/sub-xxx`. The appendix
explains why both matter.

One value in that command will look wrong if you have set up an S3 gateway before:
`--s3-endpoint` is a `https://127.0.0.1:<port>` address on the instance itself, not
an AWS endpoint. That is deliberate, and
[the next section](#how-the-gateway-reaches-s3-and-why-there-is-no-aws-key) explains
it. The port is the one `terraform/globus-config.json` declares for that gateway
(`8444` for production, `8443` for staging).

### 3.6a Register the gateway's placeholder key pair

`configure` never creates, changes or deletes credentials, so this step is done by
hand, once, in the same shell. Run the command under
[Registering the S3 credential](#registering-the-s3-credential), with the gateway id
from `globus-connect-server storage-gateway list`. It **prompts** for a key id and a
secret.

> **What you type here is not a credential, and there is no AWS key to fetch.**
> Globus Connect Server refuses to run an S3 gateway without a key pair, so one has
> to exist — but the gateway sends its requests to a proxy on the instance's own
> loopback address, and that proxy throws the pair away. Use AWS's published example
> pair, which exists for exactly this purpose and grants nothing:
>
> ```text
> AWS Access Key ID:     AKIAIOSFODNN7EXAMPLE
> AWS Secret Access Key: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
> ```
>
> Do **not** treat this pair as a secret, and do not "improve" the setup by storing
> it somewhere safer or by making the proxy reachable from off the instance. The
> proxy's only protection is that it listens on `127.0.0.1` alone, so anything that
> widens its reach trades a real control for a decorative one. Full reasoning:
> [ADR 019](decisions/019-signing-proxy-for-globus-ingress.md).

Type `exit` twice to leave the instance.

### 3.6b Check the configuration and record the collection id

Back on your own machine:

```bash
pixi run globus configure
```

`configure` compares what the endpoint has with what the answers document
declares, **shows you the plan**, asks before changing anything, and applies it.
After 3.6 the plan should show no changes. If it shows a difference, the hand
commands used a different value — `configure` fixes what Globus can change in
place, and refuses, naming the field, what it cannot.

It also records the collection's id in `/<deployment>/globus/collection-id`,
where the pipeline, `doctor` and `cleanup-endpoint` read it. It only ever fills
in Terraform's placeholder: if the parameter already holds a *different* id, it
says so and leaves it for you to decide. Run it with `--env staging` too once the
staging collection exists.

Run `configure` again whenever you change the answers document; `--plan-only`
shows the plan and stops.

### 3.7 Log in to Globus

*`setup-status` step: `setup.login` · gate: `globus_login`*

```bash
pixi run globus login
```

A browser opens. Log in with your institutional identity. The command stores the
credential the pipeline uses in Secrets Manager (`globus/refresh-token`) and asks
the cluster to pick it up at once. Nothing is written to your machine.

**Repeat this whenever the session expires** — every 7 days by default
(`session_timeout_days`). `pixi run globus status` shows how long is left.

### 3.8 Confirm the credential reached the cluster

*`setup-status` step: `setup.credential_sync` · gate: `cluster_access`*

With WARP connected this happened during step 3.7. Without it, it happens on its
own within the hour. `doctor`'s Kubernetes check confirms it.

### 3.9 Check everything

*`setup-status` step: `setup.transfer_ready`*

```bash
pixi run globus doctor
```

Every check should pass. The last two list the destination collection and the
source collection — if both succeed, a transfer can run. `doctor` names the
command or the person that fixes anything that fails, and exits non-zero if
anything did.

---

## Current deployment

| Resource | Value |
|---|---|
| Endpoint ID | `<YOUR_GLOBUS_ENDPOINT_ID>` |
| S3 gateway ID | `<YOUR_GLOBUS_S3_GATEWAY_ID>` |
| Collection ID | `<YOUR_GLOBUS_DEST_COLLECTION_ID>` |
| Collection name | `cloudpipe-s3` |
| SSM parameter | `/cloudpipe/globus/collection-id` |
| Registered credential identity (both gateways) | `<YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>` |
| Native app client ID | `<YOUR_GLOBUS_NATIVE_APP_CLIENT_ID>` |

This endpoint predates `bootstrap-endpoint`: it was created by hand with the
older interactive `endpoint setup` flow, not by the commands in section 3.

**This deployment is mid-migration, and the two gateways differ.** A new deployment
built from section 3 is keyless throughout; this one is not yet:

| | Staging `cloudpipe-s3-staging` | Production `cloudpipe-s3` |
|---|---|---|
| Gateway `s3_endpoint` | `https://127.0.0.1:8443` | `https://s3.<YOUR_AWS_REGION>.amazonaws.com` |
| Registered key pair | `AKIAIOSFODNN7EXAMPLE` — a placeholder | A real IAM user's access key |
| Writes as | role `cloudpipe-globus-staging-writer`, confined to `scratch/globus-staging/` | IAM user `<YOUR_GLOBUS_S3_IAM_USER>`, which can write to every bucket in the account |
| `doctor` checks 12 and 14 | reported | `skipped` until cutover |

The production listener is built and running on `127.0.0.1:8444`; only the gateway's
registration has not been moved to it. That cutover, and the removal of the IAM user,
are `openspec/changes/retire-globus-s3-access-keys` sections 8 and 9. Until they are
done, the IAM key rotation procedure in
[globus.md → Credential rotation](globus.md#credential-rotation) still applies to
production, and to production only.

---

## How the gateway reaches S3, and why there is no AWS key

You do not have to understand this to finish the setup. Read it if the
`https://127.0.0.1:8444` endpoint in step 3.6 looked like a mistake, or before you
change anything about it.

**The problem.** Every request to S3 has to be *signed*: the sender computes a short
code from the request and an AWS key pair, and AWS recomputes it to decide who is
asking. Globus Connect Server can only do that with a plain key pair typed into it
once and stored forever. It cannot use the instance's own AWS identity, and it cannot
use temporary credentials, because the field that carries them does not exist in its
credential format. Taken at face value that forces a permanent AWS key to live inside
Globus — the thing this setup no longer has.

**The fix, in one picture.** Point the gateway at a small program on the same
machine, and let that program do the signing:

```
GridFTP  ──►  https://127.0.0.1:8444  ──►  https://s3.<YOUR_AWS_REGION>.amazonaws.com
 (writes)      the signing proxy             (real S3)
               • ignores the key pair Globus sent
               • signs again, with credentials it gets
                 from the instance's own IAM role
               • those credentials expire every hour
```

Three things follow, and they are the whole reason this is better than a stored key:

- **There is no permanent AWS credential anywhere.** The proxy asks the instance for
  fresh credentials as it needs them. Nothing to rotate, nothing to leak, nothing to
  request from an AWS administrator.
- **What each gateway may touch is decided by AWS, not by the proxy.** Each gateway
  gets its own role, confined to its own prefix of the bucket — production to
  `mmps_mproc/`, staging to `scratch/globus-staging/`. A proxy that decides nothing
  cannot decide it wrongly.
- **The key pair Globus holds stops mattering.** It is discarded, so it is not a
  secret. See [step 3.6a](#36a-register-the-gateways-placeholder-key-pair).

**What protects the proxy, since it accepts any key pair.** It listens on
`127.0.0.1` and nowhere else, so only a process already running on the GCS instance
can reach it — not another host, not the internet, regardless of security groups.
That single property is doing the work of the discarded signature, which is why
widening it would be a mistake rather than a hardening. If you need convincing, the
verification is in [ADR 019](decisions/019-signing-proxy-for-globus-ingress.md).

**Who builds it.** Terraform creates the roles; the machine image installs the proxy
and its certificate and starts one instance of it per gateway. You do not install or
configure anything, and there is no separate step in section 3.

**How to tell it is working.** `pixi run globus doctor` — check 12 reports the
gateway's role, and check 14 reports whether its listener is serving. A gateway still
on an old static key reports both as `skipped`
([globus-contract.md](globus-contract.md)).

### The rejected alternatives, briefly

AWS Mountpoint for S3 would have let the connector use the instance role directly and
**cannot be used**: it supports only sequential writes starting from byte 0 of a new
file, while GridFTP's Extended Block Mode delivers data out of order even with
`max_parallelism` set to 1. The Mountpoint FUSE layer rejects every non-sequential
write with `EINVAL`, so transfers fail with
`500 globus_xio: System error in write: Invalid argument`. The S3 gateway bypasses
FUSE entirely.

Of the connector's three credential modes, only the third works for an automated
pipeline, and it is the one used — pointed at the proxy instead of at AWS:

| Mode | Credential mechanism | Suitable for automation |
|---|---|---|
| `--s3-unauthenticated` | Anonymous (no credentials) | Only for public buckets — not applicable |
| `--s3-user-credential` (alone) | Per-user: each Globus identity registers its own key pair in the Globus web UI | No — a manual web UI step per identity |
| `--s3-user-credential` + `--admin-managed-credentials` | One key pair registered by the admin via CLI, applying to all mapped identities | Yes — and with the proxy in front, that pair is a placeholder |

A daemon that re-registered short-lived credentials on a timer, leaving nothing in
the data path, would have been simpler than a proxy and was rejected on evidence: the
credential format has no field for the session token that temporary AWS credentials
require, so they are rejected by S3 as invalid.

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
enforced by Globus OAuth2/OIDC, not by network-layer filtering. Terraform opens
them; nothing here needs doing by hand.

---

## Appendix: under the hood

You should not need anything in this appendix to set up a deployment. It records
what the commands above run, for debugging and for the record of how the current
endpoint was built by hand. Every command here runs **on the GCS instance**, in an
SSM session (`sudo -i` first), with the service credentials loaded as
[globus.md → Service credentials](globus.md#service-credentials) describes.

| `globus` command | Runs on the instance |
|---|---|
| `bootstrap-endpoint` | SSM document `<deployment>-globus-bootstrap` → `reconcile/bootstrap.py`: `endpoint setup`, `node setup`, then writes `deployment-key` and **last** `endpoint-id` to SSM |
| `configure` | SSM document `<deployment>-globus-reconcile` → the on-instance reconcile (`reconcile/planner.py`): reads the live gateways and collections, plans against the declaration, and runs `storage-gateway update s3` for drift Globus can change in place. It plans creates but does not yet run them. It never deletes, never touches an object it was not told about, and never handles credentials. Every run also records its plan in `<prefix>/reconcile-plan`, which is what `globus doctor` check 7 reads to report drift while the instance is stopped; after an `apply` it records the state it re-reads afterwards, not the plan it applied. Then, on your machine, it records the collection id over the SSM placeholder |
| `cleanup-endpoint` | SSM document `<deployment>-globus-teardown` → `reconcile/teardown.py`: `node cleanup`, `endpoint cleanup`, then resets `endpoint-id` **first** and `deployment-key` to placeholders |

### Endpoint creation (what `bootstrap-endpoint` does)

Authenticated by the `GCS_CLI_CLIENT_ID` / `GCS_CLI_CLIENT_SECRET` variables the
document exports, so no browser flow and no `--client-id`:

```bash
globus-connect-server endpoint setup "<display name>" \
  --owner "${GCS_CLI_CLIENT_ID}@clients.auth.globus.org" \
  --organization "<org_name>" \
  --contact-email "<contact_email>" \
  --agree-to-letsencrypt-tos \
  --dont-set-advertised-owner
# --project-id <UUID> is added only when passed to bootstrap-endpoint

globus-connect-server node setup \
  -d /etc/globus-connect-server/deployment-key.json \
  --ip-address "$(curl -s https://checkip.amazonaws.com)"
```

### S3 storage gateway

Run by hand in step 3.6, with the values from `terraform/globus-config.json`:

```bash
S3_BUCKET="<YOUR_S3_BUCKET>"

globus-connect-server storage-gateway create s3 "cloudpipe-s3" \
  --bucket "$S3_BUCKET" \
  --domain <YOUR_INSTITUTION_DOMAIN> \
  --s3-endpoint https://127.0.0.1:8444 \
  --s3-user-credential \
  --admin-managed-credentials \
  --no-allow-multiple-keys \
  --high-assurance \
  --authentication-timeout-mins $((60 * 24 * 7)) \
  --format json
```

| Flag | Purpose |
|---|---|
| `--bucket` | Locks the gateway to a single S3 bucket — required with `--no-allow-multiple-keys` |
| `--domain <YOUR_INSTITUTION_DOMAIN>` | Restricts access to <YOUR_INSTITUTION_DOMAIN> Globus identities |
| `--s3-endpoint` | **The signing proxy on this instance, not AWS** — `https://127.0.0.1:8444` for production, `https://127.0.0.1:8443` for staging. One listener per gateway, each assuming a prefix-scoped role. GCS stores this under `policies.s3_endpoint`, and `doctor` check 12 compares it against the declared port. See [how the gateway reaches S3](#how-the-gateway-reaches-s3-and-why-there-is-no-aws-key) |
| `--s3-user-credential` | Enables per-identity credential slots |
| `--admin-managed-credentials` | Allows the admin to register one key pair for all identities via CLI — a placeholder, since the proxy discards it |
| `--no-allow-multiple-keys` | **Critical.** Without this flag (`s3_allow_multi_keys: true` is the default), Globus treats the first component of every path as a bucket name. Transferring to `/mmps_mproc/sub-xxx` would try to write to a bucket named `mmps_mproc` and fail with `550-Globus-S3-Error: Bucket not allowed`. With this flag, the bucket is fixed by `--bucket` and collection paths are relative to the bucket root. |
| `--high-assurance` | Required because the ABCD source collection is HA |
| `--authentication-timeout-mins` | How long before a human must re-authenticate. **No 30-day HA ceiling exists** — the ABCD source collection is High Assurance and set to `525600` (1 year), measured 2026-09-15. Our 7-day cadence is self-imposed; see [ADR 010's Correction](decisions/010-globus-ha-subscription.md). |

### Collection

```bash
globus-connect-server collection create \
  "${GATEWAY_ID}" "/${S3_BUCKET}" "cloudpipe-s3" \
  --allow-guest-collections \
  --enable-https \
  --format json
```

> **JSON shapes differ**: `collection create --format json` returns the collection
> object directly (`{"id": "..."}`) while `storage-gateway create` wraps it in
> `{"data": [...]}`. Reading `d['data'][0]['id']` from a collection create raises
> `KeyError: 'data'`.

> **Path semantics**: the GCS S3 connector always treats the first path component as
> the bucket name. Root the collection at `/<bucket-name>` (e.g., `/<YOUR_S3_BUCKET>`); then a
> collection path of `/mmps_mproc/sub-xxx` maps to `s3://<YOUR_S3_BUCKET>/mmps_mproc/sub-xxx`.
> Rooting it at `/` makes every transfer path's first component (e.g. `mmps_mproc`)
> a bucket name, and every transfer fails with
> `550-Globus-S3-Error: Bucket not allowed`.
>
> The destination path is **not** a Terraform variable. It is
> `globus_dest_base_path`, a parameter of the Prefect queue-manager flow
> (`prefect/flows/cloudpipe_queue_manager.py`), passed at flow-run time. One place
> does need to agree with it: `globus_destination_prefix` on `module.globus` scopes
> the production writer role's IAM prefix, and a role scoped to a prefix the pipeline
> does not write to fails every transfer with `AccessDenied`. Its default is held
> equal to the flow's by `tests/test_terraform_globus_s3_roles.py`, so changing the
> destination means changing both, and CI says so if only one moves.

> **After collection creation**, allow 2–3 minutes before submitting a transfer
> workflow. Globus takes a short time to propagate a new collection; transfers
> submitted immediately may fail transiently and exhaust their retries.

### Registering the S3 credential

```bash
globus-connect-server user-credentials s3-create \
  "${GATEWAY_ID}" \
  --globus-identity <YOUR_NETID>@<YOUR_INSTITUTION_DOMAIN>
# Prompts: AWS Access Key ID, then AWS Secret Access Key.
# For a gateway pointed at its signing proxy, answer with the published example pair
# (AKIAIOSFODNN7EXAMPLE / wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY). The proxy discards
# it; see step 3.6a. Where a real key is still in use — production, until the cutover —
# never pass it as an argument.
```

With the `GCS_CLI_*` service credentials set, `user-credentials list` returns an
empty table — the service identity has no credentials of its own. That is
expected; list the collection with `globus ls <collection-id>:/` to confirm S3
access instead.

### Verification by hand

```bash
globus-connect-server endpoint show
globus-connect-server storage-gateway list
globus-connect-server collection list
```

The [Globus web app](https://app.globus.org) shows the same: search for the
endpoint by name or UUID, confirm it is active, and browse the collection root.

---

## Appendix: POSIX+EBS staging (removed 2026-09 — historical only)

> **This path no longer exists in the repository.** It is kept here as a record of
> how the system once worked, because the Terraform resources, the Argo sync
> template, the `globus_use_s3_gateway` variable and the `user_data` branches
> described below were all deleted — see
> [ADR 001's Update](decisions/001-s3-gateway-over-posix-staging.md). **Nothing in
> this appendix can be enabled by setting a variable.** Rebuilding it would mean
> writing it again from scratch.
>
> If you have a Globus endpoint without the S3 add-on, the supported route is to
> stage the data into the bucket yourself and submit with `ingress-mode=presynced`
> — see [data-ingress.md](data-ingress.md).

In this mode (`globus_use_s3_gateway = false` in `terraform/terraform.tfvars`), a GCS
**POSIX storage gateway** presented a local directory backed by a 500 GB gp3 EBS volume as
the Globus collection. GridFTP writes landed on EBS at `/data/globus-staging`; the
`globus-s3-sync-template` Argo step then ran `aws s3 sync` via SSM to push files to S3
and clean up the staging area. Because it used the EC2 instance IAM role via the OS
credential chain (no static key) and mapped any authenticated identity to the local
`cloudpipe` service account via `gcs-identity-map.py`, no per-user credential
registration was required.

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

Transfer destination paths were relative to the staging root (e.g. `/mmps_mproc/sub-xxx`)
— no bucket-name prefix needed, since `aws s3 sync` prepended the bucket.
