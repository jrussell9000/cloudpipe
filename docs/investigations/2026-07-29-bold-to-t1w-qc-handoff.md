# bold_to_t1w Registration QC Handoff — 10-subject batch (2026-07-29)

**Purpose:** starting point for a new investigation session. Everything below was measured
against real data from the 2026-07-29 batch; nothing here is estimated or extrapolated unless
labelled as a hypothesis. **Do not re-run the batch before reading §5** — the raw registration
inputs needed to debug this are gone.

---

## 0. RESOLVED 2026-07-29 (later session) — this was a false alarm

**The registrations are fine. The QC gate is miscalibrated. Nothing below §0 that
interprets `nmi` as evidence of failure is correct.**

Measured on a surviving artifact set (`sub-17K4X0WD_ses-00A_task-rest_run-01`, verdict `warn`,
recorded `nmi` 1.0188 — reproduced exactly offline):

| Transform applied to the BOLD reference | NMI | MI (nats) |
|---|---|---|
| **as-registered (the shipped LTA)** | **1.0188** | **0.1313** |
| true + 3mm shift | 1.0185 | 0.1295 |
| true + 6mm shift | 1.0180 | 0.1275 |
| true + 10mm shift | 1.0168 | 0.1192 |
| true + 20mm shift | 1.0129 | 0.0911 |
| identity (no registration at all) | 1.0107 | 0.0757 |

The shipped transform is at the **optimum** — deliberately misaligning it monotonically lowers
NMI. Corroboration from `[[bold-to-t1w-lta-inverted]]`'s own 2026-07-23 bench (MI vs `T1.mgz`
within brainmask): pre-fix/broken 0.0298, identity 0.0607, post-fix/correct **0.1361**. This
batch measures **0.1313** — the known-good post-fix value.

**Where §1 went wrong:** it read `registration_qc.py`'s docstring range (`1.0 = independent,
2.0 = perfectly determined`) as an *operational* scale and concluded 1.02 ≈ independent ≈
catastrophe. That range is the theoretical bound. For cross-contrast EPI↔T1w NMI computed this
way, **~1.02 is what a good registration scores**; identity scores 1.011. The batch's
1.014–1.028 spread is the real dynamic range of this metric on this data, not a degenerate band.
(The NMI implementation itself was checked and is healthy: 3233/4096 joint-histogram cells
occupied, marginal entropies 3.59/3.53 nats, no bin collapse.)

**What the gate is actually measuring.** Across all 110 records:

- **0 of 110** runs score below the measured identity baseline — every registration *improved*
  alignment over doing nothing.
- `corr(nmi, rigid_disp_max_mm) = **+0.425**`, `corr(nmi, rigid_disp_mean_mm) = +0.417` —
  positive. Runs that moved *further* are aligned *better*.
- The `fail` group has a **higher** mean `nmi` (1.0201) than the `warn` group (1.0181), despite
  mean `rigid_disp_max_mm` of 43.07mm vs 17.44mm.

`rigid_disp_*`/`rigid_rot_deg` measure how far the transform moves brain points, which on this
data is dominated by **BOLD-vs-T1w field-of-view/scanner-positioning offset**, not registration
error. For the inspected run the ref and T1 FOV centres sit 24.4mm apart in world coordinates
before any anatomy is considered. Gating on transform magnitude therefore rejects precisely the
runs that needed — and successfully achieved — the largest legitimate corrections. This is
exactly the risk `b0927a4` flagged in its own commit message ("operator-set plausibility limits,
NOT batch-calibrated"); the 15/20 mm/deg bounds are far below the batch's genuine correction
distribution (mean `rigid_disp_max_mm` 38.9mm, max 97.6mm).

**Consequences:**
- The 92 `fail` runs were discarded and their functional preprocessing skipped **on a bad gate**,
  not on bad data. This is a data-loss bug, not a registration bug.
- Do **not** re-chase `cd33678`, `build_reference()`, `_run_synthmorph`, or FastSurfer geometry
  (the §6 suspect list). All are exonerated by the optimality test above.
- Fix direction: either drop `rigid_*` from the gate back to recorded-only (its state in `a16d412`
  before `b0927a4`), or recalibrate against this batch's distribution. `nmi` is the metric with
  real signal — but any `nmi` threshold must be set near ~1.011 (identity), not near 2.0.

Also corrected: **§5 is wrong that visual QC is foreclosed.** Only `verdict == 'fail'` exits 65 and
triggers the driver's `rmtree`; the **18 `warn` runs exited 0 and their full artifact sets
(`_warped`, `_ref`, `_brainmask`, `.lta`, `_itk.txt`) are intact in S3** under
`derivatives/registration/{subj}/{ses}/bold_to_t1w_{task}_{run}/` for `sub-17K4X0WD` and
`sub-WGVKC3KK`. Those warns sit in the same `nmi` band (1.0153–1.0195) as the fails, so they are
valid proxies. No re-run, driver patch, or GPU spend was needed to resolve this.

---

## 1. Headline finding

> ⚠️ **Superseded by §0.** The measurements in this section are accurate; the *interpretation*
> of `nmi` as evidence of failed alignment is not.

**0 of 110 `bold_to_t1w` registrations passed QC in this batch. 92 failed, 18 warned, 0 passed.**
This affects all 9 subjects that had functional scans (`sub-107UCJ69` has none — excluded, see
[[n8xt8-partial-status-validated]]).

| Subject | n runs | pass | warn | fail | Workflow | Argo status |
|---|---|---|---|---|---|---|
| sub-17K4X0WD | 17 | 0 | 12 | 5 | cloudpipe-8swrq | Succeeded |
| sub-330E63GH | 6 | 0 | 0 | 6 | cloudpipe-gctcm | Succeeded |
| sub-G86EJHZD | 6 | 0 | 0 | 6 | cloudpipe-fmggr | Succeeded |
| sub-HGNA569Y | 7 | 0 | 0 | 7 | cloudpipe-pkxsj | **Error** |
| sub-T1GJUT9Z | 20 | 0 | 0 | 20 | cloudpipe-jkf9g | Succeeded |
| sub-UNZ46TB8 | 22 | 0 | 0 | 22 | cloudpipe-8qzqp | **Error** |
| sub-WGVKC3KK | 18 | 0 | 6 | 12 | cloudpipe-r2688 | Succeeded |
| sub-WH0P4JHC | 6 | 0 | 0 | 6 | cloudpipe-b79br | Succeeded |
| sub-Z4LY1E6P | 8 | 0 | 0 | 8 | cloudpipe-xp9pr | Succeeded |
| sub-107UCJ69 (no func) | — | — | — | — | cloudpipe-xz689 | Succeeded |

Metric ranges across all 110 records:

| Field | min | max | mean |
|---|---|---|---|
| `nmi` | 1.014 | 1.028 | **1.020** |
| `rigid_disp_mean_mm` | 10.807 | 73.942 | 27.887 |
| `rigid_disp_max_mm` | 15.320 | 97.627 | 38.877 |
| `rigid_rot_deg` | 3.357 | 25.344 | 9.527 |
| `mhd_mm` | 3.048 | 8.881 | 5.061 |

**`nmi` is the strongest signal, not the gated metrics.** Per its own docstring
(`images/shared/registration_qc.py`, Studholme normalized MI): `1.0 = independent, 2.0 =
perfectly determined`. Every single run in the batch landed at 1.014–1.028 — essentially zero
shared information between the warped BOLD and T1w image, i.e. **not borderline motion, actual
failed alignment.** `nmi` is currently recorded-only (not gated) — see §4 for why the gate looks
less damning than the underlying data actually is.

⚠️ **Argo/workflow-level status does not reflect this.** 8 of 10 workflows show `Succeeded` even
though every functional run inside them failed QC — the `bold_to_t1w` driver deliberately exits 0
when runs fail (see §3), so only 2 workflows (`pkxsj`, `8qzqp`) actually errored, and only because
they *also* hit an unrelated retry/node-preemption edge case. **Use the per-run QC records or the
subject manifest, never Argo phase, to judge whether this batch's functional data is usable.**

---

## 2. What this rules out (checked, not hypothesis)

### 2.1 The known LTA-direction bug — ruled out, already fixed and deployed

Memory `[[bold-to-t1w-lta-inverted]]` flagged this fix as "uncommitted on main" as of 2026-07-24.
**That memory is now stale.** Verified 2026-07-29:

```
git merge-base --is-ancestor cd33678 HEAD   # → YES, ancestor
git show -s --format="%ci %s" cd33678
# 2026-07-24 08:58:20 -0500 fix(reg): apply the SynthMorph bold2t1w transform in the correct direction
```

And the image has been rebuilt/pinned multiple times since (`git log --oneline cd33678..HEAD` shows
9+ `ci: pin workflow images to sha-...` commits touching this path, most recently `7e8790e`,
already on `main` at the start of this session). The fix is live in the deployed image. **Do not
re-investigate this specific bug** — the memory file needs a correction pass (see §6).

### 2.2 The rigid_transform_metrics() call site — ✅ now numerically verified, and correct

> **Closed 2026-08-01.** The phantom test this section asked for exists:
> `tests/images/freesurfer/test_lta_direction.py`, section `rigid_metrics_from_lta`. The call
> site was extracted from `main()` into `bold_to_t1w.rigid_metrics_from_lta()` so the test binds
> to production code rather than re-typing the expression (a re-typed copy cannot catch a matched
> pair of errors — the exact failure mode that hid `cd33678`). Ground truth comes from a second
> implementation: `nibabel.affines.apply_affine` for the voxel→RAS step and
> `scipy.spatial.transform.Rotation.magnitude()` for the angle. **The call site is correct.**
>
> Two things the test turned up that this section did not anticipate:
>
> 1. **The whole `rigid_*` trio is invariant under inversion — provably, not just on this data.**
>    For any rigid `T(x) = Rx + t`, left-multiplying by the orthogonal `R` preserves the norm, so
>    `‖T⁻¹x − x‖ = ‖R(Rᵀx − Rᵀt − x)‖ = ‖x − t − Rx‖ = ‖Tx − x‖` at every point; and
>    `arccos((tr R − 1)/2)` is equal for `R` and `Rᵀ`. Confirmed to ~1e-10 across mask shapes and
>    rotation angles. So `rigid_disp_*`/`rigid_rot_deg` could never have caught the LTA-direction
>    bug, and must not be cited as corroborating direction. `nmi_gain` is the metric that does.
> 2. **The voxel-index-instead-of-RAS swap reads *lower*, not higher.** 6.4mm against a true
>    9.2mm on the phantom — the LIA axis permutation sends the rotation into different axes and
>    the rotational and translational components partly cancel. Anyone eyeballing the log line for
>    an implausible number would not have seen one.

<details>
<summary>Original text (code-read only)</summary>

`images/freesurfer/bold_to_t1w.py:524-527`:
```python
lta_matrix = parse_lta_matrix(out_lta)          # T1w_RAS → BOLD_RAS (per cd33678's convention)
brain_ras = brain_idx @ t1w_mask_img.affine[:3,:3].T + t1w_mask_img.affine[:3,3]  # T1w-space RAS
rigid = rigid_transform_metrics(lta_matrix, brain_ras)
```
Matrix direction and point-space match the documented convention (matrix's source space =
T1w_RAS, points passed are T1w_RAS). Not internally inconsistent as far as reading the code goes —
but this was a code-read, not a numerically-verified check. **Worth a phantom/synthetic-transform
test** (same style as the regression tests added in `cd33678`) to independently confirm, since the
LTA-direction bug already proved that "the docstrings agree" is not sufficient evidence on this
file.

</details>

### 2.3 Not a threshold-calibration problem alone — ❌ WRONG, see §0

> ⚠️ **This section's conclusion is refuted.** It is *exactly* a threshold-calibration problem.
> The argument below rests on treating the tight `nmi` band as proof of uniform failure; §0 shows
> that band is the metric's normal dynamic range, and that `nmi` correlates **positively** with
> the displacement the gate punishes.

The `b0927a4` commit (2026-07-25) that added the pass/warn/fail gate explicitly says the 15/20
deg/mm thresholds are "operator-set plausibility limits, NOT batch-calibrated... revisit against a
distribution + a known-broken anchor (sub-WGVKC3KK, `2655eea`)." It's tempting to conclude this
batch is just that recalibration data point landing outside generous-but-wrong guessed bounds.
**The `nmi` numbers rule this out** — a threshold-calibration problem would still show a real
range of registration quality (some good, some borderline). Instead every single run — regardless
of how far outside the gate `rigid_disp`/`rigid_rot` fell — landed in the same tight
near-independent `nmi` band. That pattern says the registrations are actually failing, not that
the gate is miscalibrated on genuine borderline cases.

---

## 3. Why Argo shows "Succeeded" anyway (context, not the bug)

Per `b0927a4`'s own commit message: "The bold_to_t1w driver already discards a run's outputs on
any non-zero exit (`rmtree(out_dir)`)... it computes the verdict, writes the QC record (so
verdict='fail' still uploads), then `sys.exit(65)`; the driver does the rest." Verified against
Argo node phases in a `Succeeded` workflow (`cloudpipe-fmggr` / sub-G86EJHZD):

```
bold-to-t1w-step(0)              | Failed  | main: Error (exit code 1)
record-outcome-bold-to-t1w-step  | Succeeded
functional-preprocessing-dagtask | Failed  | main: Error (exit code 1)
```

So per-session DAG nodes genuinely fail, but this doesn't propagate to workflow-level failure
(the outer `session-level-pipeline-dagtask` fan-out tolerates it). `cloudpipe-pkxsj` and
`cloudpipe-8qzqp` only ended in `Error` because their **first** `bold-to-t1w-step` attempt hit
`Pod was terminated in response to imminent node shutdown` (spot preemption — unrelated,
retryable), and the retry then hit the *same* deterministic QC failure as everyone else, at which
point `retryStrategy.expression evaluated to false` hard-failed the DAG node. **This is a red
herring** — the 8 "Succeeded" subjects have the identical underlying registration failure, they
just didn't get unlucky with a preemption on top of it.

---

## 4. Evidence trail (raw logs, for a fresh session to re-verify)

Three representative failing runs, full QC gate output:

```
sub-HGNA569Y_ses-00A_task-rest_run-01: NMI 1.0168, disp mean 31.37mm max 38.39mm, rot 6.64deg, MHD 3.17mm → fail
sub-HGNA569Y_ses-06A_task-rest_run-03: NMI 1.0169, disp mean 24.01mm max 45.17mm, rot 25.30deg, MHD 3.86mm → fail
sub-UNZ46TB8_ses-00A_task-rest_run-03: NMI 1.0215, disp mean 25.85mm max 34.23mm, rot 6.09deg, MHD 4.79mm → fail
```

Pod logs (survive workflow deletion, per `[[pod-logs-archived-to-s3]]`):
```bash
aws s3 cp s3://<YOUR_S3_BUCKET>/logs/cloudpipe-pkxsj/cloudpipe-pkxsj-bold-to-t1w-session-template-616476006/main.log -
aws s3 cp s3://<YOUR_S3_BUCKET>/logs/cloudpipe-8qzqp/cloudpipe-8qzqp-bold-to-t1w-session-template-2148915106/main.log -
```

Full QC record corpus for this batch (110 files), synced locally during this session — **may
already be gone** depending on scratchpad lifetime:
```bash
aws s3 sync s3://cloudpipe-metrics/metrics/registration/dt=2026-07-29/ <dest> --exclude "*" --include "*bold_to_t1w*"
```
Re-derive with the same command if needed — the S3 objects themselves are durable (not flushed by
`prep_test_batch.py`, which explicitly excludes per-scan QC prefixes from flushing).

Comparison point — a **known-good** registration type in the same batch, for QC-scale sanity
checking (t1w_to_mni uses `lncc`, not `nmi`, so not directly comparable, but shows the schema/gate
machinery works correctly when the registration is actually good):
```
sub-330E63GH ses-00A t1w_to_mni: lncc 0.815, mask_dice 0.981, centroid_displacement_mm 0.405 → pass
```

---

## 5. What's already gone — do not assume you can inspect raw inputs

> ⚠️ **Partly wrong — see §0.** The `rmtree` claim is real (driver at
> `registration-workflow-template.yaml:650`), but it fires only for `verdict == 'fail'`. The 18
> `warn` runs kept everything, and were sufficient to resolve the investigation without a re-run.

- Globus raw input (`mmps_mproc/{subj}/`) is deleted post-workflow by the `delete-globus-input`
  exit handler. **Confirmed empty** for these 10 subjects as of this session.
- The `bold_to_t1w` driver `rmtree()`s a failed run's *output* directory before upload — so the
  `_warped.nii.gz` QC image, brainmask, and ITK transform for a failing run **never reach S3 at
  all**. Only the QC JSON survives. **You cannot visually inspect the warped-to-T1w overlay for
  any of these 92 failures** — that visual-QC option is foreclosed unless the driver is patched to
  keep failed-run artifacts (or you set `verdict` thresholds to something permissive and rerun one
  subject to capture the intermediate images before they're deleted).
- FastSurfer derivatives (`mri/T1.mgz`, `mri/brainmask.mgz`) **do** still exist in
  `derivatives/fastsurfer/{subj}/` — these are real inputs to `bold_to_t1w.py` and are available
  for direct inspection/reproduction without re-running the whole pipeline.
- The BOLD input itself (`mmps_mproc/.../func/*.nii.gz`) is gone from S3 (input cleanup), but the
  **source** still exists on the Globus collection (`/abcd/derivatives/mmps_mproc`) — re-transfer
  is possible without re-running FastSurfer.

**Recommended first move for the next session:** re-run `bold_to_t1w.py` standalone (not through
Argo) against one already-existing FastSurfer output (e.g. `sub-330E63GH`, clean 0/6 pass — no
node-preemption noise) with a small patch to skip the `rmtree()` on failure, so the warped QC
image can actually be inspected. That's the fastest way to tell registration-genuinely-wrong from
metric-computation-wrong.

---

## 6. Housekeeping for the next session

- **Correct memory `[[bold-to-t1w-lta-inverted]]`** — its "uncommitted" status line is stale; the
  fix has been in `main` since 2026-07-24 and deployed via multiple image pins since. Leaving it
  as-is will mislead the next investigation into re-chasing an already-fixed bug (as this session
  almost did).
- **Suspect commits to look at first, in order:**
  1. `cd33678` (2026-07-24) — the LTA-direction fix itself. Ruled out at the code-read level (§2.2)
     but not numerically re-verified — do that first with a synthetic/phantom test.
  2. Anything touching `build_reference()` (temporal-mean BOLD construction) or the
     `mri_synthmorph` invocation (`_run_synthmorph`) between 2026-07-23 and 2026-07-29 — not yet
     diffed in this session. `git log --oneline fb56d24..HEAD -- images/freesurfer/bold_to_t1w.py`
     is the starting point.
  3. Whether FastSurfer's `T1.mgz`/`brainmask.mgz` conformed-space geometry changed for this batch
     vs. the 2026-07-23 batch (different FastSurfer image pin?) — would explain a systematic,
     uniform-magnitude offset across every subject.
- **Do not** re-run the 10-subject batch again before forming a hypothesis — each run costs real
  GPU time (~2h × 10 subjects) and, per §5, the failing-run artifacts are deleted every time,
  so repeating the batch blind doesn't get you closer without first patching the driver to
  preserve failed-run outputs.
- Batch subject list used: `tools/cloudpipe_test_sample_10.csv`. Prep/flush tool:
  `scripts/prep_test_batch.py` (flushes derivatives + workflow-keyed metrics, uploads the list to
  `s3://<YOUR_S3_BUCKET>/config/test_batch_subjects.csv`; does **not** touch per-scan QC records).
- Submission was via Prefect (`cloudpipe-queue-manager/cloudpipe-queue-manager`), not direct
  `argo submit` — flow run `classy-cat`,
  `https://prefect.<YOUR_DOMAIN>/runs/flow-run/<YOUR_PREFECT_FLOW_RUN_ID>`.

---

## 7. Reference

- Bucket `<YOUR_S3_BUCKET>` (data), `cloudpipe-metrics` (metrics), region `<YOUR_AWS_REGION>`.
- QC schema docs: `images/freesurfer/bold_to_t1w.py` module docstring (schema 2.3),
  `images/shared/registration_qc.py` (`rigid_transform_metrics`, `verdict`,
  `_BOLD_T1W_THRESHOLDS`).
- Related design commits: `2655eea` (empty-mask guard, the original sub-WGVKC3KK anchor),
  `a16d412` (schema 2.2, added the rigid/MHD metrics as recorded-only), `b0927a4` (schema 2.3,
  turned rigid metrics into a gate).
