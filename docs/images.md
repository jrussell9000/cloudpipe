# Docker Images

Pipeline images live in a private ECR registry (`{account-id}.dkr.ecr.<YOUR_AWS_REGION>.amazonaws.com/cloudpipe/`). Most are still **dual-pushed** to ECR Public (`public.ecr.aws/l9e7l1h1/cloudpipe/`) as a secondary — a rollback target held over from the NAT-cost migration. The registry prefix actually used is set by Terraform (`local.ecr_registry` in `terraform/argowf.tf`) and passed as the `ecr-registry` parameter to every workflow.

**`fmri-first-level-proc` is private-only** as of 2026-08-17: its dual-push and its ECR Public repository are gone, so rollback for that one image would be a rebuild rather than a registry flip. Public repos are being retired image by image via `local.ecr_images_public_retired` in `terraform/ecr.tf` — see `handoffs/ecr-private-registry-migration.md` Step 6 (internal repo only).

---

## Build system overview

Six GitHub Actions workflows manage image builds: `build-images.yaml`, `build-fmri-first-level-proc.yaml`, `build-prefect-flow-runner.yaml`, `pr-image-build.yaml` (build-only validation on pull requests, no push), `image-cache-canary.yaml` (weekly cold build, no cache, no push), and `build-gpu-nodeclass-ami.yaml` (bakes the Karpenter GPU AMI — see [pre-baked-amis.md](pre-baked-amis.md)). The repo's other two workflows, `ci.yaml` and `sync-public.yaml`, are unrelated to images.

Three of them — `build-images.yaml`, `pr-image-build.yaml`, `image-cache-canary.yaml` — get their build matrix from one place, `.github/scripts/image_deps.py`. The skip list, the `ARM_IMAGES` mapping, and the rebuild-enrolment rule live there, not in three shell blocks kept aligned by comment. `tests/test_image_deps.py` pins the behaviour against the real Dockerfiles.

### build-images.yaml (most images)

Triggers on `push` to `main` when any of the following change:
- `images/**/Dockerfile`
- `images/**/pixi.toml` or `images/**/pixi.lock`
- `images/**/*.py`
- `src/**` — library code (e.g. `src/metrics/`, `src/inventory.py`) `COPY`ed into images
- Excludes `images/fmri-first-level-proc/**` and `images/prefect-flow-runner/**` (each has its own dedicated workflow)

A `changes` job detects which image directories were modified and builds only those. `workflow_dispatch` accepts an optional `image` input to build a specific image (or all, if left blank).

When a path outside an image's own directory changes — `images/shared/…`, `src/…` — every image whose Dockerfile `COPY`s **that file** is rebuilt too. Still self-maintaining: adding a `COPY` line to a Dockerfile automatically enrolls that image, because enrolment is derived from the `COPY` sources themselves. `prefect-flow-runner` also `COPY`s `src/metrics/` but is excluded here and rebuilt by its own workflow instead.

Enrolment is **per file, not per directory** ([#260](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/260)). Five images `COPY` from `images/shared/`, but only two copy `registration_qc.py`; under the old directory rule an edit to it rebuilt all five. The wasted minutes were the smaller half of that: each spurious rebuild pushes `cache-to` and so resets the 30-day expiry on that image's ECR build-cache entry (`terraform/ecr.tf`), keeping `cache-from` warm and a broken dependency resolve unexecuted. That is precisely how [#255](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/255) stayed invisible for months — `fireANTs`' cache was last refreshed by an unrelated `registration_qc.py` change.

Two properties of the matcher are load-bearing and are asserted in `tests/test_image_deps.py`:

- **Flags must not hide a source.** A literal `COPY images/shared/` pattern once missed `COPY --chown=1000:1000 images/shared/…` in `images/freesurfer/Dockerfile`, so that image was never enrolled and went ~3 weeks emitting a stale QC schema in production. Flags are now split off before sources are read, so no future flag can reintroduce it.
- **Ambiguity resolves towards building.** Over-enrolment wastes minutes; under-enrolment ships stale code with no signal. A `COPY` source containing a glob or a `$VAR` therefore falls back to its static directory prefix, and one with no static prefix enrols the image on any change.

The `changes` job routes each image to a runner via the `ARM_IMAGES` allow-list in `.github/scripts/image_deps.py`, defaulting everything else to `linux/amd64` on `ubuntu-latest`.

> **`ARM_IMAGES` is currently dead.** It is set to `"cloudpipe-controller"`, an image that does not exist in `images/` and has no Dockerfile — so in practice **every** image this workflow builds is amd64. The two images that genuinely need arm64 (`fmri-first-level-proc`, `prefect-flow-runner`) are excluded from this workflow and built as arm64 by their own. If you add an image destined for `first-level-nodepool` (the ARM64/Graviton pool), you must add it to `ARM_IMAGES` — an amd64 image will not run there.

**Tagging**: images are tagged `sha-<full-git-sha>` only — no `:latest`. After a successful build, an `update-image-refs` job automatically rewrites all `sha-*` **and** `:latest` image references in `argo/workflows/cloudpipe_minproc/` (and `argo/workflows/cloudpipe_fullproc/`, once that pipeline exists — see [architecture.md](architecture.md#cloudpipe_fullproc-planned--design-only-not-implemented)) to the new SHA and commits with `[skip ci]`. ArgoCD picks up the updated WorkflowTemplates on the next sync. The `:latest` match handles the first-push case when a new image is initially wired in with `:latest` before its first build.

Large images free GitHub Actions disk space before building. The condition matches `fastsurfer`, `fireANTs`, `freesurfer`, and `synthmorph` — but `images/synthmorph/` no longer exists, so that arm of the test is inert. (The image directory is `fireANTs`, capital letters included; the ECR repo is lowercase `cloudpipe/fireants`.)

### image-cache-canary.yaml (weekly cold build)

Builds **every** image `build-images.yaml` would build, on a `schedule` (Sundays 09:00 UTC), with `no-cache: true`, `pull: true`, no push, and no AWS credentials. `workflow_dispatch` takes an optional image name.

Main-branch builds always have `cache-from`, so they cannot tell you whether an image still builds *from scratch*: `pixi install --locked`, `pip install`, `apt-get`, and the base-image tag are all served from cached layers instead of re-run. An image can be unbuildable for months while CI stays green — [#255](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/255) is the worked example (NVIDIA withdrew `nvidia-cudnn-cu12==9.1.0.70`, which torch 2.4–2.6 pin by `==`). The PR gate is the only other honest signal, and it fires only when a PR touches image paths; external breakage is exactly the case where nothing of ours changes, so no PR fires.

Weekly is chosen against the 30-day cache TTL: a monthly canary could fire only after the cache had already lapsed, which is the situation it exists to pre-empt. On failure an `alert` job files (or comments on) a `needs-triage` issue — a red run in the Actions tab is only a signal to someone already looking there.

Cost is roughly 30–45 runner-minutes per week for the current 8 images, dominated by `fireANTs` (~16 min cold).

**A canary failure is not a production outage.** The images currently running were built while their dependencies still resolved. It means the *next* cold build will fail, and the cache that is hiding it expires 30 days after its last push.

### build-fmri-first-level-proc.yaml

Separate workflow because `fmri-first-level-proc` requires two private repositories cloned at build time:
- `jrussell9000/ABCD_fmri_orchestrator_S3`
- `jrussell9000/fmri-first-level-proc`

Builds `linux/arm64` on `ubuntu-24.04-arm`. After push, rewrites the SHA tag in `argo/workflows/fmri_first_level_proc/fmri-first-level-proc-workflow-template.yaml` and commits.

### build-prefect-flow-runner.yaml

Triggers on changes to `images/prefect-flow-runner/**`, `src/metrics/**` (`COPY`ed into the image), `prefect/flows/**`, or `prefect/prefect.yaml`. Uses `:latest` tag (not SHA). After push, warns in the GitHub Actions job summary if `prefect.yaml` changed and `prefect deploy --all` is needed. See operations.md for the full Prefect deployment procedure.

---

## Images reference

### python

**ECR repo**: `cloudpipe/python`  
**Tag**: SHA-pinned (`:latest` match enables auto-pinning on first build)  
**Platform**: `linux/amd64`  
**Base**: `public.ecr.aws/docker/library/python:3.14.2-alpine3.23`  
**Contents**: Python 3.14 + `boto3` + `botocore` + metrics scripts (`exit_handler.py`, `writer.py`, `schemas.py`), `COPY`ed at build time from `src/metrics/` and `src/inventory.py`.

Used by all lightweight scripting steps that need boto3:
- `start-globus-instance-template` — EC2 start + SSM waiter
- `globus-s3-sync-template` — SSM send-command for `aws s3 sync`
- `subject-data-inventory-template` — S3 listing and derivative existence checks
- `published-sessions-template` — re-reads the FastSurfer completion markers after the anatomical phase to build the per-session fan-out list
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
**Base**: `nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04`  
**Contents**: Python 3, PyTorch 2.6.0 (CUDA 12.6), `fireants`, `nibabel`, `numpy`.

Was CUDA 12.1 / torch 2.5.1 until [#255](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/255): NVIDIA withdrew `nvidia-cudnn-cu12==9.1.0.70`, which torch 2.4.0–2.6.0 pins by `==` on both the cu121 and cu124 wheel lines, so the image could not be built from a cold cache. cu126 + torch 2.6.0 is the nearest combination whose cudnn pin (9.5.1.17) still resolves.

Script: `fst1w_to_mni.py` — affine + SyN registration of FreeSurfer conformed `orig.mgz` to MNI152NLin2009cAsym using FireANTs. GPU-accelerated. Called by `t1w-to-mni-template`.

Uses `orig.mgz` (conformed space) rather than the BIDS T1w so the source space matches the BOLD→T1w transform produced by SynthMorph (also in conformed space). Runs `nvidia-smi` as a background monitor to track GPU utilisation in the pod logs.

Requires `nvidia.com/gpu: 1` resource limit. Runs on `gpu-nodepool`.

---

### afni

**ECR repo**: `cloudpipe/afni`  
**Tag**: SHA-pinned  
**Platform**: `linux/amd64`  
**Build tool**: [pixi](https://pixi.sh) — reproducible conda-lock environment  
**Base**: `ghcr.io/prefix-dev/pixi:0.41.4`  
**Contents**: AFNI + Python environment defined in `pixi.toml`/`pixi.lock`. Entrypoint wraps a pixi shell-hook script.

`preproc.py` and `surf_geometry.py` are baked into the image at build time. Changes to them require a normal image rebuild (see the rebuild trigger below).

Runs `preproc.py` with 4 GB RAM (6 GB limit), 3 CPU, 20 GB ephemeral storage (30 GB limit). Runs on `cpu-heavy-nodepool`. The `--threads` value is not fixed: the driver reads the pod's own CPU request via the downward API and divides it by `jobs`, so at the default `jobs: 1` it is 3 threads. Called by `functional-preprocessing-session-template`, which also invokes `surf_geometry.py` once per session to export the surface geometry Stage 2 consumes.

Triggered to rebuild on changes to `pixi.toml`, `pixi.lock`, or `.py` files under `images/afni/`.

---

### workbench

**ECR repo**: `cloudpipe/workbench`  
**Tag**: SHA-pinned  
**Platform**: `linux/amd64`  
**Build tool**: [pixi](https://pixi.sh) — reproducible conda-lock environment  
**Base**: `ghcr.io/prefix-dev/pixi:0.41.4`  
**Contents**: Connectome Workbench (`wb_command`) plus nibabel/numpy. Uses `connectome-workbench-cli`, **not** the `connectome-workbench` metapackage — the latter also pulls the Qt GUI build, which is dead weight in a headless pod.

`cifti_assemble.py` is baked in. Resamples the cortical grayordinate component to fsLR 32k and assembles a CIFTI-2 dtseries.

Runs on `cpu-light-nodepool`, 4 GB RAM, 2 CPU. Called by `surface-resample-session-template`.

Requires the fsLR meshes staged at `s3://{bucket}/config/fsLR/` — see [pipelines.md](pipelines.md) for the file list and the two traps (the sphere must be the `fs_LR-deformed_to-fsaverage` variant; area correction uses vertex-area *metrics*, not surfaces).

---

### fastsurfer

**ECR repo**: `cloudpipe/fastsurfer`  
**Tag**: SHA-pinned  
**Platform**: `linux/amd64`  
**Base**: `deepmi/fastsurfer:latest`  
**Contents**: FastSurfer with a `nonroot` user (UID/GID 1000) added. `/opt/freesurfer` ownership transferred to 1000.

Used by the four anatomical phase templates (`fastsurfer-template-build-template`, `fastsurfer-template-parcellation-template`, `fastsurfer-long-segmentation-template`, `fastsurfer-long-parcellation-template`).

The anatomical steps are **split across two node pools**, not all on GPU: the segmentation steps take `karpenter.sh/nodepool: gpu-nodepool`, while the parcellation steps (`surf_only`) take `cpu-heavy-nodepool` — surface reconstruction is CPU-bound and gains nothing from a GPU. Note `cpu-heavy-nodepool` admits only 2xlarge/4xlarge, which constrains how those pods can be packed.

The non-root user is required because the workflow-level `securityContext` (`runAsUser: 1000`) would otherwise conflict with FastSurfer's default root execution.

Frees 20+ GB of GitHub Actions disk space before building (Docker image layer cache, Android SDK, .NET, Haskell).

---

### fsqc

**ECR repo**: `cloudpipe/fsqc`  
**Tag**: SHA-pinned  
**Platform**: `linux/amd64`  
**Base**: `python:3.10-slim-bookworm`  
**Contents**: [Deep-MI/fsqc](https://github.com/Deep-MI/fsqc) 2.1.7 (MIT), installed `--no-deps` with its runtime requirements listed explicitly. Runs as UID 1000 (`nonroot`).

Runs anatomical QC against FastSurfer + subregion output and emits the `fsqc_qc` metric records (`metrics/fsqc-qc/`). Used by the `fsqc-metrics` WorkflowTemplate.

**This is a ~930 MB python-slim image, not a FastSurfer derivative.** fsqc reads FreeSurfer *output files* through nibabel and never shells out to FreeSurfer binaries, so basing it on the 16 GB `deepmi/fastsurfer` image would carry a CUDA runtime and a full FreeSurfer tree that no enabled module touches.

The dependency trim is deliberate and subtle — the OpenGL/Qt rendering stack (`whippersnappy`, `pyopengl`, `glfw`, `pyrr`, `PyQt6`) is dropped, while `brainprint`, `lapy`, and `psutil` are kept even though they look trimmable. Two traps are documented at length in the Dockerfile header and are worth reading before any version bump or flag change:

- `fsqcMain._check_packages()` hard-fails at **startup** on a missing `brainprint`/`lapy`, ungated by the flags that would need them. A plain import smoke-test passes; only a real `run_fsqc` invocation reaches the gate.
- Enabling `--surfaces` without restoring the trimmed packages does **not** crash. fsqc catches the ImportError per module, writes `surfaces:1` to `status.txt`, and exits 0 — yielding a quietly incomplete record. Add the deps in the same change as the flag.

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

**Also carries `bold_to_t1w.py`** — this image, not a separate SynthMorph image, is where BOLD→T1w registration runs. (There is no `images/synthmorph/` and no `cloudpipe/synthmorph` ECR repo; earlier revisions of this doc described one.) The script uses `mri_synthmorph` for a contrast-agnostic rigid fit: a single neural-network forward pass (~5 s) instead of bbregister's iterative surface-based optimisation (~5–10 min), with no T1-weighted assumption. bbregister was removed outright in `61ccff7` — see [ADR 002](decisions/002-synthmorph-over-bbregister.md).

It produces the LTA transform, an ANTs/ITK `.txt` affine (RAS→LPS coordinate flip for ANTs compatibility, hand-rolled in Python to avoid a `lta_convert --outitk` segfault), the BOLD reference volume, a warped QC image, and a brain mask in BOLD space. FreeSurfer CLI dependencies are replaced with Python equivalents (`nibabel`, `numpy`, `scipy`).

Used by the `subregion-seg` WorkflowTemplate (`segment-subregions-gems-template`, `segment-subregions-dl-template`) and by `registration`'s BOLD→T1w step (`bold-to-t1w-session-template`, whose thread-pool env vars are pinned to the CPU request). All three pods run on `cpu-heavy-nodepool` — no GPU required.

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
| `fsl` | Unused in current pipelines | FSL/topup groundwork for the planned `cloudpipe_fullproc` pipeline (SDC step) — see [architecture.md](architecture.md#cloudpipe_fullproc-planned--design-only-not-implemented). Parked out of CI (`build-images.yaml`) until that pipeline is implemented. |
| `diffusion` | Present, not yet integrated | For future DWI processing; unrelated to `cloudpipe_fullproc`, no consumer planned yet |
| `fmriprep` | **Directory removed** | Legacy full-preproc approach, replaced by cloudpipe_minproc |
| `ants` | **Directory removed** | Was never buildable (no Dockerfile — only `__pycache__`) |
| `bravePy` | **Directory removed** — consolidated into `python` | Was Python+boto3 without metrics scripts. Any doc or template still saying `bravepy` is stale; the image is `cloudpipe/python`. |

Only `fsl` and `diffusion` still exist on disk. The other three rows are kept because their names persist in older docs, commit messages, and issues.

---

## Adding a new image

1. Create `images/<name>/Dockerfile`. Use a minimal base; run as a non-root UID.
2. **Add `<name>` to `local.ecr_images` in `terraform/ecr.tf` and apply.** (Root-level Terraform; not in the public repo, which publishes only `terraform/modules/`.) The repositories do not autocreate; without this the first build has nowhere to push.
3. **If the image uses pixi, generate `pixi.lock` with the pinned builder version — not your workstation's.** See the warning below.
4. Commit and push to `main` — GitHub Actions detects the new directory and builds it.
5. The new image tag (`sha-<sha>`) appears in the GitHub Actions job summary.
6. Reference it in a WorkflowTemplate. Use `:latest` for the first reference — `update-image-refs` matches `sha-*|latest` and rewrites it, but only after confirming the tag reached the registry:
   ```yaml
   image: "{{workflow.parameters.ecr-registry}}/cloudpipe/<name>:latest"
   ```
7. On the next push that changes the image, the `update-image-refs` job rewrites the SHA automatically.

To trigger a one-off build without changing image source files, use the `workflow_dispatch` trigger on `build-images.yaml` with the image directory name as input.

### pixi.lock must be generated with the builder's pixi version

Dockerfiles build on `ghcr.io/prefix-dev/pixi:0.41.4`, which reads **lockfile format v6 only**. Every `images/*/pixi.lock` here is v6. A current workstation pixi (0.73.x) writes **v7**, and `pixi lock` has no flag to target an older format — so a lock generated locally will build fine on your machine and fail in CI.

Generate it with the pinned version:

```bash
curl -fsSL -o /tmp/pixi.tar.gz \
  https://github.com/prefix-dev/pixi/releases/download/v0.41.4/pixi-x86_64-unknown-linux-musl.tar.gz
tar xzf /tmp/pixi.tar.gz -C /tmp
cd images/<name> && /tmp/pixi lock
head -1 pixi.lock          # must say: version: 6
```

Verify with the Dockerfile's exact command in a clean directory:

```bash
mkdir /tmp/lockcheck && cp images/<name>/pixi.{toml,lock} /tmp/lockcheck/
cd /tmp/lockcheck && /tmp/pixi install --locked
```

`pr-image-build.yaml` builds changed images on pull requests (build-only, no push, no AWS credentials), so a lockfile-format mismatch now fails the PR rather than `main`. It has no registry layer cache, so it is slower than the main-branch build — correctness on the PR, speed on the merge.

---

## Troubleshooting image builds

### update-image-refs reports success but no commit is pushed

**Symptom**: The `update-image-refs` job succeeds and prints `Updated cloudpipe/<name> → sha-...`, but no `ci: pin workflow images to ...` commit appears in `git log` and the WorkflowTemplate still references the old SHA.

**Cause**: The `echo "Updated cloudpipe/${name} → ..."` line runs unconditionally — it does not indicate the `sed` actually matched anything. If `git diff --staged --quiet` reports no changes, the commit step is skipped silently and `git push` outputs "Everything up-to-date".

The most common cause is using `|` as both the `sed` delimiter and the BRE alternation operator. Inside `s|...|...|g`, `\|` is interpreted as an escaped literal `|`, not alternation, so the pattern never matches. The fix (already applied) is to use `#` as the delimiter: `s#...#...#g`.

**Fix**: If image refs were not auto-updated, apply them manually:

```bash
NEW_SHA=<full-40-char-git-sha-of-the-build-commit>
for template in argo/workflows/cloudpipe_minproc/*.yaml; do
  sed -i "s#/cloudpipe/<name>:\(sha-[a-zA-Z0-9]*\|latest\)#/cloudpipe/<name>:sha-${NEW_SHA}#g" "$template"
done
git diff argo/workflows/  # verify changes look correct
git add argo/workflows/ && git commit -m "ci: pin <name> to sha-${NEW_SHA} [skip ci]" && git push
```

The correct `<full-git-sha>` is the `headSha` of the GitHub Actions run, visible in the run's URL or via `gh run view <run-id> --json headSha`.
