# bold-to-t1w OOM Handoff — is it page cache or per-run RSS? (2026-08-01)

**Purpose:** settle *why* `bold-to-t1w` OOMKills at its 6G limit before changing anything.
Everything in §1 is **measured**; §2 is explicitly hypothesis. The decision this unblocks
is whether the fix is "raise the ceiling" or "stop holding the session's data in the
cgroup", and those point in different directions.

**Do not skip to the fix.** Raising the limit is known to work and costs nothing (the
*request* drives packing, not the limit) — but if page cache is the driver, the limit has
to keep rising with session size forever, and the real fix is bounded.

---

> **UPDATE 2026-08-01 (post-handoff): §2's framing was wrong and §5 is resolved.**
> Reading the archived pod logs settled both without needing the sampler. See
> **§8**, which supersedes §2's page-cache hypothesis and §5's "unexplained".
> Short version: every OOM is on the pod's **first** run, ~20 s in, inside
> `mri_synthmorph` — so it is the `anon` row, not accumulation and not staged
> page cache. And the OOM is **perfectly segregated by node**: pods on
> AVX512_VNNI-capable hosts died 31/31, pods on every other host died 0/40.
> §1, §3 and §4 still stand as written.

## 0. Bottom line

`8c28db6` cut `bold-to-t1w-session-template` from `16G request / 16G limit` to
**`3G request / 6G limit`**. Its own comment records the basis:

> Request is ~2.3x observed peak; the limit is left well above it (rather than pinned
> equal, as before) so an underestimate degrades to burst rather than OOM-killing the
> registration. **Sized for a single run: runs execute sequentially (jobs=1).**

The sizing is per-**run**. The pod is per-**session** and processes up to 6 runs. The 16G
ceiling had been absorbing whatever the difference is; 6G does not.

This is **not** related to the GPU time-slice change (#102/#109): `bold-to-t1w` runs on
`cpu-heavy-nodepool`, holds no `nvidia.com/gpu`, and that batch had **zero** GPU-step OOMs.

---

## 1. Measured

### The failure

2026-08-01 15:20Z batch, 10 subjects, exclusive:

| | value |
|---|---|
| workflows | **6 Succeeded / 4 Error** |
| `validate_test_batch.py` | **FAIL** — func-preproc coverage 6/9 |
| exit-137 pod attempts | **27**, all `bold-to-t1w-session-template` |
| GPU-step OOMs | **0** |
| subjects losing every BOLD run | `sub-330E63GH`, `sub-HGNA569Y`, `sub-T1GJUT9Z` |

Per subject, sorted by OOM attempts. Note the two clean 6-run subjects — run count
correlates but does **not** determine the outcome, which is what makes this a peak sitting
at the ceiling rather than a threshold:

| subject | workflow | phase | OOM attempts | max runs/session |
|---|---|---|---|---|
| `sub-T1GJUT9Z` | wv4dj | Error | 9 | 6 |
| `sub-UNZ46TB8` | r7544 | Succeeded | 5 | 6 |
| `sub-HGNA569Y` | vw764 | Error | 4 | 6 |
| `sub-330E63GH` | 6pfjx | Error | 3 | 6 |
| `sub-WGVKC3KK` | ffgxq | Error | 3 | 6 |
| `sub-Z4LY1E6P` | pw2gr | Succeeded | 2 | 4 |
| `sub-G86EJHZD` | phgdf | Succeeded | 1 | 6 |
| `sub-WH0P4JHC` | 5n274 | Succeeded | **0** | 6 |
| `sub-17K4X0WD` | 729jq | Succeeded | **0** | 6 |
| `sub-107UCJ69` | mfrcj | Succeeded | 0 | 0 (no BOLD) |

**Use `sub-T1GJUT9Z` (worst) against `sub-WH0P4JHC` / `sub-17K4X0WD` (same run count, clean)
as the A/B pair.** They are the whole reason a bare "raise the limit" answer is unsatisfying.

### What is ruled out

- **In-process accumulation.** The driver shells out per run via `subprocess`; each run's
  RSS is returned to the kernel on exit. There is no TensorFlow session to reset.
- **Concurrency.** `jobs` defaults to `"1"` and no caller overrides it, so runs are strictly
  sequential. Peak is not `jobs x per-run`.
- **A pre-existing condition.** The limit was 16G before `8c28db6`; this is a regression
  from that commit, not a latent defect.
- **The GPU slice change.** Wrong nodepool, and zero GPU-step OOMs in the same batch.

### The masking effect (read before trusting any prior "PASS")

The 2026-08-01 **01:58Z** batch reported **10/10 Succeeded** and a validator **PASS** while
still recording **4 exit-137 attempts** across 3 workflows. Retries absorbed them. Workflow
phase cannot see this class of failure:

```bash
argo -n argo-workflows get <wf> -o json \
  | jq '[.status.nodes[]? | select(.type=="Pod" and .outputs.exitCode=="137")] | length'
```

---

## 2. The open question

`bold-to-t1w-session-template` declares **no volumes**. These all land on the container's
writable layer:

| Path | Contents |
|---|---|
| `/data/func` | the session's **entire** `func/` prefix, fetched in one shot |
| `/tmp/fastsurfer/<subj>_<ses>` | extracted FastSurfer tree, ~2 GB |
| `/tmp/registration_out` | every run's outputs, held until upload at pod end |

Observed `mmps_mproc/<subj>/` sizes on the subjects that failed (so their input survived):
**3.56, 3.93, 10.20, 11.88 GiB**. Against a 6G limit.

**Hypothesis (NOT established):** in cgroup v2, `memory.current` includes page cache, so
several GB of staged file data is charged to the same limit as anonymous memory. This would
explain the run-count correlation that per-run RSS cannot — every run's registration is the
same size, so per-run peak should be flat across a 2-run and a 6-run session, while staged
bytes are not.

**The counter-argument, which must be taken seriously:** clean page cache is *reclaimable*.
The kernel should evict it under pressure rather than OOM. Only dirty pages awaiting
writeback are unreclaimable, so the hypothesis really requires that multi-GB writes to
overlayfs outpace writeback.

**Competing hypothesis:** a single run genuinely peaks above 6G on some subjects, and the
run-count correlation is incidental (more runs = more chances to hit a bad one). This
predicts high **anon**, not high **file**.

The two are distinguishable in one measurement.

---

## 3. How to settle it

Sample the cgroup from inside the pod, mirroring the `nvidia-smi -l 5` monitor already used
in `t1w-to-mni-template`. Pod logs are archived to `s3://<YOUR_S3_BUCKET>/logs/{wf}/{pod}/main.log`
and outlive workflow deletion, so the data survives even if the pod is OOMKilled.

Add to the `bold-to-t1w-session-template` script, before the run loop:

The template's script is Python, so add the sampler as a thread there (or wrap the step in
`bash -c` as `t1w-to-mni-template` does). Handle **both cgroup versions** — do not assume
v2: AL2023 defaults to v2, but this has not been verified on `cpu-heavy-nodepool` and a
hardcoded v2 path silently yields nothing on v1.

```python
import threading, time, pathlib

def _sample_cgroup(stop):
    v2 = pathlib.Path("/sys/fs/cgroup/memory.current")
    v1 = pathlib.Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    while not stop.is_set():
        try:
            if v2.exists():  # cgroup v2
                cur = v2.read_text().strip()
                stat = dict(l.split() for l in
                            pathlib.Path("/sys/fs/cgroup/memory.stat").read_text().splitlines())
                keys = ("anon", "file", "file_dirty", "file_writeback",
                        "inactive_file", "active_file")
                ev = pathlib.Path("/sys/fs/cgroup/memory.events").read_text().split()
            else:            # cgroup v1
                cur = v1.read_text().strip()
                stat = dict(l.split()[:2] for l in
                            pathlib.Path("/sys/fs/cgroup/memory/memory.stat").read_text().splitlines())
                keys = ("rss", "cache", "dirty", "writeback", "inactive_file", "active_file")
                ev = ["(v1: see memory.failcnt)"]
            vals = " ".join(f"{k}={stat.get(k, '-')}" for k in keys)
            print(f"CGROUP current={cur} {vals} events={' '.join(ev)}", flush=True)
        except OSError as e:
            print(f"CGROUP sample failed: {e}", flush=True)
        stop.wait(5)

_stop = threading.Event()
threading.Thread(target=_sample_cgroup, args=(_stop,), daemon=True).start()
# ... existing run loop ...
_stop.set()
```

Print to **stdout** here (not stderr as the `nvidia-smi` monitor does) — either is archived,
but stdout keeps the samples interleaved in order with the driver's own per-run logging,
which is what lets you attribute a jump to a specific run.

**Raise `limits.memory` to 12G for the diagnostic run.** At 6G the pod dies partway up the
curve and you learn only that it hit the ceiling — not what the ceiling was made of, nor
whether it would have plateaued. This is a temporary diagnostic value, not the fix.

### Reading the result

| Signal | Conclusion | Fix |
|---|---|---|
| `anon` high at OOM, `file` small | per-run RSS genuinely exceeds 6G | raise the limit; consider reducing per-run peak |
| `file` high, `file_dirty`/`file_writeback` non-trivial | page cache from staged data | delete each run's input after processing and upload-then-delete outputs inside the loop; the limit can then stay low |
| `current` climbs monotonically with run index | accumulation of *something* | correlate against staged bytes, not run index |
| `current` flat across runs, one spike | a single bad run, not the session | ignore run count entirely; find what is different about that run |

`memory.events` gives `max` (times the limit was hit) and `oom_kill` counters, which pin
down whether reclaim was attempted before the kill. On v1 the analogue is `memory.failcnt`.

**Interpreting `file` requires care.** A high `file` number alone does *not* prove page
cache caused the kill, because clean page cache is reclaimable and the kernel prefers
evicting it. The load-bearing evidence is `file_dirty` + `file_writeback` being
non-trivial at the moment `current` approaches `max` — that is unreclaimable cache. If
`file` is high but dirty/writeback are near zero, page cache is a bystander and the answer
is the `anon` row.

### There is no cheap post-hoc corroboration — checked

The obvious shortcut is to compare `func/` sizes for the clean 6-run subjects against
`sub-T1GJUT9Z`'s 11.88 GiB. **That is not available**, and the two plausible sources both
fail:

- `mmps_mproc/<subj>/` is **deleted on success** by `delete-globus-input-template`
  (verified: it paginates and deletes `{globus-dest-base-path}/{subjID}/`). Every clean
  subject reads 0 bytes.
- `SubjectManifest` (`src/metrics/schemas.py`) records step summaries and output lists —
  **no input sizes**. Neither does `StepOutcome`.

Within the four failing subjects the inputs did survive, giving a weak within-group signal
(11.88 GiB -> 9 OOMs, 10.20 -> 3, 3.93 -> 4, 3.56 -> 3) — monotone at the extremes, noisy in
the middle, and with no clean control it proves nothing.

So the cgroup sampler below is the *only* way to settle this. Do not spend time looking for
a retrospective shortcut; it was already looked for.

---

## 4. Traps

**Input is deleted on success.** `delete-globus-input-template` runs at the end of a
successful workflow, so `mmps_mproc/<subj>/` reads **0 bytes** for every subject that
passed. Post-hoc staged-size comparison between passing and failing subjects is therefore
impossible — measure during the run, and expect a re-run to need the Globus transfer again.

**`ram_efficiency` is a lifetime average.** It read 0.083 (~1.3 GB against 16 GB) and that
is exactly what justified the cut to 6G. It cannot see the peak that OOMs. **Never size a
hard ceiling from it.** This is the origin of the bug, not a side note.

**The retry budget is shared.** `retryStrategy` has `limit: 3` and its expression matches
both spot interruptions (`imminent node shutdown`, exit 2) and OOM (exit 137). A spot
reclaim steals a retry from a genuinely-failing step — that is what tipped `cloudpipe-6pfjx`
from recoverable to Error (attempt 0 spot, attempts 1-3 OOM, "No more retries left").

**Step naming.** Four spellings; use `src/workflow_steps.py`, never string-munge. For Argo
node status the template name is `.templateName // .templateRef.template` — filtering on
`.templateName` alone silently drops every cross-WorkflowTemplate step (25 of 31 pods in
this batch) and returns zero rows without erroring.

**pod-costs `completed_at` is the scrape timestamp**, identical across every pod in a
scrape. It cannot reconstruct timing or concurrency; use Argo node `startedAt`/`finishedAt`.

**Flush destroys the comparison baseline.** `prep_test_batch.py` deletes `metrics/costs/`
and `step-outcomes` for the subjects. Back them up before re-running.

---

## 5. RESOLVED in §8 — was: unexplained, do not assume variance

Between two same-day batches with comparable spot interruption counts:

| batch | bold-to-t1w attempts | OOMKilled | spot-interrupted |
|---|---|---|---|
| 01:58Z (baseline) | 25 | 4 (16%) | 9 |
| 15:20Z (validation) | 46 | 27 (59%) | 7 |

The 16G->6G cut predates both, so it does not explain the increase. Neither does spot. The
only other changes between them were the GPU slice count (wrong nodepool) and a
`metrics-server` addon bump (no plausible path). **Something is unaccounted for**, and a
fix that merely raises the ceiling would leave it unaccounted for.

---

## 6. Suggested order of work

1. Corroborate cheaply from `subject-manifests` sizes (§3) — may already discriminate.
2. Add the cgroup sampler and set `limits.memory: 12G` as a **diagnostic**, not a fix.
3. Re-run the four failing subjects exclusively:
   `sub-T1GJUT9Z`, `sub-HGNA569Y`, `sub-330E63GH`, `sub-WGVKC3KK`, plus a clean control
   (`sub-WH0P4JHC`). Expect to re-transfer inputs.
4. Read `anon` vs `file` at peak from the archived logs; decide from the §3 table.
5. Implement the indicated fix, revert the diagnostic limit, and re-validate by counting
   **exit-137 attempts**, not workflow phases.

### Does any of this need an image rebuild?

**The diagnostic does not.** The sampler, the `limits.memory` change, and a per-run
staged-file deletion all live in the inline `script:` source in
`registration-workflow-template.yaml` or its `resources:` block. ArgoCD syncs
`argo/workflows/` on merge and that is the whole deployment.

**One branch of §3's decision table does.** If the answer is the `anon` row — peak is inside
a *single* run rather than across the session — the fix likely lands in
`images/freesurfer/bold_to_t1w.py`, which is a `COPY` source of the freesurfer image. That
costs a rebuild plus a `ci: pin workflow images to sha-...` commit before it ships, and per
`[[image-rebuild-signal-is-pin-commit]]` the change is not live until that pin lands. Budget
for it if the measurement points that way.
6. Only then re-run the #102 3-slice validation, which is still outstanding and will fail
   the same way until this is fixed.

---

## 8. Settled from the archived logs (2026-08-01, post-handoff)

All 69 `bold-to-t1w` pod logs from both 2026-08-01 batches were pulled from
`s3://<YOUR_S3_BUCKET>/logs/`, and both workflows sets were still in Argo, so pod->node
placement was recoverable too. Reproduce with the commands at the end of this
section.

### 8.1 Every OOM is on the pod's FIRST run — §2's framing was wrong

The 31 exit-137 pods (matching the 31 truncated logs exactly) all die at the
*identical* point: **~5-6 s after the first run starts**, immediately after
TensorFlow initialises inside the first `mri_synthmorph` call, with the pod
terminated ~15 s later. Not one OOM happens on run 2 or later.

```
[driver] ses-04A: 6 of 6 runs need bold-to-t1w
[driver] === sub-UNZ46TB8_ses-04A_task-nback_run-01 ===
... INFO Temporal mean of 362 steady-state frames ... as registration reference
... INFO SynthMorph: mri_synthmorph -m rigid ...
... tensorflow/core/util/port.cc:110] oneDNN custom operations are on ...
... W tensorflow/compiler/tf2tensorrt/utils/py_utils.cc:38] TF-TRT Warning   <- EOF
```

This kills the page-cache hypothesis on its own: at run 1 `/tmp/registration_out`
is empty, nothing has been uploaded, and no session accumulation has occurred.
**The answer is §3's `anon` row**, reached without the sampler. Log size is a
clean proxy for progress (~5.1 kB per completed run; 1,896 B = died in run 1).

Two further facts that constrain the cause:

- **Log-size classes** (30,795 / 25,688 / 20,530 / 5,184 / 1,896 B) track runs
  completed, and every clean pod ran its whole session. So a *successful* pod
  does 6 sequential runs inside the same 6G without trouble — accumulation
  across runs is not merely unproven, it is contradicted.
- **The same session succeeds on retry.** `cloudpipe-wv4dj` `ses-06A` OOMed at
  17:29:05, 17:32:44 and 17:36:56, then **succeeded** at 17:49:01 on byte-identical
  input. So the peak is not a property of the run's data.

### 8.2 The cause is the node's CPU, not the workload

Pod-to-node placement segregates perfectly, with **zero mixing**:

| node | pods | OOM | rate |
|---|---|---|---|
| ip-10-0-38-229 | 17 | 17 | **100%** |
| ip-10-0-46-61 | 10 | 10 | **100%** |
| ip-10-0-43-40 | 4 | 4 | **100%** |
| 14 other nodes | 40 | 0 | **0%** |

The discriminator is visible in the logs themselves — TensorFlow's
`cpu_feature_guard` line reports the host ISA:

| ISA reported | pods | OOM |
|---|---|---|
| `AVX2 AVX512F AVX512_VNNI FMA` | 31 | **31 (100%)** |
| `AVX2 AVX512F FMA` | 108 samples | 0 |
| `AVX2 FMA` | 88 samples | 0 |

**AVX512_VNNI presence predicts the OOM with no false positives or negatives
across all 71 pod attempts.** `cpu-heavy-nodepool` constrains category to
`["c","m"]` and size to `[2xlarge, 4xlarge]` but pins **no instance family or
generation**, so Karpenter takes whatever spot capacity is cheapest — a mix of
Skylake (`m5`/`c5`: AVX512F, no VNNI), AMD (`m5a`/`m6a`: AVX2 only) and
Ice Lake or newer (`m6i`/`c6i`/`m7i`: VNNI). Instance size is fixed to 2xl/4xl
and the TF/OMP thread pools are already pinned to `requests.cpu`, so core count
is held roughly constant and does not explain this.

**We know the predictor, not the mechanism.** State this plainly, because an
earlier draft of this section got it wrong and the wrong version is seductive:

- **VNNI dispatch is a *weak* mechanism and was initially overstated.**
  AVX512_VNNI is an int8/bf16 dot-product extension; SynthMorph inference is
  **fp32**, which dispatches to `avx512_core` kernels regardless of whether VNNI
  is present. And the clean `AVX2 AVX512F FMA` hosts have AVX512F, so oneDNN's
  one genuinely memory-*increasing* effect — blocked layouts (`nChw16c`) padding
  channels to the 16-wide AVX512 block, which is costly for 3D convs with few
  channels — applies on those hosts too. It does not separate clean from killed.
- **Do not assume oneDNN raises memory.** Its dominant memory effect is operator
  fusion, which *reduces* peak by eliminating intermediate tensors. Disabling it
  may well make things worse. TensorFlow's own docs do not characterise a
  memory direction for `TF_ENABLE_ONEDNN_OPTS` at all (checked).
- **VNNI is best read as a marker for Ice-Lake-or-newer**, which is confounded
  with the thing this nodepool also leaves free: **host core count**.
  `instance-size` permits **both `2xlarge` (8 vCPU) and `4xlarge` (16 vCPU)**,
  and instance size was **not recoverable** for these batches — the nodes are
  terminated, and `metrics/costs` records are workflow-level aggregates with no
  `node_instance_type` (that field is on the per-pod schema, and no
  `dt=2026-08-01` partition exists yet; the scraper runs day+1).

> **RESOLVED 2026-08-02 — see §8.6. Instance types were recoverable after all,
> from `metrics/pod-costs/` (which the day+1 scrape wrote at 02:00Z, hours after
> this section was written). Host core count is RULED OUT. The BLAS hypothesis
> below is refuted as a standalone explanation; keep reading for what survives.**

**The stronger untested hypothesis is unpinned BLAS thread pools.** The `env:`
block pins exactly four pools to `requests.cpu` — `OMP_NUM_THREADS`,
`TF_NUM_INTRAOP_THREADS`, `TF_NUM_INTEROP_THREADS`,
`ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS`. It does **not** pin
`OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS` or `NUMEXPR_NUM_THREADS`, which
default to the **host** core count rather than the cgroup's. That is exactly the
bug class issue #97 fixed for TensorFlow, left unfixed one layer down, and
`bold_to_t1w.py` runs a numpy temporal mean over ~362 frames before SynthMorph
starts. A 16-vCPU host would then allocate twice the per-thread buffers of an
8-vCPU host for identical work — matching "newer/bigger instance -> killed"
without needing any ISA story.

### 8.3 §5's 16% -> 59% jump: node draw plus a retry feedback loop

No code, image or template change is involved — `images/` diffs to **zero bytes**
between the two batches' pinned SHAs, the registration template is unchanged,
and the subject list has not moved since 2026-07-23.

| batch | nodes drawn | VNNI nodes | pods on VNNI | OOM rate |
|---|---|---|---|---|
| 01:58Z | 8 | 1 | 4 of 25 | 16% |
| 15:20Z | 9 | 2 | 27 of 46 | 59% |

The draw alone does not explain the size of the gap; the amplifier does. **A bad
node is self-reinforcing:** OOM-killing a pod frees that node's memory, which
makes it the most attractive placement for the retry, which OOMs again. One node
(`ip-10-0-38-229`) absorbed 17 attempts drawn from **7 different workflows**, all
17 killed. So drawing one extra VNNI node does not add its share of pods — it
attracts retries until the retry budget is exhausted.

This also explains §1's puzzle of two clean 6-run subjects: run count never
mattered. They drew good nodes.

### 8.4 What to do differently

The diagnostic in PR #117 is still worth running — it measures the actual peak
and confirms `anon` directly — but §3's decision table can be shortcut, and two
cheaper levers are now available that §6 did not consider:

1. **Separate ISA from core count first — they are confounded and cheap to
   split.** The sampler now prints a one-shot `[host]` line: `NODE_NAME` (via
   downward API `spec.nodeName`), `os.cpu_count()` (host), the cgroup affinity
   count, the `/proc/cpuinfo` model name, and which thread-pool env vars are
   `<unset>`. One diagnostic run answers "generation or cores?".
2. **Pin `OPENBLAS_NUM_THREADS` / `MKL_NUM_THREADS` / `NUMEXPR_NUM_THREADS` to
   `requests.cpu`**, the same way the other four already are. Template-only, no
   rebuild. **Demoted by §8.6** — core count alone is ruled out (c6a.4xlarge has
   the same 16 vCPU and never OOMs), so this is only the fix if the driver turns
   out to be AVX512 kernels *interacting* with thread count. Cheap enough to do
   regardless, since the pinning is correct on its own merits.
3. **Pin the nodepool's instance generation or family**, or narrow
   `instance-size` to one value. Removes the variance at the source, but narrows
   spot capacity and so raises interruption risk.
4. **`TF_ENABLE_ONEDNN_OPTS=0`** — worth an A/B, but **not** on the reasoning an
   earlier draft gave. See §8.2: oneDNN normally *reduces* peak memory via
   fusion, so disabling it may make this worse. Treat as an experiment with an
   unknown sign, not a candidate fix.

Raising `limits.memory` still works and is still cheap (the request drives
packing), but note it is no longer the *bounded* fix the handoff assumed the
`anon` row implied: the required ceiling is set by whatever host Karpenter draws.

**Re-validation must control for node draw.** Counting exit-137 attempts across a
batch is not enough now that the outcome is decided by the ISA of the host —
record the `cpu_feature_guard` line per pod and compare VNNI hosts against
non-VNNI hosts, or a lucky draw will read as a fix.

### 8.6 Instance types recovered — core count ruled out (2026-08-02)

§8.2 said instance size was unrecoverable. **That was wrong**, and only briefly:
`metrics/pod-costs/` carries per-pod `node` **and** `node_instance_type`, and the
day+1 Kubecost scrape wrote `dt=2026-08-01` at 02:00Z. (§8.2 checked
`metrics/costs/`, which is the workflow-level aggregate and has neither field.
Two different prefixes, two different schemas — see
`[[funcqc-registrationqc-four-place-change]]`.)

| instance type | microarch | vCPU | pods | OOM | rate |
|---|---|---|---|---|---|
| **c6i.4xlarge** | Intel Ice Lake | 16 | 31 | 31 | **100%** |
| c6a.4xlarge | AMD Milan (Zen3) | **16** | 18 | 0 | 0% |
| m5.2xlarge | Intel Skylake | 8 | 12 | 0 | 0% |
| c5.2xlarge | Intel Skylake | 8 | 10 | 0 | 0% |

**Host core count is ruled out.** `c6a.4xlarge` has the *same 16 vCPU* as the
killer and never OOMed, 0/18. So the unpinned `OPENBLAS_NUM_THREADS` /
`MKL_NUM_THREADS` story from §8.2 does not explain this on its own — it predicts
c6a should fail too. Instance *size* is ruled out for the same reason.

Corroborating: among **surviving** pods, mean memory is flat at **2.82–2.83 GB
across all three clean types** (`ram_efficiency` 0.42/0.50/0.51), i.e. 16-vCPU
c6a uses no more than 8-vCPU c5. Nothing is scaling with host cores.

That same flat 2.8 GB against a 3G request is `ram_efficiency` looking perfectly
healthy while c6i pods blow through a 6G ceiling — §4's warning, now with numbers.

**What survives:** the killer is specifically **Ice Lake / c6i**, the only
VNNI-capable family in the draw. Two things still cannot be separated, because
the 2026-08-01 draw contains no instance that would split them:

1. **VNNI dispatch vs. Ice Lake generally** — c6i is the only VNNI type present.
2. **AVX512 path interacting with thread count** — the clean AVX512 hosts
   (c5/m5) are all **2xlarge/8 vCPU**, and the only 16-vCPU clean host (c6a) has
   **no AVX512 at all**. So "AVX512 kernels × 16 threads" remains live, and would
   still be fixed by pinning the BLAS vars. The hypothesis is narrowed, not dead.

**One instance type breaks both at once: a 4xlarge Skylake (`c5.4xlarge` or
`m5.4xlarge`) — AVX512F, no VNNI, 16 vCPU.** If it OOMs, it is AVX512×threads and
pinning the BLAS pools is the fix. If it stays clean, it is Ice-Lake/VNNI-specific.
§9.4 makes getting one into the draw the goal of the host-draw check.

### 8.5 Reproduce

```bash
# archived logs for both batches (survive workflow deletion)
aws s3api list-objects-v2 --bucket <YOUR_S3_BUCKET> --prefix "logs/cloudpipe-" \
  --query 'Contents[].[LastModified,Key]' --output text \
  | rg '^2026-08-01' | rg bold-to-t1w

# ISA vs outcome: truncated (<2500B) logs are the OOMs
rg -o 'To enable the following instructions: [^,]*' <logdir>/*.log | sort | uniq -c

# pod -> node placement (note templateRef fallback; see §4 "Step naming")
argo -n argo-workflows get <wf> -o json | jq -r '
  .status.nodes[]? | select(.type=="Pod")
  | select((.templateName // .templateRef.template) == "bold-to-t1w-session-template")
  | [.id, (.outputs.exitCode // "-"), (.hostNodeName // "-"), .startedAt] | @tsv'
```

---

## 9. Running the diagnostic (written for a cold pickup)

### 9.0 RUN COMPLETE — see §9.7 for results (submitted 2026-08-02 ~02:25Z)

> **Outcome: question 1 answered (`anon`), question 2 NOT answered — the draw
> contained no VNNI host, so zero OOMs occurred. Read §9.7, not this section.**


| subject | workflow | was |
|---|---|---|
| `sub-T1GJUT9Z` | `cloudpipe-4ck84` | 9 OOMs |
| `sub-HGNA569Y` | `cloudpipe-75ppq` | 4 OOMs |
| `sub-330E63GH` | `cloudpipe-tkdh5` | 3 OOMs |
| `sub-WGVKC3KK` | `cloudpipe-wq57p` | 3 OOMs |
| `sub-WH0P4JHC` | `cloudpipe-6dhm4` | clean 6-run control |

Done already: #117 merged; template verified live in-cluster (`memory: 12G`,
`host_sampler: true`); metrics backed up to
**`s3://cloudpipe-metrics/backup/pre-b2t-diagnostic-2026-08-01/`**
(`costs/`, `step-outcomes/`, `pod-costs/` — outside every flushed prefix, all of
which live under `metrics/`); `prep_test_batch.py` flushed 398 derivative +
236 metric objects. Expect ~2-3 h, plus a Globus re-transfer (the instance was
stopped and the first workflow starts it).

**Pick up at §9.4** — check the host draw first, before anything else.

`argo -n argo-workflows submit --from workflowtemplate/cloudpipe` needs the five
`globus-*` params as well as `subjID`; they have no defaults. Recover them from
any prior workflow rather than retyping:

```bash
argo -n argo-workflows get <any-prior-cloudpipe-wf> -o json \
  | jq -r '.spec.arguments.parameters[]? | "\(.name)=\(.value)"'
```

### 9.1 State as of writing

- **PR #117** carries the whole diagnostic: the `[cgroup]` sampler, the `[host]`
  one-shot line, `[staged]` byte counts, and `limits.memory: 6G -> 12G`. All
  template-only — **no image rebuild, so no `ci: pin workflow images to sha-...`
  commit is needed** and ArgoCD sync on merge is the entire deployment.
- Nothing is deployed until #117 merges. Verify with §9.3 rather than assuming.

### 9.2 What this run has to answer

Two questions, in order:

1. **`anon` or `file`?** Expected to be `anon` — §8.1 already showed every kill is
   ~20 s into run 1, before anything accumulates. This run confirms it directly
   and, at 12G, measures the peak the 6G ceiling was truncating.
2. **Instance generation or host core count?** These are confounded in the
   2026-08-01 data (§8.2). The `[host]` line splits them: `cpu_count` is the
   host's, `affinity` is what the cgroup allows. If `cpu_count` is 16 on the
   high-peak hosts and 8 on the low-peak ones, core count is the driver and the
   fix is pinning the BLAS thread pools (§8.4 item 2).

### 9.3 Procedure

```bash
# 1. Confirm the merged template is actually live (ArgoCD selfHeal reverts
#    manual kubectl apply within seconds, so the cluster is the source of truth).
#    NOTE the WorkflowTemplate is named `registration` — NOT
#    `registration-workflow-template`, which is only the filename. Querying the
#    filename returns empty without erroring, which reads as "not synced yet".
kubectl -n argo-workflows get workflowtemplate registration -o json | jq -r '
  .spec.templates[] | select(.name=="bold-to-t1w-session-template")
  | {memory: .script.resources.limits.memory,
     host_sampler: (.script.source | test("_log_host_facts"))}'
#    expect {"memory": "12G", "host_sampler": true}

# 2. Back up what prep_test_batch.py destroys (§4)
aws s3 sync s3://cloudpipe-metrics/metrics/costs/         ./backup/costs/
aws s3 sync s3://cloudpipe-metrics/metrics/step-outcomes/ ./backup/step-outcomes/

# 3. Flush, then submit one workflow per subject
pixi run python scripts/prep_test_batch.py <subjects_csv>
argo -n argo-workflows submit --from workflowtemplate/cloudpipe -p subjID=sub-XXXX
```

**Subjects:** the four that lost every BOLD run — `sub-T1GJUT9Z` (9 OOMs),
`sub-HGNA569Y`, `sub-330E63GH`, `sub-WGVKC3KK` — plus `sub-WH0P4JHC` as a clean
6-run control. Expect the Globus transfer to re-run: `delete-globus-input-template`
removed `mmps_mproc/` for every subject that passed (§4).

### 9.4 CHECK THE HOST DRAW EARLY — this can waste the whole run

The outcome is decided by which hosts Karpenter draws from spot, and the draw is
random. Five subjects is only ~15 `bold-to-t1w` pods. **If they all land on one
host class the run proves nothing**, and — because the 12G limit means pods will
mostly *not* die — it will look like a clean, successful batch. That is the same
trap as the 01:58Z batch's "10/10 Succeeded, validator PASS" hiding 4 exit-137
attempts (§1).

`[host]` prints within seconds of each pod starting, so check it long before the
batch finishes:

```bash
for wf in $(argo -n argo-workflows list --running -o name); do
  argo -n argo-workflows logs $wf 2>/dev/null | rg '^\[host\]'
done | sort -u
```

Or, once pods have finished, straight from the recovered instance types:

```bash
aws s3 cp --recursive s3://cloudpipe-metrics/metrics/pod-costs/dt=<date>/ . --quiet
cat *.json | jq -rs '[.[]|select(.step=="bold-to-t1w")]
  | group_by(.node_instance_type)[] | {type:.[0].node_instance_type, n:length}'
```

**Two things you want in the draw** (see §8.6):

1. **At least one `c6i.4xlarge`** — the only type that has ever OOMed here. No
   c6i in the batch means no failure to measure, and the run tells you nothing.
2. **A `c5.4xlarge` or `m5.4xlarge`** — AVX512F, no VNNI, 16 vCPU. This is the
   single instance type that splits the last confound: OOM there means
   AVX512×thread-count (fix = pin the BLAS vars); clean there means
   Ice-Lake/VNNI-specific.

If neither appears, submit more subjects rather than waiting out a batch that
cannot discriminate. Do **not** pin `instance-size` to force it — that removes
the variable being measured. Karpenter picks on spot price, so the draw cannot
be steered, only sampled until the types you need appear.

### 9.5 Reading it

Pull the archived logs with the §8.5 commands (they outlive the workflows), then:

| Look at | Tells you |
|---|---|
| `[host]` `cpu_count` vs `affinity` | is anything sizing from the host, not the cgroup |
| `[cgroup] peak` grouped by `[host]` | the real per-host peak — **this is the headline number** |
| `anon` vs `file` at max `current` | confirms §8.1's `anon` conclusion |
| `file_dirty`/`file_writeback` near peak | only these would revive the page-cache story |
| `[staged]` vs `[cgroup] file` | whether staged bytes track cache at all |

Peak is per-**host class**, not per-batch. Reporting one number for the batch
re-creates exactly the averaging error (`ram_efficiency`) that caused this bug.

### 9.6 Afterwards

Revert the diagnostic — the `[cgroup]`/`[staged]`/`[host]` blocks and
`limits.memory` together — in the same change that lands the real fix. Re-validate
by counting **exit-137 attempts grouped by host class**, never workflow phase.

### 9.7 RESULTS of the 2026-08-02 diagnostic run

11 `bold-to-t1w` pods across the 5 subjects. **Zero exit-137.** All 11 succeeded.

#### 9.7.1 The draw — why question 2 is still open

| host | model | vCPU | ISA reported by TF | pods |
|---|---|---|---|---|
| ip-10-0-20-186 | AMD EPYC 7R13 (Zen3) | 16 | `AVX2 FMA` | 6 |
| ip-10-0-15-213 / -3-135 / -39-39 / -4-105 | Intel Xeon 8275CL | 8 | `AVX2 AVX512F FMA` | 5 |

**No VNNI host was drawn, so no OOM was possible.** The batch also reproduced the
2026-08-01 clean class *exactly*: every 16-vCPU host AMD, every AVX512 host
8-vCPU. §8.6's confound is untouched.

That is now **three consecutive batches** in which a 4xlarge Skylake has appeared
**zero** times. §9.4's advice to "submit more subjects until the types appear" is
retired: the draw is not merely random, it is biased away from the one type that
matters. Use §9.7.4 instead.

#### 9.7.2 Question 1: settled, it is `anon`

Peak per pod, grouped by host class (from the `[cgroup]` sampler):

| host class | peak `current` | peak `anon` | `file` | `file_dirty` | `file_writeback` |
|---|---|---|---|---|---|
| 16 vCPU AMD (6 pods) | 4.12–5.50 G | 3.23–**4.57** G | 0–1.07 G | ≤1.7 M | **0.0 M** |
| 8 vCPU Intel (5 pods) | 5.26–**7.10** G | 2.90–**4.50** G | 1.06–2.56 G | ≤3.9 M | **0.0 M** |

`file_writeback` is **0.0 MB on every sample of every pod**. §3's revival
condition for the page-cache story is not just unmet, it is identically zero.
Page cache is a bystander. §2 is closed for good.

**The trap this exposes — do not size the limit from `current`.** Three pods
peaked at **6.02 / 6.74 / 7.10 G `current`**, above the old 6G limit, on host
types that have never once OOMed. There is no contradiction: the excess is all
reclaimable `file`, and under a 6G limit the kernel evicts it instead of killing.
So `memory.current` systematically *overstates* the requirement. **Size from
`anon`.** Sizing from `current` would repeat `8c28db6`'s error with the sign
flipped — `ram_efficiency` was too low to see the peak, `current` is too high.

Peak `anon` tops out at **4.57 G**, so the 6G ceiling left only ~1.4 G of
headroom on *healthy* hosts. 6G was marginal everywhere; c6i only had to push
~30% further to cross it.

#### 9.7.3 What the `[host]` line bought us anyway

On the 16-vCPU node: `cpu_count=16 affinity=16` while `requests.cpu` is 4.
**The cgroup does not restrict CPU affinity**, so the unpinned
`OPENBLAS_NUM_THREADS` / `MKL_NUM_THREADS` / `NUMEXPR_NUM_THREADS` genuinely do
size from the host. §8.2's mechanism is confirmed live, not hypothetical.

But peak `anon` on that 16-vCPU host (4.57 G) matches the 8-vCPU hosts (4.50 G).
So thread count alone does **not** inflate `anon` on non-AVX512 hardware. §8.6
hypothesis 2 survives only in its narrow form: AVX512 kernels *interacting* with
thread count. Pinning the BLAS vars remains correct on its own merits and cheap,
but this run gives no evidence it would have prevented anything.

#### 9.7.4 Next step: stop sampling, pin the host

`scripts/manifests/b2t-oom-instance-probe.yaml` runs the real
`bold-to-t1w-session-template` (via `templateRef`, so it cannot drift from the
step under test) against a fixed session, with `podSpecPatch` adding a
`node.kubernetes.io/instance-type` nodeSelector alongside the template's existing
nodepool selector.

```bash
argo -n argo-workflows submit scripts/manifests/b2t-oom-instance-probe.yaml -p instance-type=c5.4xlarge   # decisive
argo -n argo-workflows submit scripts/manifests/b2t-oom-instance-probe.yaml -p instance-type=c6i.4xlarge  # positive control
argo -n argo-workflows submit scripts/manifests/b2t-oom-instance-probe.yaml -p instance-type=c5.2xlarge   # negative control
```

Run the **positive control**. If `c6i.4xlarge` does not OOM (or peak far above
4.57 G at the 12G limit), the premise is wrong and nothing else is interpretable.

| c5.4xlarge | conclusion | fix |
|---|---|---|
| high `anon` / OOM | AVX512 × thread count | pin `OPENBLAS`/`MKL`/`NUMEXPR_NUM_THREADS` to `requests.cpu` |
| flat with c5.2xlarge | Ice Lake / VNNI specific | raise the limit from measured `anon`, or pin the instance family |

`cpu-heavy-nodepool` already permits all three types (category `c`, sizes
`2xlarge,4xlarge`), so no nodepool change is needed. **Its `capacity-type` is
`spot` only**, so a pinned type can sit Pending if that type's spot pool is
unavailable — check `kubectl get pod -n argo-workflows` for Pending rather than
assuming the probe is running.

Inputs are preserved and need no Globus transfer:
`s3://<YOUR_S3_BUCKET>/backup/pre-b2t-diagnostic-2026-08-01/mmps_mproc/sub-T1GJUT9Z/`
(106 objects, 12,758,470,441 B — byte-identical to source; the 13 missing keys
are zero-byte Globus directory markers, which `aws s3 sync` skips by design).

#### 9.7.5 Unrelated blocker found by this run

All five workflows Errored, but **not** on `bold-to-t1w`. Every
`functional-preprocessing` pod failed exit 1, taking `surface-resample` with it
(exit 64, "artifact components failed to load"):

```
PermissionError: [Errno 13] Permission denied: '3dToutcount'
[driver] 0/6 runs produced output; func failed=6, surface failed=6
```

The AFNI binary is not executable in the image — a packaging regression, 0/6 runs
on every subject including the control. It does not touch the OOM findings
(`bold-to-t1w` runs earlier and all 11 pods succeeded) but it blocks the pipeline
end to end and needs its own fix before any re-validation batch.

### 9.8 SETTLED 2026-08-03 — Ice Lake specific. AVX512 x thread count is REFUTED.

The pinned-instance probe (§9.7.4) ran the same fixed session
(`sub-T1GJUT9Z` `ses-00A`, 6 runs) on each host type. Both completed all 6 runs
at the 12G diagnostic limit.

| | c6i.4xlarge | **c5.4xlarge** | clean hosts 08-02 |
|---|---|---|---|
| model | Xeon Platinum **8375C** (Ice Lake) | Xeon Platinum 8275CL | 8275CL / EPYC 7R13 |
| ISA | `AVX2 AVX512F **AVX512_VNNI** FMA` | `AVX2 AVX512F FMA` | no VNNI |
| `cpu_count` / `affinity` | 16 / 16 | **16 / 16** | 16/16 and 8/8 |
| BLAS vars | all `<unset>` | all `<unset>` | all `<unset>` |
| **peak `anon`** | **8.67 G** | **4.25 G** | 2.90–4.57 G |
| peak `current` | 8.72 G (`file` 0.02 G) | 4.32 G (`file` 0.02 G) | 4.12–7.10 G |

**`c5.4xlarge` is the exact test §8.6 asked for and it came back clean.** It has
AVX512F, 16 vCPU, `affinity=16`, and the same unpinned
`OPENBLAS`/`MKL`/`NUMEXPR_NUM_THREADS` — every ingredient of the "AVX512 kernels
x thread count" hypothesis — and it peaks at **4.25 G**, indistinguishable from
the 8-vCPU hosts. That hypothesis is dead.

So is everything else that was ever confounded with it:

| ruled out | by |
|---|---|
| host core count | c6a.4xlarge 16 vCPU clean (§8.6); c5.4xlarge 16 vCPU 4.25 G |
| instance size | same |
| AVX512 dispatch | c5.4xlarge has AVX512F and is clean |
| unpinned BLAS thread pools | unset on both probes; only one blew up |
| page cache | `file` 0.02 G on both; `file_writeback` 0.0 (§9.7.2) |
| session size / run count | identical session on both hosts |

**What is left: Ice Lake (Xeon 8375C / c6i) specifically — ~1.9x the anonymous
memory for byte-identical work.** VNNI is still only a *marker* for that
microarchitecture; nothing here shows VNNI kernels are the mechanism, and §8.2's
caution against an over-confident oneDNN story still applies. But for every
operational purpose the predictor is now a measured cause with a magnitude.

#### 9.8.1 The fix

**Set `limits.memory` from 8.67 G, not from any average.** The old 6G limit was
under the c6i peak by 2.7 G, which is the whole bug. Keep the diagnostic's
**12G** as the permanent limit (~38% headroom) and revert only the samplers.
This costs nothing in scheduling: `requests.memory` drives packing, and it stays
at 3G.

Do **not** pin `OPENBLAS_NUM_THREADS`/`MKL_NUM_THREADS`/`NUMEXPR_NUM_THREADS`
*as the fix* — §8.4 item 2 and §8.6 both floated it and the probe refutes it.
It remains correct hygiene (the cgroup genuinely does not restrict affinity:
`cpu_count=16 affinity=16` against `requests.cpu: 4`), so land it if convenient,
but it fixes nothing here and must not be credited with a passing re-validation.

**Why not "keep 6G and just exclude c6i from the nodepool" (considered, rejected
2026-08-03):**

- **The limit is not a cost lever.** `limits.memory` does not affect bin-packing,
  node size or price — `requests.memory` does, and it stays 3G either way.
  Keeping 6G saves nothing, while narrowing instance families shrinks the spot
  pool and *raises* price and interruption rate. Treating the limit as the
  expensive part is the exact conflation behind `8c28db6`.
- **Excluding one family does not cover the trait.** The nodepool's constraints
  (category `c,m`; size `2xlarge,4xlarge`; nitro; x86_64) permit **79 instance
  types** in <YOUR_AWS_REGION>. Five have been measured. The untested remainder includes
  `m6i`/`c6id`/`c6in`/`m6id`/`m6in` — the same Ice Lake generation as the known
  bad type — plus `c7i`/`m7i`/`c8i`/`m8i`. A denylist fails open on every family
  AWS adds later, and the failure is silent until an unlucky draw.
- **6G is thin even on good hardware**: measured peak `anon` there is 4.57 G,
  ~24% headroom, which longer runs would erode.

An allowlist (`instance-family In [c5, m5, c6a, m6a]`) *would* be safe, but it
discards most of the spot pool — the genuinely expensive option. If Ice Lake is
ever excluded for other reasons, do it **in addition to** the 12G limit.

**Open risk worth a follow-up, not a blocker:** on c6i the step uses 8.67 G
against a **3G request** — a 2.9x under-request. The limit stops the OOM-kill,
but the scheduler still packs the node believing 3G. Under memory pressure that
is a node-level eviction risk for co-tenants. Either raise the request toward
observed peak (costs packing density) or accept it knowingly.

#### 9.8.2 Re-validation

Count exit-137 attempts **grouped by instance type**, and confirm at least one
`c6i.4xlarge` is in the draw — a batch with no c6i cannot demonstrate a fix
(§9.7.1: it happens more often than not). The probe manifest is the cheap way to
prove the fix on the known-bad host without waiting for a lucky draw.

### 9.9 THE CAUSE: oneDNN. `TF_ENABLE_ONEDNN_OPTS=0` (2026-08-03)

§9.8 established *which host* and left the mechanism open. An A/B on one pinned
`c6i.4xlarge`, same session, differing in one character, closes it.

| | oneDNN **ON** (default) | oneDNN **OFF** |
|---|---|---|
| peak `anon` | **8.69 G** | **3.87 G** |
| peak `current` | 9.74 G | 4.65 G |
| wall clock, 6 runs | 8m02s | 8m28s (**+5.4%**) |
| NMI run 1 / NGF run 1 | 1.0192 / 0.6847 | **1.0192 / 0.6847** |
| QC verdicts | 6/6 pass | 6/6 pass |

**oneDNN accounted for 4.8 GB — 55% of the peak — and bought ~5% runtime.**
Its blocked layouts pad channel dimensions to the AVX512 register width and
allocate reorder scratchpads; SynthMorph is 3D convolutions with *few* channels,
so that padding dominates. The kernels are only selected on Ice Lake and newer,
which is exactly why the OOM tracked instance type (c6i 31/31, everything else
0/40) while looking like a scheduling mystery: the memory decision is made inside
a library dispatching on CPUID, invisible to Kubernetes.

With it off, **Ice Lake becomes the lowest-peak host measured** (3.87 G, under
`c5.4xlarge`'s 4.25 G). The host-to-host variance that made §8.4 warn "the
required ceiling is set by whatever host Karpenter draws" is gone.

**Output is unchanged.** NMI and NGF match to four decimals on all six runs
(1.0192/1.0194/1.0200/1.0203; NGF 0.6847/0.6857/0.6850/0.6867), arm for arm, and
every QC verdict still passes. oneDNN changed *how* the convolutions were
computed, not *what* they produced.

**This contradicts §8.4 item 4**, which argued oneDNN's dominant memory effect is
fusion — which *reduces* peak — so disabling it had an unknown sign and might
make things worse. That reasoning was sound a priori and wrong here. It was the
right call to test rather than argue: the experiment cost ~8 minutes.

#### 9.9.1 What ships

- **`TF_ENABLE_ONEDNN_OPTS=0`** in the template's `env:` block. Read by
  TensorFlow at import, so env-only: no image rebuild, no pin commit.
- **`limits.memory: 12G` stays**, as defence in depth. It is now well above the
  3.87 G peak, but the nodepool draws hosts freely and a limit sized from the
  worst *observed* peak is correct regardless. Reverting it toward 6G is
  defensible once this has run in production — but change one variable at a
  time, and the limit costs nothing (requests drive packing).

This also retires §9.8.1's under-request risk: at 3.87 G peak against a 3G
request the gap is ~1.3x rather than 2.9x, and two concurrent pods (the measured
maximum) fit any permitted node.

---

## 7. Related

- `8c28db6` — the right-sizing commit that introduced this; read its full comment block
- Issue #106 (`bold-to-t1w` CPU trim) touches the same resources block
- `docs/investigations/2026-07-31-gpu-vram-duty-cycle-handoff.md` — #102, blocked behind this
- `tests/argo/test_gpu_step_resources.py::test_gpu_steps_set_no_memory_limit` — the
  convention this step violates, and its stated reasoning
- ADR 016 — why this pod records its own StepOutcome facts
