# 009 — Argo Workflows over AWS Batch for pipeline orchestration

**Status**: Accepted

## Context

The pipeline requires DAG-based orchestration: steps with explicit dependencies, fan-out over sessions and runs, conditional skipping, and artifact passing between steps. Two options were evaluated:

**AWS Batch**: Managed job scheduling on EC2 or Fargate. Multi-step pipelines are expressed as Step Functions state machines that submit Batch jobs. The `terraform/modules/batch/` directory contains a working Batch implementation used for an earlier version of the first-level processing pipeline.

**Argo Workflows**: Kubernetes-native workflow engine that runs each pipeline step as a pod. Supports DAGs, conditional steps (`when`), fan-out (`withParam`), inter-step artifact passing, and workflow-level retry policies natively.

Batch was the original choice because it requires no EKS cluster and integrates tightly with Step Functions for orchestration. The limitations became apparent as the pipeline grew:
- Step Functions state machines are defined in JSON/ASL, which is verbose and difficult to read for complex DAGs
- Conditional fan-out (running N pods over dynamically-discovered sessions) requires Lambda functions or Map states in Step Functions, adding moving parts
- Artifact passing between Batch jobs requires explicit S3 upload/download steps; there is no first-class artifact concept
- The Batch job definition format and Step Functions ASL are separate systems; the pipeline logic is split across two resource types that reference each other by ARN
- GPU jobs in Batch require separate Compute Environments for GPU vs CPU instance types, and the job definition must specify the correct environment

Argo Workflows expresses the entire pipeline — DAG structure, fan-out, conditions, artifacts, retry policy, node selection — in a single WorkflowTemplate YAML that lives in the repository.

## Decision

Use Argo Workflows as the pipeline orchestrator. The cluster (EKS) was introduced to run Argo. The fmri-first-level-proc pipeline now runs via Argo on `first-level-nodepool` rather than Batch.

**Update (2026-07)**: AWS Batch is no longer retained even for legacy compatibility. The `terraform/modules/batch/` module was deleted in `1aee7cf`. `build-fmri-first-level-proc.yaml` kept rewriting the image SHA in the module's `jobdefs.tf` for some time afterward; because the step ran under `bash -e`, the missing file aborted it before the `git push`, so the Argo WorkflowTemplate silently stopped being re-pinned. The dead path was removed from the workflow once that surfaced.

## Consequences

- The pipeline definition (WorkflowTemplate YAML) is version-controlled in git alongside the code it orchestrates; ArgoCD syncs it to the cluster
- DAG structure, fan-out, conditions, and artifact paths are all visible in a single file per pipeline phase
- Argo UI provides live per-pod status, logs, and artifact inspection — Batch's Step Functions console shows state machine execution but not per-job logs in the same view
- EKS adds infrastructure cost and operational complexity (cluster management, node provisioning, K8s upgrades) compared to Batch's fully managed model
- The Karpenter-based node provisioning approach (ADR 007) is required to make EKS cost-efficient for burst workloads; without it, maintaining a standing GPU fleet would be prohibitive
- The Step Functions + Batch implementation in `terraform/modules/batch/` was never actively maintained after this decision and has since been deleted; recover it from git history if it is ever needed as a reference
