# GPU Time-Slice Headroom Handoff — 10-subject batch (2026-07-31)

**Purpose:** starting point for issue #102, the largest remaining cost lever. Everything in
§1–§2 is **measured** from the 2026-07-31 18:37Z batch; anything projected is labelled as a
hypothesis. The logs this rests on survive workflow deletion, so unlike the
`bold_to_t1w` handoff there is no "read before re-running" hazard here.

**Reproduce with:**

```bash
pixi run python scripts/gpu_duty_cycle.py --workflows \
  cloudpipe-c5hwv,cloudpipe-jghgf,cloudpipe-ncll6,cloudpipe-xxtd2,cloudpipe-c8rhn,\
cloudpipe-glgbt,cloudpipe-j869f,cloudpipe-mx8xt,cloudpipe-qpd5f,cloudpipe-vsn7s
```

---

## 0. Bottom line

**The GPU steps hold a slice for ~6x longer than they use the card.** `template-build` and
`long-segmentation` compute on the GPU for **~17% of their wall time** while occupying a
slice for 100% of it. That idle 83% is exactly what additional time-slices consume, and it
is the whole basis of #102.

VRAM is not the binding constraint either: peak device-wide usage is **~32% of an A10G**,
and that figure already includes both current co-tenants.

The counter-hypothesis I opened the issue with — *"if the GPU is at 100% utilization,
more slices just make each pod slower"* — is **disproved for these two steps**. It reads
100% only during short inference bursts that occupy a small minority of the pod's life.

---

## 1. Measured — 43 GPU pod logs, 10 workflows

Step names below are the canonical `cloudpipe.io/step` labels — the key pod-costs
and Athena are written against. The earlier revision of this table used
`fastsurfer-long-segmentation` / `fastsurfer-template-build`, which are neither the
step label nor the Argo template name and match nothing queryable. See
"Step names: which one to use where" in `docs/operations.md`.

| step | pods | GPU duty cycle | mean util *while busy* | peak VRAM (device-wide) | % of card |
|---|---|---|---|---|---|
| `long-segmentation` | 10 | **16.8%** | 78.2% | 6401 MiB | 27.8% |
| `template-build` | 10 | **17.3%** | 89.0% | 7385 MiB | 32.1% |
| `t1w-to-mni` | 23 | **59.1%** | 91.6% | 8893 MiB | 38.6% |

Card is `NVIDIA A10G`, 23028 MiB, on `g5.*` (the nodepool is capped to `xlarge`/`2xlarge`).
Sampling is one `nvidia-smi` row every 5 s; `duty cycle` = fraction of samples with
`utilization.gpu > 0`.

**Cross-check on the per-pod working set.** `gitops/apps/nvidia-device-plugin/values.yaml`
documents the seg working set as **~3.7 GiB, subject-independent** (256³ conform). The
measured 6401 MiB device-wide for `long-segmentation` divided by the 2 current tenants is
~3.2 GiB/pod. Those agree, which independently confirms the device-wide reading is
capturing both tenants — and means per-pod VRAM is genuinely ~3.2–3.7 GiB.

---

## 2. What the numbers permit

Two independent ceilings, both currently far from binding at `replicas: 2`:

- **VRAM:** 23028 MiB / ~3.7 GiB per pod ≈ **6 slices**
- **Compute:** 17% duty cycle ≈ **5–6 pods** before the card is continuously busy

**Hypothesis (not yet validated): `replicas: 4` is the well-supported next step** —
4 × 3.7 GiB = 14.8 GiB (64% VRAM), 4 × 17% = 68% duty. It leaves margin on both ceilings,
where 6 would sit on top of them, and duty cycle is an average that hides bursts.

`t1w-to-mni` at 59.1% duty is a different animal and should not drive the choice — it is
0.5 min of runtime and 1.7% of cost. Its issue (#105) is about the CPU request stranding a
slice, not about slice count.

---

## 3. Traps

**Do not raise `replicas` alone.** Three things move together:

1. `gitops/apps/nvidia-device-plugin/values.yaml` — `timeSlicing.resources[].replicas`
2. `terraform/modules/karpenter/.../gpu-timeslice-nodeoverlay.yaml` — the values.yaml comment
   says explicitly *"Keep replicas in sync with capacity in Karpenter's
   gpu-timeslice-nodeoverlay.yaml"*. Out of sync, Karpenter's capacity model disagrees with
   what the device plugin advertises and scheduling goes wrong in ways that look like
   scarcity. This half is **terraform** — needs an apply, and per
   `[[terraform-apply-stale-checkout]]` verify the live object afterwards, not the exit code.
3. **CPU becomes the new binding constraint.** Each GPU pod requests `cpu: 1`. At 4 slices a
   node needs 4 allocatable cores for GPU pods alone — a `g5.xlarge` (4 vCPU, ~3.9
   allocatable, minus system pods) **cannot host 4**. Either the effective packing silently
   caps at 3 on xlarge, or the instance-size floor has to move to `2xlarge`. Decide this
   deliberately; it interacts directly with the `instance-size In [xlarge, 2xlarge]` cap
   that produced the current cost win.

**Parsing traps** (already handled in `scripts/gpu_duty_cycle.py`, noted so nobody
re-derives them): the `nvidia-smi` CSV rows are interleaved *into* tqdm progress-bar lines
and do not start at column 0; and `memory.used` is device-wide, not per-process.

**Sampling resolution.** 5 s sampling under-counts sub-5s GPU bursts, so 16.8% is a floor,
not a point estimate. It would have to be wrong by ~3x to change the conclusion.

---

## 4. Suggested order of work

1. **Land #105 first** (`t1w-to-mni` 2cpu/4G → 1cpu/1G). It changes GPU packing, so measuring
   #102 on top of the current baseline would confound the two.
2. Decide the slice count from §2, and resolve the CPU/instance-size interaction in §3.3 in
   the same change.
3. Update `values.yaml` + the Karpenter nodeoverlay together; `terraform apply`; verify the
   live NodePool/overlay objects.
4. **Exclusive** 10-subject batch on `tools/cloudpipe_test_sample_10.csv` — no concurrent
   batch, because this is a placement change and a co-tenant batch changes node composition.
5. Compare against the baseline below.

---

## 5. Baseline to beat

From the 2026-07-31 18:37Z batch (`dt=2026-07-31`, workflow names in §0's command):

| | value |
|---|---|
| batch total | **$3.60** / 10 subjects |
| per subject | **$0.360** |
| `template-build` | $0.873 (24.2%), 11.5 min mean |
| `long-segmentation` | $0.833 (23.1%), 12.9 min mean |
| combined target of #102 | **47.3% of batch cost** |

Absolute dollars carry the day+1 Kubecost scrape drift (~2x settled), so compare **ratios and
per-step shares**, not billed dollars. Hold the subject CSV fixed — the earlier baseline
batch overlapped on only 2 of 10 subjects, which made total-cost comparison useless.

Expect wall time per pod to *increase* somewhat as slices contend; the win is node-hours, not
latency. Check that `long-parcellation`, which is downstream and CPU-only, does not become the
new critical path.

---

## 6. Related

- Issue #102 (this work), #105 (must land first), epic #75
- `docs/investigations/2026-07-20-cloudpipe-cost-reduction-deep-dive.md`
- `e1eb976` — GPU nodepool cap + FastSurfer right-sizing; establishes why `cpu: 1` must stay
- The FireANTs `fused_ops` measurement found time-slicing worth only +6.5% **for that step** —
  a different step with a different duty cycle. It does not transfer here, and the 17% figure
  above is the reason.
