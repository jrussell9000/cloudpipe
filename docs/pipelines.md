# Pipelines

This document walks through each pipeline in execution order: what triggers it, what each step does, what it reads and writes, and what can go wrong. Every claim here is verified against the current WorkflowTemplate YAML files.

| Pipeline | WorkflowTemplate | Submitter | Input | Output bucket |
|---|---|---|---|---|
| `cloudpipe_minproc` | `cloudpipe` | `cloudpipe-queue-manager` Prefect flow | ABCD minimally preprocessed | `<YOUR_S3_BUCKET>` |
| `cloudpipe_fullproc` | — (planned, not implemented) | — | Raw ABCD DICOMs | `<YOUR_S3_BUCKET>` |
| `subregion-seg` | `subregion-seg` | `cloudpipe` DAG (or standalone) | FastSurfer longitudinal outputs | `<YOUR_S3_BUCKET>` |
| `fmri-first-level-proc` | (separate repo) | `first-level-queue-manager` Prefect flow | cloudpipe_minproc outputs | `<YOUR_S3_BUCKET>` |

### What this pipeline does not do

The production pipeline starts from ABCD's *minimally preprocessed* release, so several steps you would expect in a from-scratch fMRI pipeline are **already applied upstream** and are deliberately not repeated here:

- head motion correction
- B0 / susceptibility distortion correction
- gradient nonlinearity correction
- between-scan motion correction

Two consequences that matter when reading the QC numbers:

- **The BOLD is never resampled out of native scanner space.** ABCD ships a fMRI→T1w matrix rather than applying it, so the BOLD↔T1w offset you see is field-of-view *prescription* — set by how the operator positioned the two acquisitions, routinely 16–97 mm, and a near-constant within a session (between-session SD 19.75 mm vs within-session 0.29 mm). Transform magnitude is therefore **not** a quality signal: it in fact *anti*-correlates with quality (corr(nmi, `rigid_disp_max_mm`) = +0.425), so a displacement gate ranks runs backwards. Nothing gates on it; the only BOLD→T1w gate is `nmi_gain > 0`.
- **That shipped matrix is not used.** BOLD→T1w is re-derived with SynthMorph instead ([ADR 002](decisions/002-synthmorph-over-bbregister.md), [ADR 012](decisions/012-abcd-matrix-rejected.md)). The sidecar is still read, but only to detect non-steady-state frames.

A `cloudpipe_fullproc` design exists for ingesting raw DICOMs and applying all of the above from scratch; it is **design only** — no WorkflowTemplates exist. It is documented at the end of this page.

---

## cloudpipe_minproc

Production pipeline. Ingests ABCD minimally preprocessed data (Hagler et al. 2019) and produces MNI-space BOLD with confounds.

### Submission

**Production (Prefect flow):**
```bash
prefect deployment run cloudpipe-queue-manager/cloudpipe-queue-manager \
  -p subjects_file=s3://<YOUR_S3_BUCKET>/config/subjects.csv
```

The flow gates concurrency: `ConcurrencyGate.count()` counts active Argo workflows and waits until below `max_concurrent` before submitting the next subject. `max_concurrent` is **not** a flow parameter — it's read live from the Prefect Variable `cloudpipe-max-concurrent` (fallback `50` when unset) on every poll cycle, so it can be changed mid-run with `prefect variable set cloudpipe-max-concurrent <N>` without restarting the flow. Use `start_index`/`end_index` to resume after a pause. The controller's `namespaceParallelism` (`100`) is the server-side backstop — see [ADR 008](decisions/008-prefect-as-queue-manager.md) for why a client-side gate alone cannot enforce the cap (#206).

**Direct single-subject submission:**
```bash
argo submit --from workflowtemplate/cloudpipe \
  -n argo-workflows \
  -p subjID=NDARINVXXXXXXXX \
  -p globus-source-collection-id=<YOUR_GLOBUS_SOURCE_COLLECTION_ID> \
  -p globus-source-base-path=/abcd/derivatives/mmps_mproc \
  -p globus-dest-collection-id=<YOUR_GLOBUS_DEST_COLLECTION_ID> \
  -p globus-dest-base-path=/mmps_mproc \
  -p globus-scan-types='["T1w","T2w","rest","nback"]'
```

### Workflow-level settings

| Setting | Value |
|---|---|
| Service account | `argo-workflows-runner` (Pod Identity → S3 + SSM) |
| Security context | `runAsUser: 1000`, `runAsGroup: 1000`, `fsGroup: 1000` |
| Karpenter annotation | `karpenter.sh/do-not-disrupt: "true"` on all pods |
| Retry | limit 8, policy `Always`, triggers on `pod deleted`/`imminent node shutdown` or exit codes 64/75/143; backoff 1m × 2, capped at 5m. Exit 137 is **not** retried — see [operations.md](operations.md) |
| `failFast` | `false` on all DAGs — a failed session does not abort other sessions or the anatomical phase |
| Active deadline | 43200 seconds (12 hours) |
| `ttlStrategy` | 86400 seconds after completion |
| `podDisruptionBudget` | `minAvailable: "100%"` |

### DAG overview

```
start-globus-instance ──► record-workflow-start
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
        ├──────────────────┬──────────────────────────────┐
        ▼                  ▼                              ▼
anatomical-        subregion-segmentation      session-level-pipeline (×N sessions)
processing         (skipped if all five                   │
(skipped if fs      region tarballs exist)                ├─ registration
 exists)                   │                              │    ├─ t1w-to-mni (if not exists)
        │                  │                              │    └─ bold-to-t1w ×runs (if not exists)
        └────────┬─────────┘                              ├─ func-preproc (one pod/session,
                 ▼                                        │   loops runs; includes Stage 5b
            fsqc-metrics                                  │   grayordinate extraction)
       (anatomical QC; non-fatal,                         └─ surface-resample ×runs
        nothing depends on it)                                (fsLR32k assembly)
        │                  │                              │
        └──────────────────┴──────────────────────────────┘
                   ▼
        [onExit] emit-workflow-metrics → delete-globus-input
```

`subregion-segmentation` gates on the same anatomical dependency as the session branch, so the two run **in parallel** rather than in sequence. `fsqc-metrics` waits for both the anatomical and subregion branches to reach a terminal state, but it is non-fatal by construction: the master DAG sets `failFast: false` and nothing depends on the task, so an anatomical-QC failure cannot hold back registration, functional preprocessing, or the derivatives themselves.

`master-pipeline-dag` has `parallelism: 3`, which prevents simultaneous GPU + CPU-heavy tasks from saturating a node: at most 3 task pods (e.g. anatomical + 2 session instances) run at once within a single subject's workflow.

**Outcome recording is not a single sibling of this diagram** — `record-outcome-*` tasks are attached at each phase, not collected at the bottom of the master DAG:

| Where | `record-outcome-*` tasks |
|---|---|
| `master-pipeline-dag` (this diagram) | `record-outcome-anatomical-dagtask`, `record-outcome-session-dagtask` (phase-level aggregates), `record-outcome-subregion-seg-dagtask`, `record-outcome-fsqc-metrics-dagtask` |
| Anatomical child DAG | `record-outcome-fastsurfer-dagtask` |
| Session-level child DAG | `record-outcome-func-preproc-dagtask` (records both `func-preproc` and `surface-sample`), `record-outcome-surface-resample-dagtask` |
| Registration child DAG | `record-outcome-t1w-to-mni-step`, `record-outcome-bold-to-t1w-step` |

Phase-level `record-outcome-*` tasks depend on `<producer>.Succeeded || .Failed || .Skipped`, so they run on every terminal status. For a task that carries no `when:` clause of its own — `fsqc-metrics` is the example — `Skipped` means *an upstream branch failed*, not that the work was already done.

The per-run recorders whose producer writes its own outcomes in-pod are instead **fallbacks**, gated on `<producer>.Failed || .Errored || .Skipped` — never on bare success, so a healthy session pays for zero recorder pods. `.Errored` covers spot preemption (`pod deleted` / node shutdown), which is phase `Error`, not `Failed`, and is the case where the in-pod records never reach S3. See [ADR 016](decisions/016-skipped-producer-deadlock-in-dag-recording.md).

> A record fan-out must never reference a **skipped** producer's output parameters: Argo cannot resolve them and the DAG deadlocks. This is why outcome recording passes the producer's `.status` rather than its outputs.

See [Phase 5 — Observability and cleanup](#phase-5--observability-and-cleanup) for the full list of recorded step names and the S3 key format.

---

### Phase 1 — Transfer

**`start-globus-instance-template`**
(image: `cloudpipe/python`, node: `cpu-light-nodepool`)

Reads the GCS EC2 instance ID from SSM (`/cloudpipe/globus/instance-id`), calls `ec2.start_instances()`, and waits for the EC2 status check to pass. Then polls port 443 on the instance's public IP directly — EC2 status checks pass before `gcs-auto-reregister.service` finishes restarting gridftp, so port polling is the correct readiness signal. Idempotent: safe to re-run when the instance is already running.

Resources: 128M memory, 100m CPU.

Failure modes:
- Instance doesn't exist at the SSM-stored ID → run `terraform apply` to update the parameter
- Status check never passes → SSM into the instance and check for boot errors
- Port 443 never opens (40 × 15s = 10 min timeout) → gridftp may have failed to start; check the instance

---

**`globus-transfer-template`**
(image: `cloudpipe/globus`, node: `cpu-light-nodepool`, semaphore: `globus-transfer` limit 8, `activeDeadlineSeconds: 7200`)

Authenticates using `GLOBUS_NATIVE_APP_CLIENT_ID` + `GLOBUS_REFRESH_TOKEN` from the `globus-credentials` K8s secret.

What `transfer.py` does:
1. Walks the source collection BIDS tree under `{source-base-path}/{subjID}` — lists sessions, then each `bids_dir` per requested scan type
2. Checks for an existing active transfer task with label `cloudpipe-{subjID}` (idempotent retry guard)
3. Submits a new transfer task with `sync_level="checksum"` and `encrypt_data=True`
4. Handles `TooManyPendingJobs` with exponential backoff
5. Polls every 60 seconds until the task `SUCCEEDED`

S3 destination pattern: `s3://{bucket}/{dest-base-path}/{subjID}/{session}/{bids_dir}/`

Resources: 256M memory, 100m CPU.

Failure modes:
- Auth error → refresh token expired; run `python images/globus/setup_auth.py` (see `docs/globus.md`)
- `FAILED` or `CANCELLED` transfer → check the Globus web app for the task error
- Semaphore timeout → 8 concurrent transfers are running; the pod waits until a slot opens

---

**`globus-s3-sync-template`**
(image: `cloudpipe/python`, node: `cpu-light-nodepool`, `activeDeadlineSeconds: 7200`)

**Skipped** when `globus-use-s3-gateway == "true"` (the current default). With the S3 gateway, GridFTP writes go directly to S3 and no local staging step is needed.

When enabled (POSIX mode), runs `aws s3 sync` on the GCS instance via SSM `send-command`, polls for up to 2 hours (480 × 15s), then deletes the local staging directory after sync completes (`rm -rf /data/globus-staging/{dest-base-path}/{subjID}`).

Resources: 128M memory, 100m CPU.

---

### Phase 2 — Inventory

**`subject-data-inventory-template`**
(image: `cloudpipe/python`, node: `cpu-light-nodepool`)

Single Python script (`src/inventory.py`) that produces all information downstream phases need. Runs once per subject after the transfer step.

The eight checks (executed in one pass):

1. **List sessions** — paginates `mmps_mproc/{subj}/` with a `/` delimiter to enumerate session prefixes
2. **List BOLD runs** — for each session, paginates `mmps_mproc/{subj}/{ses}/func/` for `.nii`/`.nii.gz` files, extracts `(task, run)` pairs filtered to `{rest, nback}`, deduplicates
3. **Check run completion** — for each run, calls `head_object` on four S3 keys:
   - `b2t_exists`: `derivatives/registration/{subj}/{ses}/bold_to_t1w_{task}_{run}/{prefix}_desc-bold2t1w_itk.txt`
   - `func_exists`: `derivatives/func/{subj}/{ses}/{prefix}_space-MNI152NLin2009cAsym_bold.tar.gz`
   - `surf_exists`: `derivatives/func_surf/{subj}/{ses}/components/{prefix}_desc-grayordcomponents_bold.tar.gz`
   - `surf_target_exists`: `derivatives/func_surf/{subj}/{ses}/fsLR32k/{prefix}_space-fsLR32k_bold.dtseries.nii`
4. **Check T1w-to-MNI completion** — calls `head_object` on `derivatives/registration/{subj}/{ses}/t1w_to_mni/{prefix}_desc-t1w2mni_affine.mat` per session
5. **Check T1w source availability** — `head_object` on `mmps_mproc/{subj}/{ses}/anat/{subj}_{ses}_run-01_T1w.nii.gz`, recorded as `t1w_available`
6. **Load nss_volumes.csv** — downloads `config/nss_volumes.csv` once, looks up `nss_frames` per subject/session; exits 1 if the entry is missing
7. **Check FastSurfer derivatives** — `head_object` on `derivatives/fastsurfer/{subj}/{subj}_{ses}_templated.tar.gz`, writing `"True"`/`"False"` to `/tmp/fastsurfer_exists.txt`
8. **Check subregion derivatives** — `head_object` on `derivatives/subregions/{subj}/{subj}_{region}.tar.gz` for all five regions, writing `/tmp/subregions_exists.txt`

Two of those deserve a note:

**Why `t1w_available` is checked separately (5).** ABCD collects T1w less often than functional runs, so a longitudinal session can have BOLD and no anatomical. `fastsurfer_exists` (7) is therefore judged **only against sessions that have a T1w source** — judging it against anat-less sessions would keep the anatomical phase re-running forever, since those sessions can never produce a templated tarball.

**Why the subregion check requires all five (8).** The five regions are `thalamus`, `brainstem`, `hippoamyg`, `hypothalamic`, `sclimbic`, and the flag is `True` only if *every* tarball exists. Requiring all rather than any means a partial or failed prior run re-runs cleanly, with the phase's own per-region resume guards skipping whatever did complete. It also prevents a subject whose tarballs predate `hippo-amygdala` from silently persisting as a four-region outlier.

Outputs:
- `result` (stdout, consumed by Argo as the `result` output parameter): JSON array, one object per session:
  ```json
  [
    {
      "session": "ses-00A",
      "runs": [
        {"task": "task-rest", "run": "run-01", "b2t_exists": false,
         "func_exists": false, "surf_exists": false, "surf_target_exists": false}
      ],
      "t1w_to_mni_exists": false,
      "t1w_available": true,
      "nss_frames": "15"
    }
  ]
  ```
- `fastsurfer-exists` (file output parameter from `/tmp/fastsurfer_exists.txt`): `"True"` or `"False"` — gates the entire anatomical phase

Resources: 256Mi memory, 100m CPU, 500M ephemeral-storage (request and limit).

Failure modes:
- Subject not found in `mmps_mproc/` → transfer failed silently; check Globus task status
- Subject/session missing from `nss_volumes.csv` → add the row and resubmit
- S3 permission error → check that `argo-workflows-runner` Pod Identity role has access to the bucket

---

### Phase 3 — Anatomical processing

**Skipped when `fastsurfer-exists == "True"`** (all sessions have valid `_templated.tar.gz` derivatives).

Four steps run as a DAG. All FastSurfer steps use image `cloudpipe/fastsurfer`. Steps A and C run on `gpu-nodepool`; B and D on `cpu-heavy-nodepool`.

**There is no shared volume.** Each step works in a private `emptyDir` at `/work`, with `SUBJECTS_DIR=/work/subjects`, and hands state to the next step through S3 per [ADR 004](decisions/004-s3-artifacts-for-inter-step-data.md). Intermediates go to `scratch/{workflow.name}/anat/` and are reaped by the `scratch-expiration` lifecycle rule (7 days) in `terraform/s3_lifecycle.tf`. They are not derivatives — nothing outside the owning workflow may read them.

```
A  fastsurfer-template-build          (gpu)   long_prepare_template.sh + --seg_only --base
        │  scratch: template-base.tar.gz
        ├──────────────────────────────┐
        ▼                              ▼
B  fastsurfer-template-parcellation  C  fastsurfer-long-segmentation
   (cpu-heavy)  --surf_only --base      (gpu)  --seg_only --long
        │  scratch:                     │  scratch:
        │  template-parcellated.tar.gz  │  sessions-seg/{ses}.tar.gz
        └──────────┬────────────────────┘
                   ▼
D  fastsurfer-long-parcellation        (cpu-heavy)  --surf_only --long
                   │
                   ▼  derivatives/fastsurfer/... (unchanged contract)
```

C depends only on A, not on B — the base brain model from `seg_only` is sufficient to start longitudinal segmentation without waiting ~30 min for surface reconstruction.

Template creation and segmentation share pod A because both are `gpu-nodepool` and strictly sequential: splitting them cost a second GPU node provision (the pool is thin — see [the spot-scarcity investigation](https://github.com/jrussell9000/cloudpipe/blob/main/docs/investigations/2026-07-17-gpu-nodepool-spot-scarcity-g6-enablement.md)) plus a full round-trip of the template, for no parallelism gain.

**Why `ephemeral-storage` requests are flat rather than per-subject.** Steps A–C request a flat 20G; only D scales (`12 + 4k` GB via `podSpecPatch`), because D holds the template, every session directory, parallel surface intermediates and the output tarballs at once. Flat sizing is safe because ephemeral storage is never the binding scheduling constraint: every eligible instance type reports ≥ ~90Gi allocatable. On `cpu-heavy` (Bottlerocket, `instanceStorePolicy: RAID0`) that is the RAID0'd NVMe on types that have one and the 100Gi `/dev/xvdb` data volume on the 55-of-79 eligible types that do not; on `gpu-nodeclass` there is no `instanceStorePolicy` at all, so every g4dn/g5/g6 reports ~140Gi from its 150Gi root volume. CPU and memory bind first.

---

**Step A — `fastsurfer-template-build-template`** (node: `gpu-nodepool`)

Two init containers run before the main pod:
- `download-t1w-inputs` (image: `cloudpipe/python`) — downloads `mmps_mproc/{subj}/{ses}/anat/{subj}_{ses}_run-01_T1w.nii.gz` for every session into an `emptyDir` at `/home/nonroot/{subj}/{ses}/anat/`
- `download-fsaverage` (image: `cloudpipe/python`) — downloads `config/fsaverage/` (~480 MiB, 312 objects) from S3 to `/work/subjects/fsaverage/`. Required before `long_prepare_template.sh` runs surface registration — `fsaverage` is not included in per-subject tarballs. B and D run the same init container for the same reason. C also runs it, precautionarily — `--seg_only` shouldn't need it, but the old shared volume guaranteed it was always present and that assumption is unverified against a real run; see C's section below.

There is no `clear-is-running` init container any more. It existed to delete `*IsRunning*` lock files so a retry could **restart over a dirty shared volume** — which is not resume, and is what produced the truncated-template bugs the exit-75 guards in B and D were added to catch. Every pod now starts from a clean `emptyDir` re-seeded from its input artifacts, so the dirty-state hazard is gone; the guards remain because an interrupted run still uploads nothing.

Main container runs `long_prepare_template.sh`, then — only if that succeeded — `run_fastsurfer.sh --seg_only --base --edits --threads 1`. Both command strings are built by the master DAG from the session list:
```
--tid {subjID}_template
--t1s /home/nonroot/{subjID}/{ses}/anat/{subjID}_{ses}_run-01_T1w.nii.gz ...
--tpids ses-00A ses-02A ...
--threads 1
```

`--threads 1` matches the 1-CPU request so two time-sliced GPU pods fit a g4dn.xlarge; see the inline comment for the full rationale. `--threads` only reaches FastSurfer's own argument parsing, so the GPU steps additionally project the CPU request into `OMP_NUM_THREADS` / `MKL_NUM_THREADS` / `OPENBLAS_NUM_THREADS` / `ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS` via the downward API — without that, PyTorch and OpenMP size their pools from the host core count (measured `cpu_efficiency` 1.10–1.22 against a 1-core request, 2026-07-31 batch).

Resources: 4G memory, 1 CPU, 20G ephemeral-storage, 1 GPU.

Outputs: `scratch/{workflow.name}/anat/template-base.tar.gz`.

Failure modes:
- GPU OOM → check for other workflows sharing the node
- T1w download fails → verify `mmps_mproc/` has the expected `run-01_T1w.nii.gz` for that session
- `fsaverage` download fails → check that `config/fsaverage/` is populated in S3
- `long_prepare_template.sh` non-zero → segmentation is skipped and the step exits with that code rather than segmenting an absent template

---

**Step B — `fastsurfer-template-parcellation-template`** (node: `cpu-heavy-nodepool`)

Surface reconstruction on the template: `--surf_only --base --edits --3T --fsaparc`, plus `--threads` derived from the pod's own `requests.cpu` via the downward API (`CPU_REQUEST`) rather than written into the master template's argstring.

Inputs: `template-base.tar.gz` from A, landed directly at `/work/subjects/{subjID}_template`, plus `config/fslicense` and the `fsaverage` init container.

On completion the step asserts `surf/{lh,rh}.white` and `mri/filled.mgz` exist and exits 75 (`EX_TEMPFAIL`, retryable) if not — `run_fastsurfer.sh` traps SIGTERM and exits 0 even when its `recon-all` child is killed mid-reconstruction by a spot reclaim, which would otherwise promote a truncated template that only detonates two steps later at `segment-subregions`.

Resources: 3G memory, 4 CPU, 20G ephemeral-storage. CPU is also the thread count, so it must not go below 2 — see Step D.

Outputs: `scratch/{workflow.name}/anat/template-parcellated.tar.gz`.

---

**Step C — `fastsurfer-long-segmentation-template`** (node: `gpu-nodepool`)

Longitudinal segmentation for all sessions simultaneously: `--seg_only --long {subjID}_template --edits --fsaparc --threads 1`. Sessions passed as `ses-00A=from-base ses-02A=from-base ...`.

Runs `brun_fastsurfer.sh` (batch variant). Takes `template-base.tar.gz` from A — it does **not** wait for B.

Also runs the `download-fsaverage` init container (see Step A). `--seg_only` shouldn't need `fsaverage` — it's a surface-registration atlas, and this step never runs `--surf_only` — but under the old shared volume it was always present regardless, so removing it here is unverified. Costs ~480 MiB of S3 transfer and a few seconds of GPU node time per pod until a real run confirms it's safe to drop.

Resources: 2G memory, 1 CPU, 20G ephemeral-storage, 1 GPU.

Outputs: `scratch/{workflow.name}/anat/sessions-seg/{ses}.tar.gz`, one per session. Declared one-per-possible-session (`ses-00A`…`ses-10A`) and all `optional: true`, because Argo resolves `outputs.artifacts` statically when the pod spec is built — a variable-length session list cannot be expressed any other way. Same pattern as `subregion-seg`'s segmentation templates' FastSurfer input artifacts.

---

**Step D — `fastsurfer-long-parcellation-template`** (node: `cpu-heavy-nodepool`)

Surface reconstruction for all sessions: `--surf_only --long {subjID}_template --3T --parallel {k}`, where k = number of sessions. `--threads` is not in the argstring — the container derives it as `CPU_REQUEST / k` from the downward API, so the `podSpecPatch` below is the single source of truth for both the reservation and the thread count.

Reassembles `SUBJECTS_DIR` from **B's parcellated template** (not A's — `long_compat_segmentHA.py`'s cortical-ribbon step needs its white surfaces) plus **C's per-session directories**, each landing directly at its final `/work/subjects/` path.

`podSpecPatch` sets resources dynamically based on session count k:
- CPU: `max(4, 2k)` — the **total** thread count, spent as `--threads cpu/k` across k `--parallel` instances. The division is exact at every k: 4/1, 4/2, 6/3, 8/4.
- Memory: `13G` at k≤3, `18G` at k=4 — its own expression, deliberately not tied to the CPU one and **not** scaled on 2k
- Ephemeral storage: `(12 + 4k)G`

**Memory is sized from measured peak, not `ram_efficiency` (#235).** The previous `max(6, 2k)G` was derived from `ram_efficiency` 0.30 and described as carrying 1.9–2.7× headroom over the mean. Against max `pod_memory_working_set` over every parcellation pod of the 2026-08-10 200-subject batch it was 0.43–0.48× of the **peak**:

| | request | n | median | p95 | max | max/request |
|---|---|---|---|---|---|---|
| k≤3 | 6G | 147 | 8.99 G | 11.44 G | 12.40 G | **2.07×** |
| k=4 | 8G | 75 | 13.94 G | 16.29 G | 17.42 G | **2.18×** |

Every k=4 pod exceeded its request, the median by 74%. A 2xlarge has only **14.82 G** allocatable, so a k≤3 pod at p95 held 77% of the whole node's `kubepods` limit while telling the scheduler it needed 6 G — the scheduler was free to pack 8.8 G of co-tenants into memory the pod was already using. `13G`/`18G` clears the observed max at both k and changes no instance class: 13 G + ~0.56 G of daemonset requests fits a 2xlarge, and k=4 is already on a 4xlarge for `cpu: 8`, where 18 G leaves ~11.9 G for the co-tenants its ~7.2 spare cores can hold.

This is the #129 lesson applied to a second step: `ram_efficiency` is a **lifetime average** and is structurally blind to a peak. Across the whole pipeline, the only two steps whose max/request is below 1.0 (`segment-subregions-dl`, `fsqc-metrics`) are the two sized from cgroup `memory.peak`; every step sized from `ram_efficiency` is over.

**No memory limit is set** — but not for the reason previously recorded ("an underestimate should degrade to burst, not to an OOM"; it degraded to an OOM anyway, at the `kubepods.slice` level). A memory limit cannot address what killed `cloudpipe-zkrmj` on 2026-08-11. The kernel's process table at the OOM instant (`rss_anon` is in 4 KB pages):

| pid | `rss_anon` | | |
|---|---|---|---|
| 81799 | 118,799 | 464 MB | |
| 81847 | 118,718 | 464 MB | |
| **82259** | **6,807,079** | **25.97 GiB** | ← victim |
| 82365 | 102,425 | 400 MB | |

One `mri_normalize` held 25.97 GiB of anonymous RSS during Intensity Normalization2 while its three siblings — same binary, same stage, same 256³ uchar inputs — sat at 464/464/400 MB. That 57× single-process outlier is an allocation fault, not demand that scales with k or with the data, and no *request* or *limit* value survives it.

> Do **not** restate this as "four concurrent streams are ~14 G" (an earlier draft of this section did). The table above is the whole pod's process list, and the three healthy streams held ~1.3 G *combined*. The ~14 G median in the sizing table is `pod_memory_working_set` across all stages **including page cache** — not four concurrent normalize processes. The request sizing rests on those pod peaks; the per-process story is separate and far smaller.

What the raised *request* buys is the thing a limit was wanted for: the scheduler now reserves what the pod actually takes, so it can no longer become the victim of a co-tenant packed into memory it was already using.

**A per-process address-space cap does what a memory limit cannot.** The container runs `brun_fastsurfer.sh` under `ulimit -v 8G` in a subshell. `RLIMIT_AS` is per-process and inherited, so it caps each FreeSurfer binary individually rather than budgeting the pod. Because `memory.oom.group` is set on this container, the single 26 GiB runaway took `argoexec`, `bash` and all four `brun_fastsurfer` instances with it and lost the whole workflow; under the cap the runaway's `malloc` returns NULL, FreeSurfer `ErrorExit`s, and only that one session fails. **8G** is 4.1× the largest per-process max-RSS FreeSurfer's own `FSTIME` wrapper recorded across four *complete* k=4 runs (`267g8`, `28dmm`, `2hpvg`, `2sq7m`): **1.93 GB**. One capped runaway plus k−1 healthy streams (~0.5 G each) stays inside the 13G/18G request at every k. `RLIMIT_AS` bounds *virtual* address space, so the margin holds only while FreeSurfer's VA/RSS ratio stays near 1 — it was 1.06 on the healthy processes above.

**What the FreeSurfer source says** (`mri_normalize.cpp` / `utils/mrinorm.cpp` @ `4d15e6a`). The failing call is `mri_normalize -seed 1234 -mprage -aseg aseg.presurf.mgz -mask brainmask.mgz norm.mgz brain.mgz`, which takes the `if (mri_aseg)` branch at `mri_normalize.cpp:735`. Three findings:

1. **The last log line does not localize the crash.** `mri_normalize` never calls `setbuf`/`setvbuf`, so stdout to the tee'd log is block-buffered and a `SIGKILL` discards whatever is pending. The last unconditional `fflush(stdout)` is at `mri_normalize.cpp:907`, just after the aseg block. So `Applying bias correction` is guaranteed flushed, but everything after line 907 — `MRI3dGentleNormalize`, both `MRI3dNormalize` passes — is invisible. **Do not read the final log line as the crash site**; the death window is "anywhere after 907".
2. **Nothing on this path allocates more than a volume.** `MRInormGentlyFindControlPoints` (`mrinorm.cpp:1096`) is O(1) memory: one 256³ uchar `MRIalloc` plus a fixed 7×7×7 window scan. The only `count`-scaled heap allocations in `mrinorm.cpp` (lines 2852, 2913–15, 2965–67) live in `MRI3dUseFileControlPoints` / `MRI3dUseLabelControlPoints` — the `-f` and label paths, which this command does not take. Everything else is a 256³ MRI, ≤67 MB as float. **Reaching 25.97 GiB needs ~400 simultaneous volume-sized allocations**, which no legitimate branch here performs — consistent with an allocation fault (runaway loop or corruption), not with data-scaled demand.
3. **There is a real leak, but it is two orders of magnitude too small.** `MRIbuildBiasImage` (`mrinorm.cpp:1347`) takes `mri_bias` as an output parameter, then unconditionally does `mri_bias = MRIclone(mri_src, NULL)` at line 1358 — discarding the caller's buffer without freeing it. `MRI3dNormalize:1446` passes `mri_bias` expecting reuse, so every call leaks a full volume. At `num_3d_iter = 2` that is ~134 MB. Real, worth reporting upstream, **not** the 26 GiB.

**Intensity scaling is ruled out; do not add an `mri_info` pre-flight gate.** `mri_info`-level and intensity-statistics checks on all four sessions of `sub-R4PV7WZ3` come back clean: every volume is 256³ uchar (`aseg` int16), range 0–255, no float and no out-of-range values, and the *first* `mri_normalize` in the same pod (`nu.mgz → T1.mgz`) prints `white matter peak found at 110` for all four sessions and exits 0 at ~572 MB max RSS. Measured WM mode is 110/110/109/109 and WM mean 104.5/104.5/103.1/104.5 — a header check would be a no-op. The one real outlier in the victim session (`ses-04A`) is **segmentation extent**, not intensity: 313,587 WM voxels vs 404–441k in its siblings, and 1211 control points removed vs 292/593/366.

The container also emits cgroup v2 `memory.peak` and a 30 s `anon`/`file` split to the archived log, so the next batch replaces the sizing table above with a figure that separates real demand from reclaimable page cache (`pod_memory_working_set` folds the two together, making those numbers an upper bound).

**The runaway is now identified: a garbage histogram bin count.** Re-running `sub-R4PV7WZ3` under the cap on 2026-08-12 (49m48s, container `memory.peak` **10.95 G** against the 18G request, no OOM kill, workflow `Succeeded`) produced the error the `SIGKILL` had been destroying:

```
ses-04A: 3d normalization pass 1 of 2
ses-04A: white matter peak found at 110
ses-04A: error: Cannot allocate memory
ses-04A: error: HISTOalloc(2001892225): could not allocate histogram
ses-04A: recon-all -s ses-04A exited with ERRORS
```

`HISTOalloc` was asked for **2,001,892,225 bins**. That is the 25.97 GiB. So the reading above needs one correction: finding 2 ("no legitimate branch allocates this") stands and is in fact *why* — the bin count is nonsense, not a large-but-legitimate volume — but the failure is **deterministic and specific to this session's data**, reproducing at the same point on a clean re-run, and it is localized to the 3d-normalization pass rather than to the "anywhere after line 907" window of finding 1. Making the process die by `ErrorExit` instead of `SIGKILL` is what made it legible: the cap bought a diagnosis, not just containment. The remaining question is which computation feeds `nbins`; `ses-04A`'s segmentation-extent outlier (313,587 WM voxels vs 404–441k in its siblings) is the obvious suspect.

**Per-session completion guard (#245).** The same re-run exposed a second defect: `brun_fastsurfer.sh` reported **`exit=0`** while `ses-04A` had errored out. Since `check_fastsurfer_derivatives` (step 7) is a bare `head_object`, an exit-0 step publishes a truncated `ses-04A_templated.tar.gz` — 66 entries, `scripts/IsRunning.lh+rh` present, no `lh.white`/`rh.white`/`lh.pial`/`rh.pial` — and that tarball marks the subject **permanently complete**. The anatomical phase is then skipped on every future submission, the subject can never self-heal, and registration and func-preproc run against a `surf/` missing its principal outputs.

So the container now validates each session before Argo can publish it, rejecting any that has an `IsRunning` marker or is missing a non-empty white/pial surface per hemisphere, and **deleting the directory**. Every per-session output artifact is `optional: true`, so a deleted directory means the artifact is skipped and the derivative is genuinely absent — the existence check then correctly reports the session missing and the next submission re-runs the phase. Self-healing is restored by making the failure legible, not by adding a second source of truth. Rejected sessions are also pruned from `base-tps` (written from the template's timepoint list, not from what is on disk), so one bad session does not fail `segment-subregions` for the whole subject. If *no* session survives, the step exits **1** — not 75 — because a deterministic allocation failure re-run is an identical failure, the same reasoning that keeps 137 out of the retry expression (#115).

This is the output-side counterpart to the input-side `find "$SD" -name '*IsRunning*' -delete` guard: the same marker, applied to what the pod produces rather than to what it inherits.

**`--threads 2` per instance is a hard floor.** `recon-surf.sh:319-321` branches rather than scales:

| `--threads` | `threads_hemi` | hemispheres |
|---|---|---|
| >1 | `threads/2` | parallel |
| 1 | 1 | **serial** |

So `--threads 1` does not halve the thread count, it removes hemisphere parallelism and roughly **doubles** the surface stage — on the pipeline's longest step, visible only as wall time. `--parallel` does not rescue it: `run_fastsurfer.sh` consumes its own `--parallel` as the obsolete `legacy_parallel_hemi` and never forwards it, and `brun_fastsurfer.sh --parallel` is subject-level. Both parcellation containers clamp to 2 and log loudly rather than take that branch.

This floor is why the CPU cut (`6 → max(4, 2k)`, issue #103) only wins at k≤2 and leaves k≥3 untouched. The observed `cpu_efficiency` 0.473 is **not** an oversized request — staging is only 1–2% here, so the idle cores are real, but they are algorithmic: `recon-surf.sh` alternates hemisphere-parallel sections with long serial joint sections, so a k=3 pod oscillates between 3 and 6 busy cores. Trimming to that average starves the parallel sections without reclaiming the serial ones.

Known wart: k=4 requests 8 and so needs a 4xlarge — and that is **32.2% of subjects**, not an edge case. Session counts across the 11,830 subjects in `tools/nss_volumes.csv` (a BOLD-derived proxy for the T1w session count):

| k | share | cpu | node |
|---|---|---|---|
| 1 | 12.3% | 4 | 2xlarge |
| 2 | 21.0% | 4 | 2xlarge |
| 3 | 34.5% | 6 | 2xlarge |
| 4 | 32.2% | 8 | **4xlarge** |

No packing trick is available: `cpu-heavy-nodepool` admits only 2xlarge and 4xlarge, and 8000m exceeds a 2xlarge's 7910m *allocatable* outright — before the ~685m of daemonsets that leave ~7225m schedulable. Trimming daemonsets cannot help; the constraint is structural.

Nor is the request oversized — a k=4 pod runs 4 instances × 2 threads = 8 threads of real work. The only waste is stranding (8000m held on ~15205m schedulable), and the ~7.2 spare cores are schedulable by other pipeline pods whenever the batch has pending work. `consolidationPolicy: WhenEmpty` means Karpenter won't repack afterwards, so the loss is real only at the tail of a batch. This is a co-packing **density** question for #75 — levers are batch shape and instance floor, not a thread cut.

This is the one anatomical step whose ephemeral storage scales, because it is the only one holding the template, all k session directories, k parallel sets of surface intermediates and the output tarballs simultaneously.

After `brun_fastsurfer.sh` completes, per-session QC is extracted via `extract_qc.py` for each session (non-fatal). Then `long_compat_segmentHA.py` runs — despite its name it performs **no** segmentation. It is a compatibility bridge that adds the files and links FreeSurfer's longitudinal model expects, and it writes the plain `base-tps` timepoint list that the `subregion-segmentation` phase hard-depends on. Its failure is **not** swallowed: if `base-tps` is missing the step exits 75 (`EX_TEMPFAIL`) to force a retry, rather than promoting an unusable template. Hippocampal/amygdala subfield segmentation itself runs later, in `subregion-segmentation`.

S3 outputs (one tarball per session plus the long-template):
```
derivatives/fastsurfer/{subjID}/{subjID}_{ses}_templated.tar.gz   (ses-00A always; ses-02A–ses-10A optional)
derivatives/fastsurfer/{subjID}/{subjID}_long-template.tar.gz
```
Plus a QC record per session (optional) in the **metrics bucket**:
```
metrics/anat-qc/dt={date}/{subjID}_{ses}_anat_qc.json
```

Failure modes:
- Scratch input missing → the producing step (B or C) failed or its `scratch/{workflow.name}/anat/` objects aged out of the 7-day lifecycle rule; check the upstream pod, not storage
- Pod evicted with `ephemeral-storage` pressure → a subject with more sessions than the `12 + 4k` budget anticipated; check `kubectl describe pod` for the eviction reason before raising it
- FastSurfer crash on a specific session T1w → inspect pod logs; T1w may be unusable
- `long_compat_segmentHA.py` fails to produce `base-tps` → exits 75 (`EX_TEMPFAIL`) to force a retry of the anatomical phase (usually missing base-template white surfaces)

---

### Phase 3b — Anatomical QC (`fsqc-metrics`)

**`fsqc-metrics-template`**
(image: `cloudpipe/fsqc`, node: `cpu-light-nodepool`)

Runs [fsqc](https://github.com/Deep-MI/fsqc) over the finished anatomical derivatives to produce a per-session quality record. It depends on **both** the anatomical and subregion branches reaching a terminal state, but it is **non-fatal by construction**: the master DAG sets `failFast: false` and no task depends on this one, so a QC failure cannot hold back registration, functional preprocessing, or the derivatives.

Reads from the derivatives bucket:
- `derivatives/fastsurfer/{subj}/{subj}_{ses}_templated.tar.gz` (all sessions)
- `derivatives/subregions/{subj}/{subj}_{hippoamyg,hypothalamic}.tar.gz`

Deliberately **not** read: the ~480 MB long-template tarball (nothing enabled reads it), nor the thalamus/brainstem/sclimbic tarballs.

Writes to the **metrics** bucket — this step produces QC records, not derivatives:
```
metrics/fsqc-qc/dt={date}/{subj}_{ses}_fsqc_qc.json
fsqc/{subj}/{ses}/*.png              (hippocampus + hypothalamus overlays, whole-brain screenshots)
fsqc/{subj}/fsqc-results.html        (browsable summary across the subject's sessions)
```

The `dt` partition comes from `{{workflow.creationTimestamp}}`, **not** the pod's own clock. A workflow starting at 23:58Z would otherwise land its QC record in a different partition than the `workflow_runs` row describing it.

**There is no skip gate — recomputation *is* the idempotency mechanism.** Each run overwrites the record for that subject+session, so the stored metrics always describe the derivatives currently in S3. A `metrics-exist` check modelled on `subregions-exist` cannot work here: that check reads a deterministic flat key, whereas an `FsqcQC` record lives under `dt={date}` and a checker has no way to know which partition a prior run used.

Resources: 5G memory request / 6G limit, 2 CPU, 4G ephemeral-storage request / 6G limit. Measured 2026-08-07 from cgroup v2 `memory.peak` against live derivatives: 3.93 G at 3 sessions and 3.97 G at 4. Memory and disk scale on **different** axes — a third more sessions moved peak memory by 1% but disk by 24% — because fsqc loops sessions and frees between them (peak is set by the largest *single* session) while every extracted tree is retained on disk for the outlier module's cross-session comparison. Sized for the 6-session maximum of the ABCD longitudinal design. Setting a memory limit is safe here precisely *because* peak is flat in session count, unlike the subregion pods where the limit was omitted deliberately.

It runs on `cpu-light-nodepool` rather than `cpu-heavy`: at 5G/2cpu it is almost exactly the shape of `surface-resample`, and `cpu-heavy` is c/m-family 2xlarge/4xlarge, badly oversized for ~1.5 cores of real work.

Note this image is **not** a FastSurfer derivative — it is a ~930 MB `python:3.10-slim` build, because fsqc never shells out to FreeSurfer binaries. See [images.md](images.md#fsqc).

---

### Phase 4 — Per-session processing

Runs once per session discovered by inventory. All sessions start simultaneously (subject to the master `parallelism: 3` limit on the master DAG). Each session runs two sub-DAGs in sequence: registration then functional preprocessing.

`session-level-pipeline-dag-template` receives per-session parameters from the inventory `withParam` fan-out: `session`, `runs`, `nss-frames`, `t1w-to-mni-exists`.

#### Registration

`registration-dag-template` orchestrates the registration phase per session. `t1w-to-mni` and `bold-to-t1w` run in parallel:
- `t1w-to-mni-step` is skipped when `t1w-to-mni-exists == "true"` (set by inventory check 4)
- `bold-to-t1w-step` is fanned out via `withParam` over the runs array; each run is skipped individually when `b2t_exists == "true"` (set by inventory check 3)

---

**`t1w-to-mni-template`**
(image: `cloudpipe/fireants`, node: `gpu-nodepool`)

**Skipped when `t1w-to-mni-exists == "true"`.**

Artifacts in:
- `derivatives/fastsurfer/{subj}/{subj}_{ses}_templated.tar.gz` → `/fastsurfer/{subj}_{ses}/`
- `config/MNI152NLin2009cAsym_T1w_brain_res-2_RAI.nii`

Runs `fst1w_to_mni.py`: affine + SyN registration of `orig.mgz` to MNI152NLin2009cAsym using FireANTs. Uses `orig.mgz` (not the BIDS T1w) to ensure the source space matches the BOLD→T1w transform, which SynthMorph produces referenced to FreeSurfer conformed space (256³ 1mm isotropic). `brainmask.mgz` provides the skull-stripped mask in the same conformed space — no `--like` conversion needed.

`nvidia-smi` streams GPU utilization to stderr every 5 s (background monitor, killed before exit).

Resources: 4G memory, 2 CPU, 1 GPU.

Outputs uploaded individually to S3 (no tar archive — avoids symlinks in output):
```
derivatives/registration/{subj}/{ses}/t1w_to_mni/
  {prefix}_desc-t1w2mni_affine.mat       ← idempotency marker (checked by inventory)
  {prefix}_desc-t1w2mni_warp.nii.gz
  {prefix}_desc-t1w2mni_T1w_brain.nii.gz (QC: skull-stripped orig-space T1w)
  {prefix}_desc-t1w2mni_T1w_orig.nii.gz  (QC)
  {prefix}_desc-t1w2mni_warped.nii.gz    (QC: orig-space T1w warped to MNI)
  {prefix}_desc-t1w2mni_qc.json
```
Plus a QC record in the **separate metrics bucket** (`{{workflow.parameters.metrics-bucket}}`, not the data bucket):
```
metrics/registration/dt={date}/{subj}_{ses}_t1w_to_mni_reg_qc.json
```

On a QC `fail` verdict the script exits 65 **before** promoting its staging
directory, so no `_affine.mat` completion marker reaches S3 and a resubmit
re-registers the session instead of skipping onto a bad transform. Exit 65 is
excluded from the retry expression, so the step fails without retrying. The
`metrics/registration/` QC record is written before that exit and uploads either
way.

Failure modes:
- GPU allocation failure → check `gpu-nodepool` capacity
- Registration diverges → inspect QC image; may indicate a poor-quality T1w
- FastSurfer tarball download fails → anatomical step may not have produced this session's file (check S3)

---

**`bold-to-t1w-session-template`**
(image: `cloudpipe/freesurfer`, node: `cpu-heavy-nodepool`)

**One pod per session**, looping over that session's runs internally. The whole
task is skipped when `b2t_exists == "true"` for every run; within the pod, each
already-complete run is skipped individually. A run that fails is logged and the
loop continues — the step fails only if *every* run failed, because Argo discards
a failed pod's output artifacts and that would throw away the successful runs.

Artifacts in (downloaded once per session, not once per run):
- `mmps_mproc/{subj}/{ses}/func/` → `/data/func/` — whole-prefix directory artifact carrying every run's BOLD + BIDS sidecar
- `derivatives/fastsurfer/{subj}/{subj}_{ses}_templated.tar.gz` → `/tmp/fastsurfer/{subj}_{ses}/`
- `config/fslicense`

Per-run inputs arrive as one directory artifact because Argo resolves
`inputs.artifacts` statically when the pod spec is built — a per-run list cannot
be declared when the run count varies by session.

Runs `bold_to_t1w.py` — **SynthMorph rigid only** (`mri_synthmorph register -m rigid`): contrast-agnostic global alignment of the BOLD reference to the T1w.

**BBR was removed in `61ccff7`.** An earlier revision ran `bbregister --bold` as a second, boundary-based refinement stage against the FastSurfer white surface. Boundary-based registration is the field standard for EPI→T1w (Greve & Fischl 2009), but it depends on detectable gray/white contrast at the boundary, and ABCD's 2.4 mm EPI does not reliably provide it. SynthMorph operates on intensity-normalized images and is contrast-agnostic, so it is the better tool at this resolution ([ADR 002](decisions/002-synthmorph-over-bbregister.md)). Consequently the `bbr_*` QC fields, and the "D7 guard" that parsed bbregister stdout to detect whether the SynthMorph init had been honored, no longer exist.

The LTA output is converted to an ANTs/ITK `.txt` affine for `antsApplyTransforms` during functional preprocessing. The conversion is **hand-rolled in Python**, not `lta_convert --outitk`: that flag crashes with `munmap_chunk` on SynthMorph LTAs (FreeSurfer 7.4.1 heap corruption when `--src`/`--trg` are passed with a geometry-less LTA). The equivalent is a basis flip, `A_lps = D·R·D` and `t_lps = D·t` with `D = diag(-1,-1,1)`, and **no inversion** — ITK affines are pullbacks (fixed → moving) and the SynthMorph matrix already is one.

Resources: 3G memory request / **12G** limit, 2 CPU, 20G ephemeral-storage request / 30G limit. Memory was 16G request = 16G limit on the stale BBR rationale but used only ~1.3 GB (`ram_efficiency` 0.083 across 25 pods), so [#97](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/97) cut it — then a 6G limit OOM-killed 27 pods in one batch, because a lifetime *average* cannot see the peak. The peak turned out to be a property of the **host**, not the workload: pinning instance types gave 8.67 G anon on `c6i.4xlarge` (Ice Lake) against 2.90–4.57 G elsewhere, and `cpu-heavy-nodepool` pins no family. The limit is that peak plus ~38%. Setting `TF_ENABLE_ONEDNN_OPTS=0` then cut the peak to 3.87 G with byte-identical output ([#120](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/120)); the headroom is retained anyway. `mri_synthmorph` is TensorFlow-based and previously had no thread cap, so it sized its pool from the host core count and consumed ~3.3 cores against a 2-core request; `OMP_NUM_THREADS`, `TF_NUM_INTRAOP_THREADS`, `TF_NUM_INTEROP_THREADS` and `ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS` are now all projected from `requests.cpu` via the downward API, so CPU request and thread count cannot diverge. Memory is sized for a single run, since runs execute sequentially (`jobs: "1"`).

> **Known gap.** The request is deliberately left at 3G because requests drive bin-packing and limits do not, so the headroom costs nothing in node size. But on Ice Lake the step can really use ~8.67 G against that 3G request, so the scheduler under-counts it — a co-tenant eviction risk, tracked in [the OOM investigation](https://github.com/jrussell9000/cloudpipe/blob/main/docs/investigations/2026-08-01-bold-to-t1w-oom-handoff.md) §9.8.1.

Outputs uploaded to S3 as one directory artifact keyed on
`derivatives/registration/{subj}/{ses}/`. The driver names each run's directory
`bold_to_t1w_{task}_{run}`, so the resulting keys are unchanged:
```
derivatives/registration/{subj}/{ses}/bold_to_t1w_{task}_{run}/
  {prefix}_desc-bold2t1w_itk.txt         ← idempotency marker (checked by inventory)
  {prefix}_desc-bold2t1w_brainmask.nii.gz
  {prefix}_desc-bold2t1w_ref.nii.gz      (QC: BOLD reference volume)
  {prefix}_desc-bold2t1w.lta             (SynthMorph LTA transform)
  {prefix}_desc-bold2t1w_warped.nii.gz   (QC: BOLD ref in T1w space)
```
Plus a QC record in the **metrics bucket**:
```
metrics/registration/dt={date}/{subj}_{ses}_{task}_{run}_bold_to_t1w_reg_qc.json
```

Failure modes:
- BOLD NIfTI not found → transfer missed this run; verify S3 key
- Missing `mri/T1.mgz` or `mri/brainmask.mgz` → FastSurfer tarball incomplete
- `mri_synthmorph` non-zero exit → check pod logs
- Exit 137 → OOM; see the host-dependent peak above before raising the limit
- `nmi: 0.0` in the QC record → early-exit failure path (NMI ≥ 1 for real tissue). Note `nmi ≈ 1.02` is a **good** score here, not a failure: identity scores ~1.011, so the theoretical 1.0–2.0 range is not the operating scale

---

#### Functional preprocessing

Runs after all registration steps for the session complete (depends on `registration-dagtask.Succeeded`). **One pod per session**, looping over all `(task, run)` pairs discovered by inventory.

**`functional-preprocessing-session-template`**
(image: `cloudpipe/afni`, node: `cpu-heavy-nodepool`)

The whole task is skipped when `func_exists == "true"` for every run; within the
pod, each already-complete run is skipped individually. As with bold-to-t1w, a
failing run does not abort its siblings.

Artifacts in (downloaded once per session, not once per run):
- `mmps_mproc/{subj}/{ses}/func/` → `/data/func/` — whole-prefix directory artifact carrying every run's BOLD, BIDS sidecar and motion params
- `derivatives/registration/{subj}/{ses}/` → `/tmp/registration/` — whole-prefix directory artifact carrying `t1w_to_mni/` (shared by every run) and each `bold_to_t1w_{task}_{run}/`
- `config/MNI152NLin2009cAsym_T1w_brain_res-2_RAI.nii`
- `derivatives/fastsurfer/{subj}/{subj}_{ses}_templated.tar.gz` → `/tmp/fastsurfer/{subj}_{ses}/` (for `mri/aseg.auto.mgz`)

Collapsing the fan-out is what makes the FastSurfer tarball, MNI template and
t1w→MNI warp a single download per session rather than one per run — see
[the cost deep-dive §3.2](https://github.com/jrussell9000/cloudpipe/blob/main/docs/investigations/2026-07-20-cloudpipe-cost-reduction-deep-dive.md).

What `preproc.py` does (its own stage numbering):
1. **Extract 3D BOLD reference** — mean of the `--nss-frames` leading volumes (nibabel)
2. **Precompute composite displacement field** — BOLD→T1w (ITK affine) + T1w→MNI (affine + SyN warp) collapsed into a single voxel-to-voxel map via `antsApplyTransforms -o [displacement_field]`
3. **Warp unmasked BOLD → MNI** — scipy `map_coordinates`, cubic B-spline (`order=3`); thread count from the pod's CPU request
4. **Warp brain mask → MNI** — same `map_coordinates` call reusing stage 3's coordinate array, nearest-neighbour (`order=0`) to preserve binary values
5. **Apply the MNI-space mask to the MNI-space BOLD** — AFNI `3dcalc`
   - **5b. Grayordinate extraction** — see below
6. **Confound estimation** — motion params + derivatives + powers, FD, DVARS, global signal, aCompCor (WM/CSF from `aseg.auto.mgz`), tCompCor, cosine regressors, NSS outlier indicators

Three things about that order are deliberate and easy to get wrong:

**Non-steady-state frames are flagged, not trimmed.** The leading volumes are used to build the stage-1 reference and then marked with one binary `non_steady_state_outlier_NN` column per frame. Nothing is removed, so the output frame count matches the input and stays aligned with task-timing files; excluding them is the analyst's decision in the GLM.

**Masking happens after the warp, not before.** A hard binary mask applied in native space creates a steep edge that sinc-family interpolation treats as high-frequency content, producing Gibbs ringing just inside and outside the cortex. So the *unmasked* BOLD is warped, the mask is warped separately with nearest-neighbour, and stage 5 zeroes non-brain voxels in MNI space.

**There is no bandpass filter and no spatial smoothing.** Neither stage exists — outputs are unsmoothed and unfiltered by design. High-pass filtering is available *implicitly* instead, as a discrete cosine basis in the confounds TSV (1/128 Hz, `floor(2·T·TR/128)` regressors, the fMRIPrep convention), so drift is removed inside the analyst's model rather than baked into the data. Slice timing correction is likewise absent, and deliberately: ABCD ships neither a `SliceTiming` sidecar field nor the NIfTI header slice fields, so the stage that once existed here detected no timing on every run ever processed and was removed (see [#119](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/119)). It would have to be reinstated as a native-space step *before* this script; it cannot be applied after the stage-3 warp.

**Float16 output rationale:** MNI BOLD is written as float16 NIfTI (`DT_FLOAT16`, datatype 512). This halves the in-memory allocation (~6.3 GB vs ~12.6 GB for a 400-frame run). FSL, AFNI, and FreeSurfer all upcast float16 to float32 on load. Confound regressors are derived entirely from native-space float32 data. Quantization error at typical BOLD baseline (~1000 units) is ~0.5 units, negligible for GLM/FC/ICA analyses. tSNR is computed with float32 accumulators before writing (required to avoid overflow when summing 300+ frames).

Resources: 4G memory request, 6G limit (TODO(perf): validate limit empirically against observed peak usage — intermediate ANTs warp operations may exceed 6G on longer runs; check `metrics/func-preproc` QC JSONs for `peak_memory_gb` before adjusting), 2 CPU, 20G ephemeral-storage request, 30G limit. CPU was cut 6 → 3 in [#96](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/96) — mean `cpu_efficiency` across 26 pods was 0.347, ~2.1 of 6 cores — then 3 → 2 after a live re-measurement over 65 pods mid-batch at 200 concurrent (2026-08-11) showed median 1.15 CPU and p90 3.19, i.e. 38% of the request, the step being largely S3-I/O-bound. At 2 CPU three pods share a `c*.2xlarge` where only two fit at 3. The request must stay an integer: `resourceFieldRef` rounds a fractional CPU request up, which would decouple the thread count from the reservation. The driver derives `--threads` and `ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS` from the pod's own CPU request (downward API `resourceFieldRef`, env `CPU_REQUEST`) rather than a hardcoded constant, so the request is the single source of truth and cannot drift from the thread count. Memory is sized for a single run, since runs execute sequentially (`jobs: "1"`); ephemeral storage grew because the pod holds the session's whole `func/` and `registration/` prefixes plus the extracted FastSurfer tree. The driver deletes each run's working directory once it is tarred, so intermediates do not accumulate across runs.

`ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS` is set by the driver per run (to `CPU_REQUEST // jobs`, floored at 1) rather than on the container, so raising `jobs` splits the CPU request across concurrent runs instead of letting each grab the whole request and over-subscribe the node. `jobs` is `1`, and with the CPU request now at 2, `jobs=2` already leaves one thread per run and anything above 2 floors at 1 while still multiplying peak memory.

Retry limit: 8, as a per-template override of the same spec-level budget. This is the pipeline's bottleneck step and it runs on the spot-only `cpu-heavy-nodepool`, making it the most reclaim-exposed pod in the workflow — two back-to-back reclaims on one step were observed 2026-08-03 ([#115](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/115)). The policy is `Always` with an expression filtering to infrastructure causes; `OnFailure` only half-honoured that filter, since a node shutdown lands in phase `Failed` but a reclaimed pod lands in phase `Error`. A retry re-runs the whole session, so a reclaim costs a session's in-flight work rather than a single run's.

S3 output — one tarball per run, built by the driver (`tar` rooted at `{task}_{run}/`) and uploaded via a single directory artifact keyed on `derivatives/func/{subj}/{ses}/`. The filename is inventory's `func_exists` marker, so it is unchanged:
```
derivatives/func/{subj}/{ses}/{subj}_{ses}_{task}_{run}_space-MNI152NLin2009cAsym_bold.tar.gz
```

#### Grayordinate extraction (Stage 5b)

The same pod also reduces each run to its CIFTI grayordinate components, between the MNI masking and confound stages. It is co-located rather than given its own pod because every input it needs is already on local disk here — the FastSurfer surfaces, the BOLD→T1w transform, the native BOLD, and the MNI BOLD it just produced. See openspec change `add-surface-func-processing`.

- **Cortical** — `n` sample points per vertex across the ribbon between `surf/?h.white` and `surf/?h.pial` (default 5, endpoints excluded), mapped tkrRAS → scanner RAS → BOLD voxel and trilinearly interpolated from the **native** BOLD, then averaged across depths. Sampling native rather than the MNI output avoids resampling already-resampled data. Output: per-hemisphere `_hemi-{L,R}_space-fsnative_bold.func.gii`.
- **Subcortical** — `aseg.auto.mgz` is warped to MNI (nearest-neighbour), restricted to the 19 standard CIFTI structures, and used to mask the MNI BOLD. Because the BOLD is already on that grid this is pure indexing, no second interpolation. Output: a 4D volume, an integer label volume, and a `wb_command -volume-label-import` label list.

Volume space for the subcortical block is `MNI152NLin2009cAsym`, matching the rest of the pipeline rather than the `MNI152NLin6Asym` grid that standard 91282-grayordinate files use. cloudpipe does not exchange dense CIFTIs with the DCAN/ABCD-BIDS ecosystem, so matching that grid would have cost a second per-session registration for no benefit.

```
derivatives/func_surf/{subj}/{ses}/components/{subj}_{ses}_{task}_{run}_desc-grayordcomponents_bold.tar.gz
```

**Independent gating.** `func_exists` and `surf_exists` are separate inventory markers, and `--emit` picks the cheapest path producing whatever is absent:

| `func_exists` | `surf_exists` | `--emit` | What runs |
|---|---|---|---|
| false | false | `both` | Stages 0–6 + extraction |
| true | false | `grayordinate` | Stages 0–1 + extraction; **skips the composite warp and 4D warp**, recovering the MNI BOLD from the existing tarball |
| false | true | `volumetric` | Stages 0–6, extraction skipped |
| true | true | — | run skipped entirely |

The short path is why `derivatives/func/{subj}/{ses}` is an `optional: true` input artifact: it is read only when an MNI BOLD must be recovered, and is empty on a fresh session.

Outcome is recorded per run under the `surface-sample` step by a second `record-step-outcome` fan-out on the same DAG task — Stage 5b has no Argo task of its own, so the two recorders are distinguished by which S3 marker each verifies.

The pod also exports the session's surface geometry once (`sphere.reg` and a derived midthickness, as GIFTI) to `derivatives/func_surf/{subj}/{ses}/anat/`. That is what lets the resample step below avoid downloading the ~2 GB FastSurfer tarball. Its failure is non-fatal: the per-run components stay valid, and the resample step simply does not run until the geometry exists.

---

#### Surface resampling and CIFTI assembly

**`surface-resample-session-template`**
(image: `cloudpipe/workbench`, node: `cpu-light-nodepool`)

**One pod per session**, looping over runs. Depends on the func-preproc task rather than on registration, since it consumes the grayordinate components that task produces. Skipped when every run already has its dtseries.

This is the one stage that is genuinely cheap to isolate, because Stage 1 already reduced its inputs: it reads vertices × frames and the subcortical block, never the raw BOLD, the MNI BOLD, or the FastSurfer tarball.

Artifacts in:
- `derivatives/func_surf/{subj}/{ses}/components` → `/data/components`
- `derivatives/func_surf/{subj}/{ses}/anat` → `/data/anat`
- `config/fsLR` → `/data/meshes`

Steps per run, all `wb_command`:
1. `-metric-resample` fsnative → fsLR 32k along `sphere.reg`, `ADAP_BARY_AREA` with midthickness area correction. ADAP_BARY_AREA rather than BARYCENTRIC because downsampling ~150k native vertices to 32k should use all the data, not just the enclosing triangle.
2. `-volume-label-import` turns Stage 1's integer labels + label list into a Workbench label volume.
3. `-cifti-create-dense-timeseries` combines cortex and subcortex. TR is read from the subcortical block's NIfTI header and passed as `-timestep`; without it the dtseries would claim a 1 s TR and every frequency-domain analysis downstream would be silently wrong.

S3 output:
```
derivatives/func_surf/{subj}/{ses}/fsLR32k/{subj}_{ses}_{task}_{run}_space-fsLR32k_bold.dtseries.nii
```

**Grayordinate count.** With the atlas ROI applied the cortical component is the standard 59412 vertices, but the subcortical block is on `MNI152NLin2009cAsym`, not the `MNI152NLin6Asym` grid that standard 91282-grayordinate files use — so the total will not be 91282. That is intended, not a defect: cloudpipe does not exchange dense CIFTIs with the DCAN/ABCD-BIDS ecosystem, and matching that grid would have cost a second per-session registration for no benefit.

**Static mesh staging (manual prerequisite).** The fsLR meshes come from the HCP `standard_mesh_atlases` package and must be staged under `s3://{bucket}/config/fsLR/` before this step can run. The template takes their filenames as parameters, templated on `{hemi}`, so restaging under different names is a parameter change rather than a code change:

| Parameter | Default |
|---|---|
| `target-sphere` | `fs_LR-deformed_to-fsaverage.{hemi}.sphere.32k_fs_LR.surf.gii` |
| `target-area` | `fs_LR.{hemi}.midthickness_va_avg.32k_fs_LR.shape.gii` |
| `area-mode` | `metrics` (`surfs` if your staging supplies a 32k midthickness surface) |
| `target-roi` | `{hemi}.atlasroi.32k_fs_LR.shape.gii` (empty string keeps all 32492/hemisphere) |

Get them from `global/templates/standard_mesh_atlases/` in [HCPpipelines](https://github.com/Washington-University/HCPpipelines) — the `resample_fsaverage/` subfolder is the fsaverage↔fsLR bundle. Confirm the exact filenames against the download; the defaults follow that package's naming but are not a guarantee.

Two traps worth stating explicitly:

- **`target-sphere` must be the `fs_LR-deformed_to-fsaverage` variant.** FreeSurfer's `sphere.reg` registers into *fsaverage* space, so a plain fsLR sphere (e.g. TemplateFlow's `tpl-fsLR_hemi-L_den-32k_sphere.surf.gii`) is not in register with it. Both are valid spheres, so `wb_command` will resample happily and emit anatomically scrambled output that looks entirely normal.
- **`area-mode` defaults to `metrics` because the pack ships vertex-area *metrics*, not surfaces.** `-area-surfs` given a `.shape.gii` fails with `Number of data arrays MUST be two in a SurfaceFile`. In `metrics` mode the subject side is converted from the exported midthickness with `wb_command -surface-vertex-areas`.

Failure modes:
- **Session geometry missing** → the step fails fast rather than once per run; check whether Stage 5b's geometry export warned.
- **A run's components absent** → that run is logged and skipped; Stage 1 failed for it alone.
- **`-metric-resample` sphere mismatch** → `sphere.reg` and the target sphere are not in register; verify the staged mesh is the `fs_LR-deformed_to-fsaverage` variant, not a bare fsLR sphere.

**Standalone submission** (for testing or reprocessing). `runs` is the JSON array
inventory produces; pass a one-element array to reprocess a single run:
```bash
argo submit --from workflowtemplate/functional-preprocessing \
  -n argo-workflows \
  -p subjID=NDARINVXXXXXXXX \
  -p session=ses-00A \
  -p runs='[{"task":"task-rest","run":"run-01","func_exists":false}]' \
  -p nss-frames=15
```

Failure modes:
- `aseg.auto.mgz` not found → FastSurfer segmentation corrupt; re-run anatomical phase
- `antsApplyTransforms` crash → transform files incomplete; re-run registration for this session
- OOM → memory limit hit; check `peak_memory_gb` in QC JSON; may need to increase limit after validation

Not a failure mode, though it used to look like one (issue #222). `preproc.py`
checks its required inputs **before** Stage 1 and exits `66`
(`EXIT_MISSING_INPUT`) if one is absent, logging `[preproc] MISSING INPUT` and
`[preproc] SKIPPING <prefix>`; the driver pre-checks the run's own
`bold_to_t1w_{task}_{run}/` outputs and does not launch it at all:

```
[driver] SKIPPED sub-VKLJZA5A_ses-06A_task-rest_run-01 — no bold_to_t1w transform (QC-rejected upstream): ..._desc-bold2t1w_itk.txt
```

That is the expected outcome when `bold-to-t1w` rejects a run on its QC floor
(exit `65`) and its driver discards the transform. The run records
`status="skipped"` with `upstream_failed_step="bold-to-t1w"` and
`failure_category="dependency"`, and it is excluded from both the driver's
failure counts and its exit rule — so a session whose every run was gated
upstream still exits 0 rather than reporting a func-preproc failure. Exit `66` is
deliberately absent from the `retryStrategy` expression: a missing upstream
artifact will not appear on a retry.

---

### Phase 5 — Observability and cleanup

#### Outcome recording (`record-step-outcome`)

After each substantive pipeline step, a sibling DAG task invokes `outcome-recorder` (WorkflowTemplate `outcome-recorder`, template `record-step-outcome`). Phase-level recorders depend on the step with `depends: "X || X.Failed || X.Skipped"` so they run regardless of the upstream step's terminal status; per-run recorders whose producer records in-pod are fallbacks gated on `X.Failed || X.Errored || X.Skipped`. Failure of the outcome recorder does not propagate — it uses `continueOn: {failed: true, error: true}`.

`step` is accompanied by an optional `step2` parameter: steps that share a producer task and gate and differ only by name are recorded by a single pod (`func-preproc` + `surface-sample`). Each still gets its own output verification and record.

Runs on `cpu-light-nodepool`, 128M memory, 100m CPU.

It takes **two** bucket parameters, for different purposes: `bucket` is the data bucket, where it verifies the step's outputs actually landed; `metrics-bucket` is where the record itself is written.
```
s3://{metrics-bucket}/metrics/step-outcomes/dt={date}/{workflow-name}__{step}__{subject}__{session}__{task}__{run}_outcome.json
```

Steps covered (step name values recorded):

| Step value | Grain |
|---|---|
| `fastsurfer-template` | subject (covers template creation **and** segmentation — one pod since the anatomical phase moved off shared EFS) |
| `fastsurfer-template-parc` | subject |
| `fastsurfer-long-seg` | session |
| `fastsurfer-long-parc` | session |
| `t1w-to-mni` | session |
| `bold-to-t1w` | run |
| `func-preproc` | run |
| `surface-sample` | run (Stage 5b grayordinate extraction) |
| `surface-resample` | run (fsLR32k assembly) |
| `subregion-segmentation` | subject |
| `fsqc-metrics` | subject |
| `anatomical-phase` | aggregate over all FastSurfer steps |
| `session-phase` | aggregate over all session-level pipeline instances |

`src/validate_test_batch.py::EXPECTED_STEP_NAMES` asserts on the first nine only — the batch-validation gate predates the surface, subregion and fsqc steps and deliberately checks a stable subset rather than the full list. Records predating the EFS removal also carry a retired `fastsurfer-template-seg` step value.

#### Workflow exit handler (`emit-workflow-metrics-template`)

Runs via `onExit` on all terminal statuses (Succeeded, Failed, Error). Defined in the master WorkflowTemplate as a steps template with two steps:

**Step 1 — `workflow-exit-handler`** (WorkflowTemplate `metrics-exit-handler`)

Runs on `cpu-light-nodepool`, 128M memory, 100m CPU. Invokes `exit_handler.py` which:
- Writes a WorkflowRun JSON to `s3://{metrics-bucket}/metrics/workflow-runs/dt={date}/{workflow-name}__{subject}_run_summary.json`
- Assembles a SubjectManifest to `s3://{metrics-bucket}/metrics/subject-manifests/dt={date}/{workflow-name}__{subject}_manifest.json`

> **A `dt` partition mismatch is expected between these two prefixes and the QC ones.** QC records partition on the workflow's **start** date; `workflow-runs` is written by the exit handler and partitions on **finish**. A single-day export window can therefore return one and not the other — widen the window a day on each side.

Query via Athena using the `workflow_runs` and `subject_manifests` tables.

**Step 2 — `delete-globus-input-template`** (runs only when `workflow.status == Succeeded`)

Runs on `cpu-light-nodepool`, 128M memory, 100m CPU. Paginates and deletes all objects under `s3://{bucket}/{globus-dest-base-path}/{subjID}/` using `delete_objects`. This avoids storing raw minimally-preprocessed input files long-term — derivatives are the durable output.

---

### Idempotency and partial reruns

Inventory runs fresh on every submission. Prior step outputs are checked via `head_object` on S3 completion marker keys.

| S3 completion marker | Gates |
|---|---|
| `derivatives/fastsurfer/{subj}/{subj}_{ses}_templated.tar.gz` for **all sessions** | Entire anatomical phase |
| `derivatives/registration/{subj}/{ses}/t1w_to_mni/{subj}_{ses}_desc-t1w2mni_affine.mat` | `t1w-to-mni` for that session |
| `derivatives/registration/{subj}/{ses}/bold_to_t1w_{task}_{run}/{subj}_{ses}_{task}_{run}_desc-bold2t1w_itk.txt` | `bold-to-t1w` for that run |
| `derivatives/func/{subj}/{ses}/{subj}_{ses}_{task}_{run}_space-MNI152NLin2009cAsym_bold.tar.gz` | `func-preproc` for that run |
| `derivatives/func_surf/{subj}/{ses}/components/{subj}_{ses}_{task}_{run}_desc-grayordcomponents_bold.tar.gz` | Grayordinate extraction (Stage 5b) for that run |
| `derivatives/func_surf/{subj}/{ses}/fsLR32k/{subj}_{ses}_{task}_{run}_space-fsLR32k_bold.dtseries.nii` | `surface-resample` for that run |
| `derivatives/subregions/{subj}/{subj}_{region}.tar.gz` for **all five** regions | Entire subregion-segmentation phase |
| `mmps_mproc/{subj}/{ses}/anat/{subj}_{ses}_run-01_T1w.nii.gz` (source, not a derivative) | Whether the session counts toward `fastsurfer_exists` at all |

To force a step to re-run, delete the marker key, then resubmit:

```bash
# Force re-run of anatomical phase for a subject
aws s3 rm --recursive s3://<YOUR_S3_BUCKET>/derivatives/fastsurfer/NDARINVXXXXXXXX/

# Force re-run of t1w-to-mni for one session
aws s3 rm s3://<YOUR_S3_BUCKET>/derivatives/registration/NDARINVXXXXXXXX/ses-00A/t1w_to_mni/NDARINVXXXXXXXX_ses-00A_desc-t1w2mni_affine.mat

# Force re-run of bold-to-t1w for one run
aws s3 rm s3://<YOUR_S3_BUCKET>/derivatives/registration/NDARINVXXXXXXXX/ses-00A/bold_to_t1w_task-rest_run-01/NDARINVXXXXXXXX_ses-00A_task-rest_run-01_desc-bold2t1w_itk.txt

# Force re-run of func-preproc for one run
aws s3 rm s3://<YOUR_S3_BUCKET>/derivatives/func/NDARINVXXXXXXXX/ses-00A/NDARINVXXXXXXXX_ses-00A_task-rest_run-01_space-MNI152NLin2009cAsym_bold.tar.gz

# Force re-run of surface-resample for one run (leaves the grayordinate
# components in place, so only the fsLR32k assembly repeats)
aws s3 rm s3://<YOUR_S3_BUCKET>/derivatives/func_surf/NDARINVXXXXXXXX/ses-00A/fsLR32k/NDARINVXXXXXXXX_ses-00A_task-rest_run-01_space-fsLR32k_bold.dtseries.nii
```

> **QC metrics live in a different bucket and are not deleted by any of the above.** The derivative keys are in `<YOUR_S3_BUCKET>`; the QC records are under `metrics/` in the versioned `cloudpipe-metrics` bucket. Re-running a step appends a new QC record rather than replacing the old one, so a re-run subject legitimately has more than one record per run — see [observability.md](observability.md).

Then resubmit with the standard `argo submit` command.

---

## cloudpipe_fullproc

**Status: planned — design only. No WorkflowTemplates exist for this pipeline; `argo/workflows/` contains only `cloudpipe_minproc/` and `fmri_first_level_proc/`. Do not submit workflows or create templates for it unless explicitly asked to implement it.**

The design: process raw ABCD DICOMs from scratch, applying preprocessing steps that are done upstream in cloudpipe_minproc: dcm2niix → despiking → STC → motion correction → SDC (FSL topup) → between-scan motion correction → then the same registration + func-preproc as cloudpipe_minproc. Gradient nonlinearity correction would be omitted (proprietary manufacturer files unavailable).

Intended to use the POSIX staging approach (globus-s3-sync always runs). Planned WorkflowTemplate name: `cloudpipe-fullproc`.

---

## subregion-seg

Subcortical subregion segmentation. Runs as a phase **inside** the `cloudpipe_minproc` DAG, and is also submittable standalone against a subject whose FastSurfer longitudinal outputs already exist.

WorkflowTemplate name: `subregion-seg`, entrypoint `subregion-seg-dag-template`.

### Where it runs in the main DAG

`subregion-segmentation-dagtask` in `cloudpipe-long-master-workflow-template.yaml`:

- `depends`: `anatomical-processing-pipeline-dagtask.Succeeded || .Skipped` — the same anatomical gate as the session-level branch, so the two run in parallel.
- `when`: `subregions-exist == "False"` — skipped entirely when all five output tarballs are already in S3.

A downstream `record-outcome-subregion-seg-dagtask` records the phase outcome on every terminal status, including `Skipped`.

### Prerequisites

FastSurfer longitudinal outputs must be in S3 — both segmentation pods read them from there directly:
```
derivatives/fastsurfer/{subjID}/{subjID}_long-template.tar.gz
derivatives/fastsurfer/{subjID}/{subjID}_ses-00A_templated.tar.gz
derivatives/fastsurfer/{subjID}/{subjID}_ses-02A_templated.tar.gz   (optional)
...
```

### Standalone submission

```bash
argo submit --from workflowtemplate/subregion-seg \
  -n argo-workflows \
  -p subjID=NDARINVXXXXXXXX \
  -p bucket=<YOUR_S3_BUCKET> \
  -p ecr-registry=public.ecr.aws/l9e7l1h1 \
  -p T1w_sessions='["ses-00A","ses-02A"]'
```

There is no longer a `fastsurfer-exists` parameter, and no `hydrate` step. Both used to gate/perform staging of FastSurfer outputs onto the shared EFS volume; both are gone as of [#77](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/77) — each segmentation pod now declares the FastSurfer S3 tarballs as its own input artifacts and extracts them onto a private `emptyDir`, so standalone submission needs no extra flag either way.

`T1w_sessions` controls which S3 artifact inputs are declared. Sessions beyond `ses-00A` are declared `optional: true`; the template reads `base-tps` from the long-template tarball at runtime to determine which timepoints to actually process.

Workflow-level settings: `serviceAccountName: argo-workflows-runner`, `securityContext: runAsNonRoot / runAsUser 1000 / fsGroup 1000`, retry limit 3. No PVC — the master workflow no longer declares `volumeClaimTemplates`.

This was formerly the **last consumer of the EFS PVC**. `segment-subregions-gems-template`'s and `segment-subregions-dl-template`'s per-region resume guards now check S3 directly (see "Resume guards" below) instead of a retry-persistent volume, which is what let the PVC come out — and let the EFS filesystem, StorageClass, and CSI driver be removed from the cluster entirely.

### Structure: gems ∥ dl

Two templates, running **concurrently**, each independently staging its own copy of the FastSurfer outputs (no shared pod, no hydrate step):

```
  segment-gems  (~50 min, 4.5G)
  segment-dl    (~1 min, 13G)
```

Artifact inputs are statically declared, since Argo requires all artifact paths to be known at template definition time. The DAG sets `failFast: false`, so one pod failing does not cancel the other — they produce independent derivatives, and a DL failure should not discard 50 minutes of GEMS work.

**`segment-subregions-gems-template`** — thalamus, brainstem, hippo-amygdala. Image `cloudpipe/freesurfer`, node `cpu-heavy-nodepool`, **4.5G / 3.5 CPU / 5G ephemeral-storage** (no memory limit). No TensorFlow is imported in this pod at all. The CPU request is deliberately half a core *below* the tool's `--threads 4`: at 4 no two pods fit on a `c*.2xlarge` (7.21 CPU usable after the DaemonSet tax), which is why 83 of 148 `cpu-heavy` nodes were carrying a single workflow pod when measured at 200 concurrent on 2026-08-11. There is no CPU limit, so the request is a scheduling weight rather than a cap — only a genuinely full node throttles the pod, and then to ~90% of its threads.

**`segment-subregions-dl-template`** — hypothalamic subunits and ScLimbic. Same image and node pool, **13G / 4 CPU / 2G ephemeral-storage** (no memory limit), with `TF_ENABLE_ONEDNN_OPTS=0`.

#### Why the phase is two pods (#134)

Until [#134](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/134) this was a single sequential pod running all five regions, whose memory request went 16G → 5G in [#95](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/95) → 13G in [#129](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/129).

The 5G came from a 3.38 GB peak inferred from `ram_efficiency`, which is a **lifetime average**. It is right for the three GEMS regions — they hold ~3.5 GB for ~50 of the fused pod's ~55 minutes — and blind to what follows: step 4 (`mri_segment_hypothalamic_subunits`, TensorFlow) allocates ~8 GB more in under a minute. With no memory limit that overrun is not a container-limit kill but the kernel firing inside `kubepods` and picking this container, so it presented as `OOMKilled` on 7/7 attempts that landed on a 16 GiB instance type and 0/10 on 32 GiB types, and the retry policy turned every one of them into a silent success. Measured directly with `scripts/manifests/subregion-oom-probe.yaml` (cgroup v2 `memory.peak`, an exact high-water mark rather than a sample): step-4 peak **16.28 G with oneDNN on, 11.87 G with it off**, for +10 s of wall clock. The peak does not scale with timepoint count (11.91 G at one timepoint, 11.87 G at three) — the tool processes sessions in a loop and frees between them. 13G sits above the measured peak, so the scheduler reserves what the pod will actually take and cannot pack a co-tenant into it — that, rather than the node size, is the fix. It does **not** exclude the 16 GiB instance type: such a node has 14.82 GB allocatable against only 0.554 GB of daemonset memory requests, so 13G fits it and the pod simply becomes the sole tenant. Do not re-derive either request from an averaged efficiency metric.

#129 fixed the size of the request; #134 fixed its **shape**. Reserving 13G for ~55 minutes covered a peak lasting ~30 seconds — 0.9% of the pod's lifetime. Splitting it lets a `c8i-flex.4xlarge` (31.44 GB / 15.89 CPU allocatable, measured 2026-08-03) hold three GEMS pods where it held two fused ones, since a 4.5G GEMS pod is CPU-bound rather than memory-bound. The later 4 → 3.5 CPU trim takes that to four per `4xlarge`.

The two pods can run concurrently because the DL tools do not consume GEMS output. Verified against the FreeSurfer 7.4.1 sources: in `--s` mode `mri_segment_hypothalamic_subunits` reads exactly one subject file, `mri/nu.mgz`, and `mri_sclimbic_seg` reads `mri/nu.mgz` plus an optional `mri/transforms/talairach.xfm.lta`. Both are FastSurfer outputs each pod has staged for itself; neither tool opens `ThalamicNuclei*`, `brainstemSs*` or `*hippoAmygLabels*`. **Re-check this before any FreeSurfer version bump.**

Each pod now has its own private `emptyDir` copy of `$SD` (no shared volume — GitHub #77), so there is nothing to race on between the two pods at all; the old "disjoint filenames in a shared directory" property is moot. Each pod still owns and uploads only its own artifacts: GEMS owns `thalamus`/`brainstem`/`hippoamyg`, DL owns `hypothalamic`/`sclimbic`.

The 4.5G GEMS request is **provisional** — it is the ~3.5 GB plateau seen on the probe's 1 Hz trace plus ~1 GB of deliberate slack, and is the one number here not read from `memory.peak`. Re-run the probe against steps 1–3 alone and set it from the high-water mark.

Both pods download `config/fslicense` and source `SetUpFreeSurfer.sh` with strict shell flags relaxed (the script is not `set -u`-safe and returns non-zero). Only the GEMS pod creates the symlinks from bare FastSurfer session IDs (`ses-00A`) to the `{tp}.long.{BASE}` naming that `--long-base` requires; the DL tools take bare session IDs via `--s`.

Results are collected into `/out`, which in each pod is its own **emptyDir volume** — the pod runs as UID 1000 and cannot `mkdir` at the image root filesystem.

**Resume guards.** Each region checkpoints to and restores from its own final S3 key (`derivatives/subregions/{subjID}/{subjID}_{region}.tar.gz` — the same key the template's own `outputs.artifacts` uploads at pod completion): before a region runs, `checkpoint_restore` HEADs that key (applying a `> 1 KB` size floor, same convention as `src/metrics/outcome_recorder.py`'s `_MIN_OUTPUT_BYTES`) and downloads+extracts it if present instead of recomputing; after a region completes, `checkpoint_save` uploads it immediately. This survives a full workflow resubmit, not just an in-workflow pod retry, since S3 outlives pod/volume lifetime. A secondary `have_all_tps <glob>` guard still checks the pod's own local `emptyDir` — it only matters within a single still-running pod (e.g. a later region in the same script), since the `emptyDir` itself does not survive a pod retry. Both helpers are duplicated in both templates rather than shared, because sharing them means baking a shell/Python fragment into the `freesurfer` image — an image rebuild and a pin commit for a few dozen lines. The two copies guard disjoint patterns and disjoint keys, so the pods never race on the same check. Two weaker forms of `have_all_tps` were tried and are wrong:

- Guarding on `{BASE}/mri/` never fires — `segment_subregions --long-base` writes outputs per-timepoint and never into the base directory, so the region recomputes from scratch on every retry. (Confirmed empirically: the `{BASE}_template/mri/` directory is empty in every uploaded tarball.)
- Guarding on the *first* timepoint only is unsafe — the tools process all timepoints in a single invocation, so an interruption can leave `ses-00A` complete and later sessions missing. A first-timepoint guard would skip the region and silently upload a partial segmentation.

### Regions

Regions 1–3 run in order inside `segment-subregions-gems-template`; regions 4–5 run in order inside `segment-subregions-dl-template`, concurrently with them. Runtimes below are per-subject for a **single-timepoint** subject and scale with timepoint count. Output filenames are verified against a real FreeSurfer 7.4.1 run — note the longitudinal stream writes `.long.` where the FreeSurfer wiki's cross-sectional examples show `.v13.T1` / `-T1.v22`.

**1. Thalamic nuclei** — `segment_subregions thalamus --long-base {BASE} --threads 4`. GEMS/Bayesian atlas deformation, CPU-only, ~30–45 min.
S3 output: `derivatives/subregions/{subjID}/{subjID}_thalamus.tar.gz`
Contents (per timepoint, in `mri/`): `ThalamicNuclei.long.mgz`, `ThalamicNuclei.long.FSvoxelSpace.mgz`, `ThalamicNuclei.long.volumes.txt`

**2. Brainstem substructures** — `segment_subregions brainstem --long-base {BASE} --threads 4`. GEMS/Bayesian, CPU-only, ~15–25 min.
S3 output: `derivatives/subregions/{subjID}/{subjID}_brainstem.tar.gz`
Contents (per timepoint, in `mri/`): `brainstemSsLabels.long.mgz`, `brainstemSsLabels.long.FSvoxelSpace.mgz`, `brainstemSsLabels.long.volumes.txt`

**3. Hippocampal subfields + amygdala nuclei** — `segment_subregions hippo-amygdala --long-base {BASE} --threads 4`. GEMS/Bayesian, CPU-only, ~30–60 min. Requires FreeSurfer 7.3+ (image is 7.4.1) and the `average/HippoSF/atlas/` atlas kept in the `freesurfer` image.
S3 output: `derivatives/subregions/{subjID}/{subjID}_hippoamyg.tar.gz`
Contents (per timepoint, in `mri/`):

```
{lh,rh}.hippoAmygLabels.long.mgz                  primary segmentation (cropped, hi-res)
{lh,rh}.hippoAmygLabels.long.FSvoxelSpace.mgz     primary, conformed 256^3
{lh,rh}.hippoAmygLabels.long.{CA,FS60,HBT}.mgz              alternative groupings, cropped
{lh,rh}.hippoAmygLabels.long.{CA,FS60,HBT}.FSvoxelSpace.mgz alternative groupings, conformed
{lh,rh}.hippoSfVolumes.long.txt                   hippocampal subfield volumes
{lh,rh}.amygNucVolumes.long.txt                   amygdala nuclei volumes
```

That is 8 label maps per hemisphere (16 total): the primary segmentation plus three alternative *groupings* of the same result — `CA` (CA1/2/3/4), `HBT` (head/body/tail), and `FS60` (FreeSurfer 6.0-compatible) — a deliberate choice to keep every grouping analyzable rather than pick one. The packaging globs stay loose (`*hippoAmygLabels*`) so a FreeSurfer upgrade that reintroduces version suffixes does not silently drop outputs. All subregion labels stay in native T1w space; the subcortical structures are small and downstream analyses use them there rather than resampled into MNI.

**4. Hypothalamic subunits** — `mri_segment_hypothalamic_subunits`, TensorFlow CNN, ~10 sec/session. Segments 5 bilateral hypothalamic subregions from model files in `$FREESURFER_HOME/models/`.
S3 output: `derivatives/subregions/{subjID}/{subjID}_hypothalamic.tar.gz`
Contents (per session): `mri/hypothalamic_subunits_seg.v1.mgz`, `mri/hypothalamic_subunits_volumes.v1.csv`, `stats/hypothalamic_subunits_volumes.v1.stats`

**5. ScLimbic** — `mri_sclimbic_seg`, U-Net, <1 min/session. Segments hypothalamus (coarse), mammillary bodies, basal forebrain, septal nuclei, NAcc, fornix.
S3 output: `derivatives/subregions/{subjID}/{subjID}_sclimbic.tar.gz`
Contents (per session): `mri/sclimbic.mgz`, `stats/sclimbic.stats`

Steps 4–5 take space-separated `--s` args built from `base-tps`, so each tool is invoked once for all sessions rather than per-session.

**T1-only:** `segment_subregions` and the two deep-learning tools expose no T2 input, so the T2w scans this pipeline ingests are not used in this phase.

S3 outputs:
- `derivatives/subregions/{subjID}/{subjID}_hypothalamic.tar.gz`
- `derivatives/subregions/{subjID}/{subjID}_sclimbic.tar.gz`

**Backfill:** `check_subregions_derivatives` in `src/inventory.py` gates on all five regions, `hippoamyg` included. A subject whose tarballs predate hippo-amygdala therefore reports `subregions-exist=False` and re-enters the phase on its next submission rather than remaining a four-region outlier. The per-region resume guards skip whatever is already present, so only hippo computes.

The real cost is upstream: if that subject's FastSurfer derivatives have been cleaned from S3, `fastsurfer-exists` is `False` and the anatomical phase regenerates them first (hours), which dominates the ~30–60 min segmentation. Backfilling an already-processed cohort is therefore a deliberate operation, not a free consequence of the gate.

---

### Failure modes

- **S3 artifact download fails (Access Denied)** → check `serviceAccountName: argo-workflows-runner` is set
- **`base-tps` not found** → long-template tarball doesn't have a valid `base-tps` file; subject's FastSurfer longitudinal phase may not have completed
- **`segment_subregions` crashes** → check that symlinks were created (logged before the command); verify FreeSurfer license is at `/opt/freesurfer/.license`
- **Model file not found** (DL tools) → verify `images/freesurfer/Dockerfile` includes `models/`

Subregion labels are left in native T1w space: the subcortical structures are small, and downstream analyses use them there rather than resampled into MNI. (An earlier `subregion-to-mni` phase warped them into `MNI152NLin2009cAsym` via each session's T1w→MNI transform; it was removed as unnecessary.)

---

## fmri-first-level-proc

**Note:** This pipeline lives in a separate repository (`jrussell9000/fmri-first-level-proc`). There is no WorkflowTemplate for it in this repo. The submission is managed by the `first-level-queue-manager` Prefect flow in `ABCD_fmri_orchestrator_S3`.

Runs after cloudpipe_minproc has finished processing a subject. Requires cloudpipe_minproc outputs in `<YOUR_S3_BUCKET>` and writes results back to `<YOUR_S3_BUCKET>`.

Reads from `<YOUR_S3_BUCKET>`:
```
derivatives/func/{subjID}/{session}/{subjID}_{session}_{task}_{run}_space-MNI152NLin2009cAsym_bold.tar.gz
```

Writes to `<YOUR_S3_BUCKET>`:
```
derivatives/first_levels/{subjID}/...
```

---

## Cross-pipeline notes

### Checking processed subjects

```bash
# Sessions with complete functional preprocessing outputs
aws s3 ls s3://<YOUR_S3_BUCKET>/derivatives/func/ --recursive \
  | grep 'space-MNI152NLin2009cAsym_bold.tar.gz' \
  | awk '{print $4}' | cut -d/ -f3 | sort -u

# Step outcomes (queryable via Athena)
# s3://cloudpipe-metrics/metrics/step-outcomes/dt=YYYY-MM-DD/

# Workflow run summaries
# s3://cloudpipe-metrics/metrics/workflow-runs/dt=YYYY-MM-DD/
#
# Metrics live in their own versioned bucket, not <YOUR_S3_BUCKET> — writes to
# <YOUR_S3_BUCKET>/metrics/* are denied. Every object is under a dt= partition;
# an object at a prefix root is invisible to Athena and DuckDB alike.
```

### Concurrency accounting

Both Prefect flows count all active Argo workflows via the same `lib/argo.py` `ConcurrencyGate` implementation. If both pipelines run simultaneously, coordinate the `cloudpipe-max-concurrent` and `first-level-max-concurrent` Prefect Variables (`prefect variable set <name> <N>`) so the combined load stays within cluster and Globus semaphore limits (semaphore cap: 8 concurrent Globus transfers) and at or below the controller's namespace-wide `namespaceParallelism` of `100`.

### Forcing a step to re-run

Delete the S3 completion marker key for the derivative that needs regeneration, then resubmit. The inventory will detect the missing file and run that step while skipping everything else that is still valid. See the [Idempotency and partial reruns](#idempotency-and-partial-reruns) section for the exact keys per step.
