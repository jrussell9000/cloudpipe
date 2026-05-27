# CloudPipe Documentation

CloudPipe is a neuroimaging preprocessing pipeline for the ABCD Study. It processes minimally preprocessed sMRI/fMRI (Hagler et al. 2019) through FastSurfer longitudinal segmentation, FireANTs registration, SynthMorph BOLD→T1w alignment, and AFNI functional preprocessing to produce MNI-space BOLD with confounds. A separate first-level GLM pipeline runs downstream.

Everything runs on AWS EKS (`cloudpipe`, <YOUR_AWS_REGION>) using Argo Workflows for step execution, Karpenter for on-demand node provisioning, ArgoCD for GitOps, and Prefect for queue management.

---

## Start here

| I want to… | Go to |
|---|---|
| Understand how the system fits together | [architecture.md](architecture.md) |
| Run the pipeline or check on a subject | [operations.md](operations.md) |
| Understand what each pipeline step does | [pipelines.md](pipelines.md) |
| Investigate a failed workflow | [operations.md → Failure handling](operations.md#failure-handling) |
| View QC metrics and cost dashboards | [observability.md](observability.md) |
| Query pipeline metrics in Python or SQL | [observability.md → Querying](observability.md#querying-with-python) |
| Update `preproc.py` | [operations.md → Updating code](operations.md#updating-code) |
| Rotate a Globus credential | [globus.md → Credential rotation](globus.md#credential-rotation) |
| Replace the GCS EC2 instance | [globus.md → Instance replacement](globus.md#instance-replacement) |
| Add a new WorkflowTemplate | [argo-workflows.md → Adding a new WorkflowTemplate](argo-workflows.md#adding-a-new-workflowtemplate) |
| Add a new Docker image | [images.md → Adding a new image](images.md#adding-a-new-image) |
| Rebuild or deploy a pre-baked GPU AMI | [pre-baked-amis.md](pre-baked-amis.md) |
| Change Terraform infrastructure | [infrastructure.md](infrastructure.md) |
| Understand a non-obvious design choice | [decisions/README.md](decisions/README.md) |

---

## Reference docs

### System

- [**architecture.md**](architecture.md) — System map, all automated processes, pipeline phases, infrastructure summary, and concurrency controls. Start here for a bird's-eye view.

- [**operations.md**](operations.md) — Service URLs, submitting workflows (Prefect and direct), monitoring, stopping and resuming, failure handling, updating code, cost monitoring, and cluster health checks.

### Pipelines

- [**pipelines.md**](pipelines.md) — Step-by-step execution walkthrough for all three pipelines: cloudpipe_minproc (production), cloudpipe_fullproc (in development), and fmri-first-level-proc. Covers what each step reads from S3, what it writes, skip conditions, and failure modes.

### Observability

- [**observability.md**](observability.md) — Unified pipeline observability layer: data flow, S3 layout, all 5 metric schemas, Grafana dashboards, Python/SQL querying (Athena + DuckDB), Kubecost scraper, and how to add a new metric.

### Infrastructure

- [**infrastructure.md**](infrastructure.md) — Terraform resource map, VPC layout, EKS cluster (add-ons, node groups, Karpenter pools), storage (S3, EFS, RDS), IAM/Pod Identity, auth/SSO, observability, and bootstrap phases.

- [**gitops.md**](gitops.md) — ArgoCD app-of-apps structure, the `selfHeal: true` boundary (anything ArgoCD owns reverts within seconds), Terraform vs ArgoCD ownership split, and common ArgoCD operations.

### Workflow execution

- [**argo-workflows.md**](argo-workflows.md) — WorkflowTemplate inventory, controller configuration, shared workflow settings, artifact storage model, concurrency controls (semaphores, `parallelism`), skip/resume logic, per-template reference, and authentication.

- [**images.md**](images.md) — Docker image build system (three GitHub Actions workflows, SHA auto-pinning), per-image reference (platform, base, contents, node pool), inactive images, and how to add a new image.

- [**pre-baked-amis.md**](pre-baked-amis.md) — GPU node AMI pre-baking with Packer: motivation, current state, rebuild procedure, how to deploy a new AMI via Terraform, planned GHA automation, and gotchas.

### Globus

- [**globus.md**](globus.md) — Architecture, current deployment values, EC2/SSM/IAM components, two-credential model (IAM user key for S3 gateway vs. OAuth2 refresh token), transfer.py walkthrough, High Assurance requirements, credential rotation, instance replacement, and operational checks.

- [**globus-s3-gateway-config.md**](globus-s3-gateway-config.md) — One-time setup walkthrough: endpoint creation, S3 storage gateway, collection, IAM credentials, SSM parameters, HA subscription, native app registration, and refresh token.

- [**globus-two-stage-transfer.md**](globus-two-stage-transfer.md) — Setup guide for the POSIX staging alternative (EBS → aws s3 sync), used by cloudpipe_fullproc.

### Design decisions

- [**decisions/README.md**](decisions/README.md) — Index of all Architecture Decision Records (ADRs): why the S3 gateway was chosen over POSIX staging, why SynthMorph replaced bbregister, why `orig.mgz` is used for T1w→MNI registration, why S3+Athena was chosen for metrics storage, and eight other non-obvious design choices.

---

## Repo layout

```
argo/workflows/
  cloudpipe_minproc/       — production pipeline WorkflowTemplates
  cloudpipe_fullproc/ — in-development pipeline (raw DICOM input)
  fmri_first_level_proc/ — first-level GLM WorkflowTemplate
images/               — Docker image source (one directory per image)
terraform/            — AWS infrastructure
  modules/
    argo-workflows/   — Argo IAM, RDS, RBAC
    globus/           — GCS EC2, SSM, EventBridge
    karpenter/        — NodePools and NodeClass
    metrics/          — Glue crawlers, Athena workgroup, Grafana IAM
    prefect/          — Prefect worker IAM
packer/               — Packer templates for pre-baked node AMIs
gitops/               — ArgoCD app-of-apps
  bootstrap/          — root ApplicationSet
  apps/               — one Helm wrapper chart per add-on
    grafana/          — Grafana + Athena datasource + 4 dashboards
prefect/flows/        — Prefect queue manager flows
tools/
  metrics/            — schemas, Athena/DuckDB query helpers, exit handler, cost scraper
docs/                 — this documentation
  decisions/          — Architecture Decision Records
```

## Key identifiers

| Resource | Value |
|---|---|
| EKS cluster | `cloudpipe` (<YOUR_AWS_REGION>) |
| S3 data bucket | `abcd-v7` |
| S3 first-level bucket | `<YOUR_INPUT_S3_BUCKET>` |
| Argo namespace | `argo-workflows` |
| ECR Public prefix | `public.ecr.aws/l9e7l1h1/cloudpipe/` |
| Globus source collection (DAIRC MMPS) | `43583c7d-29c9-4d36-9cb5-c8a1641923cb` |
| Globus destination collection | `00666689-6b52-444d-b6a8-57a3ce6ee97c` |
| Prefect server | `https://prefect.<YOUR_DOMAIN>` |
| Argo server | `https://argo.<YOUR_DOMAIN>` |
| ArgoCD | `https://argocd.<YOUR_DOMAIN>` |
| Grafana | `https://grafana.<YOUR_DOMAIN>` |
