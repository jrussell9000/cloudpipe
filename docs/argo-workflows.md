# Argo Workflows

All pipeline execution runs inside Argo Workflows on the `cloudpipe` EKS cluster, namespace `argo-workflows`. This document covers the WorkflowTemplate inventory, concurrency model, artifact storage, and per-template details.

---

## WorkflowTemplate inventory

```
argo/workflows/
  cloudpipe_minproc/                  ← production pipeline
    cloudpipe-long-master-workflow-template.yaml   (name: cloudpipe)
    globus-transfer-workflow-template.yaml         (name: globus-transfer)
    inventory-workflow-template.yaml               (name: inventory)
    fastsurfer-template-phase-workflow-template.yaml  (name: fast-tmpl)
    fastsurfer-long-phase-workflow-template.yaml      (name: fast-long)
    registration-workflow-template.yaml            (name: registration)
    functional-preprocessing-workflow-template.yaml   (name: functional-preprocessing)
    subregion-segmentation-workflow-template.yaml  (name: subregion-seg)   ← optional; standalone
    cloudpipe-semaphores-configmap.yaml
    globus-credentials-external-secret.yaml

  cloudpipe_fullproc/            ← in-development pipeline (raw DICOM input)
    cloudpipe-long-master-workflow-template.yaml   (name: cloudpipe-fullproc)
    globus-transfer-workflow-template.yaml
    inventory-workflow-template.yaml
    fastsurfer-template-phase-workflow-template.yaml
    fastsurfer-long-phase-workflow-template.yaml
    registration-workflow-template.yaml
    functional-preprocessing-workflow-template.yaml
    unpack-and-convert-workflow-template.yaml.yaml
    globus-credentials-external-secret.yaml

  fmri_first_level_proc/         ← first-level GLM analysis
    fmri-first-level-proc-workflow-template.yaml  (name: fmri-first-level-proc)
```

All files are synced from git by the `workflow-templates` ArgoCD Application. `selfHeal: true` means any manual `kubectl apply` to `argo-workflows` namespace is reverted within seconds — always push to git.

---

## Controller configuration

Managed by Terraform in the `argo-workflows-controller-configmap` ConfigMap (not by ArgoCD values.yaml). Key settings:

| Setting | Value | Effect |
|---|---|---|
| `parallelism` | `1000` | Global max concurrently running workflow pods |
| `resourceRateLimit.limit` | `20` | Max pod create calls per second to the K8s API |
| `resourceRateLimit.burst` | `35` | Burst ceiling above the rate limit |
| Artifact repository | S3 bucket from `var.globus_s3_destination_bucket` | All artifacts stored in `abcd-v7` |

The controller and server deployments have `secret.reloader.stakater.com/reload: "argo-db"` annotations — Reloader automatically rolls them when the `argo-db` Secret changes (RDS password rotation).

---

## Shared workflow settings

These settings appear at the top level of the `cloudpipe` master WorkflowTemplate and propagate to all pods it spawns:

| Setting | Value |
|---|---|
| `serviceAccountName` | `argo-workflows-runner` |
| `activeDeadlineSeconds` | `43200` (12 hours max runtime) |
| `ttlStrategy.secondsAfterCompletion` | `86400` (24 hours before deletion) |
| `podGC.strategy` | `OnWorkflowCompletion` (pods deleted when workflow finishes) |
| `volumeClaimGC.strategy` | `OnWorkflowCompletion` (EFS PVC deleted when workflow finishes) |
| `podDisruptionBudget.minAvailable` | `100%` (prevents voluntary disruption of workflow pods) |
| `securityContext` | `runAsUser/Group/fsGroup: 1000` (required for artifact file permissions across containers) |
| `retryStrategy` | Limit 3, retry on spot interruption (`pod deleted`, `imminent node shutdown`) and exit codes 64/137/143, exponential backoff starting 1 min |

All pods get `karpenter.sh/do-not-disrupt: "true"` annotation to block Karpenter from draining nodes with active workflow pods.

---

## Artifact storage

All inter-step data is passed via S3 artifacts, not the Argo artifact repository default. Each template declares its own `inputs.artifacts` (S3 download on start) and `outputs.artifacts` (S3 upload on completion), pointing directly to keys in `abcd-v7`.

This means:
- Steps can be re-run independently (artifacts are already in S3)
- The workflow does not need a shared PVC to pass data between steps that run on different nodes
- Artifacts are persisted across workflow retries (no re-work on retry)

The EFS PVC (50 Gi, `ReadWriteMany`) is used only where multiple containers in the same pod need shared scratch space (FastSurfer steps). It is named after the subject ID (lowercased) and deleted when the workflow completes.

---

## Concurrency controls

| Mechanism | Value | Scope |
|---|---|---|
| `master-pipeline-dag.parallelism` | `3` | Max pods running simultaneously within one workflow |
| `functional-preprocessing-session-level-dag-template.parallelism` | `2` | Max func-preproc pods per session |
| `globus-transfer` semaphore | `8` | Max concurrent Globus transfers cluster-wide (ConfigMap `cloudpipe-semaphores`) |
| Prefect `max_concurrent` | `50` (cloudpipe) / `25` (first-level) | Max active Argo workflows submitted by Prefect |

Semaphores are defined in `cloudpipe-semaphores-configmap.yaml` and referenced by name in the `globus-transfer-template`. Adding a new semaphore requires adding a key to that ConfigMap and a `synchronization.semaphore.configMapKeyRef` block in the relevant template.

---

## Skip / resume logic

The inventory step drives which subsequent steps are skipped. It checks S3 for existing derivatives and sets flags:

| Flag | Check | Skips |
|---|---|---|
| `fastsurfer-exists` | All sessions have `derivatives/fastsurfer/{subj}/{subj}_{ses}_templated.tar.gz` with `size > 1 KB` | Entire anatomical processing phase |
| `t1w_to_mni_exists` (per session) | `derivatives/registration/{subj}/{ses}/t1w_to_mni.tar.gz` with `size > 1 KB` | `t1w-to-mni-step` for that session |
| `b2t_exists` (per run) | `derivatives/registration/{subj}/{ses}/bold_to_t1w_{task}_{run}.tar.gz` with `size > 1 KB` | `bold-to-t1w-step` for that run |
| `func_exists` (per run) | `derivatives/func/{subj}/{ses}/{subj}_{ses}_{task}_{run}_space-MNI152NLin2009cAsym_bold.tar.gz` with `size > 1 KB` | `functional-preprocessing-dagtask` for that run |

The `size > 1 KB` guard prevents treating Argo's zero-byte artifact uploads (written on step failure) as valid completed outputs.

Resubmitting a partially processed subject is safe: inventory runs fresh, finds what is already done, and only the incomplete steps execute.

---

## Pod labels and cost tracking

Every pipeline pod is labelled for Kubecost cost attribution:

| Label | Values | Set by |
|---|---|---|
| `cloudpipe.io/phase` | `transfer`, `inventory`, `anatomical`, `registration`, `functional`, `subregion-segmentation` | Template `metadata.labels` |
| `cloudpipe.io/step` | `start-globus-instance`, `globus-transfer`, `globus-s3-sync`, `subject-data-inventory`, `template-creation`, `t1w-to-mni`, `bold-to-t1w`, `bold-preprocessing`, `thalamus`, `brainstem`, `deeplearning` | Template `metadata.labels` |
| `subjectid` | `{subjID}` | `podMetadata.labels` in master WorkflowTemplate |
| `app` | `cloudpipe` | `podMetadata.labels` in master WorkflowTemplate |

`tools/cloudpipe_minproc_costs.py` filters on `cloudpipe.io/phase` to isolate cloudpipe pods from other workloads sharing the namespace.

---

## WorkflowTemplate reference

### cloudpipe (master — `cloudpipe-long-master-workflow-template.yaml`)

Entry point for the production pipeline. Submit via:
```bash
argo submit --from workflowtemplate/cloudpipe -n argo-workflows -p subjID=NDARINVXXXXXXXX ...
```

Parameters (all read from `cloudpipe-config` ConfigMap by default):

| Parameter | Default | Description |
|---|---|---|
| `subjID` | — | Subject ID (required) |
| `bucket` | from ConfigMap | S3 data bucket |
| `ecr-registry` | from ConfigMap | ECR Public registry prefix |
| `globus-source-collection-id` | — | Source Globus collection UUID |
| `globus-source-base-path` | — | Root path on source collection |
| `globus-dest-collection-id` | — | Destination GCS collection UUID |
| `globus-dest-base-path` | — | Root path on destination collection |
| `globus-scan-types` | — | JSON array of BIDS scan types |
| `globus-use-s3-gateway` | `"true"` | Skip S3 sync step when using GCS S3 gateway |

DAG structure (see architecture.md for the full phase breakdown):
```
start-globus-instance → globus-transfer → [globus-s3-sync (skipped with S3 gateway)]
                                        ↓
                              subject-data-inventory
                                        ↓
                    ┌───────────────────┴────────────────────────┐
          anatomical-processing                    session-level-pipeline (×N sessions)
          (skipped if fs exists)                        ↓
                                         registration + functional-preprocessing
```

### globus-transfer (`globus-transfer-workflow-template.yaml`)

Three templates, called in sequence by the master DAG:

**`start-globus-instance-template`** — Reads instance ID from SSM (`/cloudpipe/globus/instance-id`), starts the EC2 instance if not running, waits for status checks. Idempotent. Node pool: `cpu-light`. Image: `bravepy`.

**`globus-transfer-template`** — Submits the Globus transfer and polls until completion. Semaphore `globus-transfer` limits to 8 concurrent transfers. Credentials come from the `globus-credentials` K8s Secret (synced from Secrets Manager via ExternalSecret every hour). Node pool: `cpu-light`. Image: `globus`.

**`globus-s3-sync-template`** — POSIX staging path only (skipped when `globus-use-s3-gateway == "true"`). SSM `send-command` runs `aws s3 sync` on the GCS instance, then cleans up local staging data. Polls SSM for up to 2 hours. Node pool: `cpu-light`. Image: `bravepy`.

### inventory (`inventory-workflow-template.yaml`)

Single template `subject-data-inventory-template`. Scans S3 to discover sessions and BOLD runs, checks derivative existence for all skip flags, and attaches `nss_frames` from `config/nss_volumes.csv`. Outputs:

- `result` (stdout JSON): array of session objects, each with `session`, `runs`, `t1w_to_mni_exists`, `nss_frames`; runs contains `task`, `run`, `b2t_exists`, `func_exists`
- `fastsurfer-exists` (file parameter): `"True"` or `"False"`

Node pool: `cpu-light`. Image: `bravepy` (pinned SHA).

### fast-tmpl (`fastsurfer-template-phase-workflow-template.yaml`)

Three templates for the longitudinal template phase:

**`fastsurfer-template-creation-template`** — Runs `long_prepare_template.sh`. Downloads T1w inputs from S3 via an init container (`bravepy`), runs the FastSurfer template creation on GPU. Node pool: `gpu-nodepool`. Image: `fastsurfer`.

**`fastsurfer-template-segmentation-template`** — Segmentation (`--seg_only --base`), 4 threads. Node pool: `gpu-nodepool`.

**`fastsurfer-template-parcellation-template`** — Surface reconstruction (`--surf_only --base --3T --fsaparc`), 4 threads. Node pool: `gpu-nodepool`.

### fast-long (`fastsurfer-long-phase-workflow-template.yaml`)

Two templates for the longitudinal session-level phase:

**`fastsurfer-long-segmentation-template`** — All sessions in parallel (`--subjects ses-00A=from-base ses-02A=from-base ...`), `--seg_only --long`. Node pool: `gpu-nodepool`.

**`fastsurfer-long-parcellation-template`** — All sessions, surface reconstruction, `--long --parallel N` where N = number of sessions, 3 threads each. Waits for both long segmentation and template parcellation to complete. Node pool: `gpu-nodepool`.

FastSurfer outputs are uploaded to `derivatives/fastsurfer/{subj}/` as per-session `_templated.tar.gz` archives. The EFS PVC is used as working space during these steps.

### registration (`registration-workflow-template.yaml`)

Entry point `registration-dag-template`, called once per session:

**`t1w-to-mni-template`** — FireANTs affine + SyN registration of FreeSurfer conformed `orig.mgz` to MNI152NLin2009cAsym. Skipped when `t1w-to-mni-exists == "true"`. Downloads FastSurfer tarball and MNI template from S3 as artifacts. GPU-accelerated (nvidia-smi monitor in background). Output: `t1w_to_mni.tar.gz` → `derivatives/registration/{subj}/{ses}/`. Node pool: `gpu-nodepool`. Image: `fireants`.

**`bold-to-t1w-template`** — SynthMorph contrast-agnostic deep learning affine registration of BOLD reference to T1w. One pod per run, fanned out via `withParam`. Skipped when `b2t_exists == "true"`. Downloads BOLD NIfTI and FastSurfer tarball from S3. Uses EFS PVC for output staging. Output: `bold_to_t1w_{task}_{run}.tar.gz` → `derivatives/registration/{subj}/{ses}/`. Node pool: `cpu-heavy-nodepool`. Image: `synthmorph`.

The T1w→MNI step uses `orig.mgz` (FreeSurfer conformed space) rather than the BIDS T1w to ensure the source space matches the BOLD→T1w transform, which bbregister produces in conformed space.

### functional-preprocessing (`functional-preprocessing-workflow-template.yaml`)

Can be submitted standalone (for testing/reprocessing) or called via `templateRef` from the master DAG.

**Standalone submission:**
```bash
argo submit --from workflowtemplate/functional-preprocessing \
  -n argo-workflows \
  -p subjID=NDARINVXXXXXXXX \
  -p session=ses-00A \
  -p task=task-rest \
  -p run=run-01 \
  -p nss-frames=15
```

**`functional-preprocessing-template`** — One pod per `(session, task, run)`. Downloads from S3: BOLD NIfTI + BIDS sidecar, motion params, brain mask and ITK affine (from bold-to-t1w), ANTs transforms (from t1w-to-mni), FreeSurfer aseg (for aCompCor), MNI template. Runs `preproc.py` (AFNI). Skipped when `func_exists == "true"`.

Resources: 16 GB RAM, 6 CPU, 10 GB ephemeral storage requested. Node pool: `cpu-heavy-nodepool`. Image: `afni`.

Output: `{subj}_{ses}_{task}_{run}_space-MNI152NLin2009cAsym_bold.tar.gz` → `derivatives/func/{subj}/{ses}/`.

Retry limit: 6 (higher than other templates due to compute cost of re-running preprocessing).

`preproc.py` is embedded in the `preproc-script` ConfigMap and mounted at runtime, so it can be updated without rebuilding the AFNI image. Update path: edit `images/afni/preproc.py` → run `tools/gen-preproc-configmap.sh` → commit both files → push (ArgoCD syncs the ConfigMap).

### subregion-seg (`subregion-segmentation-workflow-template.yaml`)

Optional standalone WorkflowTemplate for subcortical subregion segmentation. Not called by the master pipeline DAG — submitted independently after FastSurfer longitudinal outputs exist.

**Standalone submission:**
```bash
argo submit --from workflowtemplate/subregion-seg \
  -n argo-workflows \
  -p subjID=NDARINVXXXXXXXX \
  -p bucket=abcd-v7 \
  -p ecr-registry=public.ecr.aws/l9e7l1h1 \
  -p T1w_sessions='["ses-00A","ses-02A"]'
```

`T1w_sessions` is a JSON array of the session labels that have FastSurfer longitudinal outputs in S3. The template reads `base-tps` from the long-template tarball to discover actual timepoints at runtime; the parameter is used to declare the set of S3 artifact inputs.

**S3 inputs** (declared as template artifacts, downloaded before each pod starts):
- `derivatives/fastsurfer/{subjID}/{subjID}_long-template.tar.gz`
- `derivatives/fastsurfer/{subjID}/{subjID}_{session}_templated.tar.gz` — ses-00A is required; ses-02A through ses-10A are `optional: true`

**Three templates run in parallel** (`failFast: false`):

**`segment-thalamus-template`** — Runs `segment_subregions thalamus --long-base` (GEMS/Bayesian atlas). ~30–45 min with 4 threads. Symlinks FastSurfer bare session IDs (`ses-00A`) to the `{tp}.long.{base}` naming convention that `segment_subregions --long-base` expects. Output: `ThalamicNuclei.v13.T1.mgz` per session + template → `derivatives/subregions/{subjID}/{subjID}_thalamus.tar.gz`. Resources: 12G/4CPU. Node pool: `cpu-heavy-nodepool`. Image: `freesurfer`.

**`segment-brainstem-template`** — Runs `segment_subregions brainstem --long-base` (GEMS/Bayesian atlas). ~15–25 min with 4 threads. Same symlink setup as thalamus. Output: `brainstemSsLabels.v13.T1.mgz` per session + template → `derivatives/subregions/{subjID}/{subjID}_brainstem.tar.gz`. Resources: 12G/4CPU. Node pool: `cpu-heavy-nodepool`. Image: `freesurfer`.

**`segment-deeplearning-template`** — Runs two fast deep-learning tools sequentially:
1. `mri_segment_hypothalamic_subunits` — TensorFlow CNN, ~10 sec/session, 5 bilateral hypothalamic subregions. Output: `hypothalamic_subunits_seg.v1.mgz` + `hypothalamic_subunits_volumes.v1.csv` per session → `derivatives/subregions/{subjID}/{subjID}_hypothalamic.tar.gz`
2. `mri_sclimbic_seg` — U-Net, <1 min/session, hypothalamus (coarse), mammillary bodies, basal forebrain, septal nuclei, NAcc, fornix. Output: `sclimbic.mgz` + `sclimbic.stats` per session → `derivatives/subregions/{subjID}/{subjID}_sclimbic.tar.gz`

Both tools read model files from `$FREESURFER_HOME/models/` (included in the `freesurfer` image). Resources: 8G/4CPU. Node pool: `cpu-heavy-nodepool`. Image: `freesurfer`.

**Implementation note**: `segment_subregions --long-base` expects timepoints named `{tp}.long.{base}`. FastSurfer writes bare session IDs (`ses-00A`). Each template creates symlinks in `$SUBJECTS_DIR` to bridge this gap (`ln -sfn ses-00A ses-00A.long.{base}`); `segment_subregions` writes outputs into the real directories through the symlinks.

### fmri-first-level-proc (`fmri-first-level-proc-workflow-template.yaml`)

Separate pipeline for first-level GLM analysis. Submitted by `first-level-queue-manager` Prefect flow.

- `activeDeadlineSeconds: 7200` (2 hour cap)
- Scratch volume: 300 Gi emptyDir (no EFS PVC)
- Retry: 3, on spot interruption only
- Input: `subjID` parameter; reads its own config
- Output: `derivatives/first_levels/{subj}/` in `<YOUR_INPUT_S3_BUCKET>` bucket

---

## Globus credentials secret

The `globus-credentials` ExternalSecret (in `argo/workflows/cloudpipe_minproc/`) syncs two fields from the Secrets Manager secret `globus/refresh-token`:

| K8s key | Secrets Manager property |
|---|---|
| `native-app-client-id` | `native-app-client-id` |
| `refresh-token` | `refresh-token` |

These are injected as environment variables into the `globus-transfer-template` container. The ExternalSecret refreshes every hour, so token rotation takes effect within 1 hour without redeploying.

---

## Authentication

The Argo server runs with `--auth-mode=sso --auth-mode=client`:

- **SSO**: UW-Madison NetID via Dex (ArgoCD's Dex instance acts as broker). The `admin_netid` Terraform variable is mapped to the `argo-admin` service account, which has `argo-workflows-admin` ClusterRole.
- **Client token**: CLI access using a service account token (`kubectl get secret argo-admin.service-account-token -n argo-workflows`). Used by scripts and the `argo` CLI with `--token`.

TLS is terminated at the ALB; the Argo server runs `--secure=false` internally.

---

## Adding a new WorkflowTemplate

1. Create the YAML in `argo/workflows/cloudpipe_minproc/` (or the appropriate pipeline directory).
2. Give it a unique `metadata.name` in the `argo-workflows` namespace.
3. Add `cloudpipe.io/phase` and `cloudpipe.io/step` labels to each template's `metadata.labels` for cost tracking.
4. Set `nodeSelector: karpenter.sh/nodepool: <pool>` on each template that runs pipeline workloads.
5. Commit and push — ArgoCD syncs within ~30 seconds.

To reference from another WorkflowTemplate:
```yaml
templateRef:
  name: <workflow-template-name>
  template: <template-name>
```
