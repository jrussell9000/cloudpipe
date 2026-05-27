# 004 — S3 artifacts for inter-step data, not shared PVC

**Status**: Accepted

## Context

Argo Workflows needs a way to pass data between pipeline steps that run on different nodes. Two options:

1. **Shared PVC** — provision an EFS `ReadWriteMany` volume that all pods in a workflow can mount simultaneously. Any step can write to a path on the PVC and any subsequent step can read from the same path. Simple but ephemeral: if the workflow is resubmitted or a step retried, the upstream step must re-run to recreate the data.

2. **S3 artifacts** — each template declares explicit `inputs.artifacts` (downloaded from S3 at start) and `outputs.artifacts` (uploaded to S3 at completion). Data is persisted in S3 keyed by the same path layout used for the final derivatives. Downstream steps download what they need from S3 directly, regardless of whether the producing step ran in the current workflow invocation.

A shared PVC was the original approach. The problem with a PVC-only model became apparent when adding skip/resume logic: if the T1w→MNI registration output already exists in S3 from a prior run, the step should be skipped — but the functional preprocessing step still needs that output. With PVC-only data passing, a skipped step leaves nothing on the PVC for downstream consumers, requiring either re-running the skipped step or a separate "restore from S3" init container for each downstream template.

## Decision

All inter-step data that can be meaningfully identified by a stable S3 key is stored as S3 artifacts. Each template declares exactly what it needs as artifact inputs and what it produces as artifact outputs.

The EFS PVC (50 Gi `ReadWriteMany`, deleted on workflow completion) is retained only where multiple containers in the same pod share data that is truly ephemeral and does not need to be persisted between runs. In practice this means the FastSurfer working directory during anatomical processing (all five FastSurfer steps share the same EFS subpath) and the T1w→MNI working directory during registration.

Derivative S3 keys match the structure consumed by downstream pipelines, so a key written by one workflow run is immediately available to a resubmitted workflow or a standalone template invocation.

## Consequences

- Any step can be skipped or retried independently: the inventory checks S3 for each derivative and skips steps whose outputs already exist (with a `size > 1 KB` guard against zero-byte failure artifacts)
- `functional-preprocessing-template` can be submitted standalone without running the full pipeline — all its inputs (BOLD, registration outputs, FastSurfer tarball, MNI template) are declared as S3 artifact inputs
- S3 Transfer Acceleration (`s3-accelerate.amazonaws.com`) is used as the artifact endpoint to reduce per-pod download latency for large files
- Each pod incurs S3 GET costs for its input artifacts and PUT costs for its output artifact uploads; for large BOLD NIfTIs this is non-trivial at scale (offset by the elimination of EFS data transfer costs for large files)
- If an upstream step fails after writing a partial artifact, the partial tarball may be present in S3. The `size > 1 KB` guard prevents this from being treated as a valid completed output, but a manually deleted key may be needed if the partial artifact is larger than the guard threshold
- The EFS PVC is still required for FastSurfer because FastSurfer's longitudinal pipeline writes to a shared subjects directory across all five steps, and the intermediate data (surface models, parcellations) is too large and transient to upload to S3 between steps
