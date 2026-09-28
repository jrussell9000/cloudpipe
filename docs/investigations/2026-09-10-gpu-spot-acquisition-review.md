# GPU Spot Acquisition Review — why the pool starves, and what is left to pull

**Date:** 2026-09-10
**Question:** we keep hitting spot shortages on `gpu-nodepool`. What is actually happening,
what has already been tried, and what levers remain that do not rely on on-demand pricing?
**Predecessors:**
[2026-07-17-gpu-nodepool-spot-scarcity-g6-enablement.md](2026-07-17-gpu-nodepool-spot-scarcity-g6-enablement.md)
(family widening),
[2026-07-31-gpu-vram-duty-cycle-handoff.md](2026-07-31-gpu-vram-duty-cycle-handoff.md)
(the 17% duty cycle),
[2026-09-03-per-node-timeslicing-costing.md](2026-09-03-per-node-timeslicing-costing.md)
(per-node slicing, g7, CloudTrail as the instrument), PR #345 (`minValues`).

---

## 0. Bottom line

**The problem is acquisition, not interruption, and it is regional.** Over the last 7 days
Karpenter created ~15,700 GPU NodeClaims and 97% of them were killed for
`insufficient_capacity` before an instance ever existed. The pool configuration
(4 families × 2 sizes × 3 AZs, `minValues` diversification, 3-slice time-slicing, spot-only)
is already at the ceiling of what a single-region, single-NodePool, spot-only design can do.
Every further lever is one of:

1. **Serve the pods we have better** while starved (priority, time-of-day, overhead trim) — cheap.
2. **Get more pods out of each grant** (per-node slice counts, VRAM trim) — medium.
3. **Stop needing the GPU during a drought** (CPU fallback for segmentation) — medium, and the
   only lever that makes a batch immune to a drought without on-demand.
4. **Change the supply** (g7 with a Blackwell-capable driver, or a satellite cluster in a region
   that has G capacity) — high effort.

The on-demand premise has also moved since time-slicing and right-sizing landed; §7 records the
current number so the decision is made against today's figure, not the pre-slicing one.

Tier 1 and Tier 2 items are tracked as issues #370–#375 (one per subsection). Tier 3 is
deliberately not filed: each item is a project, not a task, and needs a decision first.

---

## 1. Live snapshot, 2026-09-10 ~16:50Z

| Metric | Value |
|---|---|
| GPU nodes | 1 (`g4dn.xlarge`, <YOUR_AWS_REGION>c) |
| GPU pods pending / running | 82 / 3 |
| Pending by step | 47 `template-build`, 35 `long-segmentation` |
| Pending age | 43–134 min |
| Running | 3 × `t1w-to-mni` on the one node |
| `CreateFleet` asks, 15:39–16:46Z | 22 asks of 19–29 units, all 8 types offered every time |
| Instances granted in that window | **1** |
| Karpenter log, every ~3 s | `skipping, nodepool requirements filtered out all instance types` |
| Workflows running | 86 |

The Karpenter log line is the ICE cache, not a config fault: after each
`InsufficientInstanceCapacity` Karpenter marks that (type, zone) offering unavailable for ~3
minutes, and once every offering in the pool is cached the pool is skipped. The ~3-minute cadence
of the `CreateFleet` asks above is that TTL expiring.

`get-spot-placement-scores` at 3 units, per type, <YOUR_AWS_REGION> (all three AZs):

| Type | Score |
|---|---|
| `g4dn.xlarge`, `g4dn.2xlarge`, `g5.*`, `g6.*`, `g6.4xlarge`, `gr6.4xlarge`, `g6f.2xlarge`, `g6e.xlarge` | 1 / 1 / 1 |
| `g7.2xlarge`, `g7.4xlarge` | **3 / 3 / 3** |

Same six types (`g4dn`/`g5`/`g6` × `xlarge`/`2xlarge`), 3 units, **by region**:

| Region | Score |
|---|---|
| <YOUR_AWS_REGION> | 1 |
| us-west-2 | 2 |
| us-east-1 | 5 |
| eu-west-1 | **9** |

The instrument saturates at 1 inside <YOUR_AWS_REGION> (see the 2026-09-03 doc §6), but it discriminates
clearly across regions. <YOUR_AWS_REGION> is structurally the thinnest region for these families.

---

## 2. Seven days of Karpenter counters (Prometheus, 2026-09-03 → 09-10)

| Metric | Value |
|---|---|
| `karpenter_nodeclaims_created_total{gpu-nodepool}` | 15,720 |
| `karpenter_nodeclaims_disrupted_total{reason="insufficient_capacity"}` | **15,221 (97%)** |
| `karpenter_nodes_created_total{gpu-nodepool}` | 499 |
| `karpenter_nodes_terminated_total{gpu-nodepool}` | 552 |
| disrupted `spot_interrupted` / `empty` | 924 events / 61 |
| GPU node lifetime p10 / p50 / p90 | 16 min / 2.0 h / 14 h |
| GPU node-hours delivered | 2,752 |
| interruption messages received: `spot_interrupted` / `rebalance_recommendation` | 3,126 / 3,300 (all pools) |

Two readings:

- **Almost every GPU node ends by reclaim, not consolidation.** That is fine for this workload:
  GPU pods are 12–24 min and the retry strategy absorbs it. It is not the bottleneck.
- **The bottleneck is the 97% of launch attempts that never become instances.** Nothing about
  retry tuning, consolidation, or interruption handling touches that number.

### 2.1 Hourly timeline: starved vs served

Joining hourly GPU node count (`karpenter_nodepools_usage`) with pending pods in
`argo-workflows`, and classifying each hour as *starved* (pending ≥ 10, nodes < 5), *partial*
(pending ≥ 10, nodes < 20), *served* (nodes ≥ 5), or *idle*:

| State | Hours (of 168) |
|---|---|
| served | 68 (fleet 30–79 nodes) |
| partial | 6 |
| **starved** | **6** |
| idle | 89 |

The starved windows:

| Window (UTC) | Local (ET) | Nodes | Pending |
|---|---|---|---|
| Thu 2026-09-03 19:00–22:00 | Thu 15:00–18:00 | 0–1.8 | 312–351 |
| Thu 2026-09-10 14:00–now | Thu 10:00– | 0–5.7 | 16–82 |

The served windows include Sep 4 01:00–14:00Z (overnight ET, fleet 25–79) and the entire
weekend of Sep 5–6 (fleet steady at ~35). **Both droughts were US weekday daytime; both
recoveries were overnight or weekend.** Two episodes are a hypothesis, not a law — but it is the
cheapest hypothesis in this document to test (§4.2).

---

## 3. Prices, 7-day spot mean, <YOUR_AWS_REGION> (2026-09-03 → 09-10)

| Type | Spot $/hr | On-demand $/hr | Slices | Spot $/slice-hr |
|---|---|---|---|---|
| `g4dn.xlarge` *(what we run)* | **0.26** *(was 0.17 on 09-03)* | 0.526 | 3 | **0.086** |
| `g4dn.2xlarge` | 0.35 | 0.752 | 3 (T4 VRAM) | 0.118 |
| `g6.xlarge` | 0.39 | 0.805 | 3 | 0.130 |
| `g6.2xlarge` | 0.46 | 0.978 | 3 today / 6 per-node | 0.153 / **0.077** |
| `g5.2xlarge` | 0.63 | — | 3 today / 6 per-node | 0.210 / 0.105 |
| `g6f.2xlarge` (¼ L4, 5.7 GiB) | 0.22 | 0.475 | 1 | 0.216 |
| `g6e.xlarge` | 1.05 | 1.861 | 3 | 0.351 |
| `g7.2xlarge` | 1.18 *(was 0.57 on 09-04)* | — | 7 (8 vCPU, 32 GiB) | 0.168 |
| `c7a.2xlarge` (CPU fallback ref.) | 0.12 | — | — | — |

Spot is now only ~51% off on-demand for `g4dn.xlarge`. On-demand at 3 slices is $0.175/slice-hr.

---

## 4. Tier 1 — configuration only, this week

### 4.1 PriorityClasses on the three GPU steps (#370)

Under scarcity the scheduler and Karpenter serve pending GPU pods roughly in arrival order.
Right now 47 `template-build` pods compete equally with 35 `long-segmentation` pods for every
freed slice, and each `template-build` that wins will itself spawn another GPU pod. Prioritising
completion over starts shrinks work-in-progress and the batch tail.

Order, highest first, all `preemptionPolicy: Never` so nothing running is ever evicted:

1. `t1w-to-mni` — ~30 s of GPU, unblocks a whole session's functional phase
2. `long-segmentation` — finishes a subject that already holds a template
3. `template-build` — starts new GPU demand

Argo templates carry a `priorityClassName` field. PriorityClass objects belong in `gitops/`
(ArgoCD), the field in the three templates, and a guard in
`tests/argo/test_gpu_step_resources.py` so a new GPU template cannot ship unprioritised.
Karpenter also orders provisioning by pod priority.

### 4.2 Shape GPU demand by time of day (#371)

Test the §2.1 hypothesis before building on it: add a Grafana panel of GPU nodes against pending
GPU pods (both series already exist in Prometheus). If it holds, the lever is free: start
GPU-heavy batches around 22:00 ET, or have the queue manager raise `cloudpipe-max-concurrent`
overnight and at weekends and lower it weekday daytime. The queue manager already reads that
variable every poll cycle.

### 4.3 Trim slice-hold overhead inside GPU pods (#372)

A GPU slice is allocated when the pod is admitted, so init containers and the wait-container
artifact upload both hold it:

- `download-fsaverage` fetches 312 objects (~480 MiB) sequentially with boto3, in both
  `template-build` and `long-segmentation`. The long-seg copy carries its own TODO saying
  `--seg_only` probably never reads it
  (`fastsurfer-long-phase-workflow-template.yaml`, init container comment).
- The `template-base` tarball upload after `main` exits.

Bake fsaverage into the fastsurfer image (the image is pre-baked into the AMI anyway) or ship it as
one tarball; drop it from long-seg once a run confirms it is unread. Likely 1–2 min of a 12-min pod.
Measure from Argo `status.nodes` startedAt/finishedAt against the `main` container's
`state.running.startedAt` before and after.

---

## 5. Tier 2 — structural, spot-only, medium effort

### 5.1 CPU fallback for segmentation during droughts (#373)

This is the largest "never stall" lever that honours the no-on-demand constraint.

- FastSurfer supports `--device cpu`. Its docs put CPU segmentation at "several minutes" per
  volume against 20–40 s on a modern GPU.
- `cpu-heavy-nodepool` is deep: 1,072 node launches in the same 7 days with no acquisition
  failures, and the c/m families have separate, far larger spot pools.
- **Mechanism:** the Prefect queue manager already polls Argo. When GPU pods have been pending
  above a threshold for N minutes, it submits with a workflow parameter (e.g. `fastsurfer-device:
  cpu`) that switches the argstring (`--device cpu`), the nodeSelector (`cpu-heavy-nodepool`),
  and the cpu request (8). Subjects submitted during a drought run segmentation on CPU;
  everything else is unchanged.
- **Cost:** at 8 vCPU on a ~$0.12/hr spot node, a 20-min session is ~$0.04 against ~$0.02 on
  GPU spot — a ~2× premium confined to drought hours, on a phase that is ~30% of per-subject
  cost.
- **Validate before trusting:** runtime at 8 threads for `long_prepare_template.sh` and
  `brun_fastsurfer.sh --seg_only` at k = 1..4; Dice between CPU and GPU segmentations on 10
  subjects (expected near-identical, but a derivative that differs by device must be known
  before it is mixed into the cohort).
- Note honestly: on-demand GPU at ~$0.035/step would be cheaper than this fallback. See §7.

### 5.2 Second GPU NodePool with per-node device-plugin config (#374)

The 2026-09-03 costing rejected this for a ~$30 saving. The **capacity** argument is different:
during a drought every grant should carry as many pods as the card allows.

- With per-node configs, `g6.2xlarge` at 6 slices is $0.077/slice-hr and `g5.2xlarge` $0.105,
  against $0.086 on `g4dn.xlarge` today — and each grant carries twice the pods.
- A separate `g6f.2xlarge` pool at 1 slice ($0.216/slice-hr) is a *distinct* spot pool
  (fractional L4 on shared hosts) usable as last-resort overflow. The pod working set (~3.7 GiB)
  fits its 5.7 GiB slice; `t1w-to-mni` (~4.4 GiB/pod device-wide ÷ 2 tenants) is tight and
  needs a test.
- **Fallback between pools is real and already observed:** the ICE cache (§1) empties the
  preferred pool's offerings, and a lower-`weight` NodePool is then the only one with
  offerings. This is not the `priceAdjustment` mechanism that failed for g7 on 2026-09-04 —
  that failed because the nodes were unusable and the pods stayed Pending, driving more
  launches.
- Pods must accept either pool: replace the hard `karpenter.sh/nodepool: gpu-nodepool`
  nodeSelector with a shared label set in both NodePools' `template.metadata.labels`.
- Alternative that keeps ONE NodePool: a userData shell part in `gpu-nodeclass` that reads the
  instance type from IMDS at boot and sets the `nvidia.com/device-plugin.config` node label
  via nodeadm `kubelet.flags`. This sidesteps the "Karpenter cannot label by instance type"
  obstacle the 2026-09-03 doc named.
- **Caveat:** CloudTrail showed `g5`/`g6`/`g6e` granted 0 of 16 during the 2026-09-04 crunch.
  This raises yield per grant; it does not create supply.

### 5.3 Measure `--viewagg_device cpu` (#375)

FastSurfer's own FAQ says view aggregation is the VRAM-heavy part of inference. Under
time-slicing the pod sees the whole card, so `auto` always picks the GPU. If forcing CPU view
aggregation drops the per-pod working set from ~3.7 GiB to ~2 GiB:

- `g4dn.2xlarge` (7.9 cores, 15 GiB T4) could carry 5 slices at $0.35/hr = **$0.070/slice-hr**,
  cheaper than today and nearly double the pods per T4 grant.
- Duty cycle at 5 slices is ~85%; stop there, not 6.
- CPU time per pod rises; the `cpu: 1` request and thread projection stay, so measure wall
  time too.

One exclusive 10-subject batch settles it. Update `GPU_WORKING_SET_MIB` in
`tests/argo/test_gpu_step_resources.py` from the measurement, never from the estimate.

---

## 6. Tier 3 — supply-side, high effort

### 6.1 Make g7 usable

`g7` is again the only family with capacity (3/10 in all AZs). The 2026-09-04 failure — 63
nodes Ready, none advertising `nvidia.com/gpu` — was almost certainly the driver *flavour*:
Blackwell-class parts (RTX PRO 4500) require NVIDIA's **open** kernel modules, and the stock
EKS AL2023 NVIDIA AMI ships the proprietary ones. Two gates, both real:

1. A Packer bake step (`packer/gpu-nodeclass/`) installing the open-module driver variant.
2. Both GPU images rebuilt on CUDA 12.8+ with a torch that carries sm_120 kernels. The fireANTs
   image is on cu126 (`images/fireANTs/Dockerfile`, `CUDA_TAG=12.6.3-cudnn`) and would fail on
   g7 with "no kernel image is available". The fastsurfer image inherits upstream's torch; check
   it the same way.

Then place g7 in a lower-weight second NodePool (§5.2 mechanism), not behind a price bias. At
$1.18/hr with 8 vCPU and 32 GiB it carries 7 slices → $0.168/slice-hr, on par with on-demand
`g4dn.xlarge`. Validate on one throwaway node, as the `gpu-nodepool.yaml` comment already
prescribes, before it goes near the family list.

### 6.2 Satellite GPU cluster in us-east-1

The only lever that changes supply itself (§1: us-east-1 scores 5, eu-west-1 scores 9, against 1
here). Shape: a second EKS cluster running only Argo + Karpenter with a GPU pool, receiving the
anatomical sub-workflow; S3 stays in <YOUR_AWS_REGION>. The anatomical phase moves ~1 GB per subject, so
cross-region transfer is ~$0.02/subject. Real costs: a second control plane (~$73/mo), a second
Karpenter/Argo to operate, and replicating the CloudTrail data-event controls the DUA depends on.
Last resort — but it belongs on the roadmap if leg 2 keeps stalling weekly.

### 6.3 Checked and not viable

| Option | Why not |
|---|---|
| EC2 Capacity Blocks | `describe-capacity-block-offerings` rejects `g6.xlarge` in <YOUR_AWS_REGION> |
| Larger single-GPU sizes (`g4dn.4xlarge`/`8xlarge`, `g6.4xlarge`, `gr6.4xlarge`) | all score 1/10 like the current set, and cost more per slice |
| Savings Plans / ODCRs | on-demand pricing plus a commitment; the cohort is finite |
| Karpenter rebalance-recommendation handling | Karpenter does not act on them; pods are ≤24 min so it would not matter |
| More `minValues` / zones | already 3 of 4 families, 5 types, 2 zones; the offerings are complete, the grants are the problem |
| Raising uniform slices past 3 | T4 VRAM and xlarge CPU both bind (2026-09-03 doc §2) |

---

## 7. The on-demand premise, restated against today's numbers

Everything above is spot-only by design. The figure the "too expensive" judgement was made
against predates time-slicing and right-sizing, when a GPU pod owned a whole `g4dn.8xlarge`.
Today:

| | $/slice-hr | Extra per subject |
|---|---|---|
| spot `g4dn.xlarge` @ 3 | 0.086 | — |
| on-demand `g4dn.xlarge` @ 3 | 0.175 | ~$0.02–0.04 |

A second NodePool with `capacity-type: on-demand`, lower `weight`, and `limits.nodes: 8`
engages only when spot returns nothing (§1 mechanism) and costs ~$4/hr during those hours —
roughly $17 for a drought the size of 2026-09-03's. It is also cheaper per step than the §5.1
CPU fallback. If the objection is policy, §5.1 stands. If it was price, this is the current price.

---

## 8. Loose ends noticed on the way

- The second Karpenter controller replica has been Pending on pod anti-affinity since at least
  July (2026-07-17 doc §10). Still a SPOF.
- `kubectl logs` on the controller only reaches back ~10 minutes at batch load (kubelet log
  rotation), so Prometheus is the only 7-day source for the §2 counters. The Grafana Karpenter
  dashboard should carry them.
- Spot prices for the whole G family rose ~50% between 2026-09-03 and 09-10. The cost figures in
  the 2026-09-03 doc are stale by that much.

---

## 9. Reproduce

```bash
# Live: asks vs grants (the deciding instrument)
aws cloudtrail lookup-events --region <YOUR_AWS_REGION> \
  --lookup-attributes AttributeKey=EventName,AttributeValue=CreateFleet \
  --max-results 50 --query 'Events[].CloudTrailEvent' --output text | tr '\t' '\n' > fleet.jsonl
# then the jq from 2026-09-03-per-node-timeslicing-costing.md §8

# Pending GPU pods by step and age
kubectl -n argo-workflows get pods -o json | jq -r '[.items[]
  | select(.spec.nodeSelector["karpenter.sh/nodepool"]=="gpu-nodepool")
  | {step: .metadata.labels["cloudpipe.io/step"], phase: .status.phase,
     age_min: ((now - (.metadata.creationTimestamp|fromdateiso8601))/60|floor)}]
  | group_by(.step,.phase) | map({step:.[0].step, phase:.[0].phase, n:length,
     min_age:(map(.age_min)|min), max_age:(map(.age_min)|max)})'

# 7-day counters (port-forward Prometheus first)
kubectl -n prometheus port-forward svc/prometheus-kube-prometheus-prometheus 19090:9090 &
q() { curl -s --get localhost:19090/api/v1/query --data-urlencode "query=$1" | jq -c '.data.result[]|[.metric,.value[1]]'; }
q 'sum by (reason) (increase(karpenter_nodeclaims_disrupted_total{nodepool="gpu-nodepool"}[7d]))'
q 'sum by (nodepool) (increase(karpenter_nodes_created_total[7d]))'
q 'histogram_quantile(0.5, sum by (le) (increase(karpenter_nodes_lifetime_duration_seconds_bucket{nodepool="gpu-nodepool"}[7d])))'

# Hourly starved/served timeline: query_range on
#   sum(karpenter_nodepools_usage{nodepool="gpu-nodepool",resource_type="nodes"})
#   sum(kube_pod_status_phase{namespace="argo-workflows",phase="Pending"})
# at step=600 over 7d, average per hour, join on the hour.

# Regional placement scores (the one place the instrument discriminates)
aws ec2 get-spot-placement-scores --region-names <YOUR_AWS_REGION> us-east-1 us-west-2 eu-west-1 \
  --instance-types g4dn.xlarge g4dn.2xlarge g5.xlarge g5.2xlarge g6.xlarge g6.2xlarge \
  --target-capacity 3 --target-capacity-unit-type units
```
