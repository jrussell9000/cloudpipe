# CloudPipe Architecture Overview

Neuroimaging preprocessing pipeline for the ABCD Study. Processes minimally preprocessed structural and functional MRI through registration and functional preprocessing to produce MNI-space BOLD with confounds.

This page is the bird's-eye view: what the components are and how work flows between them. For what each pipeline *step* does to the data, see [pipelines.md](pipelines.md); for the AWS resources themselves, [infrastructure.md](infrastructure.md).

**The one idea that explains the rest of the design:** nothing is persistent. Compute nodes are provisioned per pod and reclaimed after, there is no shared POSIX filesystem, and pod-local disk dies with the pod. Everything that must outlive a step — intermediate volumes, transforms, QC records, logs — is written to S3. Read the diagram below with that in mind and the otherwise-surprising choices (S3 artifacts between steps rather than a mounted volume, a Globus S3 gateway rather than a staged copy, structured metrics rather than log scraping) all follow from it.

Three control planes divide the work, and it is worth knowing which owns what before changing anything:

| Control plane | Owns | Consequence |
|---|---|---|
| **Terraform** | AWS resources — VPC, EKS, S3, RDS, IAM, Karpenter node pools, Glue/Athena | Changes require an apply; nothing self-heals |
| **ArgoCD** | in-cluster manifests — Helm releases, WorkflowTemplates | `selfHeal: true`, so a manual `kubectl apply` is **reverted within seconds** |
| **Argo Workflows** | per-subject pipeline execution | A running workflow freezes the whole stored template, so a merged fix does not reach an in-flight batch |

The ArgoCD row is the one that surprises people; see [gitops.md](gitops.md) for the ownership boundary in detail.

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
                        │  │ flows        │   │    fmri-first-level-proc             │   │
                        │  └──────┬───────┘   └──────────────────────────────────────┘   │
                        │         │                         │ schedules pods on            │
                        │         │ reads SSM               ▼                              │
                        │         │           ┌─────────────────────────────────┐         │
                        │  ┌──────▼───────┐   │  Karpenter node pools           │         │
                        │  │ SSM Params   │   │   cpu-light   t-series spot      │         │
                        │  │ /cloudpipe/  │   │   cpu-heavy   c/m 2xl-4xl spot   │         │
                        │  │   globus/*   │   │   gpu         g4dn..g6e xl/2xl   │         │
                        │  └──────────────┘   │   first-level Graviton *gd spot  │         │
                        │                     └─────────────────────────────────┘         │
                        │                                                                  │
                        │  Metadata DB:    RDS PostgreSQL (argo + prefect schemas)        │
                        └──────────────────────────────────────────────────────────────────┘
                                              │ reads/writes
                                              ▼
                        ┌──────────────────────────────────────────────────────────────────┐
                        │  S3 bucket: <YOUR_S3_BUCKET>  (primary data)                             │
                        │   mmps_mproc/{subj}/{ses}/func/      ← Globus lands here        │
                        │   derivatives/fastsurfer/{subj}/                                │
                        │   derivatives/registration/{subj}/{ses}/                        │
                        │   derivatives/func/{subj}/{ses}/     → final outputs            │
                        │   config/                            MNI template, nss_volumes  │
                        │   metrics/                           QC + cost metrics (JSON)   │
                        └──────────────────────────────────────────────────────────────────┘
                                              │ partition projection (queryable immediately)
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
                        │  S3 storage gateway → writes directly to <YOUR_S3_BUCKET>                 │
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
| **Prefect** `cloudpipe-queue-manager` | Manually triggered deployment run | Drip-feeds subjects from a CSV into the `cloudpipe` Argo WorkflowTemplate. Blocks when the `cloudpipe-max-concurrent` Prefect Variable's count (default 50) of running workflows are active. Reads Globus collection UUID from SSM at runtime. |
| **Prefect** `first-level-queue-manager` | Manually triggered deployment run | Same drip-feed pattern for `fmri-first-level-proc`. Skips subjects that already have S3 outputs. Shares the same namespace-wide `ConcurrencyGate` counter as cloudpipe-queue-manager. |
| **Argo Workflows** `cloudpipe` | Prefect submits via Argo API | Per-subject pipeline (see pipeline section below). |
| **Argo Workflows** `fmri-first-level-proc` | Prefect submits via Argo API | First-level GLM analysis (separate pipeline). |
| **Karpenter** | Pod scheduled with `nodeSelector: karpenter.sh/nodepool: <pool>` | Provisions EC2 spot instances on demand; consolidates to zero when idle (empty nodes, 10 min). |
| **Argo exit handler** `emit-workflow-metrics-template` | Every workflow completion (success or failure) | Writes `WorkflowRun` JSON to `s3://cloudpipe-metrics/metrics/workflow-runs/` via the `python+boto3` image. |
| **Parquet compaction** | Nightly | Rewrites closed `dt=` partitions into the `*_compacted` Parquet tables. Never touches the current day. |
| **Prefect** `kubecost-cost-scraper` | Nightly at 02:00 UTC (activated after Kubecost labeling work) | Calls the in-cluster Kubecost Allocation API and writes per-subject `CostAllocation` JSON to `metrics/costs/`. |

---

## cloudpipe_minproc pipeline (production)

Input: ABCD minimally preprocessed data already on DAIRC MMPS Globus endpoint. Upstream preprocessing already applied (motion correction, B0/SDC, gradient nonlinearity correction, between-scan motion correction, fMRI-T1w registration matrix) — do not re-implement these.

Each workflow processes one subject. The master DAG (`cloudpipe-long-master-workflow-template.yaml`) fans out per-session work using `withParam` over the sessions whose FastSurfer derivatives were actually published — a post-anatomical re-check of the completion markers, not the inventory result (#270).

### Phase 1 — Transfer

| Step | Template | Skips when |
|---|---|---|
| Start Globus EC2 instance | `globus-transfer` → `start-globus-instance-template` | Instance already running |
| Globus transfer | `globus-transfer` → `globus-transfer-template` | Never (always runs) |
| S3 sync (POSIX staging) | `globus-transfer` → `globus-s3-sync-template` | `globus-use-s3-gateway == "true"` (current default) |

With the S3 gateway (current config), the transfer step writes directly to `<YOUR_S3_BUCKET>` and the sync step is skipped. Semaphore caps concurrent transfers at 8.

Node pool: `cpu-light-nodepool`

### Phase 2 — Inventory

Template: `inventory` → `subject-data-inventory-template`

Scans S3 `mmps_mproc/{subj}/` to discover sessions and BOLD runs (task-rest, task-nback only). For each run, checks whether `b2t_exists` and `func_exists` in `derivatives/`. Checks `t1w_to_mni.tar.gz` per session. Checks FastSurfer templated derivatives for all sessions. Attaches `nss_frames` from `config/nss_volumes.csv`. Outputs a JSON array describing the subject's **inputs**.

A second template in the same WorkflowTemplate, `published-sessions-template`, narrows that array to the sessions the anatomical phase actually published, and it is that narrowed list the per-session fan-out iterates. The two are not interchangeable: the first is about inputs, the second about outputs, and they diverge whenever the completion guard rejects one timepoint.

Node pool: `cpu-light-nodepool`

### Phase 3 — Anatomical (skipped if all FastSurfer derivatives already exist)

Longitudinal FastSurfer pipeline, four pods (template creation and segmentation share one). Template parcellation runs in parallel with long segmentation; long parcellation waits for both.

```
fastsurfer-template-build            (gpu)  creation + --seg_only --base
         ├──────────────────────────────────┐
fastsurfer-template-parcellation     fastsurfer-long-segmentation
   (cpu-heavy)                          (gpu)
         │                                  │
         └──────────► fastsurfer-long-parcellation  (cpu-heavy)
```

Templates: `fast-tmpl` and `fast-long` WorkflowTemplates.

No shared volume — each pod uses a private `emptyDir` and passes `SUBJECTS_DIR` through `scratch/{workflow.name}/anat/` in S3 (still tarballs: write-once, read-once, invisible outside the owning workflow). Final outputs are published **in-pod** to `derivatives/fastsurfer/{subj}/{ses}/` as one object per file, with a `_links.json` symlink manifest and a `_complete.json` marker written last ([ADR 017](decisions/017-exploded-derivatives-over-tarballs.md)).

### Phase 4 — Per-session (parallel across sessions, `failFast: false`)

One iteration of the session-level DAG runs per session discovered in inventory.

#### 4a — Registration

T1w-to-MNI and BOLD-to-T1w run in parallel. BOLD-to-T1w is **one pod per session**, looping over that session's runs internally (not one pod per run) — this avoids paying node provisioning, an image pull, and a redundant FastSurfer tarball download per run.

| Step | Tool | Node pool | Skips when |
|---|---|---|---|
| T1w → MNI152NLin2009cAsym | FireANTs (GPU) affine + SyN | `gpu-nodepool` | `t1w-to-mni-exists == "true"` (per inventory) |
| BOLD reference → T1w | SynthMorph (deep learning affine) | `cpu-heavy-nodepool` | `b2t_exists == "true"` (per run, per inventory) |

T1w→MNI uses FreeSurfer conformed `orig.mgz` (not BIDS `T1w.nii.gz`) to ensure the source space matches BOLD→T1w. SynthMorph receives `brainmask.mgz` as its fixed image, so the transform it writes is referenced to the 256³ conformed frame; registering T1w→MNI from the same frame lets `antsApplyTransforms` compose BOLD→conformed→MNI in a **single interpolation**. See [ADR 003](decisions/003-orig-mgz-for-t1w-registration.md).

Outputs stored in `derivatives/registration/{subj}/{ses}/`:
- `t1w_to_mni.tar.gz` — affine.mat, warp, invwarp (reused across reruns)
- `bold_to_t1w_{task}_{run}.tar.gz` — LTA, ITK affine, brain mask (downloaded by func-preproc)

#### 4b — Functional preprocessing (after registration)

Template: `functional-preprocessing` → `functional-preprocessing-session-template`

**One pod per session**, looping over that session's `(task, run)` pairs internally (not one pod per `(session, task, run)` tuple) — the same consolidation rationale as bold-to-t1w: it collapses the FastSurfer tarball, MNI template, and t1w→MNI warp into a single download per session rather than one per run. Downloads from S3: BOLD NIfTI + BIDS sidecar, motion params, brain mask and ITK affine (from bold-to-t1w), ANTs transforms (from t1w-to-mni), FreeSurfer aseg (for aCompCor WM/CSF masks), MNI template.

Runs `preproc.py` (AFNI-based). Output: MNI-space BOLD tar.gz uploaded to `derivatives/func/{subj}/{ses}/`.

Node pool: `cpu-heavy-nodepool` (4 GB RAM, 3 CPU requested)

Skips when `func_exists == "true"` (checked in inventory).

---

## Infrastructure (Terraform)

All infrastructure is in `terraform/`. Run commands from that directory.

| Resource | Details |
|---|---|
| EKS cluster | `cloudpipe`, Kubernetes 1.35, private API endpoint (VPN required) |
| VPC | 10.0.0.0/16 (single CIDR — `var.secondary_cidr_blocks` is declared but `vpc.tf` never associates it) |
| Client VPN | **Full-tunnel** (`split_tunnel = false`, required so ALB traffic is NAT'd into the client CIDR the ALB security groups trust), client CIDR 10.3.0.0/22 |
| Karpenter — `cpu-light-nodepool` | t-series, spot only, for lightweight pods. Limits 160 CPU / 640 Gi |
| Karpenter — `cpu-heavy-nodepool` | c/m 2xl–4xl (no `r`), nitro, spot only, for compute-heavy pods. Limits 2560 CPU / 10240 Gi |
| Karpenter — `gpu-nodepool` | g4dn/g5/g6/g6e, xlarge–2xlarge only, NVIDIA GPU, spot only, for registration + FastSurfer GPU steps. GPUs are time-sliced 3 ways (`nvidia.com/gpu: 3` per node). Limits 512 CPU / 2048 Gi |
| Karpenter — `first-level-nodepool` | Graviton `{c,m,r}{6,7}gd` xl–4xl, spot only, for the first-level GLM pipeline. Limits 512 CPU / 4096 Gi |
| RDS PostgreSQL | `cloudpipe-argo` (`db.m7g.large`, Argo metadata, fronted by PgBouncer) and `cloudpipe-prefect` (`db.t4g.micro`, Prefect metadata) |
| S3 — `<YOUR_S3_BUCKET>` | Primary data bucket (unversioned; derivative prefixes are flushed per test batch) |
| S3 — `cloudpipe-metrics` | QC + cost metrics, **versioned** — kept separate from `<YOUR_S3_BUCKET>` so records survive derivative flushes |
| S3 — `cloudpipe-finops` | Cost & usage reports, Athena/Grafana query results |
| S3 — `cloudpipe-logging` | Log archive (incl. archived Argo pod logs) |
| S3 — `cloudpipe-terraform-state` | Terraform remote backend |
| ECR (private, primary) | `{account-id}.dkr.ecr.<YOUR_AWS_REGION>.amazonaws.com/cloudpipe/` — layer blobs served via VPC S3 gateway endpoint, no NAT traversal on pull |
| ECR Public (secondary, being retired) | `public.ecr.aws/l9e7l1h1/cloudpipe/` — most images are dual-pushed here, kept as a one-line rollback target (`local.ecr_public_registry` in `terraform/ecr.tf`). Repos are retired image by image via `local.ecr_images_public_retired`; `fmri-first-level-proc` is already gone |
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

Managed add-ons: `argo-workflows`, `prefect`, `external-secrets`, `cert-manager`, `aws-load-balancer-controller`, `aws-ebs-csi-driver`, `external-dns`, `reloader`, `prometheus-operator-crds`, `cluster-config`.

The separate `workflow-templates` Application (`gitops/apps/pipelines/workflow-templates.yaml`) watches `argo/workflows/` recursively and syncs WorkflowTemplates and ConfigMaps. `selfHeal: true` means any manual `kubectl apply` to `argo-workflows` namespace is reverted within seconds — always commit and push to change WorkflowTemplates.

---

## Docker images

Most images are dual-pushed to both the private ECR registry (`{account-id}.dkr.ecr.<YOUR_AWS_REGION>.amazonaws.com/cloudpipe/`) and ECR Public (`public.ecr.aws/l9e7l1h1/cloudpipe/`), a holdover from the NAT-cost migration; `fmri-first-level-proc` is private-only as of 2026-08-17. Production WorkflowTemplates resolve the `ecr-registry` parameter from Terraform's `local.ecr_registry`, which now points at the private registry (`terraform/argowf.tf`); rollback to ECR Public is a one-line change (`local.ecr_public_registry`). Every production template pins images by SHA digest — there are no `:latest` refs left anywhere in `argo/workflows/`. The only `:latest` tags are the flow-runner build tag (`.github/workflows/build-prefect-flow-runner.yaml`) and the placeholder a brand-new image carries until its first `ci: pin workflow images to sha-...` commit lands.

| Image | Used by | Purpose |
|---|---|---|
| `bravepy` | Globus control, inventory, S3 sync | Lightweight Python + boto3 for scripting steps |
| `globus` | `globus-transfer-template` | Runs `transfer.py` — submits and polls Globus transfers |
| `fireants` | `t1w-to-mni-template` | FireANTs affine + SyN T1w→MNI registration (GPU) |
| `synthmorph` | `bold-to-t1w-session-template` | SynthMorph deep learning BOLD→T1w registration |
| `afni` | `functional-preprocessing-session-template` | AFNI + `preproc.py` functional preprocessing |
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
| Max concurrent Argo workflows (namespace-wide, **enforced**) | 400 | `namespaceParallelism`, `terraform/modules/argo-workflows/main.tf` |
| Max concurrent Argo workflows (cloudpipe) | 50 (fallback when Variable unset, overridable live) | Prefect Variable `cloudpipe-max-concurrent` |
| Max concurrent Argo workflows (first-level) | 25 (fallback when Variable unset, overridable live) | Prefect Variable `first-level-max-concurrent` |
| Max pod creates per second | 50, burst 90 | `resourceRateLimit`, `terraform/modules/argo-workflows/main.tf` |
| Max concurrent Globus transfers | 8 | `cloudpipe-semaphores` ConfigMap |
| Max parallel sessions per workflow | 3 (master DAG `parallelism`) | `cloudpipe-long-master-workflow-template.yaml` |
| Argo workflow max runtime | 12 hours | `activeDeadlineSeconds: 43200` |
| Argo workflow TTL after completion | 24 hours | `ttlStrategy.secondsAfterCompletion: 86400` |

The Prefect Variables are the working caps; `namespaceParallelism` is the backstop that cannot be raced by a submission burst. It is deliberately set above the sum of the *live* Variables, not their code fallbacks — `400` = cloudpipe's 300 target + first-level's 25 + headroom — because it applies namespace-wide across pipelines, so setting it to either pipeline's cap would silently hold the *other* pipeline's workflows `Pending` whenever the first was at capacity. See [ADR 008](decisions/008-prefect-as-queue-manager.md). Note that the two Argo controller settings above are set in **Terraform**, not in the Helm chart's `values.yaml`: the chart renders them only into the controller ConfigMap, which Terraform owns (`controller.configMap.create: false`), so values placed in the chart are silently inert (#206).

---

## Observability

Every pipeline step emits a structured JSON metric file to `s3://cloudpipe-metrics/metrics/`. The `cloudpipe_metrics` Glue catalog tables are hand-declared in Terraform (there are no crawlers) and resolve `dt=` partitions by projection, so new records are queryable immediately; Athena provides SQL queries; Grafana visualizes the data via the Athena datasource.

| Metric | Source | S3 prefix |
|--------|--------|-----------|
| Functional QC (FD, tSNR, confounds, runtimes) | `preproc.py` | `metrics/func-preproc/` |
| Anatomical QC (brain volume, cortical thickness) | `extract_qc.py` in FastSurfer image | `metrics/anat-qc/` |
| Registration QC (Dice, NCC, Jacobian stats) | `fst1w_to_mni.py`, `bold_to_t1w.py` | `metrics/registration/` |
| Workflow run summary (status, duration) | Exit handler (`python+boto3` image) | `metrics/workflow-runs/` |
| Daily per-subject cost | Kubecost scraper (Prefect flow) | `metrics/costs/` |

See [observability.md](observability.md) for the full schema reference, querying guide, Grafana dashboard inventory, and instructions for adding a new metric.

---

## cloudpipe_fullproc (planned — design only, not implemented)

**No WorkflowTemplates exist for this pipeline yet.** `argo/workflows/` contains only `cloudpipe_minproc/` and `fmri_first_level_proc/`. This section describes the intended design, not a running or in-progress pipeline.

Would ingest raw DICOMs from a separate Globus base path. Would apply preprocessing from scratch: dcm2niix → despiking → slice timing correction → motion correction → SDC (FSL topup) → between-scan motion correction. Then follow the same registration + functional-preprocessing phases as cloudpipe_minproc. Gradient nonlinearity correction would be omitted (manufacturer files unavailable).

Once implemented, WorkflowTemplates would live in `argo/workflows/cloudpipe_fullproc/`, tracked by the same `workflow-templates` ArgoCD Application. The Application currently excludes that path (`gitops/apps/pipelines/workflow-templates.yaml`) since it doesn't exist; the exclusion should be dropped once templates are added there.
