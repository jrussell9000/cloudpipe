# The deployer's first hour

This page is the path from a fresh clone to a Terraform root that is ready for its first apply. It covers the three commands that collect and check your deployment's inputs, in the order you run them, and it ends where the infrastructure install begins.

Two things to know before you start, because they set expectations for everything below:

- **None of these commands create anything.** They read your environment, write three files into a directory you choose, and tell you what is still missing. No AWS, Cloudflare, Kubernetes or Globus resource is created, modified or deleted by any of them. The first thing that changes your cloud account is the Terraform apply in the last step, which you run yourself.
- **The slow parts are not the commands.** Several inputs come from gates other organisations control — a data use certification, a Globus subscription. Those take days to weeks. Read [globus-prerequisites.md](globus-prerequisites.md) before you budget time for this; the tooling here takes an hour, and it cannot shorten the rest.

## What you need to have in hand

The wizard will ask you for twelve values. Most you can read off a dashboard in a few minutes; two things you have to own first, and a third only if you want hostnames.

| You must already own | Why | Where it is checked |
|---|---|---|
| An **AWS account** you can reach with an AWS CLI v2 SSO profile | Static access keys are not supported anywhere in this tooling | `cloudpipe preflight`, check `aws.identity` |
| A **Cloudflare account with Zero Trust enabled**, and an API token scoped to it | The deployment creates Access applications and policies | `cloudflare.token` |
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

Take the default unless you already run an identity provider and want the UIs pointed straight at it. An institutional provider does not need that input: it attaches to the Cognito pool as a federated provider instead, which keeps every institution-specific value in one place. That is a documented extension point rather than something the wizard models.

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

## Step 4 — create the state bucket

If you let the wizard render `backend.tf`, it names an S3 bucket that does not exist yet — because the wizard creates no AWS resource. It prints the three commands that create it, and they are worth running as given:

```
aws s3api create-bucket --bucket <your-state-bucket> --region <region> \
  --create-bucket-configuration LocationConstraint=<region>
aws s3api put-bucket-versioning --bucket <your-state-bucket> \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket <your-state-bucket> \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

Versioning is not optional advice. It is what makes a truncated or corrupted state file recoverable, which is the failure a remote backend exists to prevent in the first place.

## Step 5 — render the Globus inputs

The eight Globus variables are owned by a separate tool, which renders them into a `globus.auto.tfvars` in the same root. Terraform loads `.auto.tfvars` files after `terraform.tfvars`, so those values win over anything you put in the latter. Setting them by hand in `terraform.tfvars` instead, as `terraform.tfvars.example` shows, works just as well — both `cloudpipe setup` and `cloudpipe preflight` check that each variable is set, not which file set it.

```
pixi run globus init
```

Read [globus-setup.md](globus-setup.md) for what that tool needs, and [globus-prerequisites.md](globus-prerequisites.md) for the gates ahead of it. Two tools writing one variable is two tools disagreeing about it, which is why the wizard reports on these variables and never writes them.

Globus is one implementation of the data-ingress contract, not a requirement. If you are staging data another way, read [data-ingress.md](data-ingress.md) first — it documents the S3 key contract the pipeline actually depends on.

## Step 6 — preflight

```
pixi run cloudpipe preflight --account <your-aws-account-id>
```

Preflight checks what your answers assert about the world, which is a different question from whether the answers are well-formed. It is read-only: every call it makes is a read, and it creates, modifies and deletes nothing. You can re-run it, paste its output into a ticket, and run it against a deployment somebody else built.

Six checks, in order:

| Check | What it confirms |
|---|---|
| `aws.identity` | Credentials resolve, are not expired, and came from an SSO profile |
| `aws.account` | The resolved account is the one you intended |
| `aws.hosted_zone` | A hosted zone with exactly your domain's name exists in that account. Skipped, not failed, with `domain: null` |
| `aws.prefix_lists` | The Globus managed prefix list ID resolves in this account and region |
| `cloudflare.token` | The token is valid, active, and can read Zero Trust configuration in your account |
| `globus.inputs` | Every Globus variable is set in a file Terraform loads — rendered by `globus init`, or by hand in `terraform.tfvars` |

Exit `3` if any check failed, `0` if they all passed or were skipped.

**Read the skips.** A check that could not be performed reports `skipped`, never `pass`, and names what was missing — usually an IAM action your credentials do not have. A skip is not a failure: the apply itself may not need you to hold that read permission, so preflight exits `0` with skips and says in its closing line how many there were. But it is also not a pass. A green exit code on a run that could not look at half the world is the one outcome this command is built to avoid handing you, and the count is there so you can tell the difference.

`--account` is an option rather than a question because nothing in the stack takes an account ID as an input. Without it, the account check reports `skipped` and its remedy quotes the command back with the resolved account already filled in, so confirming it is a paste rather than a lookup. It is worth passing: deploying into the wrong account is quiet and expensive, because every resource applies perfectly well.

## Step 7 — the phased first apply

Do **not** run a bare `terraform apply` on an empty account. The root's providers look up a cluster that does not exist yet, so the first install has to be applied in phases, each one `-target`ed at a subset.

The eight phases, what each does, and the three variables that change between them are in [infrastructure.md → Bootstrap and install sequence](infrastructure.md#bootstrap-and-install-sequence). Read the whole section before you start. Three phases are where a hand-run install goes wrong:

- **Phase 4's tunnel-token sync.** Terraform never reads the Cloudflare tunnel's connector token, so it has to be copied into Secrets Manager by hand. Without it cloudflared never connects. The section gives the exact commands, which keep the token out of your terminal, your shell history and every process argument list.
- **Phase 7's tunnel check, before Phase 8 closes the public endpoint.** After Phase 8 the EKS API is private-only, reached through the Cloudflare tunnel (or the VPN as a fallback). Close it before the tunnel is proven healthy, and the tunnel is the one route you cannot fix from outside.
- **Step 8 below, also before Phase 8.** The tunnel being healthy is not the same as somebody being able to log in to it.

Phase 6 also writes `terraform/install-state.auto.tfvars`, holding `crds_available`, `vpc_cni_network_policy_enabled` and `vpc_cni_strict_mode`, all `true`. Those three start `false` because the first phases run against a cluster with no CRDs and no kube-system NetworkPolicies, and they have to stay `true` afterwards — so they are persisted to a file every later `terraform` run loads, rather than living only on the install commands. Keep the file; it is gitignored, so a fresh clone needs it recreated before any apply. Losing it destroys the ExternalSecrets and disables NetworkPolicy enforcement, both quietly (#635; [operations.md → Updating infrastructure](operations.md#updating-infrastructure-terraform)).

The reference deployment drives these phases with a script that is not yet part of the published tree, so for now you run them by hand, in order. Publishing that script, parameterized for any deployment, is the setup wizard's next version.

## Step 8 — create the operators' sign-in accounts

**Do this before the phase that closes the public EKS endpoint.** Terraform creates the Cognito user pool, but not the users in it — creating a user is a change to your account that belongs to you, and the wizard's next version is where it moves. Until then it is two commands per operator.

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

## Where to go next

| I want to… | Go to |
|---|---|
| Understand what I am about to deploy | [architecture.md](architecture.md) |
| Read the Terraform resource map and the install phases | [infrastructure.md](infrastructure.md) |
| Decide how to get data into the bucket | [data-ingress.md](data-ingress.md) |
| Find out whether I can reproduce the Globus ingress at all | [globus-prerequisites.md](globus-prerequisites.md) |
| Run the pipeline once the cluster is up | [operations.md](operations.md) |
