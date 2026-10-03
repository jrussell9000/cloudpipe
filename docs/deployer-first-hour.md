# The deployer's first hour

This page is the path from a fresh clone to a Terraform root that is ready for its first apply. It covers the three commands that collect and check your deployment's inputs, in the order you run them, and it ends where the infrastructure install begins.

Two things to know before you start, because they set expectations for everything below:

- **None of these commands create anything.** They read your environment, write three files into a directory you choose, and tell you what is still missing. No AWS, Cloudflare, Kubernetes or Globus resource is created, modified or deleted by any of them. The first thing that changes your cloud account is the Terraform apply in the last step, which you run yourself.
- **The slow parts are not the commands.** Several inputs come from gates other organisations control — a data use certification, a Globus subscription, an OIDC registration at your institution. Those take days to weeks. Read [globus-prerequisites.md](globus-prerequisites.md) before you budget time for this; the tooling here takes an hour, and it cannot shorten the rest.

## What you need to have in hand

The wizard will ask you for fifteen values. Most you can read off a dashboard in a few minutes; four you have to obtain or create first.

| You must already own | Why | Where it is checked |
|---|---|---|
| An **AWS account** you can reach with an AWS CLI v2 SSO profile | Static access keys are not supported anywhere in this tooling | `cloudpipe preflight`, check `aws.identity` |
| A **Route53 hosted zone** in that account for the domain you will publish services under | Certificate validation is DNS-based, and it runs against the zone this account owns | `aws.hosted_zone` |
| A **Cloudflare account with Zero Trust enabled**, and an API token scoped to it | The deployment creates Access applications and policies | `cloudflare.token` |
| A **Secrets Manager secret** holding your identity provider's OIDC client ID and secret for Cloudflare Access | Terraform reads it through a data source and never creates it, so an absent secret fails the install late | `aws.access_oidc_secret` |

That last one is worth creating now rather than later. It is a hand-created secret by design — Terraform must not own a value your identity provider issued — and because it is read rather than created, an absent secret does not fail early. It fails in the final phase of the install, after everything else has applied. `cloudpipe preflight` exists largely to move that discovery to the beginning.

You will also need an **OIDC issuer URL** from whoever runs your institution's single sign-on. It is the `issuer` value, not a login page: preflight fetches `<issuer>/.well-known/openid-configuration` and requires it to publish an authorization endpoint, a token endpoint and a JWKS URI, because Cloudflare Access has to be given all three explicitly.

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

The wizard verifies the directory you point it at before writing anything. A directory that declares no module call, or that does not declare the stack's variables, is refused with exit `4`. This check exists because the failure it prevents is silent: a `terraform.tfvars` written into a directory Terraform never reads produces no error at all, and you find out when `plan` prompts you for all twenty-one values anyway.

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

Eight checks, in order:

| Check | What it confirms |
|---|---|
| `aws.identity` | Credentials resolve, are not expired, and came from an SSO profile |
| `aws.account` | The resolved account is the one you intended |
| `aws.hosted_zone` | A hosted zone with exactly your domain's name exists in that account |
| `aws.prefix_lists` | Both managed prefix list IDs resolve in this account and region |
| `oidc.discovery` | Your issuer publishes the three endpoints Cloudflare Access needs |
| `cloudflare.token` | The token is valid, active, and can read Zero Trust configuration in your account |
| `aws.access_oidc_secret` | The hand-created Access OIDC secret exists |
| `globus.inputs` | Every Globus variable is set in a file Terraform loads — rendered by `globus init`, or by hand in `terraform.tfvars` |

Exit `3` if any check failed, `0` if they all passed or were skipped.

**Read the skips.** A check that could not be performed reports `skipped`, never `pass`, and names what was missing — usually an IAM action your credentials do not have. A skip is not a failure: the apply itself may not need you to hold that read permission, so preflight exits `0` with skips and says in its closing line how many there were. But it is also not a pass. A green exit code on a run that could not look at half the world is the one outcome this command is built to avoid handing you, and the count is there so you can tell the difference.

`--account` is an option rather than a question because nothing in the stack takes an account ID as an input. Without it, the account check reports `skipped` and its remedy quotes the command back with the resolved account already filled in, so confirming it is a paste rather than a lookup. It is worth passing: deploying into the wrong account is quiet and expensive, because every resource applies perfectly well.

## Step 7 — the phased first apply

Do **not** run a bare `terraform apply` on an empty account. The root's providers look up a cluster that does not exist yet, so the first install has to be applied in phases, each one `-target`ed at a subset.

The eight phases, what each does, and the three variables that change between them are in [infrastructure.md → Bootstrap and install sequence](infrastructure.md#bootstrap-and-install-sequence). Read the whole section before you start. Two phases are where a hand-run install goes wrong:

- **Phase 4's tunnel-token sync.** Terraform never reads the Cloudflare tunnel's connector token, so it has to be copied into Secrets Manager by hand. Without it cloudflared never connects. The section gives the exact commands, which keep the token out of your terminal, your shell history and every process argument list.
- **Phase 7's tunnel check, before Phase 8 closes the public endpoint.** After Phase 8 the EKS API is private-only, reached through the Cloudflare tunnel (or the VPN as a fallback). Close it before the tunnel is proven healthy, and the tunnel is the one route you cannot fix from outside.

The reference deployment drives these phases with a script that is not yet part of the published tree, so for now you run them by hand, in order. Publishing that script, parameterized for any deployment, is the setup wizard's next version.

## Where to go next

| I want to… | Go to |
|---|---|
| Understand what I am about to deploy | [architecture.md](architecture.md) |
| Read the Terraform resource map and the install phases | [infrastructure.md](infrastructure.md) |
| Decide how to get data into the bucket | [data-ingress.md](data-ingress.md) |
| Find out whether I can reproduce the Globus ingress at all | [globus-prerequisites.md](globus-prerequisites.md) |
| Run the pipeline once the cluster is up | [operations.md](operations.md) |
