# 005 — Mount `preproc.py` from ConfigMap instead of baking it into the image

**Status**: Accepted

## Context

`preproc.py` is the core AFNI preprocessing script. It is actively developed — parameters are tuned, steps are added, and bugs are fixed on a faster cadence than the rest of the AFNI image (which depends on `pixi.toml`/`pixi.lock` and the AFNI conda environment). The AFNI image takes several minutes to build, and every change to `preproc.py` would trigger a full image rebuild followed by ArgoCD sync before the new version reached the cluster.

A Kubernetes ConfigMap can hold text content up to 1 MiB per key. `preproc.py` is well within this limit.

## Decision

`preproc.py` is baked into the image at build time (at `/app/preproc.py`) but is also stored in a ConfigMap (`preproc-script`) and mounted over the baked copy at runtime. The ConfigMap is managed by ArgoCD via `argo/workflows/cloudpipe_minproc/preproc-script-configmap.yaml`, regenerated from the source file by `tools/gen-preproc-configmap.sh`.

Update path when only `preproc.py` changes:
1. Edit `images/afni/preproc.py`
2. Run `tools/gen-preproc-configmap.sh` (regenerates the ConfigMap YAML)
3. Commit both files and push
4. ArgoCD syncs the ConfigMap within ~30 seconds; the next workflow that runs picks up the new script immediately

No image rebuild, no SHA update, no ArgoCD image tag change.

When `pixi.toml`, `pixi.lock`, or other AFNI image files change, a normal image build is triggered by GitHub Actions and the SHA reference in the WorkflowTemplate is updated automatically.

## Consequences

- `preproc.py` changes are live within ~30 seconds of a push — the same cadence as WorkflowTemplate changes
- The baked-in copy of `preproc.py` still exists in the image as a fallback reference but is never used at runtime (the ConfigMap mount takes precedence)
- The ConfigMap YAML must be kept in sync with `images/afni/preproc.py` — forgetting to run `gen-preproc-configmap.sh` will cause the running version to diverge from the source file without any error. CI does not currently enforce this. Always commit both files together.
- ConfigMap size limit (1 MiB per key) is not a practical concern for `preproc.py` but would become one if the script grew to hundreds of thousands of lines
- This pattern is not used for other images — it is specific to `preproc.py` because it is the only script with a development cadence that significantly exceeds the image rebuild cadence
