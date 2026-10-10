# The deployer's first hour

This page is the path from a fresh clone to a Terraform root that is ready for its first apply. It covers the three commands that collect and check your deployment's inputs, in the order you run them, and it ends where the infrastructure install begins.

Two things to know before you start, because they set expectations for everything below:

- **None of these commands create anything.** They read your environment, write three files into a directory you choose, and tell you what is still missing. No AWS, Cloudflare, Kubernetes or Globus resource is created, modified or deleted by any of them. The first thing that changes your cloud account is the Terraform apply in the last step, which you run yourself.
- **The slow parts are not the commands.** Several inputs come from gates other organisations control — a data use certification, a Globus subscription. Those take days to weeks. Read [globus-prerequisites.md](globus-prerequisites.md) before you budget time for this; the tooling here takes an hour, and it cannot shorten the rest.

## What you need to have in hand

The wizard will ask you for twelve values. Most you can read off a dashboard in a few minutes; two things you have to own first, and a third only if you want hostnames. Every bucket this deployment needs is created for you in [step 4](#step-4--create-the-three-buckets-that-come-before-the-stack).

| You must already own | Why | Where it is checked |
|---|---|---|
| An **AWS account** you can reach with an AWS CLI v2 SSO profile | Static access keys are not supported anywhere in this tooling | `cloudpipe preflight`, check `aws.identity` |
| A **Cloudflare account with Zero Trust enabled**, and an API token scoped to it | The deployment creates Access applications and policies | `cloudflare.token` |
| A **name for the imaging data bucket**, which you supply as `globus_s3_destination_bucket` | The bucket itself is created in [step 4](#step-4--create-the-three-buckets-that-come-before-the-stack), by Terraform, along with the other two that outlive a cluster. You only have to decide what it is called — or name one you already have, and tell that root not to create it | `aws.data_bucket` |
| *Optional:* a **Route53 hosted zone** in that account for the domain you will publish services under | Certificate validation is DNS-based, and it runs against the zone this account owns. Without a domain, answer `none`: nothing is published, and you reach each UI through a port-forward (see [below](#reaching-the-web-uis-without-a-domain)) | `aws.hosted_zone` |

You do **not** need anything from your institution's IT department. Sign-in goes through an Amazon Cognito user pool the deployment creates in your own account, with an authenticator app required as a second factor. You only decide who the operators are: the wizard asks for their email addresses, and during the install you create a Cognito user for each (step 8).

## Which deployment you are installing

Two answers change what gets built. Both are decided by what you put in the answers document, not by a mode setting — there is no third input that could contradict them.

**Where the web UIs live** follows from `domain`:

| | `domain = "example.org"` | `domain = null` (answer `none`) |
|---|---|---|
| The UIs are at | `https://<ui>.example.org`, over WARP | a fixed `localhost` port, over a port-forward |
| Built for them | an internal load balancer, an ACM certificate, DNS records, a web-application firewall | nothing |
| You must own | a Route53 hosted zone for the domain | nothing |
| To open one | a bookmark | `pixi run cloudpipe ui <name>` ([below](#reaching-the-web-uis-without-a-domain)) |

Everything else — the cluster, the pipeline, Globus ingress, the metrics store — is identical. Port-forward mode is not a reduced deployment; it is the same deployment without public hostnames for its private UIs.

**Who may sign in** follows from `external_identity`, which the wizard does not ask about:

| | Left unset (the default) | Set in your root's `main.tf` |
|---|---|---|
| Identity provider | a Cognito user pool this deployment creates | one you have already configured yourself |
| Second factor | an authenticator app, required by the pool | your provider's |
| Secrets created by hand | none | your provider's client secrets |
| Needs a domain | no | **yes** — a precondition refuses the combination, because the UIs' sign-in broker would then be reachable only from your own browser and not from inside the cluster |

Take the default unless you already run an identity provider and want the UIs pointed straight at it. An institutional provider does not need that input: it attaches to the Cognito pool as a federated provider instead, which keeps every institution-specific value in one place. See [Signing in with your institution's identity provider](#signing-in-with-your-institutions-identity-provider).

## Step 1 — clone, and run the bootstrapper

```
git clone <this repository> cloudpipe
cd cloudpipe
python3 scripts/cloudpipe-setup
```

`scripts/cloudpipe-setup` is the only command here that runs before anything is installed, so it imports nothing but the Python standard library. It checks for pixi, Terraform at or above the version the stack module requires, AWS CLI v2 and an active SSO session, reports what is missing with the command or URL that gets it, and then hands off to the wizard.

It reports rather than fixes, with one exception: if pixi is absent it offers to install it, shows you the URL it would run first, and does nothing if you decline. Under `--non-interactive` it never offers and exits `2` instead.

Useful variants:

```
python3 scripts/cloudpipe-setup --check-only              # report and stop; run nothing
python3 scripts/cloudpipe-setup --json --non-interactive  # for a script driving this
```

A missing `kubectl`, Helm or `jq` is reported but does not block you. You do not need a cluster to collect inputs, and those three are only useful once one exists.

## Step 2 — make your own Terraform root

Copy the example root to a directory **outside the clone**, and keep it in your own version control:

```
cp -r terraform/modules/stack/example ~/my-cloudpipe-deployment
```

Why outside: that directory will hold your `terraform.tfvars`, your backend configuration and your state. It is yours, it is deployment-specific, and it should not live in a checkout you will later pull upstream changes into. The example root's own `main.tf` explains the module call it makes; see [infrastructure.md](infrastructure.md) for what the module builds.

The wizard verifies the directory you point it at before writing anything. A directory that declares no module call, or that does not declare the stack's variables, is refused with exit `4`. This check exists because the failure it prevents is silent: a `terraform.tfvars` written into a directory Terraform never reads produces no error at all, and you find out when `plan` prompts you for all eighteen values anyway.

## Step 3 — collect the inputs

```
cd ~/my-cloudpipe-deployment
pixi run --manifest-path ~/cloudpipe/pixi.toml cloudpipe setup
```

The wizard asks for each input in turn, grouped into AWS, identity, source control, Cloudflare and state. For every field it shows the help text, where to obtain the value, and the rule it must satisfy — and it re-asks immediately rather than collecting a list of complaints at the end.

**Choosing a region** is the one answer worth a minute's thought, because moving a deployment afterwards means rebuilding it. Pick the region closest to you, or to wherever your imaging data already sits — the cluster reads that data constantly, and keeping both in one region avoids inter-region transfer charges on every run. Then check two things in the [AWS region list](https://docs.aws.amazon.com/global-infrastructure/latest/regions/aws-regions.html): that EKS is offered there, and that the GPU instance types the anatomical steps use are too. Not every region has both, and the ones that do are not always the nearest.

What it writes, and nothing else:

| File | Where | What it is |
|---|---|---|
| `cloudpipe-answers.yaml` | your current directory | Your answers. This is the artifact to keep and to put in version control |
| `terraform.tfvars` | the target root | Derived from the answers |
| `backend.tf` | the target root | S3 backend configuration; skip it with `--no-backend` |

Three details that matter in practice:

- **The answers document is written after every single answer.** An interrupted run resumes from where it stopped; there is no separate progress file, and re-running picks up your previous answers as defaults.
- **One field is computed, not asked.** The GitHub OIDC subject claim is derived from the repository you gave. It is displayed and you can change it: set it explicitly in the answers document and the wizard keeps your value, saying so rather than silently recomputing it. This matters because a wrong subject claim does not fail anything — it just quietly denies your CI the credentials it asked for.
- **It will not overwrite a file it did not write.** An existing `terraform.tfvars` is an error, not a merge. Pass `--force` if you mean to replace it.

The eight Globus variables are deliberately **not** asked for here. They belong to a separate tool, covered in step 5.

### Driving it from a script

Every command takes `--json` and `--non-interactive`, writes one JSON envelope to stdout, keeps human text on stderr, and uses the same five exit codes:

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Unexpected failure |
| `2` | Blocked: a human has to supply or decide something |
| `3` | A check ran and failed |
| `4` | Invalid input or configuration |

Under `--non-interactive`, a required field that is absent from the answers document exits `2` and names **every** missing field, not the first — so one run tells you the whole list. The published input schema is `src/cloudpipe_setup/schemas/inputs.schema.json`; it carries the prompts, help text, validation patterns and groupings, so a front end can render the same form from the same file. Both the schema and every JSON envelope carry a `schema_version`.

### Neither tool takes a credential

Worth stating explicitly, because it changes how you prepare:

- **AWS credentials come only from an AWS CLI v2 SSO profile in your environment.** Nothing here accepts an access key as an argument, prompts for one, or writes an AWS config or credentials file. If your credentials resolve from anything other than SSO, preflight exits `4` without making a single API call. Run `aws configure sso` and `aws sso login` first.
- **The Cloudflare API token is read from `CLOUDFLARE_API_TOKEN` and nowhere else.** It is never prompted for, never written to a file, never printed, and never placed in a command's argument list where other processes on the machine could read it.
- A credential field in your answers document is refused outright, naming the field and not its value.

## Step 4 — create the three buckets that come before the stack

Three buckets have to exist before the stack's first apply, and all three have to survive its last teardown. One published Terraform root creates them, and it is the first thing you apply:

```bash
terraform -chdir=<your-clone>/terraform/modules/bootstrap init
terraform -chdir=<your-clone>/terraform/modules/bootstrap apply \
  -var region=<region> \
  -var state_bucket=<your-state-bucket> \
  -var data_bucket=<your-imaging-data-bucket> \
  -var metrics_bucket=<your-cluster-name>-metrics
```

`cloudpipe setup` prints that command with your own answers filled in. What each bucket is, and why its settings differ:

| Bucket | Holds | Versioning |
|---|---|---|
| `state_bucket` | The stack's Terraform state, which is what `backend.tf` names | **On.** It is what makes a truncated or corrupted state recoverable, which is the failure a remote backend exists to prevent |
| `data_bucket` | The imaging data and every derivative — the stack's `globus_s3_destination_bucket` | **Off.** The contents are recomputable, and versioning hundreds of GB of derivatives that are rewritten on every reprocess is a cost with no matching benefit |
| `metrics_bucket` | The run of record for pipeline QC | **On.** A metric record is overwritten in place when a unit is reprocessed, so the previous value exists only as a noncurrent version |

All three block public access, encrypt at rest, and carry a policy refusing requests that are not over TLS.

**This root keeps its state in a local file, on purpose.** It creates the bucket the stack's backend lives in, so it cannot use that backend itself. Keep that state file; losing it costs one `terraform import` per bucket, which is a far smaller problem than the one the state bucket exists to prevent.

**Nothing can delete these buckets by accident.** They are in a state the stack's teardown cannot reach, each carries `prevent_destroy`, and none sets `force_destroy` — so Terraform refuses even when asked. `cleanup.sh` ends by naming them and the `aws s3 rb --force` that removes them, for the case where you are finished with the data too.

If a bucket already exists — a cohort someone else staged, or a deployment you are rebuilding — name it and pass `-var create_data_bucket=false`. The stack configures the data bucket by name either way, so nothing downstream can tell which happened.

## Step 5 — decide whether you are running the Globus ingress

**Skip this step unless you are standing up a Globus Connect Server.** `globus_enabled` defaults to `false`, and with it false the stack creates no Globus resource at all — 52 of them, including an EC2 host and an Elastic IP. The pipeline then runs in `ingress-mode=presynced`: it reads whatever is already under `mmps_mproc/` in the data bucket and never starts a transfer. That is a complete deployment, and it is the one most people want first, because getting imaging into the bucket by any means is a smaller problem than reproducing this ingress.

Globus is one implementation of the data-ingress contract, not a requirement. If you are staging data another way, read [data-ingress.md](data-ingress.md) — it documents the S3 key contract the pipeline actually depends on, which is the only thing that has to be true.

### If you are running it

Enabling it needs four things this repository cannot give you:

| What | Why it is not ours to give |
|---|---|
| A **Globus subscription** | The S3 connector is a paid feature. [globus-prerequisites.md](globus-prerequisites.md) covers the gates, which run days to weeks |
| A **confidential service client**, registered in the Globus Developers Portal | The endpoint is created under it rather than under a person, so it survives someone leaving |
| A **managed prefix list** in this region | The host's SSH rule references it by id, and a prefix list is regional |
| A **GCS AMI** in your own account, built from `packer/globus-gcs/` | The `globus_ami_id` committed in `terraform/modules/stack/globus.tf` is the reference deployment's image, shared with nobody. Point it at yours |

Then set `globus_enabled = true` and the seven inputs it makes mandatory. The module refuses an empty one once the flag is true, so this is one edit of eight lines rather than an apply that half-works. Those seven are owned by a separate tool, which renders them into a `globus.auto.tfvars` in the same root:

```
pixi run globus init
```

Terraform loads `.auto.tfvars` files after `terraform.tfvars`, so those values win over anything you put in the latter. Setting them by hand in `terraform.tfvars` instead, as `terraform.tfvars.example` shows, works just as well — both `cloudpipe setup` and `cloudpipe preflight` check that each variable is set, not which file set it.

Read [globus-setup.md](globus-setup.md) for what that tool needs. Two tools writing one variable is two tools disagreeing about it, which is why the wizard reports on these variables and never writes them.

`globus_s3_destination_bucket` is the exception, and it is required either way: despite the name it is the imaging data bucket from [step 4](#step-4--create-the-three-buckets-that-come-before-the-stack), which the Prefect worker, the Argo runner's IAM policy, the access logs and the lifecycle rules all name whether or not anything Globus wrote it.

## If your GitOps repository is private

Skip this if you forked the public repository and left it public: ArgoCD clones a public repository anonymously, needs no credential, and `gitops_repo_private` defaults to `false`.

For a private repository, set `gitops_repo_private = true` and give ArgoCD a credential. Two ways, and the second is the better one:

**A personal access token**, which the stack reads from a Secrets Manager secret named `cloudpipe/github-pat`:

```bash
aws secretsmanager create-secret --name cloudpipe/github-pat \
  --secret-string '{"password":"<your-token>"}'
```

The key is `password`. GitHub has no API that creates a personal access token, so this is the one credential in the whole deployment that cannot be provisioned for you — and a token that expires takes ArgoCD's syncing with it, months later, with no warning.

**A deploy key**, which avoids both problems. A read-only SSH key scoped to the one repository, created by Terraform rather than by hand, and it does not expire:

```hcl
resource "tls_private_key" "argocd" {
  algorithm = "ED25519"
}

resource "github_repository_deploy_key" "argocd" {
  repository = "<your-gitops-repo>"
  title      = "ArgoCD read-only"
  key        = tls_private_key.argocd.public_key_openssh
  read_only  = true
}
```

That needs the GitHub provider configured, which means exporting `GITHUB_TOKEN` for the apply — the same arrangement `CLOUDFLARE_API_TOKEN` already uses, and for the same reason: a credential passed as a Terraform variable would persist in state. Hand the private key to ArgoCD as a `repo-creds` secret with `sshPrivateKey` instead of `password`.

> **A GitHub App is a third option** and the right one for an organization deploying several times: ArgoCD supports `githubAppID`, `githubAppInstallationID` and `githubAppPrivateKey` directly, and mints short-lived installation tokens itself. Creating the App is still a manual step, but unlike a PAT the credential does not expire. This deployment does not use it, so nothing here is tested against it.

## Step 6 — preflight

```
pixi run cloudpipe preflight --account <your-aws-account-id>
```

Preflight checks what your answers assert about the world, which is a different question from whether the answers are well-formed. It is read-only: every call it makes is a read, and it creates, modifies and deletes nothing. You can re-run it, paste its output into a ticket, and run it against a deployment somebody else built.

Nine checks, in order:

| Check | What it confirms |
|---|---|
| `aws.identity` | Credentials resolve, are not expired, and came from an SSO profile |
| `aws.account` | The resolved account is the one you intended |
| `aws.hosted_zone` | A hosted zone with exactly your domain's name exists in that account. Skipped, not failed, with `domain: null` |
| `aws.prefix_lists` | The Globus managed prefix list ID resolves in this account and region |
| `aws.data_bucket` | `globus_s3_destination_bucket` exists in this account. The stack configures that bucket — access logging, lifecycle rules, the Argo and Prefect policies scoped to its ARN — and never creates it; [step 4](#step-4--create-the-three-buckets-that-come-before-the-stack) is what does. Says so if it is in another region, which costs transfer on every ingest. Skipped when the Globus inputs have not been rendered yet |
| `cloudflare.token` | The token is valid, active, and can read Zero Trust configuration in your account |
| `federation.metadata` | A federated provider's discovery document or SAML metadata is reachable. Skipped when nothing is federated |
| `federation.mfa_evidence` | Exactly one form of multifactor evidence is declared |
| `federation.client_secret` | A federated OIDC provider's hand-created client secret exists |
| `globus.inputs` | Every Globus variable is set in a file Terraform loads — rendered by `globus init`, or by hand in `terraform.tfvars` |

Exit `3` if any check failed, `0` if they all passed or were skipped.

**Read the skips.** A check that could not be performed reports `skipped`, never `pass`, and names what was missing — usually an IAM action your credentials do not have. A skip is not a failure: the apply itself may not need you to hold that read permission, so preflight exits `0` with skips and says in its closing line how many there were. But it is also not a pass. A green exit code on a run that could not look at half the world is the one outcome this command is built to avoid handing you, and the count is there so you can tell the difference.

`--account` is an option rather than a question because nothing in the stack takes an account ID as an input. Without it, the account check reports `skipped` and its remedy quotes the command back with the resolved account already filled in, so confirming it is a paste rather than a lookup. It is worth passing: deploying into the wrong account is quiet and expensive, because every resource applies perfectly well.

## Step 7 — the phased first apply

Do **not** run a bare `terraform apply` on an empty account. The root's providers look up a cluster that does not exist yet, so the first install has to be applied in phases, each one `-target`ed at a subset.

`scripts/stack/install.sh` drives the phases. Run it from your root, which is where your `terraform.tfvars` and `backend.tf` are:

```bash
export CLOUDFLARE_API_TOKEN=...          # never echoed, never a Terraform variable

bash <path-to-clone>/scripts/stack/install.sh --list-phases   # what it will do, and in what order
bash <path-to-clone>/scripts/stack/install.sh                 # all eight phases, in order
```

One phase at a time is equally valid, and is the better way to run a first install: `--phase 1`, then `--phase 2`, and so on. A phase that fails prints the `--from-phase N` command that continues from it. Nothing is carried between invocations — each phase re-reads the deployment and re-checks what it depends on, and says which phase to run first if something is missing.

The eight phases, what each does, and the three variables that change between them are in [infrastructure.md → Bootstrap and install sequence](infrastructure.md#bootstrap-and-install-sequence). Read that section before you start. Three things are where a hand-run install goes wrong, and the script does all three for you:

- **Phase 4's tunnel-token sync.** Terraform never reads the Cloudflare tunnel's connector token, so it has to be copied into Secrets Manager separately. Without it cloudflared never connects. The script pipes it from Cloudflare to Secrets Manager, comparing hashes, so the token never reaches your terminal, your shell history or any process argument list.
- **Phase 7's tunnel check, before Phase 8 closes the public endpoint.** After Phase 8 the EKS API is private-only, reached through the Cloudflare tunnel (or the VPN as a fallback). Close it before the tunnel is proven healthy, and the tunnel is the one route you cannot fix from outside. Phase 8 re-proves the tunnel itself, every time it runs, and leaves the endpoint open if it cannot.
- **Step 8 below, also before Phase 8.** The tunnel being healthy is not the same as somebody being able to log in to it. Phase 8 refuses to close the endpoint against an empty user pool and prints the command that fixes it.

Phase 6 also writes `install-state.auto.tfvars` in your root, holding `crds_available` and `vpc_cni_network_policy_enabled`, both `true`. (A third flag, `vpc_cni_strict_mode`, stays at its default of `false` and is yours to turn on later if you want it — strict enforcement denies any pod no NetworkPolicy selects, and this repo only ships policies for four namespaces; see #746.) Those two start `false` because the first phases run against a cluster with no CRDs and no kube-system NetworkPolicies, and they have to stay `true` afterwards — so they are persisted to a file every later `terraform` run loads, rather than living only on the install commands. Keep the file; it is gitignored, so a fresh clone needs it recreated before any apply. Losing it destroys the ExternalSecrets and disables NetworkPolicy enforcement, both quietly (#635; [operations.md → Updating infrastructure](operations.md#updating-infrastructure-terraform)).

The reference deployment runs this same script, which is the only thing that keeps it honest.

## Step 8 — create the operators' sign-in accounts

**Do this before the phase that closes the public EKS endpoint.** Terraform creates the Cognito user pool, but not the users in it, and neither does the installer. That is deliberate rather than unfinished: creating a sign-in identity is a change to your account that belongs to you, the operator then has to complete an authenticator enrollment interactively anyway, and the installer's job is to refuse to lock you out — which it does, by checking the pool before Phase 8 and printing the command below. It is two commands per operator.

Once the pool exists, for each address in `operator_emails`:

```bash
POOL=$(terraform output -raw cognito_user_pool_id)

aws cognito-idp admin-create-user \
  --user-pool-id "$POOL" \
  --username operator@example.org \
  --user-attributes Name=email,Value=operator@example.org Name=email_verified,Value=true
```

Cognito emails a temporary password, valid for seven days. The first time that operator signs in — at WARP enrollment, or at any UI, both of which send them to the pool's hosted login page — they set a real password and enroll an authenticator app. After that they can reach the cluster and the UIs. To hand the password over yourself instead of having it emailed, add `--message-action SUPPRESS` and then:

```bash
aws cognito-idp admin-set-user-password \
  --user-pool-id "$POOL" --username operator@example.org \
  --password '<a password you generated>' --permanent
```

Either way the authenticator enrollment still happens at that first sign-in: the pool requires a second factor and offers no other kind.

Three things worth knowing before you rely on this:

- **`email_verified=true` is deliberate.** Without it the address is unverified, and the UIs' sign-in — which trusts the pool's verification rather than re-checking — will not admit it.
- **Recovery is yours, not theirs.** The pool has no self-service password reset, on purpose: that would make an operator's mailbox enough to take over their account. Reset with `admin-set-user-password`.
- **The pool sends mail through Cognito's default sender**, which is rate-limited to a small number of messages per day. That is ample for a handful of operators and is not a path to build anything else on.

Why this is a step rather than a footnote: after the endpoint closes, the cluster is reachable only over WARP, and WARP admits only identities the pool knows. An empty pool at that moment leaves the mTLS VPN as the only way in.

## Signing in with your institution's identity provider

Optional, and the one input the wizard does not ask for: write `cognito_federation` into your answers document and the institution's provider is federated **into** the Cognito pool. `cloudpipe setup` renders it into `terraform.tfvars` and `cloudpipe preflight` checks it, like every other field — it is not prompted for because its shape is the institution's, and because a nested object is not a question a line-oriented prompt asks well. Cloudflare Access and the web UIs do not change — they keep authenticating against the pool, and the pool authenticates against the institution. Operators then see their institution's login button beside the pool's own form.

The institution registers **one** redirect URI, which Terraform prints:

```bash
terraform output -raw cognito_federation_redirect_uri
# https://<your-pool-domain>/oauth2/idpresponse
```

That is the whole integration on their side. Everything else — endpoint layout, signing keys, which attributes they release — stays theirs, which is why this is one input rather than a mode the tooling models.

### SAML or OIDC

In `cloudpipe-answers.yaml`:

```yaml
# SAML: Cognito reads the metadata document, so key rotations need no apply here.
cognito_federation:
  type: saml
  name: my-university
  metadata_url: https://login.example.edu/metadata
  mfa_evidence:
    attestation: Example University IT policy 4.2 (2026-01), MFA required for all staff logins

# OIDC: Cognito discovers the endpoints from the issuer.
cognito_federation:
  type: oidc
  name: my-university
  oidc_issuer: https://login.example.edu
  client_secret_id: cloudpipe/federation-oidc   # created by hand, see below
  mfa_evidence:
    claim: acr
    claim_value: https://example.edu/profile/mfa
```

An OIDC provider issues you a client ID and secret. Those are the institution's credential, so Terraform never owns them: create the secret by hand and name it in `client_secret_id`.

```bash
aws secretsmanager create-secret --name cloudpipe/federation-oidc \
  --secret-string '{"clientID":"...","clientSecret":"..."}'
```

### You must declare how their MFA is proven

Amazon Cognito "delegates all authentication processes to the IdP and doesn't offer them additional authentication factors". A federated user therefore never meets the pool's own authenticator requirement, and something has to take its place for NIST 800-171 3.5.3. Terraform refuses a federation that declares neither:

| Form | What it does | When to use it |
|---|---|---|
| `claim` + `claim_value` | Terraform maps that claim into the pool and the Cloudflare Access policy **requires** it. A login arriving without it is denied. | The provider asserts MFA per login. `acr` is the usual claim; several research federations define a standard value for an MFA login, so ask your identity team which one they assert |
| `attestation` | Records a reference to their written policy. Nothing is enforced at login; your SSP cites this string. | The provider cannot assert a per-login claim |

**SAML usually means attestation.** SAML carries its authentication context inside the assertion rather than as a releasable attribute, so there is generally no claim for Cognito to map or for Access to require. That is a real reduction in assurance compared with the claim path — you are trusting a policy document instead of checking each login — and it is why the field is named after evidence rather than after a setting.

### What preflight checks for you

Three of its checks are about this block, and each reports `skipped` for a deployment that federates nothing:

| Check | What it confirms |
|---|---|
| `federation.metadata` | An OIDC issuer publishes a discovery document with all three endpoints Cognito needs, or a SAML metadata URL answers at all |
| `federation.mfa_evidence` | Exactly one form is declared — and it prints back what you are committing to, including the attestation text |
| `federation.client_secret` | The OIDC client secret you created by hand exists, in the deployment's region |

### What to check after the first federated login

The claim path has several places it can go quiet rather than loud, so confirm it rather than assuming:

- **The operator reaches the UIs at all.** Cognito marks a federated address verified only if the attribute mapping sets `email_verified`, and the UIs refuse an unverified address. The default mapping reads a claim of that name; if the institution does not release one, map whichever claim they do.
- **On the claim path, a login *without* MFA is refused.** That is the thing the claim form buys, and it is only real if you have seen it happen.
- **`operator_emails` still governs who is an administrator.** Federation decides who may sign in; it does not grant anyone a role.

This path has not yet been exercised against a real institutional provider — see group 6 of `openspec/changes/optional-domain-and-cognito-auth`.

## Reaching the web UIs without a domain

With a domain, each UI is at `https://<ui>.<domain>` over WARP. With `domain = null`, nothing is published, and you reach a UI through a port-forward instead:

```bash
pixi run cloudpipe ui grafana        # or argocd, argo, prefect, kubecost
```

It checks the cluster is reachable, forwards the UI to its fixed local port, and opens your browser there. It runs until you press Ctrl-C. The ports are fixed because each UI's sign-in redirect is registered on its port, and a redirect URI has to match exactly:

| UI | URL |
|---|---|
| ArgoCD | `http://localhost:8080` |
| Argo Workflows | `http://localhost:2746` |
| Prefect | `http://localhost:4200` |
| Grafana | `http://localhost:3000` |
| Kubecost | `http://localhost:9090` |

It needs kubectl configured for the cluster and a connected WARP session. If the API server does not answer within about ten seconds, it exits `2` and names WARP as the likely cause, without opening a browser. It makes no AWS call and changes nothing. `--no-browser` prints the URL instead of opening it.

## Tearing it down again

`bash <your-clone>/scripts/stack/cleanup.sh --root <your-root>` destroys the cluster and everything the stack owns. It leaves the three buckets from [step 4](#step-4--create-the-three-buckets-that-come-before-the-stack) standing, and ends by naming them with the command that deletes each.

That is deliberate in three independent ways, because deleting a cohort by accident is not a recoverable mistake: those buckets live in a Terraform state the teardown never touches, each carries `prevent_destroy`, and none sets `force_destroy` — so S3 refuses to delete a non-empty one even if the other two safeguards were gone.

**Installing again into the same account reuses them.** The second install adopts the buckets rather than failing on names that already exist, which is the main reason not to clear them out reflexively between attempts. When you are genuinely finished:

```bash
aws s3 rb s3://<bucket> --force   # for each bucket the teardown named
```

## Where to go next

| I want to… | Go to |
|---|---|
| Understand what I am about to deploy | [architecture.md](architecture.md) |
| Read the Terraform resource map and the install phases | [infrastructure.md](infrastructure.md) |
| Decide how to get data into the bucket | [data-ingress.md](data-ingress.md) |
| Find out whether I can reproduce the Globus ingress at all | [globus-prerequisites.md](globus-prerequisites.md) |
| Run the pipeline once the cluster is up | [operations.md](operations.md) |
