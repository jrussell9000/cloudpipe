# Globus session CLI — integration contract

The operator CLI (`pixi run globus …`) is meant to be driven by a person *and* by
another program: a setup wizard, a CI job, an agent. This page is what such a
caller may depend on. Everything listed here is versioned; anything not listed
here is an implementation detail that may change.

**Contract 2.0** (2026-11-12). The CLI has two commands, `login` and `doctor`.
Contract 1.x also had `status`, `tasks`, `setup-status`, `init`, `configure`,
`bootstrap-endpoint`, `register-service-client`, `grant-project-admin`,
`cleanup-endpoint` and `delete-service-client`, the `--env` and
`--start-instance` flags, and eight endpoint checks in `doctor`. They went with
the Globus Connect Server host (ADR 024). A retired command or flag is now an
argparse usage error, exit 2, before any AWS or Globus request.

The version is `CONTRACT_VERSION` in `src/globus_admin/__init__.py`, and every JSON
envelope carries it as `schema_version`. Within a major version changes are
additive only: fields are never removed or renamed, and identifiers stay stable.

---

## What the contract covers

| Surface | Where it lives |
|---|---|
| The two commands and the global options | `src/globus_admin/cli.py` |
| Exit codes | `src/globus_admin/exits.py` |
| The JSON envelope | `src/globus_admin/output.py` |
| `doctor` records, their ids and their order | `src/globus_admin/doctor.py`, recorded in `tests/globus/fixtures/doctor-contract.json` |
| The answers schema | `src/globus_admin/schemas/answers.schema.json` |

## Commands and global options

```
pixi run globus [global options] {login,doctor} [global options]
```

| Command | What it does |
|---|---|
| `login` | Logs in to Globus in a browser, stores the tokens in Secrets Manager, records the login time in SSM, and asks the cluster to re-sync the credential Secret ([globus.md → Logging in](globus.md#logging-in)) |
| `doctor` | Runs the six checks below, in order, and reports each with a remedy |

`login` takes two options of its own: `--client-id UUID` (the native app client id;
default the answers document, then `$GLOBUS_NATIVE_APP_CLIENT_ID`, then the stored
one) and `--no-browser` (print a URL instead of starting a local browser flow).

Every command accepts these, before or after the command name:

```
--json              machine-readable envelope on stdout
--non-interactive   never prompt; a step that needs a person exits 2
--yes               accepted for a 1.x caller; nothing asks for confirmation
--answers PATH      the answers document ($GLOBUS_ANSWERS, or ./globus-answers.yaml)
```

`login --non-interactive` always exits 2 (`login.needs_browser`), before asking for
anything: a login needs a person at a browser.

**stdout is data, stderr is for humans.** With `--json`, stdout carries exactly one
JSON envelope; progress and warnings go to stderr and can be shown verbatim.

## Exit codes

| Code | Meaning | What a caller should do |
|---|---|---|
| 0 | Success | continue |
| 1 | Unexpected failure | report it; this is a bug or something the tool does not model |
| 2 | **Blocked on a human** — a browser login, an expired session, an expired SSO login; also an argparse usage error | show the remedy and wait for the person |
| 3 | A check failed: the system was reachable and the answer was no | show the remedy; retrying unchanged will fail again |
| 4 | Invalid input or configuration — bad answers, wrong AWS account or region, non-SSO credentials | send the person back to their answers |

Exit `2` is the one that matters for a wizard: it is the difference between
"waiting on you" and "something is wrong".

## The JSON envelope

```json
{
  "schema_version": "2.0",
  "command": "doctor",
  "env": "production",
  "ok": true,
  "exit_code": 0,
  "data": { "...": "command-specific" },
  "error": null
}
```

`env` is always `production` since 2.0, which removed the staging environment; it
is kept so a 1.x consumer reading it keeps working.

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

- `code` is stable and machine-readable. The prefixes are `aws.*`, `cluster.*`,
  `config.*`, `globus.*`, `login.*` and `prereq.*`.
- `remedy.kind` is `command` when `text` can be run as-is, `human` when a person must act.
- `gate` names the human step that is blocking, when one is: `globus_login`,
  `aws_sso_login`, `cluster_access`, or the id of a failed prerequisite.
- `raw` always keeps the untranslated error. Translation never hides the original.

## `login` data

| `data` field | What it is |
|---|---|
| `environment` | always `production` |
| `username` | the identity the login authenticated as |
| `identity_id` | its Globus identity UUID, or `null` |
| `secret` | the Secrets Manager secret the tokens were written to (`globus/refresh-token`) |
| `session` | `status`, `established_at`, `expires_at`, `remaining_hours`, `timeout_minutes` |
| `cluster_sync` | `{attempted, synced}`, plus `reason` when the sync did not land |

A sync that did not land is not a failed login: the tokens are stored, and the
command exits 0.

| Error code | Exit | When |
|---|---|---|
| `login.needs_browser` | 2 | `--non-interactive` was passed |
| `login.no_source_collection` | 4 | the answers name no `source_collection_id`, so the HTTPS scope cannot be requested |
| `config.account_id_missing` | 4 | the answers name no `aws_account_id` |
| `config.client_id_missing` | 4 | no native app client id from any source |
| `globus.identity_wrong_domain` | 3 | the identity is outside the answers' identity domains; nothing was stored |
| `globus.no_refresh_token` | 3 | the login returned no refresh token |
| `globus.no_https_token` | 3 | the login returned no token for the HTTPS scope; nothing was stored |

## `doctor` records

`doctor` emits one record per check, in a fixed order, under `data.checks`, with a
count of each severity under `data.summary`:

```json
{
  "id": "doctor.session",
  "title": "Globus session",
  "severity": "warn",
  "message": "Globus session: expires 2026-12-09 15:29Z, in 4.2 days — renew soon. …",
  "remedy": { "kind": "command", "text": "pixi run globus login" },
  "detail": { "status": "warn", "timeout_minutes": 43200, "rule_days": 30, "…": "check-specific" }
}
```

The check `id`s and their order are stable, and are what a caller should key on:

| # | `id` | What it answers |
|---|---|---|
| 1 | `doctor.prerequisites` | AWS CLI v2, an SSO login, the intended account and region, pixi; cluster reachability is a `warn`, because only check 5 needs it |
| 2 | `doctor.ssm_parameters` | the session, source collection and source base path parameters exist, and the source ones are not Terraform placeholders |
| 3 | `doctor.session` | the stored login's remaining life under the 30-day rule |
| 4 | `doctor.source_listing` | the source base path lists with the stored transfer token |
| 5 | `doctor.kubernetes_secret` | this deployment's credential Secret holds every synced key, each matching Secrets Manager |
| 6 | `doctor.https_token` | one refresh of the HTTPS token for the source collection's scope |

The surviving ids are their 1.x ids, so a caller keying on an id is unaffected by
the renumbering; `doctor.https_token` is new in 2.0. New checks are **appended**,
never inserted or renamed.

`severity` is one of `pass`, `warn`, `fail`, `skipped`. **`skipped` means the check
did not run** — because something it depends on failed, or because the cluster was
unreachable, or because the deployment has no cluster or Secret of its own — and
never that it passed. Every `warn` and `fail` carries a `remedy`. The dependencies:

- Check 1 failing skips every other check.
- Checks 4 and 6 are skipped when no login is stored, or no source collection id
  is recorded.
- Check 5 is skipped when the cluster is unreachable, when no EKS cluster is named
  for the deployment, or when that cluster has no `globus-credentials` Secret. It
  **fails** (exit 4) when kubectl's current server is not that cluster's endpoint.

The command's exit code is that of the **first** failing check, or 0. Checks run in
dependency order, so the first failure is the root cause.

`tests/globus/fixtures/doctor-contract.json` records the envelope keys, the
`data`, `summary` and record keys, the vocabularies, the exit codes and the check
ids in order. `tests/globus/test_doctor_json_contract.py` asserts it against the
real output, so the contract cannot drift silently. Titles and messages are
deliberately not recorded: they are prose for a person.

## The answers schema

`src/globus_admin/schemas/answers.schema.json` is a JSON Schema (draft 2020-12)
describing every value a human supplies, and nothing else. A caller can render an
input form from it without consulting this page.

| Field | Required | What it is |
|---|---|---|
| `aws_account_id` | yes | the 12-digit account, checked against the logged-in one |
| `native_app_client_id` | yes | the Globus native app a person logs in through |
| `source_collection_id` | yes | the collection data is copied from |
| `source_base_path` | yes | the parent directory of the subject directories on it |
| `identity_domain` | one of the two | the identity domain the source's High Assurance policy requires |
| `identity_domains` | one of the two | several, when one is not enough |
| `deployment_name` | no (`x-derived`) | derives the SSM prefix and the cluster name; default `cloudpipe` |
| `aws_region` | no (`x-derived`) | default: the AWS profile's region |

Each field carries standard annotation keywords, so a generic form generator
already knows what to do with them:

| Keyword | What it is |
|---|---|
| property name | the field identifier, and the key in the answers document |
| `title` | the prompt, one short line |
| `description` | the help text — what the value is and where to get it |
| `type`, `pattern`, `minItems` | the validation rule for that field |
| `required`, `anyOf` (document level) | which fields must be supplied |
| `x-gate` | the human gate that produces the value: `nda_duc`, `globus_app_registration` or `globus_login`, from `$defs.gate` |
| `x-derived` | an override with a working default — **do not ask for it** |

The schema is closed (`additionalProperties: false`), so a 1.x document with the
retired host's fields is refused with exit 4, naming each field to delete. No
secret is ever written in it, and there is no AWS credential, key or profile
field: credentials come from the environment, and `aws_account_id` exists only so
a command can refuse to act against an account the operator did not name.

## Prerequisites the contract assumes

The caller's environment provides these; the CLI only detects and explains them.

- **AWS CLI v2** with an IAM Identity Center (SSO) profile, logged in
  (`aws configure sso`, then `aws sso login`). Credentials are read from the
  standard chain (`AWS_PROFILE` or the default profile). **The CLI never accepts
  AWS access keys, never writes AWS configuration, and refuses non-SSO
  credentials** with exit 4.
- **pixi**, which runs the CLI. Terraform is not a prerequisite since 2.0.
- **Cloudflare WARP** for the cluster-dependent parts only — the Secret sync after
  a login, the running-workflow note, `doctor`'s Kubernetes check. See
  [infrastructure.md → Remote access](infrastructure.md#remote-access-cloudflare-warp).
  Those parts degrade with an explanation rather than failing the command.

## What the contract deliberately excludes

- Installing prerequisites, or performing the AWS SSO login. The AWS CLI owns
  that browser flow.
- Everything outside the Globus session — the stack, the cluster, ArgoCD, Prefect.
  The setup wizard (`pixi run cloudpipe`) has its own contract
  ([deployer-first-hour.md](deployer-first-hour.md)).
- A Python API. The contract is stdout JSON and exit codes.

## Related

- [globus.md](globus.md) — the runbook: login, doctor, renewal, troubleshooting
- [globus-prerequisites.md](globus-prerequisites.md) — what other people must grant first
- [data-ingress.md](data-ingress.md) — the S3 contract the ingress satisfies
