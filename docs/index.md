# CloudPipe Documentation

CloudPipe is a neuroimaging preprocessing pipeline for the [ABCD Study](https://abcdstudy.org), built to run on ephemeral cloud infrastructure rather than a shared HPC cluster.

It takes ABCD's minimally preprocessed sMRI/fMRI ([Hagler et al. 2019](https://doi.org/10.1016/j.neuroimage.2019.116091)) and carries it through FastSurfer longitudinal segmentation, FireANTs T1w→MNI registration, SynthMorph BOLD→T1w alignment, and AFNI functional preprocessing, producing **MNI-space BOLD with confound regressors** plus CIFTI grayordinates (~90k — deliberately not the standard 91282; see [pipelines.md](pipelines.md#surface-resampling-and-cifti-assembly)). A separate first-level GLM pipeline runs downstream.

## What makes it different

Most neuroimaging pipelines assume a persistent filesystem and a fixed pool of compute. CloudPipe assumes neither, and most of its design follows from that:

- **No shared POSIX filesystem.** Steps exchange data as compressed S3 artifacts, and pods read the source data through a Globus S3 gateway rather than a staged copy ([ADR 001](decisions/001-s3-gateway-over-posix-staging.md), [ADR 004](decisions/004-s3-artifacts-for-inter-step-data.md)). Nothing survives a pod, so every step is restartable and the storage bill is not a function of how long a batch takes.
- **Nodes exist only while work does.** Karpenter provisions each pipeline pod's node on demand and reclaims it after ([ADR 007](decisions/007-karpenter-for-pipeline-pods.md)), including spot GPU nodes for registration. GPUs are time-sliced three pods to a card.
- **Every run is measured, not just logged.** Each step emits structured QC and cost records to a queryable metrics corpus (S3 → Glue → Athena → Grafana), so per-run alignment quality and per-run dollar cost are both first-class queryable facts rather than something reconstructed later from logs. See [observability.md](observability.md).
- **Design choices are written down as they are made.** Sixteen [ADRs](decisions/README.md) record what was chosen, what was rejected, and — where it matters — the measurement that decided it. Several document *rejected* approaches, which are usually the more useful half.

## Scale and cost

The pipeline runs 100-subject batches at `max_concurrent=50`, completing in 1–3 hours. Scaling to the full ABCD cohort at higher concurrency is under review, and the limits for that are projected rather than measured — don't read the figures below as validated at that scale. They come from **settled** AWS billing data for the 100-subject case (list-price estimates run well over):

| | |
|---|---|
| Cost per workflow run | **~$0.304** (settled; ~$30 for a 100-subject batch) |
| Dominant cost | anatomical segmentation (FastSurfer) — with registration, ~83% of the total |
| Runtime bottleneck | functional preprocessing, driven by BOLD run count and length per subject |

Two things here took real measurement to establish, and both are worth stating plainly because the intuitive answer is wrong in each case:

- **Right-sizing was a 20% win, not a halving.** Trimming over-provisioned pod requests moved per-run cost from ~$0.379 to ~$0.304. An internal claim that it *halved* cost was an artifact of reading unsettled billing data and has been retracted.
- **Only settled cost data is usable.** AWS cost attribution keeps reconciling for days; a day-old read overstates by a median ~51%. Any cost figure here that isn't labelled *settled* should be treated as unverified.

[operations.md → Cost monitoring](operations.md#cost-monitoring) documents the method.

## Reusing this

CloudPipe is MIT-licensed and deliberately institution-neutral: deployment-specific values (account IDs, bucket names, domains, Globus UUIDs) appear as `<YOUR_*>` placeholders throughout. It is **not** a turnkey product — it is a working, documented reference deployment. The parts most likely to be useful outside this project, in rough order:

1. The **ADRs** — reusable reasoning, no infrastructure required.
2. The **metrics/QC layer** ([observability.md](observability.md), [ADR 011](decisions/011-s3-athena-for-metrics.md)) — the pattern generalises to any batch pipeline.
3. The **Argo + Karpenter execution model** ([argo-workflows.md](argo-workflows.md), [ADR 009](decisions/009-argo-over-batch.md)).
4. The **Terraform modules**, which are written to be lifted independently.

Note that CloudPipe is intentionally *not* compatible with the DCAN/ABCD-BIDS toolchain; that was never a design goal.

---

## Start here

| I want to… | Go to |
|---|---|
| Understand how the system fits together | [architecture.md](architecture.md) |
| Run the pipeline or check on a subject | [operations.md](operations.md) |
| Understand what each pipeline step does | [pipelines.md](pipelines.md) |
| Investigate a failed workflow | [operations.md → Handling failures](operations.md#handling-failures) |
| View QC metrics and cost dashboards | [observability.md](observability.md) |
| Query pipeline metrics in Python or SQL | [observability.md → Querying](observability.md#querying-with-python) |
| Look up what a metrics field means | [metrics_data_dictionary.md](metrics_data_dictionary.md) |
| Understand why `bold_to_t1w` NMI is ~1.02 and not ~2.0 | [nmi-interpretation.md](nmi-interpretation.md) |
| Update `preproc.py` | [operations.md → Updating code](operations.md#updating-code) |
| Simplify / clean up the code and remove stale code | [code-health-plan.md](https://github.com/jrussell9000/cloudpipe/blob/main/docs/code-health-plan.md) |
| Run tests or check what CI validates on a PR | [operations.md → Running tests and CI checks](operations.md#running-tests-and-ci-checks) |
| Rotate a Globus credential | [globus.md → Credential rotation](globus.md#credential-rotation) |
| Replace the GCS EC2 instance | [globus.md → Instance replacement](globus.md#instance-replacement) |
| Add a new WorkflowTemplate | [argo-workflows.md → Adding a new WorkflowTemplate](argo-workflows.md#adding-a-new-workflowtemplate) |
| Add a new Docker image | [images.md → Adding a new image](images.md#adding-a-new-image) |
| Rebuild or deploy a pre-baked GPU AMI | [pre-baked-amis.md](pre-baked-amis.md) |
| Change Terraform infrastructure | [infrastructure.md](infrastructure.md) |
| Sync changes to the public repo | [operations.md → Syncing to the public repo](operations.md#syncing-to-the-public-repo) |
| Understand a non-obvious design choice | [decisions/README.md](decisions/README.md) |

---

## Reference docs

### System

- [**architecture.md**](architecture.md) — System map, all automated processes, pipeline phases, infrastructure summary, and concurrency controls. Start here for a bird's-eye view.

- [**operations.md**](operations.md) — Service URLs, submitting workflows (Prefect and direct), monitoring, stopping and resuming, failure handling, updating code, cost monitoring, and cluster health checks.

### Pipelines

- [**pipelines.md**](pipelines.md) — Step-by-step execution walkthrough for cloudpipe_minproc (production) and fmri-first-level-proc, plus the design for cloudpipe_fullproc (planned, not implemented). Covers what each step reads from S3, what it writes, skip conditions, and failure modes.

- [**code-health-plan.md**](https://github.com/jrussell9000/cloudpipe/blob/main/docs/code-health-plan.md) — Staged plan for walking through, simplifying, and de-staling the codebase without regressing functionality: layered toolchain (ruff, vulture, deptry, mypy, coverage), recommended sequence, and verified dependency leads.

### Observability

- [**observability.md**](observability.md) — Unified pipeline observability layer: data flow, S3 layout, Grafana dashboards, Python/SQL querying (Athena + DuckDB), Kubecost scraper, and how to add a new metric.
- [**metrics_data_dictionary.md**](metrics_data_dictionary.md) — Field-by-field reference for every metrics table (9 schemas + 3 non-schema S3 prefixes): types, units, gating thresholds, and where the live emitted JSON has drifted from the `schemas.py` dataclasses.
- [**how-to-timeframe-metrics-dataframe.md**](how-to-timeframe-metrics-dataframe.md) — Recipe for assembling a date-bounded DataFrame across metrics tables. Read the partition-key caveat: QC tables partition on workflow **start** date and `workflow_runs` on **finish** date, so a single-day window silently returns only one side of a batch that crossed midnight.
- [**nmi-interpretation.md**](nmi-interpretation.md) — Why `bold_to_t1w` NMI scores ~1.02 rather than approaching 2.0, what the Studholme `[1, 2]` scale actually measures (intensity-relationship determinism, not spatial overlap), how it differs from the other normalizations also called "NMI", and why `nmi_gain` gates instead of `nmi`.

### Infrastructure

- [**infrastructure.md**](infrastructure.md) — Terraform resource map, VPC layout, EKS cluster (add-ons, node groups, Karpenter pools), storage (S3, EBS, RDS), IAM/Pod Identity, auth/SSO, observability, and bootstrap phases.

- [**gitops.md**](gitops.md) — ArgoCD app-of-apps structure, the `selfHeal: true` boundary (anything ArgoCD owns reverts within seconds), Terraform vs ArgoCD ownership split, and common ArgoCD operations.

### Workflow execution

- [**argo-workflows.md**](argo-workflows.md) — WorkflowTemplate inventory, controller configuration, shared workflow settings, artifact storage model, concurrency controls (semaphores, `parallelism`), skip/resume logic, per-template reference, and authentication.

- [**images.md**](images.md) — Docker image build system (three GitHub Actions workflows, SHA auto-pinning), per-image reference (platform, base, contents, node pool), inactive images, and how to add a new image.

- [**pre-baked-amis.md**](pre-baked-amis.md) — GPU node AMI pre-baking with Packer: motivation, current state, rebuild procedure, how to deploy a new AMI via Terraform, planned GHA automation, and gotchas.

### Globus

- [**globus.md**](globus.md) — Reference and operations for the running system: architecture, current deployment values, EC2/SSM/IAM components, two-credential model (IAM user key for S3 gateway vs. OAuth2 refresh token), transfer.py walkthrough, High Assurance requirements, credential rotation, instance replacement, operational checks, troubleshooting, and endpoint recovery.

- [**globus-setup.md**](globus-setup.md) — One-time build-from-scratch walkthrough: prerequisites, network requirements, endpoint creation, S3 storage gateway, collection, IAM credentials, SSM parameters, HA subscription, native app registration, and refresh token. Includes a historical appendix on the disabled POSIX+EBS staging alternative.

### Design decisions

- [**decisions/README.md**](decisions/README.md) — Index of all Architecture Decision Records (ADRs): why the S3 gateway was chosen over POSIX staging, why SynthMorph replaced bbregister, why `orig.mgz` is used for T1w→MNI registration, why S3+Athena was chosen for metrics storage, and twelve other non-obvious design choices (sixteen ADRs in all).

---

## Repo layout

```
argo/workflows/
  cloudpipe_minproc/       — production pipeline WorkflowTemplates
  fmri_first_level_proc/ — first-level GLM WorkflowTemplate
  # cloudpipe_fullproc/ (raw DICOM input) is planned but not yet created — see architecture.md
images/               — Docker image source (one directory per image)
terraform/            — AWS infrastructure
  modules/
    addons/           — EKS add-ons + their Pod Identity associations
    argo-workflows/   — Argo IAM, RDS, RBAC
    globus/           — GCS EC2, SSM, EventBridge
    karpenter/        — NodePools and NodeClass
    metrics/          — Glue catalog tables (hand-declared), Athena workgroup, Grafana IAM
    finops/           — CUR/Athena cost-and-usage reporting
    prefect/          — Prefect worker IAM
packer/               — Packer templates for pre-baked node AMIs
gitops/               — ArgoCD app-of-apps
  bootstrap/          — root ApplicationSet
  apps/               — one Helm wrapper chart per add-on
    grafana/          — Grafana + Athena/Prometheus datasources + 8 dashboards
prefect/flows/        — Prefect queue manager flows
src/                  — importable library code (on pytest pythonpath)
  metrics/            — schemas, Athena/DuckDB query helpers, exit handler, cost scraper,
                        nightly compactor, per-step outcome recorder
  inventory.py, validate_test_batch.py, workflow_steps.py, pullsamplestats.py
scripts/              — runnable helpers (shell, one-off python, notebook)
  manifests/          — ad-hoc k8s debug manifests
tools/                — data & reference files (subject-id CSVs, cost CSVs, NIST assessment)
                        internal-only; not published to the public repo
docs/                 — this documentation
  decisions/          — Architecture Decision Records
  investigations/     — dated deep-dives and handoffs (point-in-time, not maintained)
  agents/             — conventions for agent-assisted work (issue tracker, triage labels)
  superpowers/        — openspec change specs, plans, artifacts
```

> `docs/investigations/` is a different kind of document from everything above it. Each file
> is a snapshot of what was true on its date, kept for the reasoning trail — it is **not**
> de-staled as the system moves. When an investigation's conclusion becomes durable it gets
> promoted into a reference doc or an ADR; until then, prefer the reference docs for current
> behaviour.

## Key identifiers

> On the public repo and documentation site every value in this table is scrubbed to a
> `<YOUR_*>` placeholder by `scripts/sync-public.sh`. The table's usefulness there is as a
> **checklist of what a fresh deployment must supply** — one line per value you need to own
> before anything runs — not as a set of live identifiers.

| Resource | Value |
|---|---|
| EKS cluster | `cloudpipe` (<YOUR_AWS_REGION>) |
| S3 data bucket | `<YOUR_S3_BUCKET>` |
| Argo namespace | `argo-workflows` |
| ECR private registry (primary, `ecr-registry` param) | `{account-id}.dkr.ecr.<YOUR_AWS_REGION>.amazonaws.com/cloudpipe/` |
| ECR Public prefix (secondary — dual-push, rollback target) | `public.ecr.aws/l9e7l1h1/cloudpipe/` |
| Globus source collection (DAIRC MMPS) | `<YOUR_GLOBUS_SOURCE_COLLECTION_ID>` |
| Globus destination collection | `<YOUR_GLOBUS_DEST_COLLECTION_ID>` |
| Prefect server | `https://prefect.<YOUR_DOMAIN>` |
| Argo server | `https://argo.<YOUR_DOMAIN>` |
| ArgoCD | `https://argocd.<YOUR_DOMAIN>` |
| Grafana | `https://grafana.<YOUR_DOMAIN>` |
