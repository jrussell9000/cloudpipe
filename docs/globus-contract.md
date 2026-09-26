# Globus ingress — integration contract

The operator CLI (`pixi run globus …`) is meant to be driven by a person *and* by
another program. A guided setup experience for an independent investigator is
expected later, and this page is what it may depend on. Everything listed here is
versioned; anything not listed here is an implementation detail that may change.

> **Status**: the CLI is being built in phases (`openspec/changes/simplify-globus-ingress`).
> `doctor`, `login`, `status`, `tasks`, `init`, `setup-status`, `configure`,
> `bootstrap-endpoint` and `cleanup-endpoint` exist today. `rotate-s3-key`, once
> planned, was withdrawn along with the key it would have rotated, and it is absent
> rather than stubbed, so `--help` never advertises something that does nothing.
> The contract below was fixed before the commands were written, so they were
> written to it rather than retrofitted. A future wizard should start at
> [Hand-off note for a setup wizard](#hand-off-note-for-a-setup-wizard).

---

## What the contract covers

| Surface | What it is | Where it lives |
|---|---|---|
| Answers schema | The human-supplied inputs, with prompt text, help text, validation, and the gate each value comes from | **now** — `src/globus_admin/schemas/answers.schema.json` (see below) |
| `globus setup-status --json` | Ordered setup steps with state, what each waits on, and the next action | **now** — `src/globus_admin/setup.py` (see below) |
| `globus doctor --json` | One record per health check: id, title, severity, message, remedy, detail | **now** (see below) |
| `globus bootstrap-endpoint`/`cleanup-endpoint` `--json` | The `data` fields and error codes of the two once-per-deployment commands | **now** — `src/globus_admin/commands/` (see below) |
| Human-gate list | The steps no automation can do: the NDA DUC, an HA subscription, adding the endpoint to it, the browser login | declared **now** in `src/globus_admin/gates.py`; emitted by `setup-status` |
| Exit codes | The table below | now |

Each JSON output carries `schema_version`. Within a major version changes are
additive only: fields are never removed or renamed, and identifiers stay stable.

## Global options

Every command accepts these, so a caller drives all of them the same way:

```
--env {production,staging}   which gateway, collection, secret and parameters to act on
--json                       machine-readable envelope on stdout
--non-interactive            never prompt; a required confirmation exits 2
--yes                        supply that confirmation up front
--answers PATH               the answers document ($GLOBUS_ANSWERS, or ./globus-answers.yaml)
```

**stdout is data, stderr is for humans.** With `--json`, stdout carries exactly one
JSON envelope; progress and warnings go to stderr and can be shown verbatim.

## Exit codes

| Code | Meaning | What a caller should do |
|---|---|---|
| 0 | Success | continue |
| 1 | Unexpected failure | report it; this is a bug or something the tool does not model |
| 2 | **Blocked on a human** — a browser login, an expired session, an email to an administrator, an unanswered confirmation | show the remedy and wait for the person |
| 3 | A check failed: the system was reachable and the answer was no | show the remedy; retrying unchanged will fail again |
| 4 | Invalid input or configuration — bad answers, wrong AWS account | send the person back to their answers |

Exit `2` is the one that matters for a wizard: it is the difference between
"waiting on you" and "something is wrong".

## The JSON envelope

```json
{
  "schema_version": "1.0",
  "command": "status",
  "env": "production",
  "ok": true,
  "exit_code": 0,
  "data": { "...": "command-specific" },
  "error": null
}
```

On failure, `error` is populated and `data` is empty:

```json
{
  "error": {
    "code": "globus.session_expired",
    "message": "The Globus session behind the stored refresh token has expired …",
    "remedy": { "kind": "command", "text": "pixi run globus login" },
    "gate": "globus_login",
    "raw": "…the untranslated Globus error…",
    "detail": { "allowed_domains": ["example.edu"] }
  }
}
```

- `code` is stable and machine-readable (`globus.*`, `aws.*`, `cluster.*`, `prereq.*`, `guard.*`, `config.*`).
- `remedy.kind` is `command` when `text` can be run as-is, `human` when a person must act.
- `gate` names a human gate when one is blocking.
- `raw` always keeps the untranslated error. Translation never hides the original.

## `globus doctor` records

`doctor` emits one record per check, in a fixed order, under `data.checks`:

```json
{
  "id": "doctor.session",
  "title": "Globus session",
  "severity": "warn",
  "message": "Globus session: expires 2026-10-17 16:55Z, in 4.2 days — renew soon. …",
  "remedy": { "kind": "command", "text": "pixi run globus login" },
  "detail": { "status": "warn", "timeout_minutes": 43200, "…": "check-specific" }
}
```

The check `id`s and their order are stable, and are what a caller should key on:

| # | `id` | What it answers |
|---|---|---|
| 1 | `doctor.prerequisites` | AWS CLI v2, SSO login, intended account, Terraform, pixi, cluster reachability |
| 2 | `doctor.ssm_parameters` | the deployment's parameters exist and are not placeholders |
| 3 | `doctor.configuration` | the GCS configuration document is present and valid |
| 4 | `doctor.instance` | the GCS instance's state (stopped is normal, not a failure) |
| 5 | `doctor.gridftp` | GridFTP answers on port 443 |
| 6 | `doctor.subscription` | the endpoint is on a Globus subscription |
| 7 | `doctor.config_drift` | declared configuration versus the live endpoint, as of the last reconcile |
| 8 | `doctor.session` | remaining life of the stored Globus login |
| 9 | `doctor.destination_listing` | a real listing through the destination collection |
| 10 | `doctor.source_listing` | a real listing of the source collection |
| 11 | `doctor.kubernetes_secret` | the cluster Secret matches Secrets Manager |
| 12 | `doctor.s3_gateway_credential` | the role the gateway's signing listener assumes exists, trusts the GCS instance's role, may write where transfers land, and nothing outside it; `skipped` for a gateway not yet cut over from a static key |
| 13 | `doctor.stale_nodes` | the endpoint holds no node record for a host that no longer exists |
| 14 | `doctor.s3_listener` | the gateway's signing listener is serving on loopback, as of the last install run; `skipped` for a gateway not yet cut over |

Checks 12 and 14 are two lines on purpose, and a caller should not collapse them.
They answer the two halves of one symptom: a listener that is not running, a role
that cannot be assumed, and a role scoped to the wrong prefix all surface at the
client as an ordinary S3 403. An operator given one line reads that 403 as a
credential problem — which is the half that is usually fine.

Check 12 kept its id when its question changed (`retire-globus-s3-access-keys`
D7). It used to ask about an IAM user and its access keys; once a gateway signs
through a loopback listener, the key Globus holds authenticates nothing and the
credential worth checking is the listener's role. A caller keying on the id is
unaffected; one parsing `detail` will find `role`, `instance_role`, `permissions`
and `confinement` where `user` and `credential_secret` used to be.

Checks **7, 13 and 14 report things nothing off-host can see**, and all three work
the same way: the host writes down what it found, and the check applies the rule to
that. Checks 7 and 13 need `globus-connect-server` listings, which are answered by
the GCS Manager running on the node, and the node is stopped between batches. Check
14's reason is different and permanent — the listener binds `127.0.0.1` and nothing
else, which is what makes an unauthenticated signing proxy safe, so a workstation
has no address to probe even while the host is up.

Check 13 reads `<prefix>/node-report`, written each time the instance registers;
check 7 reads `<prefix>/reconcile-plan`, written by every reconcile run; check 14
reads `<prefix>/listener-report`, written at the end of every listener install run,
after the enable/swap/start steps, so it describes the state that run left behind.

Three consequences follow for all three, and a caller should expect them:

- The severity describes a **snapshot**, so the message names who compared and
  when. It is not a statement about the endpoint as it is at this second.
- A report written by an instance other than the current one is `skipped` rather
  than believed. For check 13 the failure avoided is naming the live node as the
  one to delete; for check 7 it is stronger, because an instance is replaced by
  pinning a new AMI and the AMI is where the reconcile's own rules live.
- "Could not look" and "nothing found" are different answers and never render
  alike: `nodes: null`, `actions: null` and `listeners: null` are `skipped` with the
  reason quoted, never `pass`.

Check 14 has no staleness window beyond that identity test, deliberately. A report
written while the host was up cannot see a listener that died afterwards, and the
host is legitimately stopped for weeks at a time, so any age threshold would be
noise. Instead every message names the observing instance and `generated_at`, and a
`serving` listener with a non-zero systemd restart count is a `warn` rather than a
`pass` — the restart count is the one recorded field that describes an interval
rather than an instant.

Check 7 carries one more refusal of its own. `globus configure` always runs against
a single gateway, so a recorded plan usually examined one environment and the
report records which (`gateway`); a plan scoped to the *other* environment's
gateway is `skipped`, because reading it as clean would report on a gateway nobody
examined. A plan trimmed to fit its parameter says so in the message but never
changes severity — the counts are measured before trimming.

New checks are **appended**, never inserted or renamed, so a caller that reads the
list by index or by id keeps working across a minor `schema_version`.

`severity` is one of `pass`, `warn`, `fail`, `skipped`. **`skipped` means the check
did not run** — because something it depends on failed, or because the cluster or
the instance was unavailable — and never that it passed. Every `warn` and `fail`
carries a `remedy`.

The command's exit code is that of the **first** failing check. Checks run in
dependency order, so the first failure is the root cause; the rest are usually the
same problem seen from further downstream.

`tests/globus/fixtures/doctor-contract.json` records this shape and is asserted
against the real output, so the contract cannot drift silently.

## `globus setup-status` steps

`doctor` answers "is anything wrong?". `setup-status` answers "what do I do
next?", and the two are deliberately different lists: a checklist is flat, a
setup sequence is ordered and has dependencies.

`data.steps` is the whole sequence, always in the same order, whatever state each
step is in. `data.next` names the one step to act on, or is `null` when setup is
complete.

| # | `id` | What it means |
|---|---|---|
| 1 | `setup.aws_cli` | AWS CLI v2 is on PATH |
| 2 | `setup.aws_sso` | credentials resolve from an SSO profile, for the intended account |
| 3 | `setup.terraform` | Terraform satisfies `required_version` |
| 4 | `setup.pixi` | pixi is on PATH |
| 5 | `setup.data_agreement` | the source collection UUID and base path are known |
| 6 | `setup.globus_apps` | the confidential client and native app are registered |
| 7 | `setup.answers` | the answers document is complete and valid |
| 8 | `setup.infrastructure` | the SSM parameters and the GCS instance exist |
| 9 | `setup.endpoint` | an endpoint UUID is recorded for this deployment |
| 10 | `setup.subscription` | the endpoint is on a High Assurance subscription |
| 11 | `setup.configure` | the storage gateway and collection are configured |
| 12 | `setup.login` | a Globus session is established |
| 13 | `setup.credential_sync` | the refresh token is present in the cluster |
| 14 | `setup.transfer_ready` | both collections can actually be listed |

Steps 1–4 are the prerequisites, and they come first because a machine missing
Terraform cannot be told anything useful about step 9.

`state` is one of:

| State | Meaning | What a caller should do |
|---|---|---|
| `done` | observed to be satisfied | nothing |
| `ready` | its predecessors are done and it can be acted on now | show `action` |
| `blocked` | it cannot be acted on: a predecessor is unfinished, a gate is unmet, or its evidence could not be read | show `waiting_on`, and `gate` where present |
| `failed` | something is wrong that nobody grants | show `action` and the reason |

**State is derived from live AWS, Globus, and Kubernetes state on every run —
there is no progress file.** That is what makes setup resumable from a different
machine, and what stops the command reporting progress the account does not
actually have.

Three rules govern the derivation, and each is asserted by a test:

1. **Unknown is never `done`.** A step whose evidence could not be read reports
   `blocked` and names what did not run. An unreachable cluster says nothing
   about the credential, so it must not read as a synced one.
2. **Blocked on a human is not `failed`.** A gate's verification failing means
   someone has to act; the step reports `blocked` and carries the full gate
   record, including request text with this deployment's values filled in.
3. **A step is only as good as what precedes it.** An unfinished predecessor
   settles the state regardless of the step's own evidence.

`waiting_on` holds step identifiers, gate identifiers, or check identifiers —
whatever the step is actually waiting for. `action` is `{kind, text}` with `kind`
one of `command` or `human`, and is `null` only where a step has no action of its
own.

The command's exit code is `3` if any step `failed`, `2` if any is `blocked` or
`ready`, and `0` only when every step is `done` — so `setup-status` works as a
readiness gate. A `blocked` step is a normal state of a setup in progress, which
is why it does not report as a fault.

`tests/globus/fixtures/setup-status-contract.json` records this shape, including
the identity and order of the steps, and is asserted against the real output.

## `register-service-client`, `grant-project-admin` and `delete-service-client`

The confidential client that owns the endpoint, the role it cannot create an
endpoint without, and its opposite. None of the three touches the GCS instance —
all are Globus Auth API calls plus SSM reads and writes — so none takes
`--keep-running` and none starts anything.

All three perform their own browser login, separate from `globus login`, and say so
before opening it. The scope is `manage_projects`, which may create and delete
Globus Auth clients; the pipeline's own credential deliberately does not hold it,
so that a compromise of `globus/refresh-token` cannot reach client registration.
Nothing from this login is stored: no refresh token is requested and the access
token lives in memory for the length of the command.

**`register-service-client` writes the id before the secret**, and the order is
load-bearing. Globus discloses a client secret exactly once, at creation, so a
failure between the two writes cannot be repaired by reading it again. The id
lands first because `delete-service-client` needs only the id: id-without-secret
is repairable in one command, while secret-without-id would be a live client whose
UUID exists only in a scrollback buffer. A store that fails anyway rolls the
registration back — the client is deleted and any parameter already written goes
back to the placeholder — and the error says whether that rollback succeeded.

Which Globus Auth project owns the client is a decision, not a default: a client
cannot be moved between projects afterwards. With exactly one administered project
it is used; with several, `--project-id` is required; with none, the command
refuses rather than creating one, and `--create-project <name>` is the explicit way
to ask for one.

**The client is then made an administrator of that project, and this is a
requirement rather than a courtesy.** `globus-connect-server endpoint setup`
refuses to run as an identity that administers no Auth project — belonging to one
is not enough, and `--project-id` does not stand in for the role. The write is
`update_project`, which *replaces* `admin_ids`, so it sends the project's existing
administrators plus the client plus the logged-in identity: the last is named
explicitly because a short read that dropped the operator could not be undone by
this tooling. `admin_group_ids` is never sent, which is what leaves a
group-administered project intact.

The grant is the **last** step, after both parameters are stored, and the order is
load-bearing for the same reason as the id-before-secret rule: a grant that fails
leaves a client that is recorded and one command away from working, whereas a store
that never ran leaves a secret that cannot be recovered. `grant-project-admin
--project-id <uuid>` is that command. It takes the client id from the deployment
rather than the operator, is idempotent, and reports `already_admin` rather than
writing when the role is already there. It is also the fix for any client
registered in the Globus Developers Portal, which does not grant the role either.

Note what the role permits: a project administrator may create and delete clients
in that project. GCS leaves no alternative, so the mitigation is scope — a
deployment's client belongs in a project of its own.

`grant-project-admin` **requires `--project-id`**, for a different reason than the
typed-UUID guards below: the project a client belongs to is recorded nowhere in the
deployment, so there is nothing to default it from. The project must be one the
operator administers, and the refusal that says otherwise is
`project_not_administered` — the same one `register-service-client --project-id`
raises, because an operator who cannot administer a project cannot write its
administrator list either.

`delete-service-client` **requires `--client-id`, and it must equal what the
deployment records** — the same guard as `cleanup-endpoint --endpoint-id`, for the
same reason. The recorded value is not echoed in the mismatch error. It also
refuses while an endpoint id is recorded: deleting the client would leave an
endpoint nothing in the deployment can manage, not even delete.

None accepts `--env staging`. Both parameters live under the shared prefix,
because there is one service client per deployment, so a staging run would act on
production's.

| `data` field | Command | What it is |
|---|---|---|
| `environment` | all | always `production`, for the reason above |
| `identity` | all | the identity the `manage_projects` login authenticated as |
| `client_id` | all | the client registered, granted the role, or deleted |
| `project_id` | register, grant | the Auth project the client belongs to |
| `id_param`, `secret_param` | register, delete | which SSM parameters hold the pair. Names only — the secret's value is never emitted |
| `name` | register | the display name in the Globus console |
| `project_created` | register | whether this command made that project |
| `stored` | register | whether both parameters were written |
| `project_admin_granted` | register | whether the client was made an administrator of its project |
| `project_admin_count` | register, grant | how many administrators the project has afterwards |
| `rollback` | register | `null`, or `{client_deleted, parameters_reset, errors}` when a store failed |
| `already_admin` | grant | the role was already there, so nothing was written |
| `granted` | grant | the role was added by this run |
| `deleted` | delete | whether Globus confirmed the client is gone |
| `parameters_reset` | delete | which parameters were returned to the placeholder |

| Error code | Exit | When |
|---|---|---|
| `service_client.client_exists` | 3 | either parameter already holds a value |
| `service_client.no_projects` | 3 | the identity administers no Globus Auth project |
| `service_client.project_ambiguous` | 4 | several projects and no `--project-id` |
| `service_client.project_not_administered` | 4 | `--project-id` is not one of them |
| `service_client.project_both_named` | 4 | `--project-id` and `--create-project` were both given |
| `service_client.no_contact_email` | 4 | `--create-project` without `contact_email` in the answers |
| `service_client.no_identity` | 3 | the login reported no identity to administer a new project |
| `service_client.client_id_missing`, `.secret_missing`, `.project_id_missing` | 1 | Globus accepted the call and returned an unusable document |
| `service_client.credential_failed` | 1 | the client exists but Globus issued no credential for it |
| `service_client.store_failed` | 1 | the client was registered and could not be stored; carries the rollback |
| `service_client.grant_failed` | 1 | the client is registered and stored, but Globus refused the administrator role; `grant-project-admin` retries it |
| `service_client.grant_missing_ids` | 4 | the grant was reached without both a project id and a client id |
| `service_client.no_client` | 3 | nothing is recorded to delete, or to grant the role to |
| `service_client.client_id_mismatch` | 4 | `--client-id` is not what the deployment records |
| `service_client.endpoint_in_service` | 3 | an endpoint id is recorded, so the client is still in use |
| `service_client.staging_shares_the_client` | 4 | `--env staging` was passed |

The `1`s are exit `1` rather than the `3` that reads as "the answer was no": in
each of them something was created that this command could not finish recording or
finish configuring, which is a fault rather than a refusal.

## `bootstrap-endpoint` and `cleanup-endpoint`

Two commands that run once per deployment rather than routinely, and the only
pair here whose mutation cannot be undone: **GCS will not let the service client
that created an endpoint create another one.** A caller that offers "try again"
after a failed `bootstrap-endpoint` is offering something that cannot work —
recovery needs a *new* service client, which is `delete-service-client` followed
by `register-service-client`. Scriptable, but not cheap: it destroys a credential
and issues another under a second `manage_projects` login.

`bootstrap-endpoint` is the action behind `setup.endpoint`. It is also the only
command that prints the `endpoint_subscription` gate's request text, because the
endpoint has to exist before anyone can be asked to put it on a subscription.

Both run on the GCS instance through SSM, so both start it when it is stopped and
stop it again afterwards unless `--keep-running` is passed — including when the
run failed, since an instance left running is a cost with no owner. Both ask for
confirmation *before* starting anything, unlike `configure`: there is no plan to
render first, so a declined confirmation costs neither a start nor a stop.

Neither accepts `--env staging`. Both environments' `endpoint-id` parameter is
the same parameter — staging shares the endpoint and differs only in its gateway
and collection — so a staging bootstrap would create production's endpoint and a
staging cleanup would delete it.

| `data` field | Command | What it is |
|---|---|---|
| `environment` | both | always `production`, for the reason above |
| `instance_id`, `document` | both | what ran, and where |
| `started_instance`, `stopped_instance` | both | whether this command changed the instance's power state |
| `report` | both | the on-instance JSON report, or `null`; carries its own `schema_version` |
| `endpoint_id` | both | the endpoint created, or the one deleted; `null` on a bootstrap that reported none |
| `endpoint_name` | bootstrap | the display name, which is also the name in the subscription request |
| `service_client_secret_param` | bootstrap | which SSM parameter the instance was expected to read the secret from |
| `deleted` | cleanup | whether the report proves the endpoint is gone |

`cleanup-endpoint` **requires `--endpoint-id`, and it must equal what the
deployment records.** That is the guard, and the reason it is a UUID rather than
a `--force` flag: the way this command gets run by mistake is an answers document
pointing somewhere unintended, so every guard derived from that document is
answered by the very thing that is wrong. A UUID cannot be typed by accident.

| Error code | Exit | When |
|---|---|---|
| `bootstrap_endpoint.endpoint_exists` | 3 | the deployment already records an endpoint |
| `bootstrap_endpoint.no_secret_ref`, `.secret_ref_not_ssm`, `.secret_ref_wrong_parameter` | 4 | `service_client_secret_ref` names a secret the instance cannot read |
| `cleanup_endpoint.no_endpoint` | 3 | nothing is recorded to delete |
| `cleanup_endpoint.endpoint_id_mismatch` | 4 | `--endpoint-id` is not what the deployment records |
| `cleanup_endpoint.collection_in_service` | 3 | a collection id is recorded under either environment's prefix |
| `*.staging_shares_the_endpoint` | 4 | `--env staging` was passed |
| `*.no_report` | 1 | the run succeeded but produced no report |

`no_report` is exit `1` on purpose, rather than the `3` that reads as "the answer
was no". The endpoint may well have been created or deleted; what the command
cannot do is say which — and a bootstrap that reported success without a report
would otherwise print an institution-bound subscription request naming an
endpoint it never read.

## The answers schema

`src/globus_admin/schemas/answers.schema.json` is a JSON Schema (draft 2020-12)
describing every value a human supplies, and it is meant to be read directly: a
caller can render a complete input form from it without consulting this page or
any other.

Each field carries standard annotation keywords rather than invented ones, so a
generic form generator already knows what to do with them:

| Keyword | What it is |
|---|---|
| property name | the field identifier, and the key in the answers document |
| `title` | the prompt, one short line |
| `description` | the help text — what the value is and where to get it |
| `type`, `pattern`, `minimum`, `enum` | the validation rule for that field |
| `required` (document level) | whether it must be supplied |
| `x-gate` | the human gate that produces the value, where one applies |
| `x-derived` | an override with a working default — **do not ask for it** |

Two rules the schema exists to keep:

- **The required set stays small.** Everything derivable from the deployment name
  — gateway and collection display names, the SSM prefix, secret names, IAM
  resource names, base paths, the session timeout — is derived or defaulted, and
  a test fails if the required set grows beyond what the
  `globus-guided-setup` capability allows. The cap is on what may be *required*,
  so a value that cannot be derived well but is not worth stopping setup for
  becomes an `x-derived` override instead of a question. `org_name`, which
  appears on the endpoint's public Globus listing, is the case that settled the
  rule: deriving it from the deployment name was the only one of the fourteen
  Terraform inputs that failed to reproduce this deployment.
- **No secret is ever written in it.** The service client's secret is given as a
  *reference*: `secretsmanager:<name>`, `ssm:<parameter-name>` for an SSM
  SecureString, or `env:<VARIABLE>`. A literal is rejected by the schema. An
  answers document is expected to be committed, shared and pasted into terminals,
  and a secret in one would have to be treated as compromised from then on.

  Three schemes rather than two because this deployment's GCS service-client
  secret is already an SSM SecureString at `/cloudpipe/globus/gcs-client-secret`,
  next to its client id and deployment key, while the endpoint's other Globus
  secrets (`globus/refresh-token`, `globus/s3-gateway/*`) are in Secrets Manager.
  The schema was widened to describe that truthfully rather than a live secret
  being moved between stores to satisfy it. The cost is real and worth stating:
  anything that enumerates Globus secrets has to look in both places.

There is no AWS credential, key or profile field, and there will not be one.
`aws_account_id` is present only so a command can refuse to act against an
account the operator did not name.

`x-gate` values come from the vocabulary in the schema's own `$defs.gate`, which
is the same list the human-gate records use.

## The human-gate list

`src/globus_admin/gates.py` declares every step that no command can perform. Each
entry carries an identifier, a title, who grants it, a paragraph explaining what
it is and why nothing here can do it, how to verify it, and — where the action is
"send someone a message" — the message itself with this deployment's values
filled in.

| Field | What it is |
|---|---|
| `id` | stable identifier; the same string `CliError.gate` carries |
| `title`, `explanation` | display text; the explanation is a paragraph, not a label |
| `grantor` | who can actually satisfy it |
| `verification` | `{"kind": "check", "text": "doctor.…"}` — a `doctor` check id — or `{"kind": "command", …}` |
| `kind` | `organizational` (someone else, days to weeks) or `operator` (you, now) |
| `recurring` | true where satisfying it once is not enough — a session that lapses |
| `blocks_setup` | false for `operator_confirmation`, which nobody grants |
| `request_text` | the drafted message, or `null` where there is nothing to send |

**Every `gate` identifier the CLI emits is declared here**, so a consumer that
reads `error.gate` from any envelope can always look it up. A test scans the
source for `gate="…"` and fails on one that is missing, and a second test checks
that every `doctor` check a gate names still exists.

The list contains no institution-specific detail and is rendered unchanged at any
deployment; a test enforces that too.

## Prerequisites the contract assumes

The caller's environment provides these; the CLI only detects and explains them.

- **AWS CLI v2** with an IAM Identity Center (SSO) profile, logged in
  (`aws configure sso`, then `aws sso login`). Credentials are read from the
  standard chain (`AWS_PROFILE` or the default profile). **The CLI never accepts
  AWS access keys, never writes AWS configuration, and refuses non-SSO
  credentials** with exit 4.
- **Terraform**, at a version satisfying `required_version` in
  `terraform/versions.tf`. The CLI reads that constraint from the file.
- **pixi**, which runs the CLI.
- **Cloudflare WARP** for the cluster-dependent parts only — the Kubernetes
  secret sync after a login, the running-workflow guard, `doctor`'s Kubernetes
  check. See [infrastructure.md → Remote access](infrastructure.md#remote-access-cloudflare-warp).
  Globus setup itself runs through AWS APIs and needs no cluster access, so those
  parts degrade with an explanation rather than failing the command.

`aws_account_id` in the answers is compared against the logged-in account before
anything is changed: the environment decides *which credentials*, the answers
decide *which account was intended*.

## What the contract deliberately excludes

- Installing prerequisites, or performing the AWS SSO login. The AWS CLI owns
  that browser flow.
- Everything outside Globus ingress — the EKS cluster, ArgoCD, Prefect, metrics.
  A larger installer would need to sequence its own steps around these; the step
  model does not assume Globus, so it can be extended rather than duplicated.
- A Python API. The contract is stdout JSON and exit codes, which work for a
  terminal wizard, a local web UI, a CI job, or an agent, with no server and no
  import of this repository's runtime.

## Hand-off note for a setup wizard

Written 2026-09-22, when the ingress work that defined this contract finished its
code. The decisions behind it are in
[ADR 018](decisions/018-declarative-gcs-config-and-operator-cli.md); the questions
below are tracked in GitHub issue #446, so they do not live only in a finished
change.

**What a wizard can rely on today.** Everything under
[What the contract covers](#what-the-contract-covers): read the answers schema to
build the form, write `globus-answers.yaml`, and loop on
`setup-status --json` — show the first step that is not `done`, run its `action`
when it is a command, show its `gate` text when it is a person, and re-read.
`setup-status` derives every state from live systems, so the wizard needs no
progress file of its own and survives being closed.

**Where it will hit a hand step.** These are steps `setup-status` knows about but
no command performs yet. A wizard should present them as instructions, not try
to automate around them:

| Step | Why it is manual | Would change if |
|---|---|---|
| Seeding the service client's id and secret into SSM ([setup 3.3](globus-setup.md#33-store-the-service-clients-id-and-secret)) | No command writes a secret value; Terraform deliberately never does | A command were added that reads the secret at a hidden prompt |
| Creating the storage gateway and collection ([setup 3.6](globus-setup.md#36-create-the-storage-gateway-and-collection)) | The reconcile plans creates but does not apply them until the create flags have run once on staging | The reconcile gains creates (`simplify-globus-ingress` 11.1b) |
| Registering the production gateway's IAM key ([setup 3.6a](globus-setup.md#36a-register-the-gateways-placeholder-key-pair)) | The key must be typed at a prompt on the instance, and the S3 connector supports no role | The cutover in `retire-globus-s3-access-keys` section 8 — after which the registered value is a placeholder, not a key |
| The subscription request | A person at the subscribing institution decides | Globus grants the service client the subscription-manager role (question 2 below) |

**Not yet proven.** The contract says setup can run end to end with
`--non-interactive --yes --json`. That has been tested command by command against
fakes, not as one unattended run against a real deployment (`simplify-globus-ingress`
T8, task 11.3). Treat the first real unattended run as a test.

**Open questions** (from the change's design, still unresolved):

1. **How far beyond Globus should the step model reach?** A full CloudPipe install
   also stands up the EKS cluster, ArgoCD, Prefect and the metrics store.
   `setup-status` models only the ingress, but its step model does not assume
   Globus, so a larger installer can extend the same list rather than keep a
   second one. Nobody has yet decided whether it should.
2. **Can `bootstrap-endpoint` also set the subscription?** Only if Globus support
   grants the service client the subscription-manager role, which the subscribing
   institution has to ask for. Until then, the printed request is the step.
3. **Should Cloudflare WARP be a global prerequisite?** Today it gates only the
   cluster-dependent steps, because Globus setup needs no cluster access and a
   fresh deployment cannot have WARP before its own Zero Trust configuration
   exists. An operator almost always has it connected, which argues for simply
   requiring it.
4. **Private access comes first for an independent investigator.** They need their
   own Cloudflare Zero Trust account, identity provider and tunnel — or another
   access path — before any cluster-dependent step works. That belongs to the
   larger install, and a wizard has to sequence it before this contract's
   cluster-dependent steps. The step model can express the dependency; nothing
   declares it yet.
5. ~~Does a fresh deployment want a staging gateway?~~ **Decided 2026-09-22: no —
   opt-in.** Staging and read-only production both exist to protect a production
   endpoint configured by hand before this tool, which a fresh deployment does not
   have. So a fresh deployment's answers default to no staging and a managed
   production (`staging_enabled`, `production_managed` in the answers schema);
   this repository's own deployment keeps both through its Terraform defaults
   ([globus.md → Staging gateway](globus.md#staging-gateway)). A wizard should not
   ask either question on a first install.

## Related

- [globus-setup.md](globus-setup.md) — the ordered build procedure
- [globus.md](globus.md) — operating the running system
- [globus-prerequisites.md](globus-prerequisites.md) — the human gates, in narrative form
- [data-ingress.md](data-ingress.md) — the S3 contract Globus exists to satisfy
