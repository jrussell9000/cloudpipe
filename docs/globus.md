# Globus session (HTTPS ingress)

CloudPipe copies ABCD minimally preprocessed data from the NIH Brain Development
Cohorts (NBDC) Data Hub collection into its S3 bucket over that collection's own
HTTPS server (`ingress-mode=https`, [data-ingress.md](data-ingress.md)). No Globus
endpoint of ours is involved. What the copy still needs from Globus is a
**session**: one person's Globus login, with two grants, stored where the cluster
can read it.

This page is the runbook for that session: what you need before the first login,
`pixi run globus login`, `pixi run globus doctor`, renewal, the SSM parameters the
ingress reads, and troubleshooting. The machine-readable outputs of both commands
are in [globus-contract.md](globus-contract.md). What other people must grant you
first is in [globus-prerequisites.md](globus-prerequisites.md).

> **History.** Until 2026-11-12 this deployment also ran a Globus Connect Server
> endpoint on EC2, with an S3 storage gateway and a signing proxy, and
> `ingress-mode=globus` transferred through it. The host, its endpoint, its
> collections, its AMIs and the CLI commands that managed them are all deleted.
> ADR 024 records the retirement. Some names keep the word `globus` because the
> HTTPS path uses them: the `globus-transfer` WorkflowTemplate, the
> `globus-credentials` Secret, the `globus-*` workflow parameters, the
> `delete-globus-input` step and the `/cloudpipe/globus/...` SSM paths.

---

## How the session reaches the copy

```
pixi run globus login  (a person, in a browser)
   │  transfer scope + the source collection's /https scope
   ▼
Secrets Manager  globus/refresh-token
   native-app-client-id · refresh-token · https-refresh-token
   │  ExternalSecret, refreshInterval 1h (login forces an immediate sync)
   ▼
Kubernetes Secret  argo-workflows/globus-credentials
   │  env: GLOBUS_NATIVE_APP_CLIENT_ID, GLOBUS_REFRESH_TOKEN, GLOBUS_HTTPS_REFRESH_TOKEN
   ▼
https-transfer pod (ingress-nodepool)
   1. refuses a login older than 30 days   ← SSM /cloudpipe/globus/session-established-at
   2. lists the subject through the Transfer API          (refresh-token)
   3. GETs each file from the source's HTTPS server       (https-refresh-token)
   ▼
s3://<bucket>/mmps_mproc/{subject}/{session}/...
```

The two tokens do different jobs. The **transfer token** lists directories through
the Globus Transfer API, because the HTTPS server cannot list. The **HTTPS token**
is refreshed into an access token for
`https://auth.globus.org/scopes/<source-collection-id>/https`, which the copier
sends with each GET. The pod writes to S3 as the Argo runner's Pod Identity, so no
AWS key is involved anywhere in the path.

---

## Before the first login

### On your machine

| Program | Why | Install |
|---|---|---|
| AWS CLI **v2** | Both commands reach AWS through your SSO login. v1 has no `aws sso login`. | [AWS CLI install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| pixi | Runs `pixi run globus` with the right Python packages. | [pixi.sh](https://pixi.sh) |
| A web browser | The Globus login is a browser flow. A machine without one can use `--no-browser`. | — |

`doctor`'s first check confirms the AWS CLI, the SSO login, the account and pixi
together, and names each one that is missing.

### Log in to AWS with single sign-on

Set up an AWS profile once:

```bash
aws configure sso
```

It asks four things:

| Prompt | What to enter | Where to get it |
|---|---|---|
| `SSO session name` | Any short label, for example `cloudpipe` | You choose it |
| `SSO start URL` | Your organization's **AWS access portal URL**, which looks like `https://<something>.awsapps.com/start` | Whoever administers your AWS account |
| `SSO region` | The region your organization's IAM Identity Center lives in | Same person. It is often, but not always, the region you deploy to |
| `SSO registration scopes` | Press Enter to accept the default | — |

A browser opens; approve the request. Pick the account and role you were given,
set the **default region to the deployment's region**, and choose a profile name.
If you named one, make it the one commands use:

```bash
export AWS_PROFILE=<the profile name you chose>
```

Then log in at the start of each working day; the login lasts hours:

```bash
aws sso login
```

> **Static AWS keys are not supported.** The `globus` tool never asks for an access
> key, and its SSO check fails when your credentials come from anything other than
> an SSO login.

### Cloudflare WARP: needed only for the cluster parts

The cluster is private and reached through Cloudflare WARP
([infrastructure.md → Remote access](infrastructure.md#remote-access-cloudflare-warp)).
Both commands work through AWS and Globus APIs without it. Without WARP:

- **`login` cannot force the Secret to re-sync.** The new tokens still reach the
  cluster, on External Secrets' hourly refresh.
- **`login` cannot see running workflows**, so it cannot tell you that transfer
  pods are running. It proceeds either way.
- **`doctor`'s Kubernetes Secret check is reported as not run**, never as passing.

If a command reports a TLS handshake timeout while WARP says it is connected, the
WARP session has lapsed (it lasts 24 hours); run `warp-cli debug access-reauth`.

### The answers document

Both commands read one small file, `globus-answers.yaml`, from the directory you
run them in (or `--answers PATH`, or `$GLOBUS_ANSWERS`). Its schema is
`src/globus_admin/schemas/answers.schema.json`, and it has eight fields:

```yaml
aws_account_id: "123456789012"      # required; login refuses another account
native_app_client_id: <UUID>         # required; the native app you log in through
source_collection_id: <UUID>         # required; the NBDC collection
source_base_path: /abcd/derivatives/mmps_mproc   # required; the parent of the subject directories
identity_domain: example.edu         # this or identity_domains: the domain the source's policy requires
# identity_domains: [example.edu, other.edu]
# deployment_name: cloudpipe         # optional; derives the SSM prefix and the cluster name
# aws_region: eu-west-1              # optional; defaults to the AWS profile's region
```

Where each value comes from is in
[globus-prerequisites.md](globus-prerequisites.md). Nothing in the file is a
secret.

A document is validated before either command acts, and every problem is reported
at once, one per field, with exit 4. A 1.x document (from before 2026-11-12) holds
fields for the retired host (bucket, gateway and collection names, the service
client, the subscription, staging, `session_timeout_days`); delete them.

With no answers document at all, `doctor` still runs against the deployment your
credentials point at, and `login` refuses on its own for the values it needs.

---

## Logging in

```bash
pixi run globus login
```

A browser opens. Sign in with the identity your NBDC data access is granted to, in
the domain the answers name, and **approve every scope on the consent screen**.
On a machine with no browser, add `--no-browser` to get a URL to open elsewhere.

What the command does, in order:

1. **Refuses up front** when `--non-interactive` is set (exit 2: a login needs a
   person), when no `source_collection_id` is configured (exit 4), when the AWS
   prerequisites fail, or when the answers name no `aws_account_id`.
2. **Finds the native app client id**: `--client-id`, then the answers document,
   then `$GLOBUS_NATIVE_APP_CLIENT_ID`, then the one stored by the last login.
3. **Notes running workflows**, when the cluster is reachable. They keep the token
   they already hold. It does not ask for confirmation.
4. **Logs in** with `prompt=login` and the answers' identity domains, so Globus
   must record a fresh authentication rather than reuse a browser session. It
   requests the Transfer scope and the source collection's `/https` scope.
5. **Refuses to store anything** when the identity is outside the declared
   domains, when no refresh token came back, or when the HTTPS grant is missing.
6. **Stores the tokens** in Secrets Manager `globus/refresh-token`, as
   `native-app-client-id`, `refresh-token` and `https-refresh-token`.
7. **Records the login time** in `/<deployment>/globus/session-established-at`,
   **last**. If this write never happens, the session reads as unknown and the
   batch gate refuses. That is the safe direction.
8. **Asks the cluster to re-sync** by annotating the ExternalSecret, then waits for
   all three keys in `globus-credentials` to carry the new values. A sync that
   does not land in time is reported and is not a failed login: the tokens are
   stored, and External Secrets re-reads them every hour.

Nothing is written to your machine. The tokens live in memory until they are in
Secrets Manager.

---

## Checking: `doctor`

```bash
pixi run globus doctor
```

Run it before a batch, and whenever ingress fails. It runs six checks in order and
names the command or the action that fixes each failure:

| # | Check | What it answers |
|---|---|---|
| 1 | Prerequisites | AWS CLI v2, an SSO login, the intended account and region, pixi. Cluster reachability is a WARN, because only check 5 needs it |
| 2 | Ingress parameters in SSM | The session, source collection and source base path parameters exist, and the source ones are not Terraform placeholders |
| 3 | Globus session | How long the stored login has left under the 30-day rule |
| 4 | Source collection listing | The source base path lists with the stored transfer token, as the batch gate lists it |
| 5 | Kubernetes credential Secret | This deployment's `globus-credentials` holds all three keys, each matching Secrets Manager |
| 6 | HTTPS token | One refresh of the HTTPS token for the source collection's scope |

A check whose dependency failed is reported as **not run**, never as passing. The
exit code is the first failure's (0 if none): 2 means a person has to act, 3 that
a check failed, 4 that the answers or the account are wrong.

Check 5 reads the Secret only through the deployment's own cluster. It looks up the
EKS cluster named for the deployment and requires kubectl's current server to be
that cluster's endpoint; when it is not, the check fails and prints the
`aws eks update-kubeconfig` command. It reports not run when the deployment has no
such cluster, or no `globus-credentials` Secret in it. Tokens are compared by hash,
never printed.

---

## Session renewal

**A login is good for 30 days.** That is this deployment's own rule, not a Globus
limit: the NBDC collection itself allows a year. Three places enforce it, and a
test holds them equal:

| Where | When | What happens |
|---|---|---|
| `doctor`'s session check | any time | WARN within 5 days of the 30; FAIL within 24 hours of it, or past it |
| The queue manager's batch gate | before the first submission | refuses the whole batch if the session would pass 30 days within its margin (Prefect variable `cloudpipe-globus-session-margin-hours`, default 24). It also lists the source and refreshes the HTTPS token once |
| The `https-transfer` pod | before any request | refuses a login older than 30 days, missing, unparseable, without a time zone or in the future. Exit 78, which the step does not retry |

The hand `argo submit` path skips the batch gate, and the pod's check still holds.

To renew, run `pixi run globus login` again from an up-to-date checkout. That
resets the 30-day clock. Then run `pixi run globus doctor` and expect six passes.
A running workflow keeps the token it already holds; the next pod reads the new
one once the Secret has synced.

**Stopgap while the session is lapsed:** subjects whose input is *already* in S3
can keep processing with `ingress-mode=presynced`, which reads nothing from Globus
([data-ingress.md](data-ingress.md#pre-staging-without-globus-ingress-modepresynced)).
The scope is narrow. A successful run deletes its subject's input, and a copy that
was refused staged nothing, so presynced helps only subjects whose input survived an
unsuccessful earlier run.

### Rotating or revoking the credential

The credential is the pair of refresh tokens in `globus/refresh-token`. To rotate
it, log in again: `login` overwrites all three keys and the session time. A revoked
grant makes every ingress pod and the batch gate fail at their first refresh until
someone logs in again.

---

## The SSM parameters

Terraform creates these in `terraform/modules/stack/ingress_session.tf`, under
`/<deployment>/globus/`. An unset value holds the placeholder
`REPLACE_AFTER_GCS_SETUP`, which every reader refuses. The name is left over from
the retired host and is kept so no reader changes.

| Parameter | Set by | Read by |
|---|---|---|
| `session-established-at` | `globus login`, never Terraform (`ignore_changes = [value]`) | the `https-transfer` pod, the queue manager's batch gate, `doctor` |
| `source-collection-id` | Terraform, from `globus_source_collection_id` | the queue manager, `fmriprep_bench.py`, `doctor` |
| `source-base-path` | Terraform, from `globus_source_base_path` | the same |
| `fasttrack-source-base-path` | Terraform, from `fasttrack_source_base_path` | nothing yet; planned for `cloudpipe_fasttrack` |

Deliberately, the login time is not the secret's `LastChangedDate`: that also moves
when the secret is edited for an unrelated reason, which would silently reset the
clock a batch depends on.

To change the source, set the two `globus_source_*` variables in your tfvars and
apply. `doctor`'s check 2 fails while either still holds the placeholder.

---

## Troubleshooting

### Recognising an expired session

There are two ways an old session shows up, and only one of them says "expired".

**The pod's own refusal** (exit 78, never retried). The `https-transfer` log ends
with one line naming the problem and `pixi run globus login`, for example:

```
error: The Globus session is 31 days old, past this deployment's 30-day rule (established 2026-10-13T17:24:00+00:00). Renew it with `pixi run globus login`.
```

**Globus's own refusal**, when the source's policy wants a fresh authentication.
It does not read as an expiry. The Transfer API returns `permission_denied` with
`not_from_allowed_domain` in the detail, which looks like an identity from the
wrong institution:

```json
{"code": "permission_denied",
 "detail": {"DATA_TYPE": "not_from_allowed_domain#1.0.0", "allowed_domains": ["<institution_domain>"]},
 "authorization_parameters": {"session_message": "Session reauthentication required (Globus Transfer)",
                              "session_required_single_domain": ["<institution_domain>"]}}
```

The `session_required_single_domain` key is what distinguishes the two. A genuinely
wrong-domain identity has a policy problem and no session requirement to satisfy.
The pod translates the case with the key into
`GLOBUS SESSION EXPIRED: run 'pixi run globus login'`, and `doctor` into
`globus.session_expired` (exit 2). A refresh token cannot fix it: Globus extends a
session through an authentication event, not a token refresh. Log in again.

### "Warning: could not retrieve userinfo" — expected, ignore it

Every `https-transfer` pod logs this before it lists anything:

```
Warning: could not retrieve userinfo: ... Token not valid for 'openid' scope
```

The transfer token is deliberately scoped to Transfer only, so the optional
`userinfo()` call in `images/globus/listing.py`, which exists to print
*Authenticated as: …*, is refused. It prints on every run, including every
successful one, and means nothing.

### The pod refuses with a missing token

```
error: GLOBUS_HTTPS_REFRESH_TOKEN is not set; the globus-credentials Secret lacks it. Run `pixi run globus login`.
```

The Secret has no `https-refresh-token`. Either no login since the HTTPS scope was
added, or the ExternalSecret has not synced. `doctor`'s check 5 says which. The
same exit 78 follows `Globus refused the HTTPS refresh token (…)` and
`The HTTPS refresh token carries no grant for the source collection`: the grant was
revoked or never approved. Log in again and approve every scope.

### The source says the subject is not there

`globus.not_found_at_source` means the subject's directory does not exist under the
source base path. That is a fact about the source, not a broken copy: check the
subject id and `source-base-path`.

### Checking the Secret by hand

`doctor` does this for you. With WARP connected:

```bash
# Which keys the cluster Secret holds
kubectl get secret globus-credentials -n argo-workflows -o json | jq '.data | keys'

# Whether the ExternalSecret is syncing
kubectl get externalsecret globus-credentials -n argo-workflows

# Force an immediate sync, which is what `globus login` does after storing the tokens
kubectl annotate externalsecret globus-credentials \
  -n argo-workflows force-sync=$(date +%s) --overwrite
```

Never `kubectl delete secret globus-credentials`: every pod that mounts it is left
without a credential until External Secrets re-creates it.

### HTTP 500s from the source, and the concurrency limit

The NBDC source fails individual requests with HTTP 500 under load. The copier
retries each file a few times within a pass and makes up to four passes, each
against what S3 actually holds (`images/globus/https_ingress.py`). The
`https-transfer` semaphore in `cloudpipe-semaphores-configmap.yaml` caps how many
subjects copy at once; the ConfigMap's comment records the agreed ceiling of
concurrent requests on the source and why. Do not raise it without asking the
source's operators. Lowering it is always safe.

---

## Related

- [data-ingress.md](data-ingress.md) — the ingress contract, the `https` and `presynced` modes, and the DAG seam
- [globus-prerequisites.md](globus-prerequisites.md) — what other people must grant before the first login
- [globus-contract.md](globus-contract.md) — the CLI's JSON outputs and exit codes
- [operations.md](operations.md) — submitting batches
