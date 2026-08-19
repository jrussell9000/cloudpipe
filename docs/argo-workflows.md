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
    surface-resample-workflow-template.yaml        (name: surface-resample)
    fsqc-metrics-workflow-template.yaml            (name: fsqc-metrics)
    metrics-workflow-template.yaml                 (name: metrics-exit-handler)
    outcome-recorder-workflow-template.yaml        (name: outcome-recorder)
    cloudpipe-semaphores-configmap.yaml
    globus-credentials-external-secret.yaml

  fmri_first_level_proc/         ← first-level GLM analysis
    fmri-first-level-proc-workflow-template.yaml  (name: fmri-first-level-proc)
```

That is **13** WorkflowTemplates in `cloudpipe_minproc/` plus one in `fmri_first_level_proc/`. The last three above are infrastructure rather than processing phases: `metrics-exit-handler` runs as the master workflow's exit handler and writes the `WorkflowRun` summary, and `outcome-recorder` writes per-step `StepOutcome` records.

**`cloudpipe_fullproc/` does not exist yet.** It's a planned pipeline (raw DICOM input) — see [architecture.md](architecture.md#cloudpipe_fullproc-planned--design-only-not-implemented) for the design. The `workflow-templates` ArgoCD Application carries a placeholder `exclude: 'cloudpipe_fullproc/**'` so a future directory there won't auto-sync mid-development.

All files are synced from git by the `workflow-templates` ArgoCD Application. `selfHeal: true` means any manual `kubectl apply` to `argo-workflows` namespace is reverted within seconds — always push to git.

---

## Controller configuration

`parallelism`, `namespaceParallelism` and `resourceRateLimit` are set by **Terraform**, in the `argo-workflows-controller-configmap` ConfigMap (`terraform/modules/argo-workflows/main.tf`) — alongside the persistence/DB and `sso` config. They are deliberately *not* in the Helm values; see the note under [Concurrency controls](#concurrency-controls) for why setting them there does nothing. Key settings:

| Setting | Value | Effect |
|---|---|---|
| `parallelism` | `1000` | Global max concurrently running **workflows** (not pods — pod concurrency is unbounded, controlled only by `resourceRateLimit`) |
| `namespaceParallelism` | `400` | Max active workflows in `argo-workflows`; the server-side backstop for the Prefect queue gate (#206). Raised from `100` for the 300-concurrent run — at `100` it was itself the binding cap |
| `resourceRateLimit.limit` | `50` | Max pod create calls per second to the K8s API |
| `resourceRateLimit.burst` | `90` | Burst ceiling above the rate limit |
| Artifact repository | S3 bucket from `var.globus_s3_destination_bucket` | All artifacts stored in `<YOUR_S3_BUCKET>` |
| `persistence.postgresql.host` | `pgbouncer` | DB connections go through PgBouncer, not directly to RDS |

The controller and server deployments carry two Reloader annotations:
- `secret.reloader.stakater.com/reload: "argo-db"` — rolls pods on RDS password rotation
- `configmap.reloader.stakater.com/reload: "argo-workflows-controller-configmap"` — rolls pods when Terraform updates the persistence or SSO config

---

## PgBouncer connection pooler

PgBouncer runs as a Deployment in the `argo-workflows` namespace and sits between Argo components and the RDS instance. Argo's persistence config points to `pgbouncer:5432`; PgBouncer forwards to the RDS endpoint it reads from the `argo-db` Secret at startup.

| Setting | Value | Reason |
|---|---|---|
| Pool mode | `session` | Argo uses pgx with prepared statement caching; transaction mode drops server-side statements between transactions and causes errors |
| `default_pool_size` | `20` | Caps real connections to RDS well below the `db.t4g.micro` max (~112) |
| `max_client_conn` | `200` | Allows Argo goroutines to queue during bulk operations instead of failing immediately |
| `server_tls_sslmode` | `require` | Enforces SSL on the PgBouncer → RDS leg |

PgBouncer is defined in `gitops/apps/argo-workflows/templates/pgbouncer.yaml` (managed by ArgoCD). Credentials are pulled from the `argo-db` Secret (`host`, `username`, `password` keys) — the same secret used by the Argo controller. The Deployment has `secret.reloader.stakater.com/reload: "argo-db"` so it restarts on password rotation.

---

## Shared workflow settings

These settings appear at the top level of the `cloudpipe` master WorkflowTemplate and propagate to all pods it spawns:

| Setting | Value |
|---|---|
| `serviceAccountName` | `argo-workflows-runner` |
| `activeDeadlineSeconds` | `43200` (12 hours max runtime) |
| `ttlStrategy.secondsAfterCompletion` | `86400` (24 hours before deletion) |
| `podGC.strategy` | `OnWorkflowCompletion` (pods deleted when workflow finishes) |
| `podDisruptionBudget.minAvailable` | `100%` (prevents voluntary disruption of workflow pods) |
| `securityContext` | `runAsUser/Group/fsGroup: 1000` (required for artifact file permissions across containers) |
| `retryStrategy` | Limit 8, retry on spot interruption (`pod deleted`, `imminent node shutdown`) and exit codes 64/75/143, exponential backoff from 1 min capped at 5 min. The codes are matched in the node **message** as well as in `exitCode`, because an init- or wait-container death never populates `exitCode` ([#277](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/277)). Exit 137 is **not** retried — the budget is for infrastructure churn, not workload failures (see [operations.md](operations.md#retries)) |

All pods get `karpenter.sh/do-not-disrupt: "true"` annotation to block Karpenter from draining nodes with active workflow pods.

---

## Artifact storage

All inter-step data is passed via S3 artifacts, not the Argo artifact repository default. Each template declares its own `inputs.artifacts` (S3 download on start) and `outputs.artifacts` (S3 upload on completion), pointing directly to keys in `<YOUR_S3_BUCKET>`.

This means:
- Steps can be re-run independently (artifacts are already in S3)
- The workflow does not need a shared PVC to pass data between steps that run on different nodes
- Artifacts are persisted across workflow retries (no re-work on retry)

There is no longer a workflow-scoped EFS PVC anywhere in this pipeline. `subregion-seg` was the last consumer (its two segmentation pods now stage FastSurfer outputs onto their own private `emptyDir`s and checkpoint per-region progress to S3 instead — see [pipelines.md](pipelines.md#subregion-seg), tracked in [#77](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/77)). The EFS filesystem, StorageClass, and CSI driver have since been removed from the cluster entirely.

---

## Concurrency controls

| Mechanism | Value | Scope |
|---|---|---|
| `master-pipeline-dag.parallelism` | `3` | Max pods running simultaneously within one workflow. This is the only `parallelism` setting in the whole template set — sessions fan out simultaneously but are throttled by it. |
| `globus-transfer` semaphore | `8` | Max concurrent Globus transfers cluster-wide (ConfigMap `cloudpipe-semaphores`) |
| Prefect Variable `cloudpipe-max-concurrent` / `first-level-max-concurrent` | `50` (cloudpipe) / `25` (first-level), when the Variable is unset | Max active Argo workflows submitted by Prefect; set live with `prefect variable set <name> <N>`, not a deployment-run parameter |
| `namespaceParallelism` | `400` | Max active workflows in `argo-workflows`, **all pipelines combined**; enforced by the controller, excess workflows held `Pending` |
| `parallelism` | `1000` | Max active workflows cluster-wide — a second, looser ceiling above `namespaceParallelism` |
| `resourceRateLimit` | `50/s`, burst `90` | Rate at which the controller creates pods, cluster-wide |

These three live in `terraform/modules/argo-workflows/main.tf`, **not** in the Helm values. The chart renders them only via `templates/controller/workflow-controller-config-map.yaml`, and `controller.configMap.create` is `false` because Terraform owns that ConfigMap — so setting them under `controller:` in `values.yaml` is silently inert. That is exactly how this repo shipped a documented pod-creation rate limit that was never in effect, and no namespace cap at all, until [#206](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/206).

Semaphores are defined in `cloudpipe-semaphores-configmap.yaml` and referenced by name in the `globus-transfer-template`. Adding a new semaphore requires adding a key to that ConfigMap and a `synchronization.semaphore.configMapKeyRef` block in the relevant template.

`namespaceParallelism` and `resourceRateLimit` live in the **Terraform-owned** controller ConfigMap (`terraform/modules/argo-workflows/main.tf`), not in `gitops/apps/argo-workflows/values.yaml`. The Argo Helm chart accepts these keys under `controller:`, but its only consumer of them is the controller ConfigMap template — and this deployment sets `controller.configMap.create: false` so Terraform can own that ConfigMap's dynamic DB credentials. Anything set for them in `values.yaml` therefore renders nowhere and is **silently inert**; that is exactly how the repo shipped a documented pod-creation rate limit that was never in effect (#206). `tests/argo/test_controller_config.py` guards both directions.

The two layers exist for different reasons. The Prefect Variables are the hot-reconfigurable working caps but are *advisory* — a client polling the API cannot see workflows the controller has not labeled yet, which is what let 100 workflows out under a cap of 50. `namespaceParallelism` is enforced by the component that owns the state, so it cannot be raced. See [ADR 008](decisions/008-prefect-as-queue-manager.md).

---

## Skip / resume logic

The inventory step (`src/inventory.py`) drives which subsequent steps are skipped. It `head_object`s S3 for each step's **completion marker** and sets flags:

| Flag | Completion marker checked | Skips |
|---|---|---|
| `fastsurfer_exists` | Every session has `derivatives/fastsurfer/{subj}/{ses}/_complete.json` | Entire anatomical processing phase |
| `subregions_exists` | All four subregion output tarballs exist | `subregion-segmentation-dagtask` |
| `t1w_to_mni_exists` (per session) | `…_desc-t1w2mni_affine.mat` — terminal output of `fst1w_to_mni.py` | `t1w-to-mni-step` for that session |
| `b2t_exists` (per run) | `derivatives/registration/{subj}/{ses}/bold_to_t1w_{task}_{run}/{prefix}_desc-bold2t1w_itk.txt` | `bold-to-t1w-step` for that run |
| `func_exists` (per run) | `derivatives/func/{subj}/{ses}/{prefix}_space-MNI152NLin2009cAsym_bold.tar.gz` | `functional-preprocessing-dagtask` for that run |
| `components_exist` (per run) | `derivatives/func_surf/{subj}/{ses}/components/{prefix}_desc-grayordcomponents_bold.tar.gz` | — (reclaimed intermediate; see below) |
| `surf_target_exists` (per run) | `derivatives/func_surf/{subj}/{ses}/fsLR32k/{prefix}_space-fsLR32k_bold.dtseries.nii` | `surface-resample` for that run |
| `surf_exists` (per run) | *derived*: `components_exist OR surf_target_exists` | Grayordinate extraction for that run |

**Markers, not size thresholds.** Each key above is the file its step writes *last*, so its presence proves the step ran to completion — there is no `size > 1 KB` guard anywhere in `inventory.py`. This replaced a size heuristic that existed to reject Argo's zero-byte failure artifacts; a terminal-file check needs no threshold and cannot be fooled by a large-but-truncated output.

`func_exists` and `surf_exists` gate **independently** even though one pod produces both derivatives, because either can be missing on its own — `preproc.py` takes a short path when only the grayordinate output is wanted. `surf_target_exists` gates the separate `surface-resample` step, which consumes the grayordinate components and cannot run before `surf_exists` is true.

**`surf_exists` is the one derived marker.** It means "grayordinate extraction finished for this run", not "its components tarball is still in S3" — the two stopped being the same thing when `surface-resample` began deleting each run's components after verifying the assembled dtseries (see [pipelines.md](pipelines.md), *Component reclaim*). The dtseries is strictly downstream of the components and 1:1 with them per run, so its presence is stronger evidence that extraction succeeded than the components' own. `components_exist` is kept separately so callers can still tell "never extracted" from "extracted and since reclaimed".

Resubmitting a partially processed subject is safe: inventory runs fresh, finds what is already done, and only the incomplete steps execute.

---

## Pod labels and cost tracking

Every pipeline pod is labelled for Kubecost cost attribution:

| Label | Values | Set by |
|---|---|---|
| `cloudpipe.io/phase` | `transfer`, `inventory`, `anatomical`, `registration`, `functional`, `subregion-segmentation`, `observability`, `cleanup`, `first-level` | Template `metadata.labels` |
| `cloudpipe.io/step` | `start-globus-instance`, `globus-transfer`, `globus-s3-sync`, `delete-globus-input`, `subject-data-inventory`, `published-sessions`, `template-build`, `template-parcellation`, `long-segmentation`, `long-parcellation`, `fsqc-metrics`, `t1w-to-mni`, `bold-to-t1w`, `bold-preprocessing`, `surface-resample`, `segment-gems`, `segment-dl`, `orchestrate`, `workflow-start-marker`, `workflow-run-metrics`, `record-step-outcome` | Template `metadata.labels` |
| `subjectid` | `{subjID}` | `podMetadata.labels` in master WorkflowTemplate |
| `app` | `cloudpipe` | `podMetadata.labels` in master WorkflowTemplate |

`scripts/cloudpipe_minproc_costs.py` filters on `cloudpipe.io/phase` to isolate cloudpipe pods from other workloads sharing the namespace.

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
| `bucket` | from ConfigMap | S3 data bucket (inputs and derivatives) |
| `metrics-bucket` | from ConfigMap | Separate versioned QC-metrics bucket (`cloudpipe-metrics`). Distinct from `bucket` so metrics survive a derivative flush — see [observability.md](observability.md). |
| `ecr-registry` | from ConfigMap | Container registry prefix. Now the **private** ECR registry (`{account-id}.dkr.ecr.<YOUR_AWS_REGION>.amazonaws.com`) — set from `local.ecr_registry` in `terraform/argowf.tf`. ECR Public remains a dual-push secondary kept for rollback for most images; it is no longer what workflows pull from, and its repos are being retired image by image (`fmri-first-level-proc` already is). |
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
                              anatomical-processing
                              (skipped if fs exists)
                                        ↓
                              published-sessions ─────────► record-outcome-rejected-sessions
                                        ↓                    (only if a session was rejected)
                    session-level-pipeline (×N published sessions)
                                        ↓
                    registration + functional-preprocessing
```

`published-sessions` is what the per-session fan-out iterates, not the inventory result: a session whose FastSurfer derivatives were never published must not start a branch that will die staging them (#270). See [pipelines.md](pipelines.md#the-fan-out-gate-published-sessions-template).

### globus-transfer (`globus-transfer-workflow-template.yaml`)

Three templates, called in sequence by the master DAG:

**`start-globus-instance-template`** — Reads instance ID from SSM (`/cloudpipe/globus/instance-id`), starts the EC2 instance if not running, waits for status checks. Idempotent. Node pool: `cpu-light`. Image: `python`.

**`globus-transfer-template`** — Submits the Globus transfer and polls until completion. Semaphore `globus-transfer` limits to 8 concurrent transfers. Credentials come from the `globus-credentials` K8s Secret (synced from Secrets Manager via ExternalSecret every hour). Node pool: `cpu-light`. Image: `globus`.

**`globus-s3-sync-template`** — POSIX staging path only (skipped when `globus-use-s3-gateway == "true"`). SSM `send-command` runs `aws s3 sync` on the GCS instance, then cleans up local staging data. Polls SSM for up to 2 hours. Node pool: `cpu-light`. Image: `python`.

### inventory (`inventory-workflow-template.yaml`)

Two templates, both `src/inventory.py` under a different `--mode`.

**`subject-data-inventory-template`** (`--mode inventory`, the default) — scans S3 to discover sessions and BOLD runs, checks derivative existence for all skip flags, and attaches `nss_frames` from `config/nss_volumes.csv`. Outputs:

- `result` (stdout JSON): array of session objects, each with `session`, `runs`, `t1w_to_mni_exists`, `t1w_available`, `nss_frames`; runs contains `task`, `run`, `b2t_exists`, `func_exists`
- `fastsurfer-exists` (file parameter): `"True"` or `"False"`
- `subregions-exist` (file parameter): `"True"` or `"False"`

**`published-sessions-template`** (`--mode published-sessions`) — runs after the anatomical gate and takes the array above as an input parameter. Re-reads each `t1w_available` session's `_complete.json` and outputs `session-items` (the fan-out list), `rejected-sessions`, and `rejected-count`. This exists because inventory describes the subject's *inputs* while the per-session fan-out needs the anatomical phase's *outputs* — see [pipelines.md](pipelines.md#the-fan-out-gate-published-sessions-template).

Node pool: `cpu-light` for both. Image: `python` (pinned SHA).

### fast-tmpl (`fastsurfer-template-phase-workflow-template.yaml`)

Two templates for the longitudinal template phase:

**`fastsurfer-template-build-template`** — Runs `long_prepare_template.sh` and then, only if that succeeded, `run_fastsurfer.sh --seg_only --base --threads 1`. Downloads T1w inputs and `fsaverage` from S3 via init containers (`cloudpipe/python`). Creation and segmentation share this pod because both are GPU-bound and strictly sequential. Node pool: `gpu-nodepool`. Image: `fastsurfer`.

**`fastsurfer-template-parcellation-template`** — Surface reconstruction (`--surf_only --base --3T --fsaparc`). Node pool: `cpu-heavy-nodepool`, 3G/4CPU. `--threads` is **derived from the cpu request** via the downward API (`resourceFieldRef` on `requests.cpu`) rather than written into the master template, so the resources block is the single source of truth and the two cannot drift. Cut 6→4 threads from measured `cpu_efficiency` 0.577; do not cut below 2 — `recon-surf.sh` runs the hemispheres serially at `threads == 1`, which roughly *doubles* the surface stage.

### fast-long (`fastsurfer-long-phase-workflow-template.yaml`)

Two templates for the longitudinal session-level phase:

**`fastsurfer-long-segmentation-template`** — All sessions in parallel (`--subjects ses-00A=from-base ses-02A=from-base ...`), `--seg_only --long`. Node pool: `gpu-nodepool`.

**`fastsurfer-long-parcellation-template`** — All sessions, surface reconstruction, `--long --parallel N` where N = number of sessions. Waits for both long segmentation and template parcellation to complete. Node pool: `cpu-heavy-nodepool`.

**No shared volume.** Each step works in a private `emptyDir` at `/work` with `SUBJECTS_DIR=/work/subjects`, passing state through `scratch/{workflow.name}/anat/` in S3 (reaped by the `scratch-expiration` lifecycle rule). Final FastSurfer outputs are published in-pod to `derivatives/fastsurfer/{subj}/{ses}/` as one object per file, plus `_links.json` and a `_complete.json` written last ([ADR 017](decisions/017-exploded-derivatives-over-tarballs.md)) — those prefixes are the contract with every downstream phase.

### registration (`registration-workflow-template.yaml`)

Entry point `registration-dag-template`, called once per session:

**`t1w-to-mni-template`** — FireANTs affine + SyN registration of FreeSurfer conformed `orig.mgz` to MNI152NLin2009cAsym. Skipped when `t1w-to-mni-exists == "true"`. Downloads FastSurfer tarball and MNI template from S3 as artifacts. GPU-accelerated (nvidia-smi monitor in background). Output: `t1w_to_mni.tar.gz` → `derivatives/registration/{subj}/{ses}/`. Node pool: `gpu-nodepool`. Image: `fireants`.

**`bold-to-t1w-session-template`** — SynthMorph contrast-agnostic deep learning affine registration of BOLD reference to T1w. **One pod per session**, looping over that session's runs internally (previously one pod per run via `withParam`). The task is skipped when `b2t_exists == "true"` for every run; individual complete runs are skipped inside the pod. Downloads the session's whole `func/` prefix and the FastSurfer tarball from S3 — the tarball once per session rather than once per run, which is the point of the change. No EFS PVC: outputs stage in `/tmp` and upload as one directory artifact to `derivatives/registration/{subj}/{ses}/`, preserving the per-run `bold_to_t1w_{task}_{run}/` keys. Node pool: `cpu-heavy-nodepool`. Image: `freesurfer` (which carries `bold_to_t1w.py` — there is no separate `synthmorph` image).

The T1w→MNI step uses `orig.mgz` (FreeSurfer conformed space) rather than the BIDS T1w to ensure the source space matches the BOLD→T1w transform, which **SynthMorph** produces in conformed space (its fixed image is `brainmask.mgz`). bbregister was removed in `61ccff7` — see [ADR 002](decisions/002-synthmorph-over-bbregister.md) and [ADR 003](decisions/003-orig-mgz-for-t1w-registration.md).

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

**`functional-preprocessing-session-template`** — **One pod per session**, looping over the session's `(task, run)` pairs (previously one pod per `(session, task, run)`). Downloads from S3 as whole-prefix directory artifacts: the session's `func/` (BOLD + BIDS sidecars + motion params) and `registration/` (bold-to-t1w brain masks and ITK affines, plus the shared t1w-to-mni transforms), along with the FastSurfer tarball (for aCompCor's aseg) and the MNI template — the last two once per session rather than once per run. Runs `preproc.py` (AFNI) per run and tars each run's output itself. The task is skipped when `func_exists == "true"` for every run; individual complete runs are skipped inside the pod.

Resources: 4 GB RAM (6 GB limit), 3 CPU, 20 GB ephemeral storage (30 GB limit) requested. Node pool: `cpu-heavy-nodepool`. Image: `afni`.

Output: `{subj}_{ses}_{task}_{run}_space-MNI152NLin2009cAsym_bold.tar.gz` → `derivatives/func/{subj}/{ses}/`.

Retry limit: 8 — the same as every other template. (An earlier revision set this to 6 and justified it as "higher than other templates"; the fleet-wide limit is now 8 and this template is not special.)

`preproc.py` is baked into the `afni` image at build time; updating it requires a normal image rebuild (edit `images/afni/preproc.py` → commit → push → CI rebuilds and updates the SHA-pinned reference).

### subregion-seg (`subregion-segmentation-workflow-template.yaml`)

Subcortical subregion segmentation. Runs as a phase inside the master pipeline DAG (`subregion-segmentation-dagtask`, gated on `subregions-exist == "False"`), and is also submittable standalone once FastSurfer longitudinal outputs exist. Full walkthrough in [pipelines.md](pipelines.md#subregion-seg).

**Standalone submission:**
```bash
argo submit --from workflowtemplate/subregion-seg \
  -n argo-workflows \
  -p subjID=NDARINVXXXXXXXX \
  -p bucket=<YOUR_S3_BUCKET> \
  -p ecr-registry=<account-id>.dkr.ecr.<YOUR_AWS_REGION>.amazonaws.com \
  -p T1w_sessions='["ses-00A","ses-02A"]'
```

`T1w_sessions` is a JSON array of the session labels that have FastSurfer longitudinal outputs in S3. The template reads `base-tps` from the long-template tree to discover actual timepoints at runtime; the parameter is used to declare the set of S3 artifact inputs.

**S3 inputs** (declared as template artifacts with `archive: none`, downloaded per-object before each pod starts):
- `derivatives/fastsurfer/{subjID}/long-template`
- `derivatives/fastsurfer/{subjID}/{session}` — ses-00A is required; ses-02A through ses-10A are `optional: true`

Prefix keys on an **input** artifact must not carry a trailing `/` (with one, Argo silently downloads nothing); an output key must keep it.

Each pod then runs `/app/restore_links.py` over every staged tree **before** reading anything, because Argo downloads objects but cannot recreate symlinks. That ordering is load-bearing rather than tidy: `base-tps` is itself one of FastSurfer's aliases (`base-tps -> base-tps.fastsurfer`), and both pods read it within a couple of lines of starting, so a later replay would abort the pod under `set -eu` before any segmentation ran.

**Two templates: `gems` ∥ `dl`**, running concurrently under `failFast: false`, both on `cpu-heavy-nodepool`, both using the `freesurfer` image. No shared PVC and no hydrate step (GitHub #77) — each pod independently declares the FastSurfer S3 prefixes as input artifacts, staged onto its own private `emptyDir`.

**`segment-subregions-gems-template`** — `segment_subregions {thalamus,brainstem,hippo-amygdala} --long-base`, GEMS/Bayesian, CPU-only, ~50 min at 4 threads. Symlinks FastSurfer bare session IDs (`ses-00A`) to the `{tp}.long.{base}` naming `--long-base` expects; `segment_subregions` writes through the symlinks into the real directories. Before each region runs, the script checks S3 for that region's final prefix (`derivatives/subregions/{subjID}/{region}/`) and stages it instead of recomputing if its `_complete.json` is present; after a region completes it publishes to that same prefix immediately, so a pod retry or workflow resubmit resumes per-region rather than redoing completed work. Outputs → `derivatives/subregions/{subjID}/{thalamus,brainstem,hippoamyg}/`. **4.5G/4CPU** (provisional — see pipelines.md).

**`segment-subregions-dl-template`** — two TensorFlow tools, ~30 s total, reading model files from `$FREESURFER_HOME/models/`, with the same per-region S3 checkpoint/restore as the GEMS pod:
1. `mri_segment_hypothalamic_subunits` — CNN, ~10 sec/session, 5 bilateral hypothalamic subregions → `derivatives/subregions/{subjID}/hypothalamic/`
2. `mri_sclimbic_seg` — U-Net, <1 min/session, hypothalamus (coarse), mammillary bodies, basal forebrain, septal nuclei, NAcc, fornix → `derivatives/subregions/{subjID}/sclimbic/`

**13G/4CPU** with `TF_ENABLE_ONEDNN_OPTS=0` — a measured 11.87 GB peak lasting ~30 s, which is why it is its own pod ([#129](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/129), [#134](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/134)). Needs no symlinks and no GEMS output: both tools read only `mri/nu.mgz` (plus an optional `talairach.xfm.lta`), which this pod has already staged for itself.

### fmri-first-level-proc (`fmri-first-level-proc-workflow-template.yaml`)

Separate pipeline for first-level GLM analysis. Submitted by `first-level-queue-manager` Prefect flow.

- `activeDeadlineSeconds: 7200` (2 hour cap)
- Scratch volume: 300 Gi emptyDir (no EFS PVC)
- Retry: limit 8 with `retryPolicy: Always`, filtered to infrastructure causes by expression (spot reclaim, exit codes 64/75/143 in either `exitCode` or the node message — see [#277](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/277)). `Always` is deliberate: a reclaimed pod lands in phase **Error**, not Failed, so the earlier `OnFailure` policy could never honour the spot clause it was paired with. The 2 h deadline is the real ceiling — retries cannot extend it, so the limit is an upper bound the cap may cut short ([#115](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/115)).
- Input: `subjID` parameter; reads its own config
- Output: `derivatives/first_levels/{subj}/` in `<YOUR_S3_BUCKET>` bucket

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

### CLI access to the Argo Server (archive commands, etc.)

Commands that hit the Kubernetes API directly (`argo submit`, `argo delete`, `argo list`, ...) work via kubeconfig with no extra setup. Commands that must go through the Argo Server itself (`argo archive list`, `argo archive delete`, ...) need explicit auth — SSO tokens copied from the browser session are not reliable for this. Use the client service-account token instead:

```bash
export ARGO_SERVER=argo.<YOUR_DOMAIN>:443
export ARGO_HTTP1=true   # ALB in front of the server doesn't support gRPC (HTTP/2)
export ARGO_TOKEN="Bearer $(kubectl get secret -n argo-workflows argo-admin.service-account-token -o=jsonpath='{.data.token}' | base64 --decode)"
```

Example — bulk-delete succeeded/failed workflows from the archive (the archive is a separate Postgres-backed store; `argo delete` alone only removes live Workflow CRs and does not clear entries shown in the UI's workflow list):

```bash
argo -n argo-workflows archive list -o json | \
  jq -r '.[] | select(.status.phase=="Succeeded" or .status.phase=="Failed") | .metadata.uid' | \
  xargs -r -n1 argo -n argo-workflows archive delete
```

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
