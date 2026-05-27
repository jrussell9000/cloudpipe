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

Use Argo Workflows as the pipeline orchestrator. The cluster (EKS) was introduced to run Argo. AWS Batch is retained only for legacy compatibility: the `terraform/modules/batch/` module still provisions Batch compute environments and job definitions referenced by `build-fmri-first-level-proc.yaml` (which writes the new image SHA to `terraform/modules/batch/jobdefs.tf`), but the fmri-first-level-proc pipeline now runs via Argo on `first-level-nodepool` rather than Batch.

## Consequences

- The pipeline definition (WorkflowTemplate YAML) is version-controlled in git alongside the code it orchestrates; ArgoCD syncs it to the cluster
- DAG structure, fan-out, conditions, and artifact paths are all visible in a single file per pipeline phase
- Argo UI provides live per-pod status, logs, and artifact inspection — Batch's Step Functions console shows state machine execution but not per-job logs in the same view
- EKS adds infrastructure cost and operational complexity (cluster management, node provisioning, K8s upgrades) compared to Batch's fully managed model
- The Karpenter-based node provisioning approach (ADR 007) is required to make EKS cost-efficient for burst workloads; without it, maintaining a standing GPU fleet would be prohibitive
- The Step Functions + Batch implementation in `terraform/modules/batch/` is not actively maintained; it exists as a fallback reference and for the SHA-update CI job, not as a production path
