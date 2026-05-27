# 007 — Karpenter for pipeline node provisioning

**Status**: Accepted

## Context

Pipeline pods have sharply heterogeneous resource requirements: Globus transfer and inventory pods need only 256 MB RAM and 0.1 CPU; functional preprocessing pods need 16 GB RAM and 6 CPU; FastSurfer and FireANTs steps require a GPU. If all pipeline pods ran on a fixed managed node group sized for the largest pod, most nodes would be severely underutilized between GPU steps.

EKS managed node groups with per-pool `nodeSelector` handle heterogeneity but scale slowly (ASG group scaling typically takes 3–5 minutes to provision a new node) and require pre-specifying a fixed set of instance types. For a pipeline that fans out to dozens of pods simultaneously and then goes idle, pre-provisioned nodes either cause long queue times or an expensive standing fleet.

## Decision

Four Karpenter NodePools provision pipeline nodes on demand, each constrained to a specific instance family and capacity type:

| NodePool | Arch | Instances | Capacity | Consolidation | Use |
|---|---|---|---|---|---|
| `cpu-light-nodepool` | amd64 | t-family (burstable) | spot + on-demand | `WhenEmptyOrUnderutilized` after 10m | Globus, inventory, lightweight scripting |
| `cpu-heavy-nodepool` | amd64 | c/m/r, 2xlarge or 4xlarge | spot only | `WhenEmpty` after 10m | SynthMorph, AFNI func-preproc |
| `gpu-nodepool` | amd64 | g4dn.xlarge, g6.xlarge (NVIDIA GPU) | spot + on-demand | `WhenEmpty` after 10m | FastSurfer, FireANTs |
| `first-level-nodepool` | arm64 | m/r/c {6g,7g}d (Graviton, NVMe) | spot only | `WhenEmpty` after 10m | fmri-first-level-proc |

Three managed node groups run fixed infrastructure: `backend` (Prefect worker, ArgoCD, etc.), `karpenter` (Karpenter controller itself), and `argo` (Argo controller and server). These are not subject to scale-to-zero.

`first-level-nodepool` selects `d`-suffix instance families (NVMe instance store). The `EC2NodeClass` configures `instanceStorePolicy: RAID0`, which formats the NVMe disks in a RAID-0 stripe and mounts them on the data partition. Kubernetes `emptyDir` volumes land on this local NVMe automatically, making the 300 Gi scratch volume for `fmri-first-level-proc` fast local storage rather than EBS-backed ephemeral.

Karpenter provisions a new node within ~30 seconds of an unschedulable pod appearing. `karpenter.sh/do-not-disrupt: "true"` is set on all pipeline pods to prevent voluntary eviction while work is in progress.

## Consequences

- Node startup latency is ~30 seconds, which is acceptable since most pipeline steps run for minutes to hours; the Globus transfer phase (longest step) absorbs any provisioning delay
- Cluster cost scales with actual pipeline throughput; idle periods between subject batches incur only the cost of the three fixed managed node groups
- `cpu-heavy` and `first-level` pools are spot-only; the retry strategy (exit codes 137/143, `imminent node shutdown` message) handles the ~2-minute spot interruption notice
- `cpu-light` allows on-demand as fallback because inventory and start-instance pods are short-lived but their failure can block the entire workflow; paying on-demand rates for a burstable instance is cheap insurance
- `gpu-nodepool` allows on-demand as fallback because GPU spot availability is limited and a failed GPU step is expensive to retry (FastSurfer runs for 30–60 minutes)
- The `WhenEmptyOrUnderutilized` policy on `cpu-light` reclaims partially-empty nodes; the `WhenEmpty` policy on other pools avoids consolidation mid-step since those pods hold long-running workloads that must not be disrupted
- g5g (Graviton GPU) instances are explicitly excluded from `gpu-nodepool` via the amd64 architecture constraint — pipeline GPU images are built for linux/amd64 only
