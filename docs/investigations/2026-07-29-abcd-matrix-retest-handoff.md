# Handoff — re-test ABCD `registration_matrix_T1` for BOLD→T1w (opened 2026-07-29)

**Task for a fresh session.** Determine whether ABCD's shipped `registration_matrix_T1` can
replace, or usefully cross-check, the SynthMorph BOLD→T1w registration. This re-opens
[ADR 012](../decisions/012-abcd-matrix-rejected.md), which rejected the matrix on 2026-06-25.

**Read §3 before designing anything** — one of ADR 012's supporting arguments is now known to be
an artifact of a bug fixed a month after it was written. **Read §6 before interpreting any
number** — the immediately preceding investigation on this exact code produced a confident,
well-sourced, and completely wrong conclusion, and §6 is how to not repeat it.

---

## 1. Pre-registered decision rule

Commit to this **before** running anything. The failure mode this guards against is real and
recent (§6).

Let `gain(X) = NMI(BOLD resampled by X into T1w space, T1.mgz within brainmask) − NMI(identity)`,
per run. `gain(SynthMorph)` is the reference; measured range **0.00456–0.00894** over 18 runs.

| Outcome | Criterion | Action |
|---|---|---|
| **Adopt** | some interpretation reaches ≥ 90% of `gain(SynthMorph)` on ≥ 15/18 runs | write an ADR superseding 012; SynthMorph optional |
| **Cross-check only** | beats identity (`gain > 0`) on ≥ 15/18 but stays below SynthMorph | document the convention; consider as an independent QC anchor, not a replacement |
| **Confirm rejection** | no interpretation beats identity on a majority of runs | append the result to ADR 012 and close; do not revisit without new information |

Record which bucket you landed in **before** writing any narrative interpretation.

---

## 2. Why this is worth re-testing (it was not, in June)

Four things changed since ADR 012:

1. **The direction bug is fixed.** `cd33678` (2026-07-24) corrected how the SynthMorph transform
   is applied. ADR 012 predates it by a month.
2. **We now have a correct, validated scoring harness** — `normalized_mutual_information` plus a
   measured identity baseline, and a SynthMorph reference known to score ~2× identity.
3. **`nmi_gain` exists** (schema 2.4, PR #89) and is exactly the right scoring function for this
   comparison — self-calibrating per session.
4. **The dismissal argument is dead** (§3), so the matrix is no longer excluded on its face.

Cost was never the objection: ADR 012 puts SynthMorph at ~5 s/run, negligible.

---

## 3. What ADR 012 got right, and the one thing it got wrong

### Still standing (do not re-litigate)

ADR 012 measured MI against `orig.mgz` within brain mask on sub-BKN88GVE ses-00A:

| Transform | MI |
|---|---|
| identity (c_ras-corrected) | **0.066** |
| `inv(M_abcd)` | 0.033 |
| `M_abcd` | 0.025 |
| `M_abcd` + best refining translation | 0.047 |
| voxel-to-voxel | 0.000 (BOLD outside FOV) |
| center-of-volume scanner-coordinate | 0.018–0.041 |

**Every interpretation lost to identity, and both directions were tested** — so this result is
robust to the direction bug. Post-fix SynthMorph scores ~0.131, roughly 2× identity and ~3× the
best matrix result. Any new interpretation must clear a high bar.

Caveat: this is **n = 1 subject**.

### Now invalid — do not reuse this reasoning

> *"The c_ras-corrected identity sits on a broad MI optimum (±10 mm), indicating the ABCD BOLD is
> already approximately aligned to the T1w in native scanner coordinates; `registration_matrix_T1`
> (with its ~22 mm translation) pushes it off that optimum."*

At the time, the shipped SynthMorph path scored MI **0.0298 — worse than identity's 0.0607** —
because it was applying its transform backwards. From that vantage point *everything* lost to
identity, which made "identity is optimal" look like a finding. Post-fix SynthMorph reaches 0.131.
**Identity is not optimal.**

So "the matrix translates ~22 mm, therefore it is wrong" is not valid. Measured on the 2026-07-29
batch, legitimate BOLD→T1w corrections span **16–97 mm (mean 39 mm)**, because the ABCD BOLD stays
in native scanner space and the offset is field-of-view prescription. A 22 mm translation is
unremarkable.

Concretely, from `sub-086U18RD_ses-00A_task-nback_run-01_bold.json`:

- `registration_matrix_T1`: translation norm **13.0 mm**, rotation **2.05°**
- SynthMorph LTA for a real run (`sub-17K4X0WD/ses-00A/task-rest/run-01`): translation norm
  **8.3 mm**, rotation **7.79°**

Same order of magnitude. The matrix is not obviously absurd.

ADR 012's actual, honest reason for rejection is its last line: the convention *"could not be
reconstructed from the published sidecar and ABCD source alone."* **This is a re-test of our
ability to consume the matrix, not of whether the matrix is correct.**

---

## 4. Data

### Preferred: the 18-run calibration set

These already have SynthMorph references and **measured `gain(SynthMorph)`** (§7), so they give a
direct paired comparison. Registration artifacts are durable in S3:

```bash
aws s3 sync s3://<YOUR_S3_BUCKET>/derivatives/registration/<subj>/<ses>/bold_to_t1w_<task>_<run>/ <dest>/
```

| subject | session | runs |
|---|---|---|
| sub-17K4X0WD | ses-00A | nback 01–02, rest 01–04 |
| sub-17K4X0WD | ses-02A | nback 01–02, rest 01–04 |
| sub-WGVKC3KK | ses-04A | nback 01–02, rest 01–04 |

FastSurfer targets (`mri/T1.mgz`, `mri/brainmask.mgz`) are in
`s3://<YOUR_S3_BUCKET>/derivatives/fastsurfer/<subj>/<subj>_<ses>_templated.tar.gz`.

⚠️ **The BIDS sidecars for these 18 runs are gone from S3** — `mmps_mproc/` is empty for both
subjects (deleted by the `delete-globus-input` exit handler; confirmed 2026-07-29). The owner
confirms **re-transferring them from Globus is easy** (`/abcd/derivatives/mmps_mproc`); you only
need the `*_bold.json` sidecars, not the 4D BOLD.

### Fallback: 89 subjects with both already in S3

If Globus is inconvenient, 89 subjects currently have **both** `mmps_mproc/` (sidecar) and
`derivatives/registration/` present. Verify per *session*, not per subject — the overlap was
computed at subject level and `sub-086U18RD/ses-00A` has a sidecar but no `bold_to_t1w` output.

```bash
aws s3 ls s3://<YOUR_S3_BUCKET>/mmps_mproc/ | awk '{print $2}' | tr -d '/' | sort > /tmp/mm.txt
aws s3 ls s3://<YOUR_S3_BUCKET>/derivatives/registration/ | awk '{print $2}' | tr -d '/' | sort > /tmp/rg.txt
comm -12 /tmp/mm.txt /tmp/rg.txt
```

Downside: you would need to re-derive `gain(SynthMorph)` for those runs (cheap — §7 script).

Confirmed present in the sidecar:
```
registration_matrix_T1 = [[0.99940, 0.03449, -0.00005, 0.63510],
                          [-0.03449, 0.99936, 0.00968, -11.18649],
                          [0.00038, -0.00968, 0.99995, 6.61217],
                          [0, 0, 0, 1]]
```

---

## 5. Method — enumerate conventions, don't guess three

ADR 012 hand-tried three interpretations. The convention space is small and enumerable; brute-force
it and score every candidate with the same function. This is the main methodological upgrade.

Build candidates as compositions of known basis changes:

- **Direction**: `M` and `inv(M)`
- **Coordinate frame**: RAS↔LPS flip (`diag(-1,-1,1,1)`), voxel↔RAS (`A_mov`, `A_ref` and inverses)
- **Origin**: `c_ras` shift between the ABCD T1w NIfTI (`c_ras = 0`) and FastSurfer `orig.mgz`
  (`c_ras ≈ brain centroid`), and FOV-center vs corner origin
- **Space composition**: the matrix references **DICOM-space T1w**, but our target is the
  **FastSurfer conformed** grid (this is ADR 002's stated incompatibility). The orig→conformed
  transform is recoverable from the affines of `orig.mgz` vs `T1.mgz`; compose it in. **ADR 012
  does not appear to have tried this**, and it is the most likely missing piece.
- **FSL FLIRT voxel-mm convention** (ADR 012 attempt 3) — include for completeness

That is on the order of 50–150 combinations. Each costs one resample + one NMI (a few seconds).
Score all of them, rank by mean `gain`, and only then interpret.

Sanity anchor: include `identity` and `SynthMorph` as candidates in the same sweep. If the ranking
does not put SynthMorph on top and identity mid-pack, your harness is wrong — stop and fix it.

---

## 6. Guardrails — how the last investigation on this code went wrong

The previous session produced a thorough, well-sourced handoff whose headline conclusion was
false. See [2026-07-29-bold-to-t1w-qc-handoff.md](2026-07-29-bold-to-t1w-qc-handoff.md) §0. The
failure mode, and the rules that follow from it:

1. **It read a metric against its docstring's theoretical range instead of measured anchors.**
   `normalized_mutual_information` documents `1.0 = independent, 2.0 = perfectly determined`; the
   session concluded that an observed 1.02 meant "essentially zero shared information" and
   therefore catastrophic failure. In fact a *good* registration here scores ~1.019 and identity
   scores ~1.011. **Never interpret an absolute similarity value. Always measure a known-good and
   a known-bad anchor on the same data with the same code.**
2. **It never measured the null.** One identity-baseline computation would have collapsed the
   entire wrong conclusion in minutes.
3. **It reasoned about a metric without checking whether the metric varies with what it claims to
   measure.** `rigid_disp_max_mm` turned out to be a session-level constant (69× between/within SD
   ratio) — it could not possibly have been per-run quality. **For any metric you gate or conclude
   from, check its variance decomposition first.**
4. **It generalized a mechanism from one code path to the whole population.** It concluded all
   artifacts were deleted because `fail` runs are `rmtree`d — but `warn` runs exit 0 and survive,
   and 18 intact specimens were sitting in S3 the whole time. **Check whether a lesser verdict
   preserved an equivalent specimen before paying to reproduce one.**
5. **Repo prose is not evidence.** The false premise was written in a code comment, and four
   agreeing docstrings previously encoded a wrong transform direction
   (`[[bold-to-t1w-lta-inverted]]`). Verify against data.

---

## 7. Reproducing `gain(SynthMorph)`

The measurement script from the prior session is reproduced below; it takes `warn_runs.txt` lines
of `subject session task run nmi`, plus per-run `ref.nii.gz` + `x.lta` and per-session
`T1.mgz`/`brainmask.mgz`. Its output for the 18 runs (this is the target to beat):

```
registered NMI : 1.01527 – 1.01951 (mean 1.01812)
identity   NMI : 1.00947 – 1.01329 (mean 1.01110)
ΔNMI (gain)    : +0.00456 – +0.00894 (mean +0.00702)
MI ratio       : 1.33× – 1.83× (mean 1.62×)
18/18 beat identity
```

Per-run gains, in `warn_runs.txt` order (17K4X0WD ses-00A ×6, ses-02A ×6, WGVKC3KK ses-04A ×6):
```
0.00821 0.00851 0.00814 0.00853 0.00860 0.00894
0.00621 0.00622 0.00479 0.00456 0.00622 0.00593
0.00799 0.00788 0.00572 0.00603 0.00673 0.00713
```

Core of the scorer (full version was in the session scratchpad; rewrite is ~40 lines):

```python
# resample_with_matrix(moving_path, T, reference_path, order=1) -> np.ndarray
# lands on the PR #89 branch as a public helper in images/freesurfer/bold_to_t1w.py
w   = resample_with_matrix(ref, T, t1_path, order=1)
nmi = normalized_mutual_information(w, t1_data, mask=brainmask > 0)
gain = nmi - normalized_mutual_information(
    resample_with_matrix(ref, np.eye(4), t1_path, order=1), t1_data, mask=brainmask > 0)
```

Note `resample_with_matrix` is **new on the PR #89 branch** (`fix/bold-t1w-qc-gate-calibration`).
On `main` you have only `apply_lta`, which takes an LTA *path* and writes a file — either branch
off PR #89 or extract the same core locally.

---

## 8. Do not

- **Do not re-run the 10-subject batch.** ~2 h GPU × 10, and it answers nothing here — this
  question is answerable entirely offline from existing artifacts.
- **Do not re-chase the registration itself.** `cd33678`, `build_reference()`, `_run_synthmorph`
  and FastSurfer conformed geometry are all exonerated by the perturbation-optimality test.
  SynthMorph is at its optimum.
- **Do not change the QC gate here.** That is PR #89's scope.
- **Do not treat "beats identity" as sufficient to adopt.** SynthMorph beats identity ~2×; the
  bar is §1's rule.
- **Do not trust `[[bold-to-t1w-lta-inverted]]`'s "MI vs T1.mgz" numbers as directly comparable to
  ADR 012's** — ADR 012 measured against `orig.mgz`, the memory and pipeline against `T1.mgz`.
  Pick one target and hold it fixed.

---

## 9. Reference

- [ADR 012 — ABCD matrix rejected](../decisions/012-abcd-matrix-rejected.md) (2026-06-25)
- [ADR 002 — SynthMorph over bbregister](../decisions/002-synthmorph-over-bbregister.md) — states
  the DICOM-space vs FastSurfer-conformed incompatibility
- [Prior investigation §0](2026-07-29-bold-to-t1w-qc-handoff.md) — the resolved QC false alarm
- PR #89 `fix/bold-t1w-qc-gate-calibration` — schema 2.4 gate, `resample_with_matrix`
- Hagler et al. 2019 §2.7.1 — the matrix's published definition
- Buckets: `<YOUR_S3_BUCKET>` (data), `cloudpipe-metrics` (metrics); region `<YOUR_AWS_REGION>`

**Open doc-consistency issue** (small, worth fixing regardless of outcome): root `CLAUDE.md` lists
*"fMRI-T1w registration matrix"* under **"Already done upstream (do not re-implement)"**, which
directly contradicts ADR 012's decision to re-implement it with SynthMorph. This inconsistency
will keep regenerating the question that started this investigation.
