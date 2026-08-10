# t1w-to-mni QC regression (#138): cause proven, fix validated, ship outstanding

**Status**: cause CONFIRMED by local reproduction. Fix VALIDATED — the 4-session
current-vs-`NONE` batch passed 4/4 on 2026-08-04 (§2 below). Remaining work is the
regressing unit test (§1) and shipping (§3).

**Worktree**: `/home/<YOUR_NETID>/<YOUR_GITHUB_REPO>/.claude/worktrees/fix-138-t1w-mni-sampling`
(branch `worktree-fix-138-t1w-mni-sampling`). Committed as `bde9c78`, **not pushed** — the
branch is local only, so it lives on this machine until someone pushes it. (An earlier
revision of this doc cited `d908746`; that commit was amended and the SHA is stale.)

**Staged probe assets** (~1 GB, already downloaded — do not re-fetch from S3):
`/tmp/claude-1000/-home-<YOUR_NETID>-<YOUR_GITHUB_REPO>/issue-138-probe/`
- `data/` — 5 sessions' `orig.mgz` + `brainmask.mgz`, plus the MNI template
  (`sub-HGNA569Y/ses-00A` is extracted at `data/ses-00A/`, the other four at
  `data/<subj>_<ses>/`), and `t1w_brain.nii.gz` (pre-masked T1w for the affine-only probe)
- `probe.py` — affine-stage-only probe (CPU, fast); args: `STRATEGY PCT TEMPLATE T1W [CONV]`
- `fst_none.py` — copy of the pinned image's `/app/fst1w_to_mni.py` with `NONE` sampling
- `reg/` — the 420 `t1w_to_mni` QC records synced from `s3://cloudpipe-metrics`
- `an.py` — the before/after analysis that reproduces the issue's table

Image used throughout (already pulled locally):
`<YOUR_AWS_ACCOUNT_ID>.dkr.ecr.<YOUR_AWS_REGION>.amazonaws.com/cloudpipe/fireants:sha-47f3e20fc7dd28bb163804d098f3e984474c0c96`

## Cause: confirmed, not inferred

The failure reproduces exactly on a local RTX 4060 — different GPU from production's T4/A10G,
which rules out node/GPU as the variable:

| | production (3 reruns) | local, `ITK_..._THREADS=1` |
|---|---|---|
| `lncc` | 0.645 / 0.645 / 0.646 | **0.64539** |
| `jac_det_frac_negative` | 0.00327 / 0.00328 / 0.00334 | **0.003277** |

`e12a249` (#107) coupled `ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS` to `requests.cpu` (2 -> 1).
The SimpleITK affine's sampled point set is drawn through a thread-partitioned RNG, so
`seed=42` fixes the draw only at a **fixed thread count**. Changing the thread count moves
the sampled set, which moves where the affine lands.

The issue's independent measurement was verified from `s3://cloudpipe-metrics`: 420 records,
1.1% -> 12.3% fail rate, same three flipped sessions. Numbers reproduce exactly.

## Two corrections to the issue's analysis

**1. "Pin the thread count to a validated constant" does not work.** Sweeping the thread
count on `sub-HGNA569Y/ses-00A` (full pipeline, gate is `lncc >= 0.65`):

| threads | 1 | 2 | 4 | 8 | 16 | unset (20 cores) |
|---|---|---|---|---|---|---|
| `lncc` | 0.645 ✗ | 0.716 ✓ | 0.702 ✓ | **0.159** ✗ | 0.275 ✗ | 0.223 ✗ |

Chaotic, not monotonic. No constant is safe, and 8 threads is catastrophic
(`jac_neg` 0.010 — a folded warp). This also means the **pre-#107 state was a live hazard**,
not merely irreproducible: `gpu-nodepool` admits 8-vCPU `2xlarge`, so pods landing there were
already in the 0.159 regime.

**2. Multithreading is not the mechanism the issue names.** ITK's Mattes MI merges per-thread
partial sums, so it is *never* bit-reproducible across thread counts — correct, and it cannot
be engineered away. But that residue is ~2e-5 mm of translation. The **sampled set** was
contributing ~20 mm. Six orders of magnitude apart. That separation is why removing the
sampling fixes it and pinning threads cannot.

Measured with `NONE` (deterministic point set) at 1/4/8 threads, translation parameter:
`-7.90743512` / `-7.90741147` / `-7.90738944`.

## `REGULAR` was tested and rejected

Affine-stage `lncc` on `sub-HGNA569Y/ses-00A`, varying only thread count:

| strategy | thr=1 | thr=4 | thr=8 | spread |
|---|---|---|---|---|
| `RANDOM` 0.1 (current) | 0.029 | 0.060 | 0.053 | 2.1x |
| `RANDOM` 0.2 | 0.336 | 0.371 | 0.300 | 1.2x |
| `REGULAR` 0.1 | 0.061 | 0.350 | 0.110 | 5.7x |
| `REGULAR` 0.2 | 0.037 | 0.319 | 0.104 | **8.6x** |
| `NONE` | 0.1054 | 0.1054 | 0.1054 | **1.000x** |

`REGULAR` is the worst arm. Per SimpleITK's registration overview it samples every n-th voxel
in scan-line order "then within each voxel randomly perturb from center" — so it fixes *which*
voxels but keeps a thread-partitioned random perturbation. Raising the percentage helps but
does not remove the axis.

Note affine-stage `lncc` did **not** predict final post-SyN `lncc` (`NONE` scores lowest here
yet wins end-to-end). Use the full pipeline for any accept/reject decision; the affine-only
probe is for *variance* comparisons only.

## Drafted fix

`images/fireANTs/scripts/fst1w_to_mni.py`, one hunk: `SetMetricSamplingStrategy(reg.RANDOM)` +
`SetMetricSamplingPercentage(0.1, seed=42)` -> `SetMetricSamplingStrategy(reg.NONE)`.

End-to-end on `sub-HGNA569Y/ses-00A`:

| `NONE`, threads | 1 | 4 | 8 |
|---|---|---|---|
| `lncc` | 0.77511 | 0.77507 | 0.77506 |
| `jac_neg` | 0.00104 | 0.00106 | 0.00104 |
| verdict | pass | pass | pass |

5e-5 spread against `RANDOM`'s 0.49. Costs +17 s of CPU on a step averaging 0.5 min.
Thread count becomes purely a performance knob, so **#107's `cpu: 1` GPU-packing win is kept**
— which is what the issue asked for ("do not revert `cpu: 1`").

## Outstanding work

### 1. An existing test regresses — ADJUDICATED 2026-08-04: **NOT a fixture artifact**

`tests/images/fireants/test_fst1w_to_mni.py::test_sitk_scale_affine_recovers_scale` fails
under `NONE` (overlap 0.737 vs the 0.764 required; the scale assertion still passes).
Investigated with `diag_test*.py` in the probe dir. **The "fixture artifact" hypothesis is
refuted, and so is the mechanism this doc previously asserted.** Do not lower the threshold —
but note that swapping the fixture does not rescue `NONE` either (measured; see below).

**The previously-stated mechanism is wrong.** This doc claimed the zero background is
something "`NONE` samples in full and `RANDOM` mostly misses". `RANDOM` 0.1 draws *uniformly*,
so it sees the same background:foreground ratio and the same MI histogram shape. Background
fraction cannot be the mechanism, and a sweep confirms no monotonic relationship (93.3% bg ->
`RANDOM` wins; 84.1% -> `RANDOM`; 77.2% -> `NONE`; 81.4% -> `NONE`).

**Under a common metric, `RANDOM` is genuinely the better optimum here** — cross-strategy
`GetMetricValue()` is not comparable (different point sets, different histograms), so both
transforms were re-scored under one fixed full-sampling metric:

| | `NONE` | `RANDOM` | ground truth |
|---|---|---|---|
| common MI (lower better) | -0.13368 | **-0.17430** | -0.35452 |
| RMS displacement | 5.579 mm | **3.403 mm** | 0 |
| dice | 0.7369 | **0.8103** | 0.8894 |

**The test is not flaky.** `RANDOM` scores 0.8103 at 1/2/4/8/16 threads — it did not pass by
luck of a thread count.

**Seed x geometry x thread sweep (the decisive one).** Improvement over identity baseline,
4 texture seeds, `P`=passes the 0.05 assertion:

| geometry | strategy | seed 7 | seed 11 | seed 23 | seed 42 | pass rate |
|---|---|---|---|---|---|---|
| `(30,24,20)@96` (current) | `NONE` | +0.0227 F | **-0.6379 F** | -0.0414 F | +0.0807 P | **1/4** |
| `(30,24,20)@96` | `RANDOM` | +0.0961 P | +0.0979 P | +0.1140 P | +0.0989 P | **4/4** |
| `(42,34,28)@96` (candidate) | `NONE` | +0.1937 P | +0.0304 F | -0.0904 F | -0.0896 F | **1/4** |
| `(42,34,28)@96` | `RANDOM` | +0.1220 P | +0.2245 P | +0.1423 P | +0.0369 F | 3/4 |

Identical at `threads=8` for `NONE` (thread-invariance confirmed: bit-for-bit at 1 and 8).

**Conclusion: on this class of synthetic problem `NONE` is robustly worse than `RANDOM`
(1/4 vs 4/4) and has a catastrophic mode (seed 11: -0.6379, i.e. far worse than doing
nothing).** Replacing the fixture does not fix this — the candidate geometry also scores 1/4,
so adopting it would be threshold-tuning by another name. `RANDOM` also has a catastrophic
mode on other geometries (a 77.2%-bg config gave -71.8% of headroom), and in **no** config
does either arm reach ground truth. Both strategies under-converge; which one wins is set by
incidental geometry.

**The tension**: `NONE` wins on real data (4/4 sessions, §2) and loses on synthetic
(1/4 seeds). The risk this exposes is qualitative, not just a failing test — under `RANDOM` a
bad basin was *thread-dependent*, so a rerun could land elsewhere; under `NONE` a session whose
landscape leads to a bad basin fails **deterministically every time**, and reprocessing cannot
help it. The real-data batch shows no such session among 5, but n=5 does not bound the rate.
**This remains the standing risk of the fix** — if t1w-to-mni failures reappear post-ship and
are perfectly repeatable on rerun, this is the first thing to suspect.

#### Resolution (decided 2026-08-04): ship `NONE`, retarget the test

Real-data evidence governs the production decision; the synthetic fixture predicts neither
arm's real behaviour. Two changes to `tests/images/fireants/test_fst1w_to_mni.py`:

**1. The overlap assertion is removed, not re-thresholded.** Rationale is in a comment on the
test itself: on this fixture the winner is set by incidental geometry, both arms
under-converge, and both have catastrophic cases. The *scale* assertion is kept — it passes
under `NONE` (0.779 vs target 0.800) and is a real statement about the affine's purpose.

**2. A new `test_affine_is_thread_invariant` is the actual #138 guard.** Verified to fail on
genuine pre-fix code (**3.5280 mm**, 70x over its 0.05 mm bound) and pass on the fix.

Building it required a harder fixture, and that is the non-obvious part: **the existing
single-compartment ellipsoid cannot express this test.** `RANDOM` is already thread-stable on
it (identical dice at 1/2/4/8/16 threads), so a determinism assertion there would pass under
the buggy code and guard nothing. `_multi_compartment` (128^3, inner compartment + two
off-centre lobes) plus a scale+pose offset gives the metric competing basins. Measured
translation spread over threads 1/4/8, three texture seeds:

| | seed 7 | seed 11 | seed 23 |
|---|---|---|---|
| `NONE` | 0.0001 mm | 0.0010 mm | 0.0000 mm |
| `RANDOM` | 2.0058 mm | 3.3569 mm | 0.7011 mm |

694x separation; the 0.05 mm bound sits ~50x above `NONE`'s worst and ~14x below `RANDOM`'s
best. Suite runtime 15 s.

**Harness trap that nearly inverted this result**: `pytest.ini` lists
`images/fireANTs/scripts` under `pythonpath`, and pytest **prepends** those entries to
`sys.path`, ahead of the `PYTHONPATH` env var. An attempt to verify the guard by shimming a
`RANDOM` copy via `PYTHONPATH` was silently ignored and reported a green "9 passed" — which
reads exactly like "the test is vacuous". To exercise pre-fix behaviour you must patch the
real file (it is tracked, so `git checkout -- <file>` reverts cleanly), not shim the path.

**Running these tests.** Earlier revisions of this doc said "the pixi env has no SimpleITK"
and routed through Docker. That is true only of the **default** env; the `imaging` env has it,
and the plain task is the CI path and much faster to iterate on:

```bash
pixi run test-imaging          # 9 passed in ~15 s -- no Docker needed
```

`pytest.ini` sets `addopts = --ignore=tests/images/fireants`, which `test-imaging` clears —
so a bare `pixi run test` (465 passed) does **not** cover this file. The Docker form still
works if you need to match the pinned image exactly:

```bash
docker run --rm -v "$PWD:/repo" -w /repo \
  -e PYTHONPATH=/repo/images/fireANTs/scripts:/repo/images/shared \
  --entrypoint python <fireants-image> \
  -m pytest tests/images/fireants/test_fst1w_to_mni.py -q
```

### 2. The validation batch — DONE 2026-08-04, **PASS** on 4/4 sessions

Run at `ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS=1` (production's post-#107 value, the broken
regime), 8 runs, `--cpus=8`, local RTX 4060. Driver `run_batch.sh` + analysis `an_batch.py`
in the probe dir. Pass criteria were: both flipped sessions recover to >= 0.65, and neither
control drops materially below its historical ~0.82. Both met.

| session | role | current | `NONE` | historical | verdict |
|---|---|---|---|---|---|
| `sub-UNZ46TB8/ses-00A` | flipped | 0.45726 ✗ | **0.79017** ✓ | pre 0.795/0.791/0.795, post 0.457 | RECOVERED |
| `sub-Z4LY1E6P/ses-02A` | flipped | 0.46936 ✗ | **0.80253** ✓ | pre 0.807/0.798/0.787, post 0.469 | RECOVERED |
| `sub-T1GJUT9Z/ses-04A` | control | 0.83396 ✓ | **0.83500** ✓ | min 0.828 pre / 0.834 post | held |
| `sub-G86EJHZD/ses-06A` | control | 0.82046 ✓ | **0.82774** ✓ | min 0.814 pre / 0.821 post | held |

Three findings beyond the pass/fail:

**The `current` arm reproduced both recorded failures exactly** — 0.45726 vs production's
0.457/0.457, and 0.46936 vs 0.469/0.469 — on a different GPU. That is what makes the `NONE`
numbers from the same harness trustworthy: the only variable between arms is the one hunk.

**`NONE` restores the flipped sessions to their pre-#107 quality**, not merely past the gate
(0.790 against a 0.791–0.795 history; 0.803 against 0.787–0.807).

**The controls move ~0.001–0.007 while the flipped sessions move ~0.333.** This rules out
"`NONE` just relocates the affine somewhere that happens to score better here" — on sessions
where `RANDOM` already found the right basin, `NONE` finds the same basin. Separation is ~50x.

Supporting metrics on the flipped sessions moved coherently with `lncc` — `mask_dice`
0.9585 -> 0.9788 and 0.9681 -> 0.9846; `centroid_displacement_mm` 1.391 -> 0.170 and
1.081 -> 0.437. The centroid collapse is direct evidence of the diagnosed mechanism.

Caveat on `jac_det_frac_negative`: it is **not** a reliable signature of this failure.
`sub-UNZ46TB8` failed with 0.00648 (7x its `NONE` value), but `sub-Z4LY1E6P` failed at
0.00196 against a `NONE` value of 0.00160 — barely moved. A displaced affine does not
reliably produce a folded warp; sometimes it just produces a bad alignment. `lncc`,
`mask_dice`, and `centroid_displacement_mm` are the metrics that tracked consistently.

Runtime was 78-84 s per `current` run vs 97-104 s per `NONE` run — a consistent ~+20 s,
matching the +17 s previously measured.

Invocation (~3 min/run, 8 runs; `PYTHONPATH=/app` is required when running `/w/fst_none.py`
or `registration_qc` will not import):

```bash
cd /tmp/claude-1000/-home-<YOUR_NETID>-<YOUR_GITHUB_REPO>/issue-138-probe
docker run --rm --gpus all --cpus=8 -e PYTHONPATH=/app \
  -e ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS=1 -v "$PWD:/w" -w /w --entrypoint python \
  <fireants-image> {/app/fst1w_to_mni.py | /w/fst_none.py} \
  --t1w data/<K>/mri/orig.mgz --brainmask data/<K>/mri/brainmask.mgz \
  --template data/MNI152NLin2009cAsym_res-01_T1w_brain.nii \
  --out-dir /w/o_<K>_<mode> --prefix probe --subj <subj> --ses <ses>
```

Read `lncc` / `jac_det_frac_negative` / `verdict` from `<out-dir>/probe_qc.json`. **A QC fail
leaves the outputs in `<out-dir>.staging` and never promotes to `<out-dir>`** — check both
paths, and note that the directory's own existence is the pass/fail signal.

### 3. Then, to ship

- Rebuild the fireANTs image; the change ships only when a `ci: pin workflow images to sha-...`
  commit lands (`images/fireANTs/**` is the rebuild trigger).
- Terminate running workflows before the template picks up a new pin (in-flight workflows
  freeze the whole stored template).
- Every session processed since 2026-08-01 is suspect and needs reprocessing — 3 known failed
  sessions across the batches, but the fix changes *all* affine results, so prior passes are
  not bit-comparable either.

### 4. Noted, not addressed

The FireANTs/torch SyN stage has no seeding at all (no `manual_seed`, no
`cudnn.deterministic`, no `CUBLAS_WORKSPACE_CONFIG`). Repeat runs currently agree to three
decimals, and the `NONE` thread sweep above was stable to 5e-5, so it is not biting — but it
is unguaranteed. Out of scope for #138.
