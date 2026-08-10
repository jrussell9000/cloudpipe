# Results — ABCD `registration_matrix_T1` re-test (2026-07-29)

Executes the pre-registered plan in
[2026-07-29-abcd-matrix-retest-handoff.md](2026-07-29-abcd-matrix-retest-handoff.md). Reopens
[ADR 012](../decisions/012-abcd-matrix-rejected.md).

**Outcome: Cross-check only.** Per §1's pre-registered rule, this does **not** clear the bar to
adopt or supersede ADR 012. SynthMorph remains the BOLD→T1w method.

---

## Data

The 18-run calibration set's sidecars (deleted by the `delete-globus-input` exit handler after the
prior batch) were re-pulled from Globus for exactly the 2 subjects needed, via a standalone
transfer-only Argo Workflow (not the full `cloudpipe` production template, which would have
re-triggered the same deletion on success). `T1w` scan type was added in a second pull — needed to
get the native/DICOM-space T1w NIfTI, since neither `orig.mgz` nor
`derivatives/registration/.../t1w_to_mni/*_T1w_orig.nii.gz` turned out to be it (both are on the
FastSurfer-conformed 256³ grid, confirmed by identical affines — no native-space geometry survives
the FastSurfer step other than the raw NIfTI itself).

Note also: the handoff's §4 "fallback: 89 subjects with both present" does not hold up. Checking
`bold_to_t1w_*` presence (not just the `derivatives/registration/` folder existing, which is
satisfied by `t1w_to_mni/` alone) shows only the original 3 calibration sessions have SynthMorph
BOLD→T1w outputs anywhere in `<YOUR_S3_BUCKET>`. The 89-subject overlap was real but not useful for this
question.

## Method

Implemented in `scripts/abcd_matrix_sweep.py`. Candidates are compositions of:

- **Direction**: `M` (as shipped) or `inv(M)`
- **T1w-space reading**: `ras-direct` (treat T1.mgz RAS and the raw T1w NIfTI's RAS as the same
  coordinate system) or `vox2vox` (voxel-index correspondence between the conformed and native
  grids, i.e. `A_t1w_raw @ inv(A_t1w_conformed)`)
- **RAS↔LPS flip**, independently on the BOLD side and the T1w side of `M`

16 candidates total, plus `identity` and the `SynthMorph` LTA as sanity anchors, scored identically
via `resample_with_matrix` (reused verbatim from the PR #89 branch) and
`normalized_mutual_information` (`images/shared/registration_qc.py`, already on `main`).

**Harness validated against the known reference before trusting any candidate ranking**: measured
`identity_nmi` (1.00948–1.01329) and `synthmorph_gain` (0.00455–0.00894, mean 0.00702) reproduce
the handoff's §7 reference numbers exactly, including per-run values (e.g. sub-17K4X0WD/ses-00A
nback run-01: identity 1.01030, SynthMorph gain 0.00821 — matches to 5 decimal places).

## Result

| Candidate | mean gain | beats identity | ≥90% of SynthMorph gain |
|---|---|---|---|
| `M-fwd`, ras-direct, LPS flip on both sides | **0.00600** | **18/18** | **8/18** |
| every other candidate (15 remaining) | ≤ 0.00061 mean | ≤13/18 | 0/18 |

Only one candidate is a serious contender: apply the matrix **forward, as shipped**, reading the
sidecar's convention as **DICOM/LPS** rather than RAS and converting with the standard
`T_RAS = D · M_LPS · D` conjugation (`D = diag(-1,-1,1)`), with no additional origin correction
needed (`ras-direct`, not `vox2vox` — i.e. treating the FastSurfer-conformed T1.mgz RAS and the raw
T1w NIfTI's RAS as numerically the same coordinate system, which held up empirically rather than
needing a hand-derived c_ras shift).

This candidate beats identity on **18/18** runs — a real, consistent signal, not noise. But per
§1's rule:

- **Adopt** requires ≥90% of SynthMorph's gain on ≥15/18 runs. Actual: **8/18** (44%), well short.
- **Cross-check only** requires beating identity on ≥15/18 while staying below SynthMorph. This is
  what the data shows: 18/18 beats identity, mean gain 0.00600 vs SynthMorph's 0.00702 (86% of
  SynthMorph on average, but highly session-dependent — see below).

**→ Cross-check only.**

### The ratio to SynthMorph is session-dependent, not run-dependent

| Session | ratio-to-SynthMorph range |
|---|---|
| sub-17K4X0WD ses-00A (6 runs) | 95.7%–99.2% |
| sub-17K4X0WD ses-02A (6 runs) | 68.5%–91.9% |
| sub-WGVKC3KK ses-04A (6 runs) | 61.3%–86.5% |

Within a session the ratio is tight (±3.5 points); across sessions it spans 38 points. **Confirmed,
not just consistent with**: `registration_matrix_T1` is byte-identical across every run within a
session (checked all 6 sidecars per session, max elementwise diff 0.000000 in all three sessions).
It is a one-time fieldmap↔T1w estimate — computed per Hagler et al. 2019 §2.7.1 from MI
registration between the spin-echo fieldmap and the T1w, not from any head-tracking hardware —
stamped into every BOLD file's sidecar for that session. It cannot reflect between-run head motion,
which is common over an ABCD session's ~hour of scanning across tasks. SynthMorph, by contrast,
registers a fresh per-run mean image, so it captures that run's actual head position. This is the
likely mechanism behind the session-level (not run-level) clustering above, and is a second,
independent reason (beyond convention uncertainty) the matrix is not a SynthMorph substitute. Worth
knowing if this is revisited: n=3 sessions is not enough to characterize the session-to-session
variance further.

## Decision (per §1, recorded before writing the above)

**Cross-check only.** Do not adopt `registration_matrix_T1` as a SynthMorph replacement — it
doesn't clear the pre-registered bar. It could, in principle, serve as an independent QC anchor
(the 18/18 identity-beat is a genuine signal), using the exact convention identified above
(`M` forward, `T_RAS = D · M_LPS · D`, no vox2vox correction). Implementing it as a QC anchor is a
separate, not-yet-scoped follow-up — this investigation only answers the adopt/reject question.

## Reference

- [ADR 012 — ABCD matrix rejected](../decisions/012-abcd-matrix-rejected.md) — updated with this
  outcome
- [Retest handoff](2026-07-29-abcd-matrix-retest-handoff.md) — pre-registered rule and method
- `scripts/abcd_matrix_sweep.py` — sweep implementation
- Raw output: `sweep_full.csv` (18 runs × 16 candidates + anchors) — not committed, regenerable via
  the script against the 3 re-fetched sessions
