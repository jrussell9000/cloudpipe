# 012 — ABCD `registration_matrix_T1` evaluated and rejected for BOLD→T1w

**Status**: Rejected (SynthMorph rigid retained per [ADR 002](002-synthmorph-over-bbregister.md))

> ### ⚠️ Correction (2026-07-29) — one supporting argument below is stale
>
> **The decision stands and the MI table is still valid.** Every coordinate interpretation lost to
> identity, and both directions were tested, so that result is robust. Two caveats have since
> emerged.
>
> **1. The "identity is already optimal" argument is an artifact of a bug fixed after this ADR.**
> The Investigation section concludes that identity "sits on a broad MI optimum (±10 mm)" and that
> the matrix's ~22 mm translation "pushes it off that optimum". This ADR predates `cd33678`
> (2026-07-24), which fixed the SynthMorph transform being applied in the wrong direction. At the
> time, the shipped SynthMorph path scored MI **0.0298 — worse than identity's 0.0607**. Everything
> lost to identity, which made identity look optimal. Post-fix SynthMorph scores **~0.131, roughly
> 2× identity.** Identity is not optimal.
>
> **Therefore transform magnitude is not evidence against the matrix.** Measured over the
> 2026-07-29 10-subject batch, legitimate BOLD→T1w corrections span **16–97 mm (mean 39 mm)**,
> because the ABCD BOLD stays in native scanner space and the offset is field-of-view prescription.
> The sidecar matrix's own magnitude (13.0 mm / 2.05°) is the same order as a real SynthMorph LTA
> (8.3 mm / 7.79°). See `docs/investigations/2026-07-29-bold-to-t1w-qc-handoff.md` §0.
>
> **2. This rejected our ability to consume the matrix, not the matrix itself.** Per the closing
> line of the Investigation section, its true convention "could not be reconstructed". The
> evaluation was also **n = 1 subject** (sub-BKN88GVE ses-00A).
>
> A re-test was specified in `docs/investigations/2026-07-29-abcd-matrix-retest-handoff.md`
> (pre-registered decision rule, systematic enumeration of conventions, 18 runs with measured
> SynthMorph baselines) and has since been **run**. Result: **Cross-check only** — see
> `docs/investigations/2026-07-29-abcd-matrix-retest-results.md`. The best-scoring convention beats
> identity on 18/18 runs but reaches ≥90% of SynthMorph's gain on only 8/18, well short of the
> pre-registered adoption bar. **The rejection stands: SynthMorph remains the BOLD→T1w method.**

## Context

The ABCD minimally preprocessed dataset includes a `registration_matrix_T1` field in each BOLD BIDS sidecar JSON. Per Hagler et al. 2019 §2.7.1, it is "a registration matrix... provided to specify the rigid-body transformation between fMRI and T1w images," computed via mutual-information registration between the spin-echo field maps and the T1w. The appeal was to skip BOLD→T1w registration entirely — saving ~5 s/run and reusing a fieldmap-informed transform.

Three successive attempts were made to consume it, all reaching poor QC:

1. **Direct RAS-to-RAS LTA** (`c5cf630`) — wrote the matrix straight into a FreeSurfer type-1 LTA. NCC near zero / wrong sign.
2. **c_ras translation correction** (`106924f`, `1d32f6d`) — `M_lta = A_orig @ inv(A_t1w) @ M_abcd`, accounting for the origin shift between the ABCD T1w NIfTI (c_ras = 0) and the FastSurfer template `orig.mgz` (c_ras ≈ brain centroid, ~16.7 mm for the test subject). No improvement.
3. **FSL FLIRT→RAS conversion** (`225a7f7`) — assumed the matrix was an FSL FLIRT matrix in voxel-mm coordinates and applied the standard reflection/scaling conversion. Produced a ~171° rotation; worse than (1).

## Investigation

The premise driving attempts 1–3 — that a correct BOLD→T1w registration yields NCC ≈ −0.3 to −0.5 — was shown to be **false**. An exhaustive rigid-transform search found *no* transform that produces meaningfully negative intensity NCC for this EPI↔T1w pair: the brain silhouette dominates the correlation and keeps NCC weakly positive regardless of internal alignment. Intensity NCC is the wrong QC metric for cross-contrast registration.

Re-evaluating with **mutual information** (the metric ABCD itself used, polarity-independent), against `orig.mgz` within the brain mask, on sub-BKN88GVE ses-00A:

| Transform applied | MI (higher = better) |
|---|---|
| identity (c_ras-corrected, no matrix) | **0.066** |
| `inv(M_abcd)` (RAS) | 0.033 |
| `M_abcd` (RAS, the shipped pipeline behaviour) | 0.025 |
| `M_abcd` + best refining translation | 0.047 |
| voxel-to-voxel interpretations | 0.000 (BOLD lands outside the FOV) |
| center-of-volume scanner-coordinate interpretations | 0.018–0.041 |

The MI metric was validated to penalise misalignment (a deliberate 40 mm shift drops MI to 0.025 — the same score `M_abcd` earns). **Every coordinate interpretation of the matrix scored worse than applying no matrix at all.** The c_ras-corrected identity sits on a broad MI optimum (±10 mm), indicating the ABCD BOLD is already approximately aligned to the T1w in native scanner coordinates; `registration_matrix_T1` (with its ~22 mm translation) pushes it off that optimum.

The matrix's true coordinate convention (likely an MMPS/MATLAB internal frame) could not be reconstructed from the published sidecar and ABCD source alone, and reverse-engineering it offered uncertain payoff against a validated alternative.

## Decision

Do **not** consume `registration_matrix_T1`. Use `mri_synthmorph` rigid (6 DOF) unconditionally for BOLD→T1w registration — a validated, contrast-agnostic method that directly optimises alignment and avoids the unresolved convention. This reverts the bold-to-t1w step to the SynthMorph path of [ADR 002](002-synthmorph-over-bbregister.md), now without the abandoned matrix branch.

## Consequences

- `bold_to_t1w.py` no longer reads the BIDS sidecar for a registration matrix; `register()` always calls `mri_synthmorph register -m rigid`. The `write_lta()` helper and `--t1w` argument were removed.
- The registration workflow template no longer stages the `t1w-nifti` artifact or passes `--t1w` to the bold-to-t1w step. The `--json` sidecar is retained for NSS-frame detection only.
- The "future QC work" this ADR anticipated has since happened, and neither `ncc` nor `dice` survived it. `dice` is a dead field (hardcoded `0.0`; EPI↔T1w Dice is not meaningful cross-contrast) and `ncc` is not emitted at all. The recorded metric is **normalized mutual information** — `nmi`, plus its identity baseline `nmi_identity` and the gated `nmi_gain` — exactly the mutual-information direction this ADR pointed at. The edge/gradient idea was also pursued: `ngf` and `seg_bbr_contrast` are recorded (schema 2.5+), and `seg_bbr_contrast` measured as the strongest of seven candidates against a known misregistration, though only within-session.
- Cost: ~5 s/run of SynthMorph compute, which is negligible against the functional-preprocessing bottleneck.
- If a fieldmap-informed BOLD→T1w transform is ever wanted, the matrix convention would first need to be confirmed against MMPS documentation or the ABCD data providers.
