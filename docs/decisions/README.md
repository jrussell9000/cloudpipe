# Architecture Decision Records

Key design choices that are not obvious from reading the code. Each record captures the context that made the decision necessary, what was decided, and what it constrains or enables.

| # | Decision | Status |
|---|---|---|
| [001](001-s3-gateway-over-posix-staging.md) | S3 gateway over POSIX staging for Globus transfers | Accepted |
| [002](002-synthmorph-over-bbregister.md) | SynthMorph over bbregister for BOLD→T1w registration | Accepted |
| [003](003-orig-mgz-for-t1w-registration.md) | Use `orig.mgz` instead of BIDS T1w for T1w→MNI registration | Accepted |
| [004](004-s3-artifacts-for-inter-step-data.md) | S3 artifacts for inter-step data, not shared PVC | Accepted |
| [005](005-preproc-py-in-configmap.md) | Mount `preproc.py` from ConfigMap instead of baking it into the image | Superseded |
| [006](006-pod-identity-over-irsa.md) | EKS Pod Identity over IRSA | Accepted |
| [007](007-karpenter-for-pipeline-pods.md) | Karpenter for pipeline node provisioning | Accepted |
| [008](008-prefect-as-queue-manager.md) | Prefect flow as Argo submission queue manager | Accepted |
| [009](009-argo-over-batch.md) | Argo Workflows over AWS Batch for pipeline orchestration | Accepted |
| [010](010-globus-ha-subscription.md) | UW-Madison High Assurance Globus subscription | Accepted |
| [011](011-s3-athena-for-metrics.md) | S3 + Glue + Athena for pipeline metrics storage | Accepted |
| [012](012-abcd-matrix-rejected.md) | ABCD `registration_matrix_T1` evaluated and rejected for BOLD→T1w | Rejected |
| [013](013-src-scripts-tools-reorg.md) | Split `tools/` into `src/`, `scripts/`, and data-only `tools/` | Accepted |
| [014](014-cloudflare-tunnel-over-vpn.md) | Cloudflare Tunnel + Access to replace AWS Client VPN | Proposed |
| [015](015-s3-backend-after-state-loss.md) | S3 remote backend for Terraform state, adopted after a state-loss incident | Accepted |
| [016](016-skipped-producer-deadlock-in-dag-recording.md) | Never let a DAG task reference a skipped producer's outputs or exit code | Accepted |
| [017](017-exploded-derivatives-over-tarballs.md) | Exploded S3 objects with a symlink manifest, not tarballs, for FastSurfer and subregion derivatives | Accepted |
