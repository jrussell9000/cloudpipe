# Per-Node GPU Time-Slicing — Costing

**Date:** 2026-09-03
**Question:** the cluster-wide `timeSlicing.replicas` is capped by the weakest card in the
pool. What does moving to per-node configs actually buy?
**Predecessor:** [2026-07-31-gpu-vram-duty-cycle-handoff.md](2026-07-31-gpu-vram-duty-cycle-handoff.md)
(issue #102) — its §1 measurements are reused here and still stand.

---

## 0. Bottom line

**#102 is already closed** (2026-08-01, after its duty-cycle measurement drove the 2→3 slice
move). This document answers the obvious follow-up — *"so why not 4 or 5?"* — and the answer
is **don't, and here is the evidence, so nobody re-derives it.**

**The answer is: not worth doing — on cost-benefit, and now also because the one node type
that could benefit cannot be acquired.**

- **Cost:** saving over the ~6,100 subjects left in leg 2 is **$22–73 on day+1 figures** —
  roughly **$11–37 settled**. Under 1% of remaining cohort cost.
- **Only one node type could ever benefit: `g6.2xlarge`.** Every other combination either
  cannot exceed 3 slices or costs more per slice than today's baseline.
- **And `g6` cannot be acquired.** CloudTrail shows `g5`/`g6`/`g6e` offered on **16 of 16**
  real fleet requests and granted **0 times** — every instance returned was `g4dn.xlarge`
  (§6). The cost win has no reachable destination.
- **Implementation is not a one-value change.** Pods hard-select the nodepool by name, and
  Karpenter cannot label nodes conditionally by instance type (§5). It needs a second
  NodePool, relaxed pod selection, a new overlay, a two-profile plugin config, and an
  exclusive validation batch that cannot run while leg 2 owns the cluster.

**That combination — ~$30 of savings against a nodepool restructure — is the whole case for
not doing it.** It does not depend on any capacity claim.

> ### Revision history (§6)
>
> **v1** claimed the capacity argument was *refuted* and `g6.2xlarge` *thin*, from
> `get-spot-placement-scores`. **v2 withdrew both** — `g4dn.xlarge`, the type serving 100% of
> GPU work, scores the same 1/10 as every family that never appears, so the instrument had no
> discriminating power there.
>
> **v3 (this version) reinstates the "g6 is thin" conclusion on CloudTrail evidence.** Across
> 16 real `CreateFleet` requests, `g5`/`g6`/`g6e` were offered **16 times and granted 0
> times**; every instance returned was `g4dn.xlarge`. The pools are empty, not un-offered.
> v2's withdrawal was still correct: the claim was right, the evidence for it was not.
>
> v3 also corrects two inherited premises — **`CreateFleet` partially fulfils** (it is not
> all-or-nothing), and small asks are not reliably fulfilled either — and identifies **`g7` as
> the only family with capacity**. See §6.

---

## 1. What changed since the 2026-07-31 handoff

That document was written at `replicas: 2` and proposed 4 as the next step. The cluster has
since moved to **3** (`gitops/apps/nvidia-device-plugin/values.yaml`, overlay renamed
`gpu-timeslice-3x`), so its headline recommendation is one step stale. Two other inputs moved:

| | 2026-07-31 | now (2026-09-02 cost partition, n=592) |
|---|---|---|
| `timeSlicing.replicas` | 2 | **3** |
| GPU share of run cost | 47.3% (2 steps) | **31.0%** |
| cost per subject | $0.360 | **$0.120** |
| GPU cost per subject | — | **$0.0372** |

The GPU share fell because the 2→3 slice move already banked part of this lever, and
right-sizing landed. **The remaining headroom is smaller than #102's framing implies.**
Per-subject spread is wide (p10 12.9%, median 28.6%, p90 95.6%) — subjects whose FastSurfer
derivatives already exist skip the GPU phase entirely.

> All dollar figures carry `scrape_age_days: 1`. Per `[[kubecost-cost-drift-is-large]]`,
> day+1 overstates settled cost by a median ~51%. Ratios are trustworthy; absolutes are not.

---

## 2. The three ceilings

Slice count per node is `min(CPU, VRAM, duty cycle)`. All three are measured, not assumed.

**CPU.** Every GPU step requests exactly `cpu: 1` — verified live: the `main` container
requests 1, the `wait` sidecar requests none, and the `download-fsaverage` init requests 1
(init containers don't stack with the main request). So slices ≤ floor(allocatable cores).

- `g4dn.xlarge` allocatable is **3920m** → 3 pods (3000m). A 4th needs 4000m > 3920m.
- Confirmed in production: **24 nodes running 3 GPU pods, 3 running 2**. The current config
  is already at its ceiling.

**VRAM.** Working set is ~3.7 GiB/pod, subject-independent (values.yaml; corroborated by the
handoff's measured 6401 MiB device-wide ÷ 2 tenants ≈ 3.2 GiB).

**Duty cycle.** 16.8% / 17.3% for `long-segmentation` / `template-build` (43 pod logs). The
card is continuously busy at ~6 pods, so **5 (≈85%) is the safe target, 6 (≈102%) sits on the
ceiling**. The handoff explicitly warns duty cycle is an average that hides bursts.

### Effective slices per node type

| Node | CPU | VRAM | Duty | **Effective** | vs today |
|---|---|---|---|---|---|
| `g4dn.xlarge` (T4, 15109 usable) | 3 | **3** | 5 | **3** | — |
| `g4dn.2xlarge` (T4) | 7 | **3** | 5 | **3** | — |
| `g5.xlarge` (A10G) | **3** | 6 | 5 | **3** | — |
| `g5.2xlarge` (A10G, 23028) | 7 | 6 | **5** | **5** | +2 |
| `g6.xlarge` (L4) | **3** | 6 | 5 | **3** | — |
| `g6.2xlarge` (L4, ~23034) | 7 | 6 | **5** | **5** | +2 |
| `g6e.xlarge` (L40S) | **3** | 12 | 5 | **3** | — |
| `g6e.2xlarge` (L40S, 46068) | 7 | 12 | **5** | **5** | +2 |

**Two structural findings:**

1. **No `xlarge` can ever exceed 3** — CPU binds at 3920m regardless of card. Per-node
   time-slicing is worthless unless the fleet also moves to `2xlarge`.
2. **`g4dn` is stuck at 3 at any size** — the T4's 15109 MiB usable admits only 3 × 3.7 GiB
   (a 4th would need 15156 MiB). And `g4dn` is the cheapest node, so today's
   price-capacity-optimized winner is precisely the one that cannot improve.

---

## 3. Cost per slice-hour

Spot means, <YOUR_AWS_REGION>, 7-day window ending 2026-09-03.

| Node | $/node-hr | Slices | **$/slice-hr** | vs baseline |
|---|---|---|---|---|
| `g4dn.xlarge` @3 *(today's baseline)* | 0.1737 | 3 | **0.0579** | — |
| `g4dn.2xlarge` @3 | 0.1985 | 3 | 0.0662 | +14% ❌ |
| **`g6.2xlarge` @5** | 0.2344 | 5 | **0.0469** | **−19%** ✅ |
| **`g6.2xlarge` @6** *(aggressive)* | 0.2344 | 6 | **0.0391** | **−32%** ✅ |
| `g5.2xlarge` @5 | 0.4448 | 5 | 0.0890 | +54% ❌ |
| `g6e.2xlarge` @5 | 0.9720 | 5 | 0.1944 | +236% ❌ |

**Only `g6.2xlarge` beats the baseline.** `g5` and `g6e` are too expensive per node to win
even at doubled density. So "per-node time-slicing" is, concretely, *"get GPU work onto
`g6.2xlarge` at 5–6 slices, keeping `g4dn.xlarge` at 3 as the fallback."*

This is favourable for one narrow reason: `g6.2xlarge` costs only **35% more per node** than
`g4dn.xlarge` while carrying **67–100% more slices**.

---

## 4. Dollar impact

GPU cost per subject is **$0.0372** (day+1). Saving scales with the fraction *f* of GPU
node-hours that actually migrate to `g6.2xlarge` — Karpenter will only place there when g6
spot capacity exists.

Over the **~6,103 subjects remaining** in leg 2:

| | f = 0.5 | f = 0.7 | f = 1.0 |
|---|---|---|---|
| **5 slices** (−19%) | $22 | $30 | $43 |
| **6 slices** (−32%) | $36 | $51 | $73 |

Halve these for settled cost: **~$11–37**.

For scale: that is **0.3–1.0% of the ~$730 day+1 cost** of the remaining cohort. It is not a
material cost lever at this cohort size, and it is dwarfed by the ~$180/mo the metrics
buckets alone cost.

---

## 5. Implementation cost is the real obstacle

The device plugin supports multiple named profiles under `config.map`, selected per node by
the `nvidia.com/device-plugin.config` label. But **Karpenter cannot set that label
conditionally by instance type** — `spec.template.metadata.labels` is static per NodePool. And
all three GPU templates hard-code the pool:

```yaml
nodeSelector:
  karpenter.sh/nodepool: gpu-nodepool
```

(`fastsurfer-template-phase`, `fastsurfer-long-phase`, `registration` workflow templates.)

| Option | Work | Verdict |
|---|---|---|
| **A. Second NodePool for dense families** + relax the 3 templates from a hard nodepool selector to a shared label; new NodeOverlay; 2-profile device-plugin config | new nodepool, new overlay, 3 template edits, label wiring, exclusive validation batch | Cleanest; uses only existing mechanisms. Karpenter bin-packs to the cheaper-per-pod pool and falls back to `g4dn` when g6 is short — exactly the resilience we want. |
| **B. External node labeller** (NFD/GFD or custom DaemonSet) to label by instance type | new cluster component; this cluster runs the bare device plugin, not the GPU Operator | Adds standing infra for a $30 saving. |
| **C. Drop `g4dn`, go g6-only `2xlarge`, raise uniform replicas to 5** | one values.yaml + one overlay edit | Simplest, but **sacrifices the deepest spot pool** — the opposite of what the crunches demand. Reject. |

Option A also changes WorkflowTemplates, so per `[[argo-registry-flip-needs-drain]]` the
in-flight batch would not pick it up — it needs a drain or lands only for the next cohort.

---

## 6. The node-count argument — measured, then the measurement withdrawn

At 5 slices instead of 3, the same concurrent GPU pod count needs **40% fewer nodes** — the
~34–40 node fleet becomes ~20–24. The theory was that this shrinks the all-or-nothing
`CreateFleet` ask described in
[2026-07-17-gpu-nodepool-spot-scarcity-g6-enablement.md](2026-07-17-gpu-nodepool-spot-scarcity-g6-enablement.md)
§3, where a 15-unit ask failed and a 1-unit ask succeeded.

**`get-spot-placement-scores` — AWS's own estimate for the exact instance set, 2026-09-03:**

| Instance set | Target units | use2-az1 | use2-az2 | use2-az3 |
|---|---|---|---|---|
| Full pool (8 types) | 40 *(today's fleet)* | 1 | 1 | 1 |
| Full pool | **24** *(post-change)* | **1** | **1** | **1** |
| Full pool | 10 | 1 | 1 | 1 |
| Full pool | **1** | **2** | **2** | **2** |
| `g6.2xlarge` only | 24 | 1 | 1 | 1 |
| `g6.2xlarge` only | **5** | **1** | **1** | **1** |
| `g4dn.xlarge` only | 40 | 1 | 1 | 1 |
| `g4dn.xlarge` only | 24 | 1 | 1 | 1 |

### ⚠️ Correction (2026-09-04): this measurement does not support the conclusions first drawn from it

The first version of this section drew two conclusions — *"the 40% reduction buys nothing"*
and *"`g6.2xlarge` is thin"*. **Both are withdrawn.** The control that should have been run
first was run later:

| Instance set | Target units | Score | Do we actually run it? |
|---|---|---|---|
| `g4dn.xlarge` only | 5 | **1** | **Yes — 100% of GPU work, acquired continuously** |
| `g6.xlarge` only | 5 | **1** | Never observed |
| `g5.xlarge` only | 5 | **1** | Never observed |
| `g6.2xlarge` only | 5 | **1** | Never observed |

**`g4dn.xlarge` scores 1/10 — the same as every family that never appears — while we hold 9
of them and grew the fleet 11 → 34 → 40 over 2026-09-03.** A 1/10 score plainly does not mean
"cannot acquire." The instrument is pinned at its floor for every GPU query in this region, so
it cannot distinguish "works fine" from "never happens."

Consequences:

1. **"g6 is thin" is unsupported.** g6 scores exactly what the working type scores. The cost
   win is **untested**, not unreachable.
2. **"Ask-size reduction is flat" is weakened.** The full-pool sweep (40/24/10 → 1) was a
   fair within-instrument comparison, but with the instrument saturated at 1 for nearly every
   query, flatness is as consistent with *no resolution* as with *no effect*. It cannot carry
   the weight originally placed on it.

**What the placement-score data can still be used for:** nothing decisive about this change.
It is retained here as a record of what was measured and why it was insufficient.

### Settled by CloudTrail (2026-09-04): overflow does not work

`get-spot-placement-scores` was the wrong instrument. **CloudTrail is the right one** — it
records what Karpenter actually asked for and what AWS actually returned.

16 real GPU `CreateFleet` requests, 2026-09-04 00:50–01:19Z. Every one offered **all 8 types**
(`g4dn`/`g5`/`g6`/`g6e` × `xlarge`/`2xlarge`) — though see the correction below: this does
**not** confirm `minValues` was engaged, because for the first four rows it was not:

| Time | Target | Granted | Type granted | Errors |
|---|---|---|---|---|
| 01:19 | 29 | **0** | — | UnfulfillableCapacity |
| 01:15 | 29 | **0** | — | UnfulfillableCapacity |
| 01:12 | 28 | **0** | — | Insufficient / Unfulfillable |
| 01:09 | 29 | 2 | `g4dn.xlarge` | Insufficient / Unfulfillable |
| 01:06 | 29 | **0** | — | UnfulfillableCapacity |
| 01:03 | 32 | 14 | `g4dn.xlarge` | Insufficient / Unfulfillable |
| 01:03 | 1 | 1 | `g4dn.xlarge` | InsufficientInstanceCapacity |
| 00:59 | 32 | **0** | — | Insufficient / Unfulfillable |
| 00:56 | 32 | **0** | — | UnfulfillableCapacity |
| 00:53 | 4 | 1 | `g4dn.xlarge` | Insufficient / Unfulfillable |
| 00:50 | 32 | **0** | — | Insufficient / Unfulfillable |

**`g5`, `g6` and `g6e` were offered 16 times out of 16 and granted 0 times.** Every instance
ever returned is `g4dn.xlarge`. This is a revealed-preference test, and it is decisive where
placement scores were not: the alternative pools are **empty, not un-offered**.

So the "g6 is thin" conclusion is **reinstated** — on this evidence, not on the placement
scores that could not support it.

#### Two premises this also corrects

1. **`CreateFleet` is NOT all-or-nothing.** Target 32 → **14 granted**; target 29 → **2
   granted**. It partially fulfils. This contradicts §3 of
   [2026-07-17-gpu-nodepool-spot-scarcity-g6-enablement.md](2026-07-17-gpu-nodepool-spot-scarcity-g6-enablement.md)
   and the rationale recorded in PR #345, both of which state a partially-fulfillable request
   launches nothing. That report likely observed a request where zero were available and
   generalised it into a rule. **#345 remains harmless and directionally sensible** — with
   partial fulfilment, offering more pools can only help — but diversification is not the
   binding problem.
2. **Small asks are not reliably fulfilled either.** Several `target=1` requests returned 0
   with `UnfulfillableCapacity`. The 2026-07-17 finding that 1-unit asks succeed where 15-unit
   asks fail does not hold under these conditions.

#### Where capacity actually is: `g7`

Re-running placement scores across excluded types shows the instrument **does** discriminate
when a real difference exists — which is why the uniform 1s above are meaningful:

| Instance | Score @ 10 units | In the pool? |
|---|---|---|
| **`g7.8xlarge`** | **3 / 3 / 3** | ❌ excluded by the size rule |
| **`g7.4xlarge`** | **3 / 3 / 1** | ❌ excluded by the size rule |
| **`g7.2xlarge`** | **3 / 1 / 3** | ❌ family not listed |
| `g4dn.xlarge` *(what we run)* | 1 / 1 / 1 | ✅ |
| `g4dn.4xlarge`, `g4dn.8xlarge` | 1 / 1 / 1 | ❌ |
| `g5.xlarge`, `g6.xlarge`, `g6.2xlarge` | 1 / 1 / 1 | ✅ |

**`g7` is the only GPU family with meaningful spot capacity in <YOUR_AWS_REGION> right now.** This
also refutes a "larger sizes are deeper pools" hypothesis — `g4dn.4xlarge`/`8xlarge` score 1
as well, so the 2026-07-31 observation that PCO reached for `g4dn.8xlarge` was point-in-time,
not structural.

The follow-up this opens is a **capacity** question, not a cost one, and is tracked separately
from per-node time-slicing — see §7.

### ⚠️ Correction (2026-09-04 01:45Z): the CloudTrail window is partly contaminated, and `g6` was granted at 01:41Z

Two corrections to the section above. Neither changes the §7 recommendation.

**1. `minValues` was absent from the cluster for part of the CloudTrail window.**
While diagnosing the 2026-09-03 GPU stall, PR #345's `minValues` were reverted on the live
`gpu-nodepool` and then restored. Pinned from the NodePool's `managedFields`:

| Event | Time (UTC) |
|---|---|
| `minValues` reverted (removed) | ~00:46 |
| Verified absent on live NodePool | 00:47:46 |
| `minValues` restored | **01:01:24** |

So the `00:50`, `00:53`, `00:56` and `00:59` rows were captured with **no `minValues` on the
NodePool**. They still offered all 8 types. That is the useful finding: offering the full
compatible set is Karpenter's **default** behaviour, not something `minValues` produces — so
"every one offered all 8 types" cannot be used as confirmation that `minValues` is engaged.
Verify that from the NodePool spec instead.

This does **not** weaken the grant data. What AWS returned is unaffected by which requirement
floors were set, and the `01:03`–`01:19` rows postdate the restore. The 16/16-offered,
0-granted result for `g5`/`g6`/`g6e` stands.

**2. A `g6.2xlarge` was granted at 01:41:31Z — after the window closed.**

```
NodeClaim gpu-nodepool-9kb6c   created 2026-09-04T01:41:31Z
  g6.2xlarge / <YOUR_AWS_REGION>a / spot
  Launched=True Registered=True Initialized=True Ready=True
  node ip-10-0-0-32.<YOUR_AWS_REGION>.compute.internal
```

This is the first non-`g4dn` GPU node observed in this fleet, and it launched with `minValues`
live (restored 01:01:24). Fleet at 01:43Z: 26 nodeclaims — 25 `g4dn.xlarge`, 1 `g6.2xlarge`;
zones 2a×1, 2b×4, 2c×21.

So **"the alternative pools are empty" must be time-scoped, not stated as settled.** They were
empty across 00:50–01:19Z and demonstrably not empty at 01:41Z, ~22 minutes later. The honest
reading is that `g6` capacity in <YOUR_AWS_REGION> is *intermittent* at this ask size — which is a
weaker claim than either "thin" or "available", and is the claim the evidence actually supports.

Consequences:

- The "`g6` is thin" reinstatement above should be read as **"`g6` was unobtainable during a
  30-minute window during a spot crunch"**, not as a standing property of the pool.
- §3 is still not re-costable: one grant does not establish sustained availability, and the
  cost win remains **untested** rather than either proven or refuted.
- The recommendation in §7 is unchanged and never depended on this — it rests on implementation
  cost versus ~$11–37 of settled savings.
- The instrument point stands and is reinforced: placement scores read `1/1/1` for `g6.2xlarge`
  both while it was ungrantable and ~20 minutes before it was granted. The score is not
  predictive at this range.

---

## 7. Recommendation

**Leave #102 closed for now, on cost-benefit grounds alone.** ~$11–37 of settled savings does
not justify a second NodePool, relaxed pod selection, a new overlay, a two-profile plugin
config, and an exclusive validation batch that cannot run while leg 2 owns the cluster.

That case stands on its own and does **not** depend on any capacity claim — which matters,
because the capacity claims in the first version of this document were withdrawn (§6).

Status of the two questions this document set out to answer:

1. **Verify `2xlarge` allocatable CPU** — still unverified; no `2xlarge` GPU node has existed
   to measure. Assumed ~7910m → 7 pods. Moot while (2) is negative.
2. **Confirm g6 spot depth** — **answered: g6 is thin.** CloudTrail shows `g5`/`g6`/`g6e`
   offered on 16 of 16 real fleet requests and granted 0 times (§6). The cost win in §3
   depends on migrating to `g6.2xlarge`, and that capacity does not exist.

### Separate follow-up: `g7` is where the capacity is

§6 turned up a finding that has nothing to do with time-slicing and should not be buried
here: **`g7` is the only GPU family with meaningful spot capacity in <YOUR_AWS_REGION>**
(`g7.8xlarge` 3/3/3 vs 1/1/1 for everything currently permitted).

That is a **capacity** question, not a cost one — during a drought the comparison is not
"g7 vs `g4dn`" but "g7 vs 208 pods Pending". Adding it needs three things, and the third
should gate rollout:

1. `g7` added to `instance-family` in `gpu-nodepool.yaml`. Note the current
   `instance-size In [xlarge, 2xlarge]` rule admits only `g7.2xlarge` — and `g7.8xlarge` is
   both **deeper** (3/3/3 vs 3/1/3) and **cheaper per node** ($0.4086 vs $0.5691), so the
   size rule costs us twice here.
2. `g7` added to `gpu-timeslice-nodeoverlay.yaml`, or Karpenter models it at 1 GPU/node and
   over-launches ~3x.
3. **A `priceAdjustment` NodeOverlay to keep g7 last-resort — not optional.**
   `price-capacity-optimized` weights capacity, and g7 currently has the *best* capacity
   score, so it could become the default and triple GPU cost (rough exposure: **+$500** over
   the remaining cohort at $0.1897/slice-hr vs $0.0579).
4. **The AMI driver is unvalidated for the RTX PRO 4500.** Stock AL2023 ships 580.159.03 /
   CUDA 13.0. If it does not support that part, nodes launch but never advertise
   `nvidia.com/gpu`. Validate with a single pinned test pod first — the same check
   [2026-07-17](2026-07-17-gpu-nodepool-spot-scarcity-g6-enablement.md) §10 prescribed for g6.

What remains solid and worth preserving from #102:

- Its duty-cycle measurement was sound and was the right analysis. The 2→3 slice move it
  drove **already banked the bulk of the available win** — GPU share of run cost fell
  47.3% → 31.0%, and per-subject cost $0.360 → $0.120.
- The two hard ceilings in §2 are arithmetic and hold regardless: **no `xlarge` can exceed 3
  slices** (3920m allocatable ÷ 1 cpu per pod), and **`g4dn` caps at 3 at any size** (T4
  VRAM). Any future attempt must start by moving the fleet off `g4dn.xlarge`.

**Re-open when** a crunch produces `g5`/`g6` GPU nodes, demonstrating those pools are
obtainable. At that point re-cost §3 with real placement data rather than a saturated proxy.

The purpose of this document is to stop the 3→5 question being re-litigated from first
principles — the duty-cycle numbers make it *look* like obvious headroom, and §2 shows why
the headroom is not reachable on the hardware we actually run.

**The cheaper move on the same problem already shipped:** PR #345 (`minValues` diversification)
widens the override list within each fleet request rather than shrinking the request — which,
given the flat response measured above, is the only one of the two mechanisms with a plausible
path to helping.

---

## 8. Reproduce

```bash
# GPU cost share (per-subject cost allocation records)
aws s3 cp s3://cloudpipe-metrics/metrics/costs/dt=2026-09-02/ /tmp/costs/ --recursive --quiet
jq -s '{n:length, gpu:([.[].gpu_cost_usd]|add), total:([.[].total_cost_usd]|add)}' /tmp/costs/*.json

# Live packing density (GPU pods per node)
kubectl -n argo-workflows get pods -o json | jq -r '[.items[]
  | select(.spec.nodeSelector["karpenter.sh/nodepool"]=="gpu-nodepool" and .status.phase=="Running")
  | .spec.nodeName] | group_by(.) | map(length) | group_by(.) | map({pods:.[0], nodes:length})'

# Allocatable CPU / advertised GPU on a live node
kubectl get nodes -l karpenter.sh/nodepool=gpu-nodepool -o json \
  | jq -r '.items[0] | {type:.metadata.labels["node.kubernetes.io/instance-type"],
      cpu:.status.allocatable.cpu, gpu:.status.allocatable["nvidia.com/gpu"]}'

# Spot price ladder
for t in g4dn.xlarge g4dn.2xlarge g6.xlarge g6.2xlarge g5.2xlarge g6e.2xlarge; do
  aws ec2 describe-spot-price-history --region <YOUR_AWS_REGION> --instance-types $t \
    --product-descriptions "Linux/UNIX" --start-time "$(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%S)" \
    --query 'SpotPriceHistory[].SpotPrice' --output text \
  | tr '\t' '\n' | awk -v t=$t 'NF{s+=$1;n++} END{printf "%-16s %.4f\n", t, s/n}'
done

# Duty cycle (unchanged since 2026-07-31)
pixi run python scripts/gpu_duty_cycle.py --workflows <comma-separated>

# THE DECIDING MEASUREMENT (§6) — what Karpenter ASKED for vs what AWS RETURNED.
# This is the instrument that settles overflow questions; placement scores are not.
aws cloudtrail lookup-events --region <YOUR_AWS_REGION> \
  --lookup-attributes AttributeKey=EventName,AttributeValue=CreateFleet \
  --max-results 25 --query 'Events[].CloudTrailEvent' --output text \
  | tr '\t' '\n' > fleet.jsonl

# offered types, target capacity, granted count, granted type, errors
jq -r 'select(.eventName=="CreateFleet")
  | select([.requestParameters.CreateFleetRequest.LaunchTemplateConfigs.Overrides[].InstanceType]|any(startswith("g")))
  | select(.responseElements != null)
  | [ .eventTime,
      (.requestParameters.CreateFleetRequest.TargetCapacitySpecification.TotalTargetCapacity|tostring),
      ([.responseElements.CreateFleetResponse.fleetInstanceSet.item?]|flatten|map(select(.!=null))
        |map(.instanceIds.item | if type=="array" then length else 1 end)|add // 0|tostring),
      ([.responseElements.CreateFleetResponse.fleetInstanceSet.item?]|flatten|map(select(.!=null))
        |map(.instanceType)|unique|join(",")),
      ([.responseElements.CreateFleetResponse.errorSet.item?]|flatten|map(select(.!=null))
        |map(.errorCode)|unique|join(","))
    ] | @tsv' fleet.jsonl

# Placement scores — useful only for COMPARING types, never as an absolute.
# Everything currently permitted reads 1; g7 reads 3.
aws ec2 get-spot-placement-scores --region-names <YOUR_AWS_REGION> \
  --instance-types g7.8xlarge \
  --target-capacity 10 --target-capacity-unit-type units --single-availability-zone
```

## 9. Related

- Issue #102 — **closed 2026-08-01** after driving the 2→3 slice move; this document is the
  answer to its follow-up ("why not 4 or 5?"). Epic #133.
- [2026-07-31-gpu-vram-duty-cycle-handoff.md](2026-07-31-gpu-vram-duty-cycle-handoff.md) — §1 measurements
- [2026-07-17-gpu-nodepool-spot-scarcity-g6-enablement.md](2026-07-17-gpu-nodepool-spot-scarcity-g6-enablement.md) — the all-or-nothing CreateFleet mechanism
- PR #345 — `minValues` diversification, the cheaper first move on the same problem
