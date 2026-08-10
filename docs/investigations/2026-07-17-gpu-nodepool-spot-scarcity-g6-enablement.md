# GPU Nodepool Spot Scarcity & g6/g6f Enablement — Findings Report

**Date:** 2026-07-17
**Pipeline:** `cloudpipe_minproc` (test batch — `fastsurfer-template-creation` step)
**Scope:** Why ~100 `fastsurfer-template-creation` pods sat `Pending` for tens of minutes,
and the nodepool change that widened GPU capacity headroom.
**Related:** [decisions/007-karpenter-for-pipeline-pods.md](../decisions/007-karpenter-for-pipeline-pods.md),
[2026-07-16-vpn-remote-access-troubleshooting.md](2026-07-16-vpn-remote-access-troubleshooting.md)
(same session — the Prefect access fix that unblocked submitting this batch).

## 1. Summary

A 100-subject test batch submitted through Prefect left almost every
`fastsurfer-template-creation` pod `Pending`, with Karpenter unable to add GPU nodes. It
looked like an implausibly severe spot shortage. It was **genuine intermittent
`g4dn`/`g5` spot scarcity in <YOUR_AWS_REGION>**, but the *severity* was an artifact of how
Karpenter asks for capacity: it batches all pending GPU pods into one `CreateFleet` call of
`Type: instant`, which is **all-or-nothing**. A 15-unit request against pools that could
each supply only a trickle failed wholesale with `UnfulfillableCapacity`, while a 1-unit
request (the single node that had launched earlier) succeeded. AWS's own
`get-spot-placement-scores` scored the request **1/10 even for a single unit** across all
three AZs during the incident — independent confirmation the capacity really was that thin.

The durable fix was to **widen the GPU nodepool** from `["g4dn","g5"]` to
`["g4dn","g5","g6","g6f"]` (all single-GPU NVIDIA parts: T4 / A10G / L4). This required
overturning a **stale, misdiagnosed exclusion comment** that had kept g6 out. The comment
claimed `nvidia-fabricmanager` fails on single-GPU L4 instances — which is true, but
irrelevant: it fails identically on the `g5` (A10G) already in the allowed list, and those
nodes serve GPU workloads fine. `fabricmanager` is only required for NVSwitch multi-GPU
systems; its failure on any single-GPU instance is benign and does not block node readiness
or GPU allocation. The stock EKS AL2023 NVIDIA AMI ships driver **580.159.03 (CUDA 13.0)**,
which fully supports L4, so the original blocker (an older driver) no longer exists.

**Lesson (transferable): a mass-`Pending` GPU backlog with `UnfulfillableCapacity` is almost
never a config bug — measure the capacity directly (`get-spot-placement-scores`, the
`CreateFleet` request in CloudTrail) before touching YAML, and treat batched `instant` fleet
requests as all-or-nothing when reasoning about "why did nothing launch."**

## 2. Symptom

- `argo list`/`kubectl get pods -n argo-workflows` showed ~100 pods `Pending`, nearly all
  `*-fastsurfer-template-creation-template-*`, ages ranging 5–36 min.
- `kubectl get nodepools` showed `gpu-nodepool` stuck at the **single** node that had
  launched before the burst; it never scaled to meet the backlog.
- Per-pod events: `FailedScheduling … Insufficient nvidia.com/gpu … didn't match node
  affinity/selector` — the pods carry a hard `nodeSelector: karpenter.sh/nodepool=gpu-nodepool`,
  so only that nodepool can satisfy them.

## 3. Root cause

Two layers, one root:

1. **Genuine spot scarcity.** Karpenter logs showed
   `skipping, nodepool requirements filtered out all instance types` for `gpu-nodepool` and,
   at the NodeClaim layer, repeated `failed launching nodeclaim … aws-error-code=UnfulfillableCapacity`
   from `CreateFleet`. All the failures shared **one `aws-request-id`** — Karpenter had
   batched ~15 NodeClaims into a single fleet call.

2. **`instant` fleet = all-or-nothing.** The `CreateFleet` request (from CloudTrail) was
   well-formed: `Type: instant`, `TotalTargetCapacity: 15`, `SpotOptions.AllocationStrategy:
   price-capacity-optimized`, **no** `MaxTotalPrice`, correct AMI + subnet per (type × zone)
   across all 12 combinations. AWS returned `200` with `UnfulfillableCapacity` — it could not
   confidently fulfill *all 15* and so fulfilled *none*. The one node that existed had come
   from an earlier 1-unit request when a pool briefly had headroom.

`get-spot-placement-scores` for the exact instance set:

| Target capacity | <YOUR_AWS_REGION>a | <YOUR_AWS_REGION>b | <YOUR_AWS_REGION>c |
|---|---|---|---|
| 15 units (what Karpenter tried) | 1/10 | 1/10 | 1/10 |
| 1 unit (what had succeeded) | 1/10 | 1/10 | 2/10 |

Even a single unit rated 1–2/10 — the pools were genuinely near-empty during the window.
Capacity later recovered on its own (dozens of `g4dn.xlarge` nodes launched in <YOUR_AWS_REGION>b),
confirming this was transient, not a hard block.

## 4. What was ruled out (and how)

Everything that can *also* produce `UnfulfillableCapacity` or block scaling was checked
empirically, not assumed:

| Candidate | Check | Result |
|---|---|---|
| Service quota exhausted | `service-quotas get-service-quota … L-3819A6DF` (All G and VT Spot) | 2000 vCPU limit, ~4 in use — not it |
| Cluster placement group | `Placement.GroupName` on the running GPU instance | empty — none |
| Dedicated tenancy | `Placement.Tenancy` | `default` |
| Targeted capacity reservation | `CapacityReservationSpecification` | `CapacityReservationPreference: none` |
| Subnet IP exhaustion | `describe-subnets` on the 3 GPU subnets | 3,600–4,000+ free IPs each |
| Spot max-price cap below market | `CreateFleet` request body in CloudTrail | no `MaxTotalPrice` / per-override `MaxPrice` — Karpenter v1 delegates to `price-capacity-optimized` with no ceiling |
| AMI architecture mismatch | `describe-images` + the fact one node launched | `x86_64`, matches the `amd64` requirement; proven launchable |
| Driver / GPU misconfig on nodes | see §6 | red herring — see below |

None of these — nothing in the launch template beyond the instance-type/family list — was
narrowing the pool. The request was correct; the capacity simply was not there.

## 5. Red herring: "Error" device-plugin pods

Mid-investigation, several `nvidia-device-plugin` pods showed `0/2 … Error`, which looked
like a driver fault. It was not. Container exit codes told the real story:

- init container: exit `0` (Completed)
- `nvidia-device-plugin-ctr`: exit **`0`** (Completed)
- `nvidia-device-plugin-sidecar`: exit **`143`** (SIGTERM), same instant

Exit `0` + `143` at the same timestamp is the signature of the **node being torn down
underneath the pod** (spot reclamation / Karpenter churn), not a crash. It is downstream of
the same scarcity: nodes launch, then get reclaimed minutes later. Not a driver problem.

## 6. The stale g6 exclusion (the real unlock)

The nodepool had excluded g6 with this comment:

```
# g6 excluded: nvidia-fabricmanager fails on single-GPU L4 instances (no NVLink).
# g4dn (T4) and g5 (A10G) use DKMS-archived open kernel modules that support both families.
```

Three findings retired it:

1. **The AMI does no custom driver work.** `packer/gpu-nodeclass/fastsurfer.pkr.hcl` only
   pre-bakes the fastsurfer container into containerd; the driver comes entirely from AWS's
   stock `amazon-eks-node-al2023-x86_64-nvidia-<eks_version>-*` base AMI. That AMI now ships
   **driver 580.159.03 / CUDA 13.0**, which supports L4. The comment's "DKMS-archived open
   kernel modules" describes an older setup that no longer applies.

2. **The stated mechanism does not distinguish g5 from g6.** g5 (A10G) is *also* single-GPU
   with no NVLink, yet it is in the allowed list and works. "No NVLink" cannot be what breaks
   g6 if it does not break g5.

3. **Direct verification via SSM on a running g5 node** (`systemctl status
   nvidia-fabricmanager`): the service is `enabled` **and `failed`** — right now, on a node
   that is serving GPU workloads with an allocatable GPU. `fabricmanager` is only needed for
   NVSwitch multi-GPU systems (p4d/p5); on any single-GPU instance it fails to start and that
   is **benign** — it does not block node readiness or GPU allocation. g6 (L4) behaves
   identically to the g5 (A10G) that already works.

### Aside — why the device plugin exists at all if the AMI has the driver + CUDA

They do different jobs and both are required:

- **AMI driver + CUDA** = the GPU works at the *host OS* level (kernel module + userspace libs).
- **`nvidia-device-plugin`** (a Kubernetes DaemonSet) = the *Kubernetes* glue. Kubernetes'
  scheduler has no native GPU concept; the plugin implements kubelet's Device Plugin API,
  registers `nvidia.com/gpu` as an allocatable resource, and injects the `/dev/nvidia*` device
  nodes + driver libraries into each GPU pod's container namespace. Without it, a pod
  requesting `nvidia.com/gpu` stays `Pending` forever even though the driver is fully working.

This is also why a `fabricmanager` failure is harmless but a device-plugin failure is not:
the device plugin is on the critical path for GPU scheduling; `fabricmanager` is not.

## 7. The change

[terraform/modules/karpenter/helm-values/gpu-nodepool.yaml](../../terraform/modules/karpenter/helm-values/gpu-nodepool.yaml)
— `karpenter.k8s.aws/instance-family` values `["g4dn","g5"]` → `["g4dn","g5","g6","g6f"]`,
and the exclusion comment replaced with the corrected rationale.

Applied via Terraform — the nodepool is `kubectl_manifest.gpu-nodepool` in
[terraform/modules/karpenter/main.tf](../../terraform/modules/karpenter/main.tf) (reads the
YAML via `file()`), **not** ArgoCD, so there is no `selfHeal` race:

```bash
cd terraform
terraform plan  -target=module.karpenter.kubectl_manifest.gpu-nodepool   # 0 add, 1 change, 0 destroy
terraform apply -target=module.karpenter.kubectl_manifest.gpu-nodepool -auto-approve
```

`g6e` (L40S) and `g7` were deliberately **not** added: g6e is markedly pricier (keep as
overflow only), and g7's GPU generation was not validated against this AMI's driver at the
time. `g5g`/`g6g` are Graviton (ARM) and are excluded by the `amd64` arch requirement.

## 8. Verifying / reproducing

```bash
# AWS's own capacity estimate for the exact instance set (authoritative)
aws ec2 get-spot-placement-scores --region-names <YOUR_AWS_REGION> \
  --instance-types g4dn.xlarge g4dn.2xlarge g5.xlarge g5.2xlarge g6.xlarge g6.2xlarge \
  --target-capacity 15 --target-capacity-unit-type units --single-availability-zone

# The actual CreateFleet request AWS rejected (Type/TotalTargetCapacity/SpotOptions)
aws cloudtrail lookup-events --region <YOUR_AWS_REGION> \
  --lookup-attributes AttributeKey=EventName,AttributeValue=CreateFleet --max-results 5

# fabricmanager is benign — confirm it's enabled/failed on a working single-GPU node
aws ssm send-command --region <YOUR_AWS_REGION> --instance-ids <gpu-instance-id> \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["systemctl status nvidia-fabricmanager --no-pager | head -15","cat /proc/driver/nvidia/version"]'

# Live nodepool families + GPU nodeclaims
kubectl get nodepool gpu-nodepool -o jsonpath='{range .spec.template.spec.requirements[?(@.key=="karpenter.k8s.aws/instance-family")]}{.values}{"\n"}{end}'
kubectl get nodeclaims -o wide | rg gpu-nodepool
```

## 9. Levers when GPU capacity is tight again

- **Widen the pool** (done — g6/g6f). Highest-leverage; attacks the one dimension that
  genuinely limits capacity. Next candidates if needed: `g6e` (L40S, pricier), then validate
  `g7` against the AMI driver before adding.
- **Shrink Karpenter's batch size** — lower the Prefect Variable `cloudpipe-max-concurrent`
  so fewer subjects hit `fastsurfer-template-creation` at once, reducing the per-`CreateFleet`
  unit count (1-unit asks fare measurably better than 15-unit asks against thin pools).
- **Allow on-demand fallback** for `fastsurfer-template-creation` via a `capacity-type`
  spread if a batch must not wait on spot at all (cost tradeoff).

## 10. Loose ends (not addressed here)

- **Karpenter controller has no HA.** 2 of 3 `karpenter` controller replicas have been
  `Pending` for ~12 days — `FailedScheduling` from pod anti-affinity (`DoNotSchedule` zone
  topology spread) that cannot be satisfied on the current node set. Only 1 replica runs.
  Not causing this incident, but it means a single controller pod is a SPOF. Worth its own
  follow-up (relax the spread constraint, or ensure enough distinct-zone nodes for the
  controller pool).
- **First g6/g6f launch not yet directly observed.** The `fabricmanager`/driver evidence
  makes g6 a safe prediction, but at apply time `g4dn`/`g5` capacity had recovered and
  `price-capacity-optimized` prefers the cheapest available (`g4dn`), so g6/g6f sit as
  fallback headroom. Confirm a g6/g6f node reaches `Ready` with an allocatable GPU the first
  time one launches (or force it with a single test GPU pod pinned to g6).

## 11. Reference values (verify before trusting if stale)

| What | Value |
|---|---|
| GPU AMI in use | `ami-06eac2eb71c795efe` (`cloudpipe-gpu-fastsurfer-<tag>`), selected by `gpu-nodeclass` `amiSelectorTerms` tags |
| NVIDIA driver / CUDA (stock AL2023 NVIDIA AMI) | `580.159.03` / `13.0` |
| Service quota code (All G and VT Spot Instance Requests) | `L-3819A6DF` (2000 vCPU) |
| GPU nodepool source | `terraform/modules/karpenter/helm-values/gpu-nodepool.yaml` |
| Applied by | `kubectl_manifest.gpu-nodepool` in `terraform/modules/karpenter/main.tf` (Terraform, not ArgoCD) |
| AMI build | `packer/gpu-nodeclass/fastsurfer.pkr.hcl` (base: `amazon-eks-node-al2023-x86_64-nvidia-*`, owner `602401143452`) |
| GPU families now allowed | `g4dn` (T4), `g5` (A10G), `g6`/`g6f` (L4) — all single-GPU, `amd64`, `xlarge`/`2xlarge` |
