# 018 — Declarative GCS configuration, reconciled from one answers document, behind one operator CLI

**Status**: Accepted (implemented 2026-09; not yet deployed to production — the
Terraform, the Packer AMI and the reconcile it carries are unapplied as of
2026-09-22)

## Context

The Globus ingress was built by hand, once, by one expert. The endpoint came from
`gcs-finalize-setup`, a script rendered into Terraform `user_data` that installed
whatever `globus-connect-server54` was current at first boot and needed two
browser logins. The storage gateway and collection were created with commands
typed into an SSM session, and their settings existed nowhere but on the
endpoint. Operating it meant `sudo -i`, exporting three `GCS_CLI_*` variables,
and knowing which of four Globus registrations a given command wanted.

Three problems followed from that:

- **Nobody else could run it.** The overriding constraint on this work was that
  someone without deep AWS or Globus knowledge must be able to set it up,
  operate it and recover it. The hand-built path asked that person to hold
  dozens of concepts.
- **The configuration was unreviewable.** The gateway's session timeout — the
  setting that forces the weekly re-login — lived only on the endpoint. Changing
  it, or noticing that it had changed, required knowing to look.
- **A future setup wizard had nothing to stand on.** A guided setup for other
  investigators is expected later. Without a stable input surface and
  machine-readable state, it would have to reimplement the setup rather than
  drive it.

The full design, with the alternatives weighed for each piece, is
`openspec/changes/simplify-globus-ingress/design.md` (D2–D4, D11, D13–D16).

## Decision

### 1. One answers document is the only human input

`globus-answers.yaml`, validated against
`src/globus_admin/schemas/answers.schema.json`, holds every value no machine can
know — about a dozen required fields. Everything else (gateway and collection
names, SSM prefix, secret names, base paths) derives from `deployment_name`.
`globus init` renders it into `terraform/globus.auto.tfvars` and a preview of the
GCS configuration document.

- **No secret is ever in it.** The service client's secret is a *reference*
  (`ssm:`, `secretsmanager:` or `env:`); the schema rejects a literal, because an
  answers document is expected to be committed and pasted around.
- **A separate `.auto.tfvars`, not `terraform.tfvars`.** Terraform loads it after
  `terraform.tfvars`, so rendering needs no merge, and `terraform.tfvars` stays a
  hand-owned file.

### 2. The GCS configuration is declared and reconciled, never hand-applied

Terraform renders the configuration document into SSM. A stdlib-only Python
reconcile, baked into the GCS AMI, reads it, reads the endpoint's live state with
`globus-connect-server -F json`, and plans the difference. Its rules are the
minimum for a tool a non-expert can run against a production endpoint:

- **It never deletes** — not gateways, collections, roles or credentials. The
  bucket is the only copy of what has been transferred.
- **It never touches what it was not told about.** Objects are matched by display
  name; anything live and undeclared is reported and left alone.
- **It refuses rather than recreates.** A difference in a field Globus cannot
  update in place (`high_assurance`, the bucket, a collection's gateway or base
  path) is an error naming the field.
- **`managed: false`** declares an object, reports its drift, and forbids acting
  on it. It is how this deployment's production — configured by hand before the
  tool existed — is held read-only while the same code is proved on staging. A
  fresh deployment, built by the tool from nothing, has neither: its answers
  default to a managed production and no staging gateway.

An SSM association runs it in `plan` mode only, whenever the rendered
configuration changes, so drift is visible; whether that fires usefully on an
instance that is normally stopped is still to be tested, and the association is
one variable away from removal if not. `apply` happens only when an operator runs
`globus configure`, which shows the plan and asks first — an unattended apply
against production during a batch is the surprise the confirmation exists to
prevent.

Every run — association or operator, `plan` or `apply` — also records its plan in
`<prefix>/reconcile-plan`, which is what makes the drift visible from a workstation
rather than only in the output of whichever run produced it. `globus doctor` check 7
reads that parameter and applies the severity rule off-host, the same split
[check 13 uses for node records](../globus-contract.md): the instance is stopped
between batches, and correcting a rule that lives in the AMI means rebuilding it.
Publishing never changes a reconcile's exit code — a diagnostic that could not be
stored must not make a good plan look like a failure.

The reconcile does not yet *create* objects: its `apply` plans creates and skips
them, because create-time flags baked into an AMI before they have been run once
would be guesswork. Until that lands, a new deployment's gateway and collection
are created by hand, and `configure` then verifies them and records the
collection id.

### 3. One operator entry point, run from the workstation

`pixi run globus <command>` (`src/globus_admin/`) is the whole operator
interface: `doctor`, `setup-status`, `status`, `init`, `configure`, `login`,
`tasks`, `bootstrap-endpoint`, `cleanup-endpoint`. It reaches the instance only
through SSM Run Command, so the operator never opens a shell on it for routine
work, and it can check what the instance cannot see — the Kubernetes secret,
Secrets Manager, the cluster's running workflows.

- **`doctor` explains, in order.** Each failed check names the command, or the
  person, that fixes it, and a check it could not run is reported as not run,
  never as passing.
- **`setup-status` derives state from live systems,** never from a progress file,
  so setup can be abandoned, resumed from another machine, or partly done by hand
  and still be read correctly.

### 4. The endpoint is created by the service client, through SSM

`bootstrap-endpoint` runs `endpoint setup` on the instance under the confidential
service client, with `--owner <client>@clients.auth.globus.org`. The endpoint no
longer needs a browser login; only `globus login` — the High Assurance session
the pipeline transfers under — still does, and that is a Globus control that
exists on purpose.

Two rules shaped it and its opposite, `cleanup-endpoint`:

- **The marker is written last and cleared first.** `endpoint-id` is what every
  tool reads as "this deployment has an endpoint". Setting it last on create and
  clearing it first on destroy means every interrupted run leaves the deployment
  reading "no endpoint", the state with an automatic way forward.
- **The destructive guard reads nothing from the answers document.** A stale
  answers file is *how* a destructive command gets run by mistake, so
  `cleanup-endpoint` requires the endpoint UUID typed on the command line, which
  must match SSM, and refuses while any environment records a collection.

A consequence worth knowing before it bites: **Globus will not let a service
client create a second endpoint.** Recreating an endpoint starts with registering
a new client, so teardown is not `terraform destroy` with extra steps.

### 5. The contract is JSON on stdout, and the CLI is its only implementation

Every command takes `--json`, `--non-interactive` and `--yes`. The seam a wizard
consumes is five outputs — the answers schema, `setup-status --json`,
`doctor --json`, the human-gate list, and the exit codes — documented in
`docs/globus-contract.md` and pinned by tests. The exit codes carry the one
distinction a front end needs: `2` waiting on a human, `3` a check failed, `4`
fix your answers. There is no API, daemon or importable library for a wizard; it
drives the same commands a person runs.

### 6. AWS credentials come from an SSO login, and nothing else

The CLI resolves credentials from the environment, as Terraform does, and
refuses anything not resolved from an IAM Identity Center profile — static keys
on an investigator's laptop are what this setup should not teach. The answers
document's `aws_account_id` says which account is *intended*, and every mutating
command compares it with the one logged in to.

### 7. The GCS host is a pinned AMI

`packer/globus-gcs/` builds Ubuntu 24.04 with a pinned `globus-connect-server54`,
the reconcile, and the boot-registration unit (`cloudpipe-gcs-boot`). Terraform
takes the AMI id explicitly, so a rebuild never replaces the instance by itself.

## Alternatives rejected

- **Extending the hand-built scripts.** Kept the knowledge scattered, and their
  create-if-absent logic cannot see drift — they could never have raised a
  timeout.
- **A Terraform provider for GCS objects.** No maintained provider for GCS v5
  management objects exists; the supported interface is the CLI.
- **Having a wizard write `terraform.tfvars` directly.** tfvars follows
  Terraform's shape; an answers document can stay stable while the Terraform
  interface changes underneath it.
- **Auto-applying the reconcile when the configuration changes.** See §2.
- **A `--force` flag on `cleanup-endpoint`.** One character of protection against
  an operation whose recovery needs a web console and another person.

## Consequences

- **Setup is guided, but not yet fully automated.** Registering two Globus
  applications, the data use agreement, the subscription request, the weekly
  login, the production gateway's IAM key, and — until the reconcile creates —
  the gateway and collection remain human steps. Each is named in `setup-status`
  and in [globus-prerequisites.md](../globus-prerequisites.md).
- **The reconcile ships in the AMI.** Changing it means rebuilding the AMI and
  replacing the instance, which keeps the running host reproducible and makes a
  reconcile fix slower to deploy.
- **The configuration now has an owner.** The session timeout, domains and
  collection layout are reviewed in git and held there; hand edits on the
  endpoint show up as drift in the next plan.
- **Two sources remain for secrets.** The service client's secret is an SSM
  SecureString beside its id; the pipeline's Globus credentials are in Secrets
  Manager. Anything that enumerates Globus secrets must look in both.
- **The S3 access-key rotation first designed here was withdrawn.** It needed a
  free key slot the production IAM user does not have, and a keyless design for
  the gateway's S3 access is being pursued separately. Rotation is a documented
  hand procedure ([globus.md → Credential rotation](../globus.md#credential-rotation)).

## Related

- [ADR 001](001-s3-gateway-over-posix-staging.md) — the S3 gateway this configures
- [ADR 010](010-globus-ha-subscription.md) — the High Assurance subscription, and
  the correction that no 30-day HA ceiling exists
- [globus-setup.md](../globus-setup.md), [globus.md](../globus.md),
  [globus-contract.md](../globus-contract.md)
