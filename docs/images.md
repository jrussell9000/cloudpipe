# Docker Images

All pipeline images are pushed to ECR Public at `public.ecr.aws/l9e7l1h1/cloudpipe/`. Registry prefix is set in the `cloudpipe-config` ConfigMap and passed as `ecr-registry` to every workflow.

---

## Build system overview

Three GitHub Actions workflows manage image builds:

### build-images.yaml (most images)

Triggers on `push` to `main` when any of the following change:
- `images/**/Dockerfile`
- `images/**/pixi.toml` or `images/**/pixi.lock`
- `images/**/*.py`
- Excludes `images/fmri-first-level-proc/**`

A `changes` job detects which image directories were modified and builds only those. `workflow_dispatch` accepts an optional `image` input to build a specific image (or all, if left blank).

All images except `cloudpipe-controller` build for `linux/amd64` on `ubuntu-latest`. `cloudpipe-controller` builds for `linux/arm64` on `ubuntu-24.04-arm`.

**Tagging**: images are tagged `sha-<full-git-sha>` only — no `:latest`. After a successful build, an `update-image-refs` job automatically rewrites all `sha-*` **and** `:latest` image references in `argo/workflows/cloudpipe_minproc/` and `argo/workflows/cloudpipe_fullproc/` to the new SHA and commits with `[skip ci]`. ArgoCD picks up the updated WorkflowTemplates on the next sync. The `:latest` match handles the first-push case when a new image is initially wired in with `:latest` before its first build.

Large images (`fastsurfer`, `fireants`, `freesurfer`, `synthmorph`) free GitHub Actions disk space before building.

### build-fmri-first-level-proc.yaml

Separate workflow because `fmri-first-level-proc` requires two private repositories cloned at build time:
- `jrussell9000/ABCD_fmri_orchestrator_S3`
- `jrussell9000/fmri-first-level-proc`

Builds `linux/arm64` on `ubuntu-24.04-arm`. After push, rewrites the SHA tag in `argo/workflows/fmri_first_level_proc/fmri-first-level-proc-workflow-template.yaml` (and `terraform/modules/batch/jobdefs.tf` for legacy Batch) and commits.

### build-prefect-flow-runner.yaml

Triggers on changes to `images/prefect-flow-runner/**`, `prefect/flows/**`, or `prefect/prefect.yaml`. Uses `:latest` tag (not SHA). After push, warns in the GitHub Actions job summary if `prefect.yaml` changed and `prefect deploy --all` is needed. See operations.md for the full Prefect deployment procedure.

---

## Images reference

### python

**ECR repo**: `cloudpipe/python`  
**Tag**: SHA-pinned (`:latest` match enables auto-pinning on first build)  
**Platform**: `linux/amd64`  
**Base**: `public.ecr.aws/docker/library/python:3.14.2-alpine3.23`  
**Contents**: Python 3.14 + `boto3` + `botocore` + metrics scripts (`exit_handler.py`, `writer.py`, `schemas.py`).

Used by all lightweight scripting steps that need boto3:
- `start-globus-instance-template` — EC2 start + SSM waiter
- `globus-s3-sync-template` — SSM send-command for `aws s3 sync`
- `subject-data-inventory-template` — S3 listing and derivative existence checks
- Init containers in fastsurfer templates that download T1w inputs from S3
- `workflow-exit-handler` — writes workflow run summary JSON to S3

The metrics scripts (`/app/exit_handler.py` etc.) are present in all containers but only invoked by the exit handler step; their presence does not affect other steps.

---

### globus

**ECR repo**: `cloudpipe/globus`  
**Tag**: SHA-pinned  
**Platform**: `linux/amd64`  
**Base**: `public.ecr.aws/docker/library/python:3.12-slim-bookworm`  
**Contents**: Python 3.12 + `globus-sdk>=4,<5`. Runs as UID 1000 (`cloudpipe`).

Scripts:
- `transfer.py` — discovers BIDS files on the source collection, submits a Globus transfer, polls until completion. Handles `TooManyPendingJobs` with exponential backoff (10 attempts, starting at 60s). Reuses an existing active task with the same label if found (idempotent on retry).
- `setup_auth.py` — one-time interactive script to generate a Globus refresh token and store it in Secrets Manager. Run locally before first use.
- `cancel_transfers.py` — utility to cancel active transfers (used operationally, not in workflows).

Valid scan types: `T1w`, `T2w`, `rest`, `nback`, `sst`, `mid`, `dwi`. Source layout expected: `{subject}/{session}/{bids_dir}/`.

---

### fireants

**ECR repo**: `cloudpipe/fireants`  
**Tag**: SHA-pinned  
**Platform**: `linux/amd64`  
**Base**: `nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04`  
**Contents**: Python 3, PyTorch (CUDA 12.1), `fireants`, `nibabel`, `numpy`.

Script: `fst1w_to_mni.py` — affine + SyN registration of FreeSurfer conformed `orig.mgz` to MNI152NLin2009cAsym using FireANTs. GPU-accelerated. Called by `t1w-to-mni-template`.

Uses `orig.mgz` (conformed space) rather than the BIDS T1w so the source space matches the BOLD→T1w transform produced by SynthMorph (also in conformed space). Runs `nvidia-smi` as a background monitor to track GPU utilisation in the pod logs.

Requires `nvidia.com/gpu: 1` resource limit. Runs on `gpu-nodepool`.

---

### synthmorph

**ECR repo**: `cloudpipe/synthmorph`  
**Tag**: SHA-pinned  
**Platform**: `linux/amd64`  
**Base**: `freesurfer/synthmorph:latest`  
**Contents**: SynthMorph + FreeSurfer utilities + `bold_to_t1w.py`. Runs as UID 1000 (`nonroot`).

Script: `bold_to_t1w.py` — replaces bbregister with `mri_synthmorph` for BOLD→T1w registration. A single neural network forward pass (~5s) vs bbregister's iterative surface-based optimisation (~5–10 min). Contrast-agnostic — no T1-weighted assumption.

Produces: LTA transform, ANTs/ITK `.txt` affine (RAS→LPS coordinate flip for ANTs compatibility), BOLD reference volume, warped QC image, and brain mask in BOLD space. All FreeSurfer CLI dependencies replaced with Python equivalents (`nibabel`, `numpy`, `scipy`) to stay compatible with the lightweight SynthMorph container.

Runs on `cpu-heavy-nodepool` (8 GB RAM, 2 CPU). Called by `bold-to-t1w-template`.

---

### afni

**ECR repo**: `cloudpipe/afni`  
**Tag**: SHA-pinned  
**Platform**: `linux/amd64`  
**Build tool**: [pixi](https://pixi.sh) — reproducible conda-lock environment  
**Base**: `ghcr.io/prefix-dev/pixi:0.41.4`  
**Contents**: AFNI + Python environment defined in `pixi.toml`/`pixi.lock`. Entrypoint wraps a pixi shell-hook script.

`preproc.py` is baked into the image at build time but is **also** embedded in the `preproc-script` ConfigMap and mounted over the baked copy at runtime. This allows updating the preprocessing script without rebuilding the image — edit `images/afni/preproc.py`, run `tools/gen-preproc-configmap.sh`, commit both files, push.

Runs `preproc.py` with 6 threads, 16 GB RAM, 6 CPU, 10 GB ephemeral storage. Runs on `cpu-heavy-nodepool`. Called by `functional-preprocessing-template`.

Triggered to rebuild on changes to `pixi.toml`, `pixi.lock`, or `.py` files under `images/afni/`.

---

### fastsurfer

**ECR repo**: `cloudpipe/fastsurfer`  
**Tag**: SHA-pinned  
**Platform**: `linux/amd64`  
**Base**: `deepmi/fastsurfer:latest`  
**Contents**: FastSurfer with a `nonroot` user (UID/GID 1000) added. `/opt/freesurfer` ownership transferred to 1000.

Used by the four anatomical phase templates (`fastsurfer-template-creation-template`, `fastsurfer-template-segmentation-template`, `fastsurfer-template-parcellation-template`, `fastsurfer-long-segmentation-template`, `fastsurfer-long-parcellation-template`).

Runs on `gpu-nodepool` for all anatomical steps. The non-root user is required because the workflow-level `securityContext` (`runAsUser: 1000`) would otherwise conflict with FastSurfer's default root execution.

Frees 20+ GB of GitHub Actions disk space before building (Docker image layer cache, Android SDK, .NET, Haskell).

---

### freesurfer

**ECR repo**: `cloudpipe/freesurfer`  
**Tag**: SHA-pinned  
**Platform**: `linux/amd64`  
**Base**: `ubuntu:22.04`  
**Contents**: Pruned FreeSurfer 7.4.1 — only the subset required for subcortical subregion segmentation. Runs as UID 1000 (`nonroot`).

Downloaded with `aria2c` (16 parallel connections) for speed; the 3 GB tarball is extracted in a single pass with targeted `--exclude` rules to keep the image small.

**Kept**:
- `python/` — bundled Python interpreter, `surfa`, `samseg` packages
- `average/HippoSF/atlas/`, `average/ThalamicNuclei/atlas/`, `average/BrainstemSS/atlas/` — probabilistic atlases for the GEMS segmentation tools
- `bin/segment_subregions`, `bin/fspython`, `bin/mri_convert`, `bin/mri_segment_hypothalamic_subunits`, `bin/mri_sclimbic_seg`
- `models/` — model files for the deep-learning tools (`hypothalamic_subunits.h5`, `sclimbic.fsm+ad.t1.nstd00-50.nstd32-50.h5`, etc.)
- Setup scripts and color LUTs

**Excluded** (to reduce image size): large recon-all GCA atlases (`average/*.gca`), legacy MATLAB-compiled binaries, GUI/GPU libraries (`lib/cuda`, `lib/qt`, `lib/vtk`), `matlab/`, `mni/`, `diffusion/`, `fsfast/`, `subjects/`, Python build headers (`python/include/`, `python/share/`).

Used exclusively by the `subregion-seg` WorkflowTemplate (`segment-thalamus-template`, `segment-brainstem-template`, `segment-deeplearning-template`). All three run on `cpu-heavy-nodepool` — no GPU required.

---

### fmri-first-level-proc

**ECR repo**: `cloudpipe/fmri-first-level-proc`  
**Tag**: SHA-pinned  
**Platform**: `linux/arm64` (runs on Graviton `backend` or Karpenter ARM nodes)  
**Base**: `ubuntu:24.04`  
**Contents**: AFNI (ARM build from AFNI official), micromamba, conda environment from `environment.yaml` (includes ABCD_fmri_orchestrator_S3 and fmri-first-level-proc packages). Schaefer 400-parcel + Tian S4 subcortical atlas baked in.

Two private repositories cloned at build time by the GitHub Actions workflow:
- `jrussell9000/ABCD_fmri_orchestrator_S3` — orchestration logic
- `jrussell9000/fmri-first-level-proc` — first-level GLM implementation (installed as a Python package)

Entrypoint: `python3 orchestrate_first_level.py`. Config files `orch_config_final.yaml` and `proc_config_final.yaml` are baked in.

Used by the `fmri-first-level-proc` WorkflowTemplate. 2-hour active deadline; 300 Gi emptyDir scratch volume.

---

### cloudpipe-flow-runner (Prefect)

**ECR repo**: `cloudpipe/cloudpipe-flow-runner`  
**Tag**: `:latest`  
**Platform**: `linux/arm64` (Graviton `backend` node group)  
**Base**: `prefecthq/prefect:3-python3.12`  
**Contents**: Prefect 3 + `hera` + `boto3` + all flow code under `prefect/flows/`.

Flow code is baked in — changing any `.py` file under `prefect/flows/` requires a new image build and push. Build path: push to `main` (GitHub Actions) or run `images/prefect-flow-runner/build.sh` locally (also runs `prefect deploy --all`).

`PYTHONPATH=/opt/prefect/flows` is set so relative imports within the flows directory work without installing the flows as a package.

Build context is the repo root (not `images/prefect-flow-runner/`) so the `COPY prefect/flows/` instruction can reach flow source.

---

## Inactive / legacy images

| Image | Status | Notes |
|---|---|---|
| `fsl` | Unused in current pipelines | FSL tools; retained for potential future use |
| `fmriprep` | Unused — replaced by cloudpipe_minproc | Legacy full-preproc approach |
| `diffusion` | Not yet integrated | For future DWI processing |
| `bravePy` | Removed — consolidated into `python` | Was Python+boto3 without metrics scripts |

---

## Adding a new image

1. Create `images/<name>/Dockerfile`. Use a minimal base; run as a non-root UID.
2. Commit and push to `main` — GitHub Actions detects the new directory and builds it.
3. The new image tag (`sha-<sha>`) appears in the GitHub Actions job summary.
4. Reference it in a WorkflowTemplate:
   ```yaml
   image: "{{workflow.parameters.ecr-registry}}/cloudpipe/<name>:<sha-tag>"
   ```
5. On the next push that changes the image, the `update-image-refs` job rewrites the SHA automatically.

To trigger a one-off build without changing image source files, use the `workflow_dispatch` trigger on `build-images.yaml` with the image directory name as input.

---

## Troubleshooting image builds

### update-image-refs reports success but no commit is pushed

**Symptom**: The `update-image-refs` job succeeds and prints `Updated cloudpipe/<name> → sha-...`, but no `ci: pin workflow images to ...` commit appears in `git log` and the WorkflowTemplate still references the old SHA.

**Cause**: The `echo "Updated cloudpipe/${name} → ..."` line runs unconditionally — it does not indicate the `sed` actually matched anything. If `git diff --staged --quiet` reports no changes, the commit step is skipped silently and `git push` outputs "Everything up-to-date".

The most common cause is using `|` as both the `sed` delimiter and the BRE alternation operator. Inside `s|...|...|g`, `\|` is interpreted as an escaped literal `|`, not alternation, so the pattern never matches. The fix (already applied) is to use `#` as the delimiter: `s#...#...#g`.

**Fix**: If image refs were not auto-updated, apply them manually:

```bash
NEW_SHA=<full-40-char-git-sha-of-the-build-commit>
for template in argo/workflows/cloudpipe_minproc/*.yaml argo/workflows/cloudpipe_fullproc/*.yaml; do
  sed -i "s#/cloudpipe/<name>:\(sha-[a-zA-Z0-9]*\|latest\)#/cloudpipe/<name>:sha-${NEW_SHA}#g" "$template"
done
git diff argo/workflows/  # verify changes look correct
git add argo/workflows/ && git commit -m "ci: pin <name> to sha-${NEW_SHA} [skip ci]" && git push
```

The correct `<full-git-sha>` is the `headSha` of the GitHub Actions run, visible in the run's URL or via `gh run view <run-id> --json headSha`.
