# cloudpipe_minproc Cost Reduction — Structural Deep Dive

**Date:** 2026-07-20
**Pipeline:** `cloudpipe_minproc`
**Scope:** Independent, code-and-infrastructure-grounded analysis of where run cost is incurred
and the highest-leverage ways to reduce it without a material loss of throughput (runtime per
subject/session).
**Method:** Derived from the workflow templates, Karpenter nodepools, and device-plugin config as
they exist on `main` at this date. Cost *magnitudes* are deliberately not quoted from prior
scrapes — see [§7 Measurement prerequisite](#7-measurement-prerequisite-do-this-before-committing-effort).

---

## 1. Two framing facts

Before any specific lever, two facts reframe where the money is:

- **Every nodepool is already spot-only** —
  [gpu-nodepool.yaml:37-39](../../terraform/modules/karpenter/helm-values/gpu-nodepool.yaml),
  [cpu-heavy-nodepool.yaml:37-39](../../terraform/modules/karpenter/helm-values/cpu-heavy-nodepool.yaml),
  [cpu-light-nodepool.yaml:36-38](../../terraform/modules/karpenter/helm-values/cpu-light-nodepool.yaml).
  The spot discount is fully captured. The remaining levers are **utilization** (paying for idle
  capacity) and **right-sizing** (over-requesting), not pricing.

- **The GPU device plugin runs `migStrategy: none` with no time-slicing** —
  [gitops/apps/nvidia-device-plugin/values.yaml:12-17](../../gitops/apps/nvidia-device-plugin/values.yaml).
  Every GPU pod owns a whole physical GPU even though the FastSurfer working set is ~3.7 GiB of a
  16–24 GiB card. This is the single largest inefficiency in the system.

## 2. Structural cost model

Cost = **node-hours × instance price**, and node-hours split two ways:

1. **Anatomical (FastSurfer)** dominates wall-clock — hours per subject on both GPU (segmentation,
   template creation) and cpu-heavy (surface parcellation). Most spend lives here.
2. **Registration + functional** is short compute per unit but **fans out into many small pods**, so
   per-pod overhead — node provisioning, image pull, repeated S3 downloads — dominates its cost
   rather than the compute itself.

The two buckets to attack are therefore **GPU idle/exclusivity** and **per-run pod overhead**.

---

## 3. Tier 1 — highest expected value

### 3.1 Enable GPU time-slicing (pack 2–4 FastSurfer pods per physical GPU)

> **UPDATE 2026-07-20 (measured, deployed).** Shipped + tested on a 10-subject batch. The mechanism
> works (nodes advertise 2 GPU, two pods share a T4, no OOM), but the "divide node-hours by the
> replica count" impact below was **overstated: CPU is co-binding, not just GPU.** The seg pods
> requested `cpu: 2`, and a g4dn.xlarge has only 3920m allocatable, so two never fit CPU-wise —
> Karpenter ran ~1 node/pod. Fix (`b482d58`): drop the 3 GPU-slice steps to `cpu: 1`/`--threads 1`,
> which packs 2/g4dn.xlarge. Measured cost of that: segmentation ~1.7× slower (under the 2× bar),
> net ≈ **-45% GPU node-hours** (not -50%) *when packing is realized*. Packing needs GPU-slot
> pressure or the §4.2 consolidation change to trigger — time-slicing is necessary, not sufficient.
> Full data: [handoffs/gpu-time-slicing.md](../../handoffs/gpu-time-slicing.md) "Measured results".

**Evidence.** Measured peak working set is documented as *"~3.7 GiB (3694 MiB); brains are conformed
to a fixed 256³ volume"*
([gpu-nodeoverlay.yaml:26-27](../../terraform/modules/karpenter/helm-values/gpu-nodeoverlay.yaml)).
GPU steps request only `2G/2cpu` + one whole GPU
([fastsurfer-template-phase:222-227](../../argo/workflows/cloudpipe_minproc/fastsurfer-template-phase-workflow-template.yaml),
[fastsurfer-long-phase:62-65](../../argo/workflows/cloudpipe_minproc/fastsurfer-long-phase-workflow-template.yaml)).
A T4 (g4dn) has 16 GiB; A10G/L4 (g5/g6) have 24 GiB. One pod uses **15–23%** of card memory and,
during I/O and CPU phases, ~0% of compute.

**Mechanism.** Set `replicas: 2` (or 3–4) in the device-plugin time-slicing config. `nvidia.com/gpu: 1`
then maps to a time-sliced slice; Karpenter packs multiple GPU pods per node.

**Impact.** Divides GPU node-hours for the seg-heavy steps by the replica count. If GPU is a large
share of spend, 2× slicing is a very large cut.

**Risk / bounding.** Time-slicing does **not** isolate VRAM (unlike MIG), so `replicas × 3.7 GiB`
must stay under the card: 4× = 14.8 GiB is tight on a 16 GiB T4 but comfortable on 24 GiB A10G/L4.
Pair slicing with a nodepool preference for g5/g6, or cap at 2× to stay safe on T4. Compute
contention slows each pod, but throughput-per-dollar is the objective. The `nvidia-smi` monitors
already streaming in every GPU pod
([fastsurfer-template-phase:179-180](../../argo/workflows/cloudpipe_minproc/fastsurfer-template-phase-workflow-template.yaml))
give the utilization traces to confirm headroom before rollout.

### 3.2 Coalesce per-run pods into per-session pods

> **UPDATE 2026-07-21 (implemented, not yet run on the cluster).** Both fan-outs are
> now one pod per session: `bold-to-t1w-session-template` and
> `functional-preprocessing-session-template`, each looping over the session's runs
> inside the container with per-run try/continue. The shared FastSurfer tarball, MNI
> template and t1w→MNI warp are downloaded once per session instead of once per run.
>
> Two mechanics were forced by Argo rather than chosen. (1) `inputs.artifacts` is
> resolved statically when the pod spec is built, so a varying run count cannot be
> enumerated — per-run inputs now arrive as **whole-prefix directory artifacts**
> (`mmps_mproc/{subj}/{ses}/func/`, `derivatives/registration/{subj}/{ses}/`).
> (2) One static output artifact cannot emit N differently-named tarballs, so the
> func-preproc driver builds each run's `_bold.tar.gz` itself and uploads them via a
> single directory artifact. S3 keys are unchanged, so `inventory.py`'s `b2t_exists` /
> `func_exists` markers still gate reruns.
>
> Costs of the change, beyond the doc's retry-granularity note: a directory artifact
> re-downloads BOLDs for already-complete runs on a **partial** rerun, and a **spot
> reclaim now loses a session's in-flight work rather than one run's**, because Argo
> only saves output artifacts when the pod completes. Ephemeral-storage requests rose
> 4–5G → 20G (limit 30G) to hold a session's inputs at once.
>
> **Directory-artifact behaviour, measured on-cluster** (throwaway probe workflows,
> Argo v4.0.7, since this was the load-bearing unknown):
>
> | prefix | key form | result |
> |---|---|---|
> | contains a 0-byte marker object | trailing `/` | **0 files, exit 0** |
> | contains a 0-byte marker object | no trailing `/` | downloads correctly |
> | no marker object | either form | downloads correctly |
>
> The Globus S3 gateway leaves a zero-byte "directory marker" object whose key *equals*
> the prefix under every `mmps_mproc/…` path. With a trailing slash Argo's S3 driver
> then silently downloads **nothing and still succeeds** — the failure would surface
> much later as confusing missing-input errors inside the driver. **Input** directory
> keys therefore carry no trailing slash; **output** directory keys must keep it (Argo
> docs: artifact GC cannot clean the directory otherwise). Verified against the real
> `mmps_mproc/{subj}/{ses}/func` and `…/anat` prefixes, large NIfTIs included. The
> output side was confirmed to map local `{subdir}/{file}` onto `{key}{subdir}/{file}`,
> which is what preserves the `bold_to_t1w_{task}_{run}/` keys.
>
> **Still unverified:** a full session end-to-end (real compute, real runtimes). The
> artifact plumbing itself is now measured rather than assumed.

**Evidence.** `bold-to-t1w` and `functional-preprocessing` both fan out per `(session, task, run)`.
Each func-preproc pod independently pulls the large AFNI image and re-downloads **the same
per-session FastSurfer tarball**
([functional-preprocessing:210-216](../../argo/workflows/cloudpipe_minproc/functional-preprocessing-workflow-template.yaml)),
the MNI template, etc. `bold-to-t1w` *also* downloads that same tarball per run
([registration:356-362](../../argo/workflows/cloudpipe_minproc/registration-workflow-template.yaml)).
With many BOLD runs/subject, a session's tarball is fetched and decompressed a dozen-plus times, and
a dozen pods each pay node-provisioning + image-pull startup for short compute.

**Mechanism.** Loop over a session's runs inside a single pod: download shared inputs once, iterate
runs. Amortizes image pull, node spin-up, and the redundant tarball/template downloads across the
session's runs.

**Impact.** Cuts the *count* of cold-starts and redundant S3/decompression by ~runs-per-session on
the entire registration+functional branch — the dominant CPU-side inefficiency.

**Risk.** Loses per-run retry granularity (one run's failure retries the whole session). Mitigate
with an inner per-run try/continue and per-run idempotency checks (outputs already gate on
`_itk.txt` existence).

---

## 4. Tier 2 — resource right-sizing

### 4.1 `bold-to-t1w` requests 16 GiB (request = limit) — audit and likely shrink

**Evidence.**
[registration:449-457](../../argo/workflows/cloudpipe_minproc/registration-workflow-template.yaml),
rationale *"BBR can spike to ~12 GB on long BOLD runs."* But the step header says it uses
**SynthMorph** (a ~5 s forward pass), not BBR
([registration:310-311](../../argo/workflows/cloudpipe_minproc/registration-workflow-template.yaml)),
and [decisions/002-synthmorph-over-bbregister.md](../decisions/002-synthmorph-over-bbregister.md)
records that migration — so the 16 GiB BBR rationale may be **stale**. cpu-heavy allows `c` and `m`
families at 2xlarge/4xlarge
([cpu-heavy-nodepool.yaml:41-46](../../terraform/modules/karpenter/helm-values/cpu-heavy-nodepool.yaml));
a 16 GiB request cannot fit a `c`-2xlarge (16 GiB total minus system overhead) and forces the
pricier `m` family or a 4xlarge.

**Action.** Measure real peak (same nodes; pull cgroup peak or add it to the QC JSON). If SynthMorph
peak is well under 8 GiB, drop the request so it packs onto cheaper `c`-2xlarge, 2-per-node.

### 4.2 GPU node `consolidateAfter: 30m` — idle time on the most expensive nodes

**Evidence.**
[gpu-nodepool.yaml:6-8](../../terraform/modules/karpenter/helm-values/gpu-nodepool.yaml) uses
`WhenEmpty` + **30 min** linger. cpu-light uses `WhenEmptyOrUnderutilized` + 10 min
([cpu-light-nodepool.yaml:6-8](../../terraform/modules/karpenter/helm-values/cpu-light-nodepool.yaml)).
An empty GPU node bleeds node cost for up to 30 min at every batch-tail or phase gap.

**Tradeoff (real, documented).** The
[2026-07-17 GPU spot-scarcity investigation](2026-07-17-gpu-nodepool-spot-scarcity-g6-enablement.md)
shows GPU re-provisioning can stall on thin spot pools, so the 30 min linger is partly deliberate
warm-keeping. Don't blindly cut to 1 min. Measure idle GPU node-hours in Kubecost; if the batch
keeps GPUs busy (time-slicing from §3.1 helps), lower to ~5–10 min. This lever **interacts** with
§3.1: slicing keeps nodes fuller, making a shorter linger safe.

### 4.3 `template-creation` holds a full GPU during CPU-bound alignment

**Evidence.** The step's own comment: brainmask inference uses the GPU, but *"mri_robust_template is
invoked with no thread flag"* and the multi-session T1w alignment is CPU-bound
([fastsurfer-template-phase:164-171](../../argo/workflows/cloudpipe_minproc/fastsurfer-template-phase-workflow-template.yaml)).
For a multi-session subject the GPU sits near-idle during robust-template alignment while still
billing GPU node time.

**Action.** Either (a) split GPU brainmask inference from CPU robust-template into separate pods, or
(b) let time-slicing (§3.1) absorb it — a shared idle GPU largely moots the waste without
restructuring. (b) is far cheaper to implement, reinforcing §3.1 as the anchor change.

---

## 5. Tier 3 — methodological / software

### 5.1 Cross-sectional fast path for single-session subjects

The longitudinal FastSurfer stream runs a **base template** (seg + surf) *plus* per-session
**long-segmentation** and **long-parcellation**
([fastsurfer-long-phase](../../argo/workflows/cloudpipe_minproc/fastsurfer-long-phase-workflow-template.yaml)).
For a single-session subject that is roughly double the anatomical work of a plain cross-sectional
run, for no longitudinal benefit. Branch single-session subjects (known from inventory) to a
cross-sectional `run_fastsurfer.sh` and skip base+long. This is a tail optimization (the cohort
averages multiple sessions/subject) but a correctness-neutral, free saving on that tail.

### 5.2 Confirm what downstream actually consumes from surface parcellation

`surf_only` parcellation is the long CPU pole (hours on cpu-heavy). func-preproc's aCompCor only
needs `aseg.auto.mgz`
([functional-preprocessing:292-293](../../argo/workflows/cloudpipe_minproc/functional-preprocessing-workflow-template.yaml)),
produced by the *segmentation* step. If full surface parcellation is only required where
subregion/subfield outputs are consumed, and those are not needed for every subject, parcellation
could be made conditional. **Requires a downstream-consumer audit before acting** — this is a
question, not a confirmed cut. (Subregion segmentation depends on surfaces, so parcellation is
likely required wherever subfields are.)

### 5.3 NAT egress on public image pulls

Per the [2026-07-20 NAT/ECR investigation](2026-07-20-nat-ingress-ecr-public-image-pulls.md), init
containers pull `public.ecr.aws/docker/library/alpine:3.21`
([fastsurfer-template-phase:139](../../argo/workflows/cloudpipe_minproc/fastsurfer-template-phase-workflow-template.yaml))
and similar through the NAT gateway ($/GB). Mirror these to private ECR (behind the S3/ECR gateway
path) or bake them into the AMI. Small per-pull, multiplied across every pod in every batch.

---

## 6. Waste elimination (measure first)

- **Retry storms.** `retryStrategy` limits run 3–6
  ([functional-preprocessing:117-118](../../argo/workflows/cloudpipe_minproc/functional-preprocessing-workflow-template.yaml))
  with `failFast: false`. A subject failing repeatedly on a GPU step burns node time with zero
  output. Quantify failed-run node-hours — spend that produces nothing is the cheapest to eliminate.
- **func-preproc consolidation block.** `karpenter.sh/do-not-disrupt: "true"` + PDB
  `minAvailable: 100%`
  ([functional-preprocessing:33-38](../../argo/workflows/cloudpipe_minproc/functional-preprocessing-workflow-template.yaml))
  protect in-flight runs but also pin nodes open against consolidation. Reasonable for long runs;
  revisit if runs are actually short.

---

## 7. Measurement prerequisite (do this before committing effort)

Because cost *magnitudes* are intentionally not quoted here, the honest prerequisite is to
**re-derive the GPU-hour vs CPU-hour split and idle-node-hours from Kubecost** — aggregate by the
`cloudpipe.io/phase` and `cloudpipe.io/step` labels the templates already emit. That determines
whether to spend first on the GPU levers (§3.1, §4.2, §4.3) or the CPU-fanout levers (§3.2, §4.1).
The `nvidia-smi` utilization traces already in the GPU pod logs are the fastest way to confirm the
GPU-idle premise behind §3.1 and §4.3 without new instrumentation.

> **STATUS 2026-07-21 — NOT DONE, and deliberately gated.** No per-phase/per-step node-hour or
> idle-hour aggregation has been run or recorded. The Tier-1 work (§3.1, §4.2, and the §3.2
> implementation) went ahead *without* this measurement, validated instead against **physical**
> proxies that survive cost-reconciliation error: GPU node count on an identical 10-subject batch
> (9 → 5) and per-step wall-clock from Argo `startedAt`/`finishedAt`. Ratios are robust to a
> systematic pricing multiplier; absolute dollars are not.
>
> **Why it is gated, not forgotten.** Kubecost prices usage at near-on-demand estimates and
> reconciles toward billed cost as the AWS CUR finalizes. On these spot-only nodepools a **day+1
> scrape is only ~20% reconciled and overstates settled cost by roughly 2×**, while per-day
> allocation data *degrades* after ~3-4 weeks (workflow counts collapse). That leaves a narrow
> **~day+14-20 window that is both settled and complete** — the only window this measurement is
> worth running in. The reconciliation-convergence probe (PR #31, `f407e99`) is now wired into the
> nightly Prefect flow and writes to `s3://<YOUR_S3_BUCKET>/metrics/cost-drift-probe/`; it needs ~2 weeks of
> readings, i.e. ripe around **2026-08-04**.
>
> Any number taken from the `metrics/costs/` S3 history is a scrape-time (day+1) snapshot and
> carries the same ~2× overstatement — including the GPU ~50% / CPU ~49% component split in the
> [2026-07-18 handoff §1](2026-07-18-cost-reduction-handoff.md). That split is a *component* split,
> not the per-phase node-hour split §7 asks for, and it should not be used to prioritise.

### 7.1 Checklist — run this once the probe is ripe (~2026-08-04)

- [ ] **Confirm the gate.** List `s3://<YOUR_S3_BUCKET>/metrics/cost-drift-probe/` and check there are ~2
      weeks of readings. Track one batch date (e.g. `2026-07-17`) as it ages and find where its
      `*CostAdjustment` **plateaus** — that age is the true settled lag. Cross-sectional estimate is
      ~day+16; the plateau measurement supersedes it.
- [ ] **Pick a settled window.** Choose a real batch date inside the day+14-20 band and express it
      as an explicit RFC3339 UTC range — not a relative `--window`, which would drift out of the
      band on re-runs. Skip dates whose workflow counts come back degraded/partial.
- [ ] **Per-phase split** (the prioritisation number):
      `pixi run python scripts/cloudpipe_minproc_costs.py <START> <END> --by phase -o phase-settled.csv`
- [ ] **Per-step split** (locates the pole inside the winning phase):
      `pixi run python scripts/cloudpipe_minproc_costs.py <START> <END> --by step -o step-settled.csv`
- [ ] **Read idle out of the CSV, not from a flag.** With the default `shareIdle=true`, idle is
      distributed into each allocation and lands in the `cpuCostIdle` / `ramCostIdle` /
      `gpuCostIdle` columns — those are the per-phase idle-hour attribution. `--no-share-idle`
      does *not* expose an idle bucket: [cloudpipe_minproc_costs.py:87](../../scripts/cloudpipe_minproc_costs.py#L87)
      filters `__idle__` and `__unallocated__` out of the response entirely.
- [ ] **Cluster-level idle total** (the §4.2 number — GPU nodes lingering empty) needs a raw call
      that keeps the `__idle__` key. Use the `curl` form in
      [docs/observability.md § "Direct API access"](../observability.md) with
      `aggregate=label:cloudpipe.io/phase`, and read `__idle__` rather than discarding it.
- [ ] **Sanity-check against physical reality** before believing the dollars: `gpuHours` in the
      per-step CSV should be consistent with the node counts and step durations recorded in
      [handoffs/gpu-time-slicing.md](../../handoffs/gpu-time-slicing.md). If they disagree, the
      window is wrong (unsettled, degraded, or spanning a config change), not the pipeline.

**Access requirements.** Kubecost is reachable only over the VPN / Cloudflare Access path. The
script itself needs no AWS credentials unless `-o s3://…` is used; `AWS_PROFILE=<YOUR_NETID>` is
required for that (the `[default]` profile has no SSO config).

**What this unblocks.** §4.1 (`bold-to-t1w` 16 GiB audit), §4.3, and all of Tier 3 were explicitly
gated on knowing the split. §4.2's remaining question — how many GPU node-hours are *empty* linger
— has no physical proxy anyone has collected, so it depends on this measurement alone.

---

## 8. Recommended sequence

1. **GPU time-slicing** (§3.1) — largest structural win; also de-risks §4.2 and moots §4.3.
2. **Per-session pod coalescing** (§3.2) — dominant CPU-fanout win.
3. **`bold-to-t1w` memory audit** (§4.1) — cheap right-sizing, unlocks `c`-family packing.
4. **GPU consolidation tuning** (§4.2) — safe to tighten once §3.1 keeps GPUs fuller.

Tier 3 and §6 are opportunistic follow-ups gated on the §7 measurement and (for §5.2) a
downstream-consumer audit.

---

## 9. Reference — key locations

| Concern | Location |
|---|---|
| GPU device plugin (no time-slicing) | [gitops/apps/nvidia-device-plugin/values.yaml](../../gitops/apps/nvidia-device-plugin/values.yaml) |
| GPU nodepool (spot-only, consolidation 30m) | [terraform/modules/karpenter/helm-values/gpu-nodepool.yaml](../../terraform/modules/karpenter/helm-values/gpu-nodepool.yaml) |
| Measured GPU working set (~3.7 GiB) | [gpu-nodeoverlay.yaml:26-27](../../terraform/modules/karpenter/helm-values/gpu-nodeoverlay.yaml) |
| GPU seg steps | [fastsurfer-template-phase](../../argo/workflows/cloudpipe_minproc/fastsurfer-template-phase-workflow-template.yaml), [fastsurfer-long-phase](../../argo/workflows/cloudpipe_minproc/fastsurfer-long-phase-workflow-template.yaml) |
| Per-run fan-out | [registration](../../argo/workflows/cloudpipe_minproc/registration-workflow-template.yaml), [functional-preprocessing](../../argo/workflows/cloudpipe_minproc/functional-preprocessing-workflow-template.yaml) |
| `bold-to-t1w` 16 GiB request | [registration:449-457](../../argo/workflows/cloudpipe_minproc/registration-workflow-template.yaml) |
| cpu-heavy nodepool (c/m, 2xl/4xl) | [terraform/modules/karpenter/helm-values/cpu-heavy-nodepool.yaml](../../terraform/modules/karpenter/helm-values/cpu-heavy-nodepool.yaml) |
