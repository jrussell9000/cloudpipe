# 005 — Mount `preproc.py` from ConfigMap instead of baking it into the image

**Status**: Superseded (2026-07-01)

## Context

`preproc.py` is the core AFNI preprocessing script. It is actively developed — parameters are tuned, steps are added, and bugs are fixed on a faster cadence than the rest of the AFNI image (which depends on `pixi.toml`/`pixi.lock` and the AFNI conda environment). The AFNI image takes several minutes to build, and every change to `preproc.py` would trigger a full image rebuild followed by ArgoCD sync before the new version reached the cluster.

A Kubernetes ConfigMap can hold text content up to 1 MiB per key. `preproc.py` is well within this limit.

## Decision (original, no longer in effect)

`preproc.py` would be baked into the image at build time (at `/app/preproc.py`) but also stored in a ConfigMap (`preproc-script`) and mounted over the baked copy at runtime, managed by ArgoCD via `argo/workflows/cloudpipe_minproc/preproc-script-configmap.yaml` and regenerated from the source file by `scripts/gen-preproc-configmap.sh`.

## Why superseded

A 2026-07-01 audit found that `functional-preprocessing-workflow-template.yaml` (the only template that runs `preproc.py`) never actually mounted the `preproc-script` ConfigMap — no template in `cloudpipe_minproc/` referenced it as a volume, in its full git history. Every run used the baked-in `/app/preproc.py`, so the ConfigMap was dead weight: it had to be manually regenerated and committed alongside every `preproc.py` change, but doing so had no runtime effect. The equivalent mount did exist in the then-in-development `cloudpipe_fullproc` templates (`tmp/cloudpipe_fullproc/*.yaml`), suggesting this pattern was designed there and the ConfigMap file was copied into `cloudpipe_minproc` without the accompanying volume wiring ever being added.

The ConfigMap (`preproc-script-configmap.yaml`) and its generator (`scripts/gen-preproc-configmap.sh`) were removed. (`tmp/cloudpipe_fullproc/` has since been removed too — see the note at the end of this ADR.) `preproc.py` is now updated exclusively through the normal image rebuild path: edit `images/afni/preproc.py` → commit → push → CI rebuilds the `afni` image and updates the SHA-pinned reference in the WorkflowTemplate.

## Consequences

- `preproc.py` changes now require a full image rebuild (several minutes) before taking effect, same as any other AFNI image change — no more live ~30s ConfigMap sync
- One less artifact to keep in sync; no more risk of the ConfigMap silently diverging from `images/afni/preproc.py`
- If this pattern is ever wanted again, it must be written from scratch. The `tmp/cloudpipe_fullproc/` templates this ADR cited as a working reference **no longer exist** — that directory was removed (see issue #69: `cloudpipe_fullproc` was documented in six places while existing nowhere in the repo). There is no `argo/workflows/cloudpipe_fullproc/` either. The Argo mechanism itself is unexceptional (a `configMap` volume plus a `mountPath` over `/app/preproc.py`); it is the *sync discipline* — regenerate, commit, let ArgoCD reconcile — that made it not worth keeping
