# Pipelines

This document walks through each pipeline in execution order: what triggers it, what each step does, what it reads and writes, and what can go wrong.

Three independent pipelines share the same cluster and some infrastructure:

| Pipeline | WorkflowTemplate | Submitter | Input | Output bucket |
|---|---|---|---|---|
| `cloudpipe_minproc` | `cloudpipe` | `cloudpipe-queue-manager` Prefect flow | ABCD minimally preprocessed | `abcd-v7` |
| `cloudpipe_fullproc` | `cloudpipe-fullproc` | Manual / in development | Raw ABCD DICOMs | `abcd-v7` |
| `subregion-seg` | `subregion-seg` | Manual / standalone | FastSurfer longitudinal outputs | `abcd-v7` |
| `fmri-first-level-proc` | `fmri-first-level-proc` | `first-level-queue-manager` Prefect flow | cloudpipe_minproc outputs | `<YOUR_INPUT_S3_BUCKET>` |

---

## cloudpipe_minproc

Production pipeline. Ingests ABCD minimally preprocessed data (Hagler et al. 2019) and produces MNI-space BOLD with confounds.

### How it starts

The `cloudpipe-queue-manager` Prefect flow reads a CSV of subject IDs and submits each one via the Argo API:

```bash
prefect deployment run cloudpipe-queue-manager/cloudpipe-queue-manager \
  -p subjects_file=s3://abcd-v7/config/subjects.csv \
  -p max_concurrent=50
```

The flow gates concurrency in-process: `count_running()` returns the total active Argo workflow count, and the flow waits until that count drops below `max_concurrent` before submitting the next subject. It does not check per-subject completion — use `start_index`/`end_index` to resume after a pause.

To submit a single subject directly:
```bash
argo submit --from workflowtemplate/cloudpipe \
  -n argo-workflows \
  -p subjID=NDARINVXXXXXXXX \
  -p globus-source-collection-id=43583c7d-29c9-4d36-9cb5-c8a1641923cb \
  -p globus-source-base-path=/abcd/derivatives/mmps_mproc \
  -p globus-dest-collection-id=00666689-6b52-444d-b6a8-57a3ce6ee97c \
  -p globus-dest-base-path=/mmps_mproc \
  -p globus-scan-types='["T1w","T2w","rest","nback"]'
```

### Workflow-level settings

Every pod in the workflow gets:
- Service account: `argo-workflows-runner` (Pod Identity → S3 + SSM permissions)
- Security context: UID/GID/fsGroup 1000
- Karpenter annotation: `do-not-disrupt: "true"` (no voluntary eviction)
- EFS PVC: 50Gi `ReadWriteMany`, named after the subject ID (lowercased), deleted on completion
- Retry: limit 3, exponential backoff (1m, 2m, 4m), triggers on pod deletion/node shutdown or exit codes 64/137/143
- `failFast: false` on all DAGs — session-level failures do not abort other sessions
- Active deadline: 12 hours per workflow

### DAG overview

```
start-globus-instance
        │
        ▼
globus-transfer
        │
        ▼  (skipped when globus-use-s3-gateway == "true")
globus-s3-sync
        │
        ▼
subject-data-inventory
        │
        ├──────────────────────────────┐
        ▼                              ▼
anatomical-processing              session-level-pipeline (×N sessions, parallel)
(skipped if fs exists)                  │
                                        ├─ registration-dagtask
                                        │    ├─ discover-bold-runs  ─┐
                                        │    ├─ lookup-nss           │ concurrent
                                        │    ├─ check-t1w-to-mni   ─┘
                                        │    ├─ t1w-to-mni (if not exists)
                                        │    └─ bold-to-t1w (×runs, if not exists)
                                        │
                                        └─ functional-preprocessing-dagtask
                                             └─ func-preproc (×runs, parallelism: 2)
```

`master-pipeline-dag` has `parallelism: 3`, so at most 3 major DAG pods run concurrently within a single subject's workflow.

---

### Phase 1 — Transfer

**`start-globus-instance-template`** (image: `bravepy`, node: `cpu-light-nodepool`)

Reads the GCS EC2 instance ID from SSM (`/cloudpipe/globus/instance-id`), calls `ec2.start_instances()`, and waits for the status check to pass. Idempotent — safe to re-run if already running.

Failure modes:
- Instance doesn't exist at the SSM-stored ID (instance was replaced without updating SSM) → run `terraform apply` to update the parameter
- Status check never passes → SSM into the instance and check `/var/log/cloud-init-output.log` for boot errors

---

**`globus-transfer-template`** (image: `globus`, node: `cpu-light-nodepool`, semaphore: `globus-transfer` limit 8)

Authenticates using `GLOBUS_NATIVE_APP_CLIENT_ID` + `GLOBUS_REFRESH_TOKEN` from the `globus-credentials` K8s secret (synced from Secrets Manager via ExternalSecret every hour).

Steps inside `transfer.py`:
1. Walk the source collection BIDS tree under `{source-base-path}/{subjID}` — lists sessions, then each `bids_dir` for each requested scan type
2. Check for an existing active transfer task with label `cloudpipe-{subjID}` (idempotent on retry)
3. Submit a new transfer task with `sync_level="checksum"` and `encrypt_data=True`
4. Handle `TooManyPendingJobs` with exponential backoff (10 attempts, 60–600s)
5. Poll every 60 seconds until the task `SUCCEEDED`

S3 destination: `s3://abcd-v7/mmps_mproc/{subjID}/{session}/{bids_dir}/`

Failure modes:
- Auth error → refresh token expired; run `python images/globus/setup_auth.py` (see `docs/globus.md`)
- `FAILED` or `CANCELLED` transfer → check the Globus web app for the task error; usually a source-side permission or network issue
- Semaphore timeout → 8 concurrent transfers are already running; the pod will wait until a slot opens

---

**`globus-s3-sync-template`** (image: `bravepy`, node: `cpu-light-nodepool`, S3 gateway mode only)

**Skipped** when `globus-use-s3-gateway == "true"` (the current default). With the S3 gateway, GridFTP writes go directly to S3 and no local staging step is needed.

When enabled (POSIX mode), runs `aws s3 sync` on the GCS instance via SSM `send-command` and polls for up to 2 hours. Deletes the local staging directory after sync completes.

---

### Phase 2 — Inventory

**`subject-data-inventory-template`** (image: `bravepy` SHA-pinned, node: `cpu-light-nodepool`)

Single script that produces all information downstream phases need. Runs once per subject.

What it does:
1. Lists S3 prefixes under `mmps_mproc/{subjID}/` to discover sessions
2. For each session, lists `mmps_mproc/{subjID}/{session}/func/` to find BOLD NIfTIs matching `task-{rest,nback}` + run pattern
3. For each run, calls `head_object` on the expected `b2t_exists` and `func_exists` S3 keys; requires `ContentLength > 1024` bytes to treat as valid (guards against zero-byte failure artifacts)
4. For each session, checks `t1w_to_mni.tar.gz` existence (same size guard)
5. Downloads `config/nss_volumes.csv` once and attaches `nss_frames` to each session (hard fails with exit 1 if the subject/session is missing from the table)
6. Checks `derivatives/fastsurfer/{subjID}/{subjID}_{ses}_templated.tar.gz` for every session; writes `"True"` or `"False"` to `/tmp/fastsurfer_exists.txt`

Outputs:
- `result` (stdout): JSON array — one object per session:
  ```json
  [
    {
      "session": "ses-00A",
      "runs": [
        {"task": "task-rest", "run": "run-01", "b2t_exists": false, "func_exists": false}
      ],
      "t1w_to_mni_exists": false,
      "nss_frames": "15"
    }
  ]
  ```
- `fastsurfer-exists` (file parameter): `"True"` or `"False"`

Failure modes:
- Subject not found in `mmps_mproc/` → transfer failed silently; check Globus task status
- Subject/session missing from `nss_volumes.csv` → add the row and re-run
- S3 permission error → check that the `argo-workflows-runner` Pod Identity role has access to the bucket

---

### Phase 3 — Anatomical processing

**Skipped when `fastsurfer-exists == "True"`** (all sessions have valid `_templated.tar.gz` derivatives).

Five steps run in sequence. All run on `gpu-nodepool` (image: `fastsurfer`, UID 1000). The EFS PVC is used as working space — FastSurfer writes its subjects directory there across all five steps.

```
fastsurfer-template-creation
        │
        ▼
fastsurfer-template-segmentation
        │
        ├──────────────────────────────┐
        ▼                              ▼
fastsurfer-template-parcellation   fastsurfer-long-segmentation
        │                              │
        └──────────┬────────────────────┘
                   ▼
        fastsurfer-long-parcellation
```

Long segmentation depends only on template segmentation (not parcellation) so it starts as soon as the template brain model is available. Long parcellation waits for both template parcellation and long segmentation.

**Step 1 — `fastsurfer-template-creation-template`**

Downloads T1w NIfTIs for all sessions from `mmps_mproc/{subjID}/{session}/anat/` via an init container (`bravepy`). Runs `long_prepare_template.sh` to create the within-subject template, registering all session T1ws to a common space. Handles subjects with a single timepoint automatically.

Command built by the master DAG (example for two sessions):
```
--tid NDARINVXXXXXXXX_template
--t1s /home/nonroot/NDARINVXXXXXXXX/ses-00A/anat/NDARINVXXXXXXXX_ses-00A_run-01_T1w.nii.gz
      /home/nonroot/NDARINVXXXXXXXX/ses-02A/anat/NDARINVXXXXXXXX_ses-02A_run-01_T1w.nii.gz
--tpids ses-00A ses-02A
--threads 8
```

**Step 2 — `fastsurfer-template-segmentation-template`**

Runs FastSurfer deep learning segmentation on the within-subject template (`--seg_only --base --edits --threads 4`).

**Step 3 — `fastsurfer-template-parcellation-template`**

Surface reconstruction on the template (`--surf_only --base --edits --3T --fsaparc --threads 4`).

**Step 4 — `fastsurfer-long-segmentation-template`**

Longitudinal segmentation for all sessions simultaneously (`--seg_only --long {subjID}_template --edits --threads 4`). Subjects passed as `ses-00A=from-base ses-02A=from-base ...`.

**Step 5 — `fastsurfer-long-parcellation-template`**

Surface reconstruction for all sessions (`--surf_only --long {subjID}_template --3T --fsaparc --parallel N --threads 3`, where N = number of sessions). Runs N surfaces in parallel, 3 threads each.

Outputs uploaded to S3 per session:
```
derivatives/fastsurfer/{subjID}/{subjID}_{session}_templated.tar.gz
```

Failure modes:
- GPU OOM → reduce threads or check for other workflows sharing the node
- EFS mount failure → check `kubectl get pvc -n argo-workflows` for the subject's PVC
- FastSurfer crash on a specific session T1w → inspect pod logs; the T1w from that session may be unusable
- Template creation fails with single timepoint → `long_prepare_template.sh` handles this case, but log the subject ID for manual review

---

### Phase 4 — Per-session processing

Runs once per session discovered by the inventory, in parallel (all sessions start simultaneously, subject only to the global `parallelism: 3` limit on the master DAG). Each session runs two sub-DAGs in sequence: registration then functional preprocessing.

#### Registration

Three cheap S3 checks run concurrently first:

**`discover-bold-runs-template`** (image: `bravepy`, node: `cpu-light`) — lists `mmps_mproc/{subjID}/{session}/func/` for NIfTIs matching `task-{rest,nback}`; for each run, checks whether `bold_to_t1w_{task}_{run}.tar.gz` already exists in `derivatives/registration/`. Returns a JSON array of `{task, run, b2t_exists}` objects. This feeds `withParam` for the bold-to-t1w fan-out — only real runs get pods.

**`lookup-nss-template`** (image: `python:3.12-slim`, node: `cpu-light`) — downloads `config/nss_volumes.csv` from S3 (declared as an Argo artifact input), looks up `nss_frames` for this subject/session. Fails hard if not found.

**`check-t1w-to-mni-template`** (image: `bravepy`, node: `cpu-light`) — calls `head_object` on `derivatives/registration/{subjID}/{session}/t1w_to_mni.tar.gz`. Returns `"True"` or `"False"`.

---

**`t1w-to-mni-template`** (image: `fireants`, node: `gpu-nodepool`)

**Skipped when `check-t1w-to-mni-step` returns `"True"`**.

Downloads:
- `derivatives/fastsurfer/{subjID}/{subjID}_{session}_templated.tar.gz` → `/fastsurfer/`
- `config/MNI152NLin2009cAsym_T1w_brain.nii.gz`

Runs `fst1w_to_mni.py`: affine + SyN registration of FreeSurfer `orig.mgz` to MNI152NLin2009cAsym using FireANTs. Uses `orig.mgz` (not the BIDS T1w) so the source space matches the BOLD→T1w transform, which SynthMorph produces referenced to FreeSurfer conformed space. `brainmask.mgz` provides the skull-stripped mask in the same space.

Resources: 16 GB RAM, 4 CPU, 1 GPU. Runs `nvidia-smi` as a background monitor.

Outputs uploaded to S3:
```
derivatives/registration/{subjID}/{session}/t1w_to_mni.tar.gz
  {prefix}_T1w_brain.nii.gz    (QC: skull-stripped in orig space)
  {prefix}_affine.mat
  {prefix}_warp.nii.gz
  {prefix}_invwarp.nii.gz
  {prefix}_warped.nii.gz       (QC: orig space warped to MNI)
```

Failure modes:
- GPU allocation failure → check `gpu-nodepool` capacity; FireANTs requires exactly 1 GPU
- Registration diverges (warped image looks wrong) → inspect QC image; may indicate a poor-quality T1w
- FastSurfer tarball download fails → the anatomical step may not have produced this session's file (check S3)

---

**`bold-to-t1w-template`** (image: `synthmorph`, node: `cpu-heavy-nodepool`)

Runs once per run (fan-out via `withParam`). **Skipped for each run where `b2t_exists == "true"`**.

Downloads:
- `mmps_mproc/{subjID}/{session}/func/{subjID}_{session}_{task}_{run}_bold.nii.gz`
- `derivatives/fastsurfer/{subjID}/{subjID}_{session}_templated.tar.gz`
- `config/fslicense`

Runs `bold_to_t1w.py` (SynthMorph): contrast-agnostic deep learning affine registration of the BOLD reference volume (first non-steady-state frame, determined by `nss-frames`) to the FreeSurfer conformed T1w. A single forward pass (~5 seconds) replaces bbregister's iterative surface-based optimization (~5–10 minutes).

The LTA transform output is converted to an ANTs/ITK `.txt` affine (RAS→LPS coordinate flip) for use in `antsApplyTransforms` during functional preprocessing.

Resources: 8 GB RAM, 2 CPU, 4 GB ephemeral storage.

Outputs written to EFS PVC and uploaded to S3:
```
derivatives/registration/{subjID}/{session}/bold_to_t1w_{task}_{run}.tar.gz
  {prefix}_desc-bold2t1w_ref.nii.gz          (QC: BOLD reference)
  {prefix}_desc-bold2t1w.lta                 SynthMorph LTA
  {prefix}_desc-bold2t1w_warped.nii.gz       (QC: BOLD ref in T1w space)
  {prefix}_desc-bold2t1w_itk.txt             ANTs/ITK affine
  {prefix}_desc-bold2t1w_brainmask.nii.gz    brain mask in BOLD space
```

Failure modes:
- `nss-frames` lookup fails → subject/session not in `nss_volumes.csv`
- BOLD NIfTI not found → transfer missed this run; verify S3 key exists
- SynthMorph import error → image pull issue or CUDA/CPU incompatibility (cpu-heavy-nodepool is CPU only — synthmorph runs without GPU here)

---

#### Functional preprocessing

Runs after all registration steps for the session complete. Fanned out over all `(task, run)` pairs, `parallelism: 2` per session.

**`functional-preprocessing-template`** (image: `afni`, node: `cpu-heavy-nodepool`)

**Skipped for each run where `func_exists == "true"`** (checked during inventory via the `size > 1KB` guard).

Downloads:
- `mmps_mproc/{subjID}/{session}/func/{subjID}_{session}_{task}_{run}_bold.nii.gz`
- `mmps_mproc/{subjID}/{session}/func/{subjID}_{session}_{task}_{run}_bold.json` (BIDS sidecar)
- `mmps_mproc/{subjID}/{session}/func/{subjID}_{session}_{task}_{run}_motion.tsv`
- `derivatives/registration/{subjID}/{session}/bold_to_t1w_{task}_{run}.tar.gz` (brainmask + ITK affine)
- `derivatives/registration/{subjID}/{session}/t1w_to_mni.tar.gz` (affine + warp)
- `derivatives/fastsurfer/{subjID}/{subjID}_{session}_templated.tar.gz` (for `aseg.mgz`)
- `config/MNI152NLin2009cAsym_T1w_brain.nii.gz`

The `preproc.py` script is mounted from the `preproc-script` ConfigMap at runtime, overriding the copy baked into the image. This allows updating the preprocessing script without rebuilding. Runs AFNI with 6 threads.

What `preproc.py` does (in order):
1. Trim non-steady-state frames (`nss-frames` volumes from the start)
2. Apply BOLD brain mask (from SynthMorph)
3. Confound estimation (native-space float32 BOLD): `aCompCor` using WM and CSF masks derived from `aseg.mgz`, plus motion regressors; DVARS, global signal, tCompCor — all computed here before the MNI warp
4. Precompute composite displacement field: BOLD→T1w (ITK affine) + T1w→MNI (affine + SyN warp) collapsed into a single voxel-to-voxel map via `antsApplyTransforms -o [displacement_field]`
5. Warp BOLD → MNI (scipy `map_coordinates`, cubic B-spline, 6 threads); warp brain mask → MNI (nearest-neighbour); single interpolation step for both
6. Bandpass filtering
7. Spatial smoothing

Output dtype: MNI BOLD is written as **float16 NIfTI** (`DT_FLOAT16`, datatype 512). This halves the in-memory allocation (~6.3 GB vs ~12.6 GB for a 400-frame run) and reduces Stage 3 peak RAM from ~14 GB to ~8 GB. FSL, AFNI, and FreeSurfer all upcast float16 to float32 on load. Confound regressors are derived entirely from native-space float32 data and are unaffected. Quantization error at typical BOLD baseline (~1000 units) is ~0.5 units, roughly 3% of thermal noise at tSNR 60 — negligible for GLM, FC, and ICA analyses. tSNR is computed with float32 accumulators before writing to disk (required to avoid overflow when summing 300+ frames in float16).

Resources: 10 GB RAM (request), 12 GB limit, 6 CPU, 10 GB ephemeral storage, 20 GB ephemeral-storage limit. Retry limit: 6 (higher than other templates due to compute cost).

Outputs uploaded to S3:
```
derivatives/func/{subjID}/{session}/{subjID}_{session}_{task}_{run}_space-MNI152NLin2009cAsym_bold.tar.gz
```

Can be submitted standalone for testing or reprocessing:
```bash
argo submit --from workflowtemplate/functional-preprocessing \
  -n argo-workflows \
  -p subjID=NDARINVXXXXXXXX \
  -p session=ses-00A \
  -p task=task-rest \
  -p run=run-01 \
  -p nss-frames=15
```

Failure modes:
- `aseg.mgz` parsing error → FastSurfer segmentation was corrupt; re-run anatomical phase
- `antsApplyTransforms` crash (composite warp step) → transform files incomplete; re-run registration for this session
- OOM → 12 GB limit hit; Stage 3 peak is ~8 GB (float16); check whether another pod is co-located on the same node driving ephemeral-storage contention
- ConfigMap not mounted → ArgoCD hasn't synced the latest preproc-script yet; wait ~30s after pushing (fullproc only — minproc bakes `preproc.py` into the image)

---

### Resubmitting partially processed subjects

Inventory runs fresh each time. If some derivatives already exist, those steps are skipped:
- All sessions have FastSurfer tarballs → entire anatomical phase skipped
- `t1w_to_mni.tar.gz` exists for a session → `t1w-to-mni-step` skipped for that session
- `bold_to_t1w_{task}_{run}.tar.gz` exists for a run → `bold-to-t1w-step` skipped for that run
- `_space-MNI152NLin2009cAsym_bold.tar.gz` exists for a run (and is > 1 KB) → `func-preproc` skipped for that run

To force a specific step to re-run, delete the corresponding S3 key before submitting.

---

## cloudpipe_fullproc

**Status: in development.** Processes raw ABCD DICOMs from scratch. Not yet in production.

### Differences from cloudpipe_minproc

| Aspect | cloudpipe_minproc | cloudpipe_fullproc |
|---|---|---|
| Input | Minimally preprocessed (mmps_mproc/) | Raw DICOMs (sourcedata/) |
| Globus staging | S3 gateway (direct to S3) | POSIX staging (EBS → aws s3 sync always runs) |
| Early preprocessing | None (already done upstream) | dcm2niix → despiking → STC → motion correction → SDC (FSL topup) → between-scan motion correction |
| Gradient nonlinearity correction | Included upstream | Omitted (proprietary manufacturer files unavailable) |
| Inventory | Single combined step | Two separate parallel steps (data inventory + derivatives inventory) |
| Registration | DAG-based per session | Steps-based per session (includes func-preproc at end of same template) |
| Source collection | DAIRC MMPS (minimally preprocessed) | ABCD raw DICOMs collection |

### DAG overview

```
start-globus-instance
        │
        ▼
globus-transfer (to POSIX staging)
        │
        ▼
globus-s3-sync (always enabled — no S3 gateway)
        │
        ├──────────────────────────────┐
        ▼                              ▼
subject-data-inventory         derivatives-inventory (runs independently)
        │                              │
        └──────────────┬───────────────┘
                       ▼
           anatomical-processing
           (skipped if derivatives exist)
                       │
                       ▼
           registration-processing (×N sessions)
           [discover + t1w-to-mni + bold-to-t1w + func-preproc in one steps template]
```

The `derivatives-inventory-template` in cloudpipe_fullproc is a separate lightweight step (just checks whether the FastSurfer prefix exists in S3) that runs in parallel with the main inventory, rather than being embedded in the inventory script.

### Unpack-and-convert step

The `unpack-and-convert-workflow-template.yaml.yaml` (note: double `.yaml` extension — a filename typo in the repository) handles the initial DICOM processing. Before registration begins, per-session steps unpack DICOM archives and convert to NIfTI using `dcm2niix`, then apply despiking, slice-timing correction, motion parameter estimation, and SDC.

This phase is where cloudpipe_fullproc diverges most from cloudpipe_minproc — once the preprocessed NIfTIs reach S3 in the same layout as `mmps_mproc/`, the registration and functional preprocessing phases are identical.

---

## subregion-seg (optional, standalone)

Optional subcortical subregion segmentation. **Not part of the main cloudpipe_minproc DAG** — submitted independently after FastSurfer longitudinal outputs for a subject are complete.

### Prerequisites

FastSurfer longitudinal outputs must exist in S3 for the subject:
```
derivatives/fastsurfer/{subjID}/{subjID}_long-template.tar.gz
derivatives/fastsurfer/{subjID}/{subjID}_ses-00A_templated.tar.gz
derivatives/fastsurfer/{subjID}/{subjID}_ses-02A_templated.tar.gz   (optional)
...
```

### How to submit

```bash
argo submit --from workflowtemplate/subregion-seg \
  -n argo-workflows \
  -p subjID=NDARINVXXXXXXXX \
  -p bucket=abcd-v7 \
  -p ecr-registry=public.ecr.aws/l9e7l1h1 \
  -p T1w_sessions='["ses-00A","ses-02A"]'
```

`T1w_sessions` controls which S3 artifact inputs are declared for the pods. Sessions beyond ses-00A are declared as `optional: true`; the template reads `base-tps` from the long-template tarball at runtime to determine which timepoints to process.

### Steps (run in parallel, `failFast: false`)

**`segment-thalamus-template`** (image: `freesurfer`, node: `cpu-heavy-nodepool`)

Runs `segment_subregions thalamus --long-base` using the GEMS/Bayesian atlas. ~30–45 min per subject with 4 threads. Creates symlinks from bare FastSurfer session IDs (`ses-00A`) to the `{tp}.long.{base}` naming convention that `--long-base` requires.

S3 output: `derivatives/subregions/{subjID}/{subjID}_thalamus.tar.gz`  
Contents: `ThalamicNuclei.v13.T1.mgz` + `ThalamicNuclei.v13.T1.volumes.txt` per session and template.

---

**`segment-brainstem-template`** (image: `freesurfer`, node: `cpu-heavy-nodepool`)

Runs `segment_subregions brainstem --long-base` using the GEMS/Bayesian atlas. ~15–25 min per subject with 4 threads.

S3 output: `derivatives/subregions/{subjID}/{subjID}_brainstem.tar.gz`  
Contents: `brainstemSsLabels.v13.T1.mgz` + `brainstemSsVolumes.v13.txt` per session and template.

---

**`segment-deeplearning-template`** (image: `freesurfer`, node: `cpu-heavy-nodepool`)

Runs two tools sequentially (both CPU-only, no GPU):

1. `mri_segment_hypothalamic_subunits` — TensorFlow CNN, ~10 sec/session. Segments 5 bilateral hypothalamic subregions. Reads `models/hypothalamic_subunits.h5` from the FreeSurfer models directory.

2. `mri_sclimbic_seg` — U-Net, <1 min/session. Segments hypothalamus (coarse), mammillary bodies, basal forebrain, septal nuclei, NAcc, and fornix. Reads the sclimbic model file from `models/`.

S3 outputs:
- `derivatives/subregions/{subjID}/{subjID}_hypothalamic.tar.gz` — `hypothalamic_subunits_seg.v1.mgz` + `hypothalamic_subunits_volumes.v1.csv` per session
- `derivatives/subregions/{subjID}/{subjID}_sclimbic.tar.gz` — `sclimbic.mgz` + `sclimbic.stats` per session

---

### Failure modes

- **S3 artifact download fails**: init container reports "Access Denied" → check `serviceAccountName: argo-workflows-runner` is set in the WorkflowTemplate spec
- **`base-tps` not found**: the long-template tarball doesn't contain a valid `base-tps` file — the subject's FastSurfer run may have failed or not completed the longitudinal phase
- **`segment_subregions` crashes**: check that symlinks were created correctly (logged before the command); also check that the FreeSurfer license file was downloaded to `/opt/freesurfer/.license`
- **Model file not found** (DL tools): the `freesurfer` image must include `models/` — verify `images/freesurfer/Dockerfile` does not exclude this directory

---

## fmri-first-level-proc

Standalone pipeline for first-level GLM analysis. Runs after cloudpipe_minproc has finished processing a subject. Requires cloudpipe_minproc outputs in `abcd-v7` and writes results to `<YOUR_INPUT_S3_BUCKET>`.

### How it starts

The `first-level-queue-manager` Prefect flow checks S3 for existing outputs before submitting:

```bash
prefect deployment run first-level-queue-manager/first-level-queue-manager \
  -p subjects_file=s3://<YOUR_TEMP_S3_BUCKET>/first-level-subjects.csv \
  -p max_concurrent=25
```

The flow calls `is_completed()` for each subject, which does a `list_objects_v2` on `s3://<YOUR_INPUT_S3_BUCKET>/derivatives/first_levels/{subjID}/`. If any object exists, the subject is skipped.

`count_running()` counts **all** active Argo workflows — both cloudpipe and first-level. If both pipelines run simultaneously, set `max_concurrent` to account for cloudpipe workflows already in flight.

### What it runs

A single pod per subject on `first-level-nodepool` (Graviton ARM64):

```
/bin/bash -c "cd /app/ABCD_fmri_orchestrator_S3 && \
  python3 orchestrate_first_level.py \
    --orchestrate_config orch_config_final.yaml \
    --proc_config proc_config_final.yaml \
    --subj_id ${SUBJ_ID#sub-}"
```

The `${SUBJ_ID#sub-}` stripping is required because Argo uses the full BIDS subject ID (`NDARINVXXXXXXXX`) while `orchestrate_first_level.py` expects the bare NDAR ID without `sub-`.

Resources: 1200m–2 CPU, 10 Gi RAM, 300 Gi emptyDir scratch. Active deadline: 2 hours. Retry: 3, spot interruption only.

### What it reads and writes

The orchestrator and GLM code are baked into the image from two private repositories:
- `jrussell9000/ABCD_fmri_orchestrator_S3` — downloads cloudpipe_minproc outputs from `abcd-v7`, orchestrates the first-level GLM
- `jrussell9000/fmri-first-level-proc` — GLM implementation (installed as a Python package)

Config files `orch_config_final.yaml` and `proc_config_final.yaml` are baked in and specify the atlas parcellations (Schaefer 400-parcel + Tian S4 subcortical), GLM design, and output format.

Reads from `abcd-v7`:
```
derivatives/func/{subjID}/{session}/{subjID}_{session}_{task}_{run}_space-MNI152NLin2009cAsym_bold.tar.gz
```

Writes to `<YOUR_INPUT_S3_BUCKET>`:
```
derivatives/first_levels/{subjID}/...
```

### Failure modes

- 2-hour deadline exceeded → the subject has an unusual number of runs or the scratch volume I/O is slow; check pod logs for which step was running at timeout
- S3 download fails → cloudpipe_minproc outputs not present; verify the subject ran successfully in cloudpipe_minproc
- Atlas lookup error → `orch_config_final.yaml` references an atlas file that is baked into the image; rebuild the image if the atlas was updated
- `sub-` prefix not stripped correctly → should not happen with the current command override, but check the Argo workflow parameters if the orchestrator reports an unknown subject ID

---

## Cross-pipeline notes

### Concurrency accounting

`count_running()` in both Prefect flows uses the same `lib/argo.py` implementation and returns the total active workflow count across both `cloudpipe` and `fmri-first-level-proc` WorkflowTemplates. If you run both simultaneously, coordinate `max_concurrent` settings so the combined load stays within cluster and Globus semaphore limits.

### Checking which subjects have been processed

```bash
# Sessions with complete functional preprocessing outputs
aws s3 ls s3://abcd-v7/derivatives/func/ --recursive \
  | grep 'space-MNI152NLin2009cAsym_bold.tar.gz' \
  | awk '{print $4}' | cut -d/ -f3 | sort -u

# Subjects with first-level outputs
aws s3 ls s3://<YOUR_INPUT_S3_BUCKET>/derivatives/first_levels/ | awk '{print $2}' | tr -d '/'
```

### Forcing a step to re-run

Delete the S3 key for the derivative that needs to be regenerated, then resubmit. The inventory will detect the missing file and run that step while skipping everything else that is still valid.

```bash
# Example: force bold-to-t1w re-run for one session/run
aws s3 rm s3://abcd-v7/derivatives/registration/NDARINVXXXXXXXX/ses-00A/bold_to_t1w_task-rest_run-01.tar.gz

# Then resubmit
argo submit --from workflowtemplate/cloudpipe -n argo-workflows -p subjID=NDARINVXXXXXXXX ...
```
