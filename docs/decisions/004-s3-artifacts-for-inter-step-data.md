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
- `functional-preprocessing-session-template` can be submitted standalone without running the full pipeline — all its inputs (BOLD, registration outputs, FastSurfer tarball, MNI template) are declared as S3 artifact inputs
- S3 Transfer Acceleration (`s3-accelerate.amazonaws.com`) is used as the artifact endpoint to reduce per-pod download latency for large files
- Each pod incurs S3 GET costs for its input artifacts and PUT costs for its output artifact uploads; for large BOLD NIfTIs this is non-trivial at scale (offset by the elimination of EFS data transfer costs for large files)
- If an upstream step fails after writing a partial artifact, the partial tarball may be present in S3. The `size > 1 KB` guard prevents this from being treated as a valid completed output, but a manually deleted key may be needed if the partial artifact is larger than the guard threshold

## Update (2026-07): the FastSurfer exception is retired

This ADR originally carved out an exception — "the EFS PVC is still required for FastSurfer because FastSurfer's longitudinal pipeline writes to a shared subjects directory across all five steps, and the intermediate data is too large and transient to upload to S3 between steps." That is no longer true, and the sizing premise behind it did not hold up:

- The intermediate `SUBJECTS_DIR` is ~1.2 GB for the base template and ~0.5 GB per session, extrapolated from the 228–418 MiB `_long-template.tar.gz` and ~180 MiB `_{ses}_templated.tar.gz` artifacts the phase already uploads. In-region S3 transfer is free, so the round-trips cost wall-clock, not dollars.
- The anatomical chain is now four pods passing state through `scratch/{workflow.name}/anat/`, reaped by a 7-day lifecycle rule. Template creation and segmentation were merged (both `gpu-nodepool`, strictly sequential), which removed one boundary and one GPU node provision.
- The PVC never delivered the spot-resume property it was credited with. `clear-is-running` deleted FreeSurfer lock files so a retry could *restart over a dirty directory*; `recon-surf.sh` has no skip-completed-stages logic. That dirty-restart behaviour is what produced the truncated-template failures the exit-137 guards were added to catch. A clean `emptyDir` re-seeded from S3 is strictly safer, and S3 state also survives workflow deletion, which `volumeClaimGC: OnWorkflowCompletion` does not.

The remaining PVC consumer is `subregion-segmentation`, whose per-region resume guard reads completed outputs off the retry-persistent volume. Converting it to S3 checkpointing is tracked in **#77**; the EFS filesystem, CSI driver and StorageClass come out once that lands.

## Update (2026-08): the PVC exception is fully retired

`subregion-segmentation`'s `segment-subregions-gems-template` and `segment-subregions-dl-template` now stage the FastSurfer tarballs onto their own private `emptyDir`s (the `hydrate-fastsurfer-template` pod is gone — each segmentation pod declares those inputs directly) and checkpoint each region to its final `derivatives/subregions/{subj}/{subj}_{region}.tar.gz` key immediately after that region completes, rather than relying on a retry-persistent volume. `cloudpipe-long-master-workflow-template.yaml` no longer declares `volumeClaimTemplates`. The "Decision" section above (the FastSurfer/registration EFS exception) is now historical only — no template in this pipeline mounts a PVC. The EFS filesystem, CSI driver, and StorageClass have been removed from the cluster and Terraform entirely.

Three pieces of EFS residue outlived that removal by roughly three months, all now cleaned up:

- `terraform/install.sh` and `cleanup.sh` still targeted `module.aws_efs_csi_pod_identity`, which
  was declared in no `.tf` file — enough to break a fresh bootstrap, latent only because the
  running cluster predates the removal and never re-executes it
  ([#211](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/211)). Both scripts now share
  a validated target list (`terraform/targets.sh`).
- An `efs.csi.aws.com` CSIDriver object and an empty `aws-efs-csi-driver` namespace survived
  in-cluster with no owning ArgoCD Application
  ([#213](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/213)), deleted by hand.

> **The removal-residue lesson generalizes past EFS.** A capability retired from this stack has
> three surfaces to clean, and they fail differently: Terraform *config* (a dangling `-target`
> errors loudly, but only on a code path nobody exercises), ArgoCD *Applications* (removal can
> leave `helm.sh/resource-policy: keep` objects behind by design), and *live cluster state* (a
> CSIDriver with no controller silently accepts PVCs that can never bind). Nothing sweeps the
> second and third automatically.
