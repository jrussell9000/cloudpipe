# 020 — The public repo is the upstream; <YOUR_GITHUB_REPO> is a private deployment layer

**Status**: Accepted (2026-09-28). The decision is made; the flip itself is gated
on the preconditions in §4 and has not happened. Until it does, the sync described
under Context keeps running unchanged.

## Context

cloudpipe lives in two repositories. `<YOUR_GITHUB_REPO>` (private) is the source of
truth: production Terraform, ArgoCD and CI all read from it. `jrussell9000/cloudpipe`
(public) is a derived copy. `scripts/sync-public.sh`, run by
`.github/workflows/sync-public.yaml` on every push to `main`, does three things:

1. `rsync --delete` of an allowlist of directories (`argo`, `gitops`, `images`,
   `prefect`, `terraform/modules`, `docs`, `src`, `scripts`) plus a few root files.
   The root Terraform configuration and `tests/` are not synced.
2. A `sed` scrub that swaps each deployment-specific literal in the ordered
   `REPLACEMENTS` list for a `<YOUR_*>` placeholder.
3. A fail-closed check (`VERIFY_PATTERNS`, then `gitleaks`) that aborts the sync if a
   known-sensitive pattern survives.

That model was built to publish a reference copy safely, and it does that. Three
goals now ask more of it:

- **Outside groups will deploy cloudpipe.** A guided setup is planned for them (see
  [ADR 018](018-declarative-gcs-config-and-operator-cli.md), whose operator CLI was
  shaped with that wizard in mind).
- **Outside contributions are wanted.**
- **The code will be the basis of a grant or paper**, which will cite it.

Under the current model none of these goals is well served:

- **The published code runs nowhere.** The public tree is the internal tree after a
  text rewrite. No CI tests it in that form and no deployment runs it, so an outside
  group would be the first to run it.
- **The public repo cannot be deployed.** The root Terraform configuration (194
  `resource`/`module`/`data` blocks across 28 files as of this ADR) is excluded as
  account-specific. The public repo carries only the modules it calls.
- **Every public edit is overwritten.** Because the sync is a one-way `rsync --delete`,
  an outside PR merged in the public repo is deleted by the next sync. A
  contribution has to be hand-ported to internal and synced back.
- **A citation names code production does not run.** Production follows internal
  `main` within seconds (ArgoCD `selfHeal`). No tagged release links a cited version
  to the version that produced a result.
- **Leak safety depends on a denylist.** The scrub and its check know only the
  identifiers someone thought to list. A new kind of identifier (the Globus IAM user
  name was one) gets through until a pattern is added. Every sync pushes the whole
  tree through that filter again.

The scrub is also a measure of the work still to do. Each `REPLACEMENTS` entry marks a
value the code holds as a literal instead of reading it from configuration. As of
this ADR, at least 89 files in the synced code directories (excluding `docs/`) hold
one of the account ID, `<YOUR_S3_BUCKET>`, `<YOUR_AWS_REGION>`, the deployment domain or `<YOUR_INSTITUTION_DOMAIN>`.

## Decision

### 1. The public repo becomes the source of truth for generic code

After the flip, `jrussell9000/cloudpipe` holds everything that is not specific to this
deployment:
- workflow templates, images, Prefect flows, Terraform modules and a deployable stack
- the setup wizard
- generic documentation and ADRs
- the tests for all of these

Development of that code happens there, in public, and outside PRs merge there
directly.

### 2. Internal consumes pinned public releases; it is not a fork

`<YOUR_GITHUB_REPO>` keeps no copy of the generic code. It references it:

| Component | Consumed as |
|---|---|
| Terraform | `source = "git::https://github.com/jrussell9000/cloudpipe//terraform/modules/stack?ref=<tag>"`, plus the internal root, backend and tfvars |
| Argo WorkflowTemplates, gitops | ArgoCD `repoURL` at the public repo, `targetRevision: <tag>`; private values from an internal overlay or the `cloudpipe-config` ConfigMap |
| Images | Built from a public tag and pushed to this deployment's ECR |
| Prefect flows | Installed from a public tag |

Internal keeps only what is private or deployment-specific:
- tfvars and backend configuration
- migration history: `moved` blocks and one-off retirement files such as
  `abcd_v7_metrics_retire.tf`
- investigations, handoffs, cost analyses and compliance documents
  (`tools/nist-800-171-*`)
- subject lists and other data files

### 3. Production pins by tag or commit SHA, never a branch

A public repo accepts PRs from strangers. Production must never follow a public branch.
- Every internal reference names a protected tag or a full commit SHA.
- Upgrading production is a reviewed change in internal that bumps the pin.
- Public CI holds no AWS credentials. Images for this deployment are built by internal
  CI from a public tag.

### 4. The flip is gated on zero literals, not on a date

The flip happens when the scrub has nothing left to replace:
- no deployment literal remains in any synced path, and
- the stack module can deploy a new account from wizard answers alone.

At that point the sync is a plain copy, and the flip is a small, reversible change:
1. Tag the public repo.
2. Point internal's Terraform `source` and ArgoCD `targetRevision` at the tag.
3. Delete the synced directories from internal.
4. Retire `sync-public.sh`.

Until then the sync runs unchanged, and each parameterization change shrinks what it
has to scrub.

This reverses one earlier call. The public-sync hardening change left `<YOUR_AWS_REGION>` and
the domain in gitops Helm values under the scrub, because moving them into a
ConfigMap risked the GitOps contract
(`openspec/changes/archive/2026-07-06-harden-public-sync-sanitization/tasks.md`,
task 4.2). Under this decision they must be parameterized: after the flip, nothing
scrubs them.

### 5. Leak checks move to the source

Once nothing scrubs the public tree, the only defense is to never write a sensitive
value there. The `VERIFY_PATTERNS` set therefore becomes:
- a check in internal CI over the synced paths now, reporting a literal count; it
  becomes a failure once the count reaches zero, and
- after the flip, a pre-commit hook and a required CI check in the public repo.

The set gains an ABCD subject-ID pattern (`NDARINV` plus eight characters), with the
placeholder `NDARINVXXXXXXXX` allowed. As of this ADR the synced paths hold only that
placeholder. Nothing has checked that, and outside contributors raise the risk under
the ABCD data use agreement.

### 6. Releases are citable

The public repo publishes semantic-versioned GitHub releases, archived to Zenodo so
that each release has a DOI, with a `CITATION.cff` at the root. The paper cites a
release, and production runs a pin to that same release, so the version cited is the
version that produced the results.

## Alternatives rejected

- **Keep the current model and only add the wizard.** The wizard would then be
  offered to outside groups as code that production never runs. The public repo also
  could not accept a contribution, and the paper could not cite a version production
  runs.
- **Internal as a long-lived fork of public** (public added as a git remote, upstream
  merged in). This is the simplest setup, but every private edit to a shared file
  becomes a merge conflict. One mistaken push from internal to the public remote
  publishes everything, which is today's leak risk with the direction reversed.
- **Two-way sync tooling (Copybara-style).** It keeps one monorepo and maps paths in
  both directions. That is too much infrastructure for a project with one maintainer.
- **Build the wizard in the public repo and leave internal as it is.** The sync's
  `rsync --delete` would delete it. Keeping it outside the synced directories would
  cut it off from the code it drives.

## Consequences

- **Fixing a production incident takes more steps.** A fix becomes a public PR, a tag
  and a pin bump, instead of one push to internal `main`. Temporarily pinning internal
  to a fix-branch SHA softens this, but the pin must return to a tag once the fix is
  released.
- **The public repo becomes part of the production supply chain.** The SSP names
  `<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>` as the source repository and states that
  "Compromise of the repository is a path to production via ArgoCD"
  (`tools/nist-800-171-ssp.md`). The flip must update the SSP and add public-repo
  controls: branch protection on `main`, required review, and protected tags.
- **Root Terraform must be split, and state must move with it.** The 194 root blocks
  move into a stack module, and the production state must follow with `moved` blocks,
  which stay in internal. A fresh deployment inherits none of them.
- **Docs and tests are sorted one by one.** Generic documentation, ADRs and tests go
  public. Incident investigations and anything naming this deployment's history stay
  internal. `tests/`, never synced today, becomes available to outside users.
- **Every open design must keep literals out.** A change that adds a literal to a
  synced path now adds work before the flip. The source-side check (§5) makes the
  count visible in every PR.

## Related

- [ADR 013](013-src-scripts-tools-reorg.md) — the `src/` / `scripts/` / `tools/` split
  the sync allowlist follows
- [ADR 018](018-declarative-gcs-config-and-operator-cli.md) — the answers document and
  operator CLI the wizard extends
- `openspec/specs/public-sync-sanitization/spec.md` — the scrub and fail-closed check
  this decision eventually retires
- `openspec/changes/public-upstream-readiness/` — the first implementation change
  (source-side checks, stack module, parameterization)
