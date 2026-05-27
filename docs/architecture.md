# CloudPipe Architecture Overview

Neuroimaging preprocessing pipeline for the ABCD Study. Processes minimally preprocessed structural and functional MRI through registration and functional preprocessing to produce MNI-space BOLD with confounds.

---

## System map

```
                        ┌──────────────────────────────────────────────────────────────────┐
                        │  GitHub (<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>)                        │
                        │  push to main                                                    │
                        │    ├── images/prefect-flow-runner/** ─► GitHub Actions ─► ECR   │
                        │    ├── prefect/flows/**               ─► GitHub Actions ─► ECR   │
                        │    ├── gitops/**                      ─► ArgoCD (Helm releases)  │
                        │    └── argo/workflows/**              ─► ArgoCD ─► WorkflowTemplates│
                        └──────────────────────────────────────────────────────────────────┘
                                              │ ArgoCD pull/sync
                                              ▼
                        ┌──────────────────────────────────────────────────────────────────┐
                        │  EKS cluster: cloudpipe  (<YOUR_AWS_REGION>)                            │
                        │                                                                  │
                        │  ┌──────────────┐   ┌──────────────────────────────────────┐   │
                        │  │ Prefect       │   │ Argo Workflows (argo-workflows ns)   │   │
                        │  │ (prefect ns)  │   │                                      │   │
                        │  │              │──►│  WorkflowTemplates:                  │   │
                        │  │ queue-manager│   │    cloudpipe (v2 production)         │   │
                        │  │ flows        │   │    cloudpipe-fullproc (dev)           │   │
                        │  │              │   │    fmri-first-level-proc             │   │
                        │  └──────┬───────┘   └──────────────────────────────────────┘   │
                        │         │                         │ schedules pods on            │
                        │         │ reads SSM               ▼                              │
                        │         │           ┌─────────────────────────────────┐         │
                        │  ┌──────▼───────┐   │  Karpenter node pools           │         │
                        │  │ SSM Params   │   │   cpu-light   t-series spot      │         │
                        │  │ /cloudpipe/  │   │   cpu-heavy   c/m/r 2xl-4xl spot│         │
                        │  │   globus/*   │   │   gpu         g4dn/g6 xlarge     │         │
                        │  └──────────────┘   └─────────────────────────────────┘         │
                        │                                                                  │
                        │  Shared storage: EFS (50 Gi PVC per workflow, deleted on done)  │
                        │  Metadata DB:    RDS PostgreSQL (argo + prefect schemas)        │
                        └──────────────────────────────────────────────────────────────────┘
                                              │ reads/writes
                                              ▼
                        ┌──────────────────────────────────────────────────────────────────┐
                        │  S3 bucket: abcd-v7  (primary data)                             │
                        │   mmps_mproc/{subj}/{ses}/func/      ← Globus lands here        │
                        │   derivatives/fastsurfer/{subj}/                                │
                        │   derivatives/registration/{subj}/{ses}/                        │
                        │   derivatives/func/{subj}/{ses}/     → final outputs            │
                        │   config/                            MNI template, nss_volumes  │
                        │   metrics/                           QC + cost metrics (JSON)   │
                        └──────────────────────────────────────────────────────────────────┘
                                              │ Glue crawlers (daily 01:00 UTC)
                                              ▼
                        ┌──────────────────────────────────────────────────────────────────┐
                        │  AWS Glue: cloudpipe_metrics database                           │
                        │  Athena workgroup: cloudpipe_metrics_workgroup                  │
                        │                                                                  │
                        │  ┌──────────────────────────────────────────────────────────┐   │
                        │  │ Grafana (grafana ns)   grafana.braveresearchcoll...org   │   │
                        │  │   Pipeline Throughput / Functional QC / Anat QC / Costs  │   │
                        │  └──────────────────────────────────────────────────────────┘   │
                        └──────────────────────────────────────────────────────────────────┘
                                              ▲
                        ┌─────────────────────┴────────────────────────────────────────────┐
                        │  Globus Connect Server (EC2, stopped when idle)                  │
                        │  S3 storage gateway → writes directly to abcd-v7                 │
                        │  Source: DAIRC MMPS Globus endpoint                             │
                        │  Instance ID / collection UUID stored in SSM                    │
                        └──────────────────────────────────────────────────────────────────┘
```

---

## Automated processes — what triggers what

| Process | Trigger | What it does |
|---|---|---|
| **GitHub Actions** | `push` to `main` touching `images/prefect-flow-runner/**` or `prefect/flows/**` | Builds `cloudpipe-flow-runner` image, pushes to ECR Public |
| **ArgoCD** (`cluster-addons` ApplicationSet) | New commit on `main` in `gitops/apps/**` | Reconciles Helm releases (Argo Workflows, Prefect, cert-manager, external-secrets, etc.) |
| **ArgoCD** (`workflow-templates` Application) | New commit on `main` in `argo/workflows/**` | Applies WorkflowTemplates + ConfigMaps to `argo-workflows` namespace. `selfHeal=true` reverts any manual `kubectl apply` within seconds. |
| **Prefect** `cloudpipe-queue-manager` | Manually triggered deployment run | Drip-feeds subjects from a CSV into the `cloudpipe` Argo WorkflowTemplate. Blocks when `max_concurrent` (default 50) running workflows are active. Reads Globus collection UUID from SSM at runtime. |
| **Prefect** `first-level-queue-manager` | Manually triggered deployment run | Same drip-feed pattern for `fmri-first-level-proc`. Skips subjects that already have S3 outputs. Shares the same `count_running()` counter as cloudpipe-queue-manager. |
| **Argo Workflows** `cloudpipe` | Prefect submits via Argo API | Per-subject pipeline (see pipeline section below). |
| **Argo Workflows** `fmri-first-level-proc` | Prefect submits via Argo API | First-level GLM analysis (separate pipeline). |
| **Karpenter** | Pod scheduled with `nodeSelector: karpenter.sh/nodepool: <pool>` | Provisions EC2 spot instances on demand; consolidates to zero when idle (empty nodes, 10 min). |
| **Argo exit handler** `emit-workflow-metrics-template` | Every workflow completion (success or failure) | Writes `WorkflowRun` JSON to `s3://abcd-v7/metrics/workflow-runs/` via the `python+boto3` image. |
| **AWS Glue crawlers** (5) | Nightly at 01:00 UTC | Crawl `s3://abcd-v7/metrics/` prefixes and update the `cloudpipe_metrics` Glue database schema. |
| **Prefect** `kubecost-cost-scraper` | Nightly at 02:00 UTC (activated after Kubecost labeling work) | Calls the in-cluster Kubecost Allocation API and writes per-subject `CostAllocation` JSON to `metrics/costs/`. |

---

## cloudpipe_minproc pipeline (production)

Input: ABCD minimally preprocessed data already on DAIRC MMPS Globus endpoint. Upstream preprocessing already applied (motion correction, B0/SDC, gradient nonlinearity correction, between-scan motion correction, fMRI-T1w registration matrix) — do not re-implement these.

Each workflow processes one subject. The master DAG (`cloudpipe-long-master-workflow-template.yaml`) fans out per-session work using `withParam` over the inventory result.

### Phase 1 — Transfer

| Step | Template | Skips when |
|---|---|---|
| Start Globus EC2 instance | `globus-transfer` → `start-globus-instance-template` | Instance already running |
| Globus transfer | `globus-transfer` → `globus-transfer-template` | Never (always runs) |
| S3 sync (POSIX staging) | `globus-transfer` → `globus-s3-sync-template` | `globus-use-s3-gateway == "true"` (current default) |

With the S3 gateway (current config), the transfer step writes directly to `abcd-v7` and the sync step is skipped. Semaphore caps concurrent transfers at 8.

Node pool: `cpu-light-nodepool`

### Phase 2 — Inventory

Template: `inventory` → `subject-data-inventory-template`

Scans S3 `mmps_mproc/{subj}/` to discover sessions and BOLD runs (task-rest, task-nback only). For each run, checks whether `b2t_exists` and `func_exists` in `derivatives/`. Checks `t1w_to_mni.tar.gz` per session. Checks FastSurfer templated derivatives for all sessions. Attaches `nss_frames` from `config/nss_volumes.csv`. Outputs a JSON array that drives the per-session fan-out.

Node pool: `cpu-light-nodepool`

### Phase 3 — Anatomical (skipped if all FastSurfer derivatives already exist)

Longitudinal FastSurfer pipeline. All four steps are sequential (template segmentation must precede long segmentation; template parcellation runs in parallel with long segmentation; long parcellation waits for both).

```
fastsurfer-template-creation
         │
fastsurfer-template-segmentation
         ├──────────────────────────────────┐
fastsurfer-template-parcellation     fastsurfer-long-segmentation
         │                                  │
         └──────────► fastsurfer-long-parcellation
```

Templates: `fast-tmpl` and `fast-long` WorkflowTemplates.

Outputs uploaded to `derivatives/fastsurfer/{subj}/` as per-session `_templated.tar.gz` archives.

### Phase 4 — Per-session (parallel across sessions, `failFast: false`)

One iteration of the session-level DAG runs per session discovered in inventory.

#### 4a — Registration

T1w-to-MNI and BOLD-to-T1w run in parallel. Each run in the session gets its own BOLD-to-T1w pod (fanned out via `withParam`).

| Step | Tool | Node pool | Skips when |
|---|---|---|---|
| T1w → MNI152NLin2009cAsym | FireANTs (GPU) affine + SyN | `gpu-nodepool` | `t1w-to-mni-exists == "true"` (per inventory) |
| BOLD reference → T1w | SynthMorph (deep learning affine) | `cpu-heavy-nodepool` | `b2t_exists == "true"` (per run, per inventory) |

T1w→MNI uses FreeSurfer conformed `orig.mgz` (not BIDS `T1w.nii.gz`) to ensure the source space matches BOLD-to-T1w (bbregister produces a transform in conformed space).

Outputs stored in `derivatives/registration/{subj}/{ses}/`:
- `t1w_to_mni.tar.gz` — affine.mat, warp, invwarp (reused across reruns)
- `bold_to_t1w_{task}_{run}.tar.gz` — LTA, ITK affine, brain mask (downloaded by func-preproc)

#### 4b — Functional preprocessing (after registration, parallelism: 2 per session)

Template: `functional-preprocessing` → `functional-preprocessing-template`

One pod per `(session, task, run)` tuple. Downloads from S3: BOLD NIfTI + BIDS sidecar, motion params, brain mask and ITK affine (from bold-to-t1w), ANTs transforms (from t1w-to-mni), FreeSurfer aseg (for aCompCor WM/CSF masks), MNI template.

Runs `preproc.py` (AFNI-based). Output: MNI-space BOLD tar.gz uploaded to `derivatives/func/{subj}/{ses}/`.

Node pool: `cpu-heavy-nodepool` (16 GB RAM, 6 CPU requested)

Skips when `func_exists == "true"` (checked in inventory).

---

## Infrastructure (Terraform)

All infrastructure is in `terraform/`. Run commands from that directory.

| Resource | Details |
|---|---|
| EKS cluster | `cloudpipe`, Kubernetes 1.35, private API endpoint (VPN required) |
| VPC | 10.0.0.0/16 + secondaries 10.1.0.0/16, 10.2.0.0/16 |
| Client VPN | Split-tunnel, client CIDR 10.3.0.0/22 |
| Karpenter — `cpu-light-nodepool` | t-series, spot + on-demand, for lightweight pods |
| Karpenter — `cpu-heavy-nodepool` | c/m/r 2xl–4xl, spot only, for compute-heavy pods |
| Karpenter — `gpu-nodepool` | g4dn/g6 xlarge, NVIDIA GPU, spot + on-demand, for registration |
| RDS PostgreSQL | Two databases: `argoworkflows` (Argo metadata), `prefect` (Prefect metadata) |
| EFS | Shared storage for per-workflow PVCs; `efs-sc` StorageClass |
| S3 — `abcd-v7` | Primary data bucket |
| S3 — `cloudpipe-logging` | Log archive |
| ECR Public | `public.ecr.aws/l9e7l1h1/cloudpipe/` — all pipeline images |
| Route53 | `<YOUR_DOMAIN>` — Argo UI, Prefect UI, ArgoCD |
| SSM Parameter Store | Globus instance ID, collection UUIDs, base paths (see below) |

Key SSM parameters:

| Parameter | Content |
|---|---|
| `/cloudpipe/globus/instance-id` | EC2 instance ID of the Globus Connect Server |
| `/cloudpipe/globus/collection-id` | Destination GCS collection UUID (updated on instance replacement) |
| `/cloudpipe/globus/source-collection-id` | Source collection UUID (DAIRC MMPS) |
| `/cloudpipe/globus/source-base-path` | Root path on source collection |

---

## GitOps (ArgoCD)

ArgoCD uses an app-of-apps pattern. The `cluster-addons` ApplicationSet in `gitops/bootstrap/root-app.yaml` generates one Application per directory under `gitops/apps/`. Each directory is a Helm chart.

Managed add-ons: `argo-workflows`, `argo-events`, `prefect`, `external-secrets`, `cert-manager`, `aws-load-balancer-controller`, `aws-ebs-csi-driver`, `aws-efs-csi-driver`, `external-dns`, `reloader`, `prometheus-operator-crds`, `cluster-config`.

The separate `workflow-templates` Application (`gitops/apps/pipelines/workflow-templates.yaml`) watches `argo/workflows/` recursively and syncs WorkflowTemplates and ConfigMaps. `selfHeal: true` means any manual `kubectl apply` to `argo-workflows` namespace is reverted within seconds — always commit and push to change WorkflowTemplates.

---

## Docker images

All images pushed to `public.ecr.aws/l9e7l1h1/cloudpipe/`. Production templates pin images by SHA digest; only `bravepy` and `cloudpipe-flow-runner` use `:latest`.

| Image | Used by | Purpose |
|---|---|---|
| `bravepy` | Globus control, inventory, S3 sync | Lightweight Python + boto3 for scripting steps |
| `globus` | `globus-transfer-template` | Runs `transfer.py` — submits and polls Globus transfers |
| `fireants` | `t1w-to-mni-template` | FireANTs affine + SyN T1w→MNI registration (GPU) |
| `synthmorph` | `bold-to-t1w-template` | SynthMorph deep learning BOLD→T1w registration |
| `afni` | `functional-preprocessing-template` | AFNI + `preproc.py` functional preprocessing |
| `fastsurfer` (fast-tmpl / fast-long) | Anatomical phase | FastSurfer longitudinal segmentation + parcellation |
| `cloudpipe-flow-runner` | Prefect work pool | Prefect flows (queue managers); rebuilt on push via GitHub Actions |
| `fmri-first-level-proc` | `fmri-first-level-proc` workflow | First-level GLM; separate build pipeline |

---

## Prefect deployment notes

Flow code is baked into the `cloudpipe-flow-runner` Docker image. Changing flow code requires:
1. Push to `main` → GitHub Actions rebuilds and pushes the image.
2. If `prefect/prefect.yaml` changed, also run `prefect deploy --all` manually (GitHub Actions posts a warning in the job summary when this is needed).

Prefect API is at `https://prefect.<YOUR_DOMAIN>/api`.

---

## Concurrency controls summary

| Control | Value | Location |
|---|---|---|
| Max concurrent Argo workflows (cloudpipe) | 50 (default, overridable) | Prefect `max_concurrent` parameter |
| Max concurrent Argo workflows (first-level) | 25 (default, overridable) | Prefect `max_concurrent` parameter |
| Max concurrent Globus transfers | 8 | `cloudpipe-semaphores` ConfigMap |
| Max parallel sessions per workflow | 3 (master DAG `parallelism`) | `cloudpipe-long-master-workflow-template.yaml` |
| Max parallel func-preproc runs per session | 2 | `functional-preprocessing-session-level-dag-template` |
| Argo workflow max runtime | 12 hours | `activeDeadlineSeconds: 43200` |
| Argo workflow TTL after completion | 24 hours | `ttlStrategy.secondsAfterCompletion: 86400` |

---

## Observability

Every pipeline step emits a structured JSON metric file to `s3://abcd-v7/metrics/`. AWS Glue crawlers build the `cloudpipe_metrics` catalog database nightly; Athena provides SQL queries; Grafana visualizes the data via the Athena datasource.

| Metric | Source | S3 prefix |
|--------|--------|-----------|
| Functional QC (FD, tSNR, confounds, runtimes) | `preproc.py` | `metrics/func-preproc/` |
| Anatomical QC (brain volume, cortical thickness) | `extract_qc.py` in FastSurfer image | `metrics/anat/` |
| Registration QC (Dice, NCC, Jacobian stats) | `fst1w_to_mni.py`, `bold_to_t1w.py` | `metrics/registration/` |
| Workflow run summary (status, duration) | Exit handler (`python+boto3` image) | `metrics/workflow-runs/` |
| Daily per-subject cost | Kubecost scraper (Prefect flow) | `metrics/costs/` |

See [observability.md](observability.md) for the full schema reference, querying guide, Grafana dashboard inventory, and instructions for adding a new metric.

---

## cloudpipe_fullproc (in development)

Ingests raw DICOMs from a separate Globus base path. Applies preprocessing from scratch: dcm2niix → despiking → slice timing correction → motion correction → SDC (FSL topup) → between-scan motion correction. Then follows the same registration + functional-preprocessing phases as cloudpipe_minproc. Gradient nonlinearity correction is omitted (manufacturer files unavailable).

WorkflowTemplates are in `argo/workflows/cloudpipe_fullproc/` and tracked by the same `workflow-templates` ArgoCD Application.
