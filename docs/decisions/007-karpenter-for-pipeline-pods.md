# 007 — Karpenter for pipeline node provisioning

**Status**: Accepted

## Context

Pipeline pods have sharply heterogeneous resource requirements: Globus transfer and inventory pods need only 256 MB RAM and 0.1 CPU; functional preprocessing pods need several GB and multiple cores (4G/3 CPU after the issue #96 rightsizing, down from 6 CPU); FastSurfer and FireANTs steps require a GPU. If all pipeline pods ran on a fixed managed node group sized for the largest pod, most nodes would be severely underutilized between GPU steps.

EKS managed node groups with per-pool `nodeSelector` handle heterogeneity but scale slowly (ASG group scaling typically takes 3–5 minutes to provision a new node) and require pre-specifying a fixed set of instance types. For a pipeline that fans out to dozens of pods simultaneously and then goes idle, pre-provisioned nodes either cause long queue times or an expensive standing fleet.

## Decision

Four Karpenter NodePools provision pipeline nodes on demand, each constrained to a specific instance family. **Every pool is spot-only** — `karpenter.sh/capacity-type` is `In ["spot"]` in all four:

| NodePool | Arch | Instances | Capacity | Consolidation | Use |
|---|---|---|---|---|---|
| `cpu-light-nodepool` | amd64 | t-family (burstable) | spot only | `WhenEmptyOrUnderutilized` after 10m | Globus, inventory, lightweight scripting |
| `cpu-heavy-nodepool` | amd64 | c/m, 2xlarge or 4xlarge | spot only | `WhenEmpty` after 10m | SynthMorph, AFNI func-preproc |
| `gpu-nodepool` | amd64 | g4dn, g5, g6, g6e (single-GPU NVIDIA) | spot only | `WhenEmpty` after 10m | FastSurfer, FireANTs |
| `first-level-nodepool` | arm64 | {m,r,c}{6g,7g}d (Graviton, NVMe), xlarge–4xlarge | spot only | `WhenEmpty` after 10m | fmri-first-level-proc |

Three managed node groups run fixed infrastructure: `backend` (Prefect worker, ArgoCD, etc.), `karpenter` (Karpenter controller itself), and `argo` (Argo controller and server). These are not subject to scale-to-zero.

`first-level-nodepool` selects `d`-suffix instance families (NVMe instance store). The `EC2NodeClass` configures `instanceStorePolicy: RAID0`, which formats the NVMe disks in a RAID-0 stripe and mounts them on the data partition. Kubernetes `emptyDir` volumes land on this local NVMe automatically, making the 300 Gi scratch volume for `fmri-first-level-proc` fast local storage rather than EBS-backed ephemeral.

Karpenter provisions a new node within ~30 seconds of an unschedulable pod appearing. `karpenter.sh/do-not-disrupt: "true"` is set on all pipeline pods to prevent voluntary eviction while work is in progress.

## Consequences

- Node startup latency is ~30 seconds, which is acceptable since most pipeline steps run for minutes to hours; the Globus transfer phase (longest step) absorbs any provisioning delay
- Cluster cost scales with actual pipeline throughput; idle periods between subject batches incur only the cost of the three fixed managed node groups
- **All four pools are spot-only, by design, for cost.** There is no on-demand fallback anywhere — not on `cpu-light`, not on `gpu-nodepool`. The pipeline absorbs interruption through retries instead: the retry strategy keys on exit codes 137/143 and the `imminent node shutdown` message to handle the ~2-minute spot interruption notice. Reintroducing on-demand as insurance would be a real decision to revisit, not a description of the current state
- Spot scarcity is handled by **widening the pool rather than falling back**. `gpu-nodepool` was extended from `["g4dn","g5"]` to `["g4dn","g5","g6","g6e"]` — T4, A10G, L4, L40S, all single-GPU amd64 — so `price-capacity-optimized` has more families to reach for. `g6f` is deliberately excluded: its ~5.59 GiB fractional-L4 slice cannot honour the cluster-wide 3-slice time-slicing config
- The `WhenEmptyOrUnderutilized` policy on `cpu-light` reclaims partially-empty nodes; the `WhenEmpty` policy on other pools avoids consolidation mid-step since those pods hold long-running workloads that must not be disrupted. All pools now use `consolidateAfter: 10m`; `cpu-heavy` waited 30m until 2026-08-11, when a mid-batch measurement at 200 concurrent found 25 of 148 nodes (~17%) holding zero workflow pods while they waited out the timer. Note that on `cpu-heavy` the policy choice is forced regardless: 264 of 267 workflow pods carry `karpenter.sh/do-not-disrupt: true`, so `WhenEmptyOrUnderutilized` could not repack them anyway — `consolidateAfter` only governs the empty-node tail
- g5g/g6g (Graviton GPU) instances are excluded from `gpu-nodepool` via the amd64 architecture constraint — pipeline GPU images are built for linux/amd64 only. `g7` is omitted separately, pending driver validation against the baked AMI
