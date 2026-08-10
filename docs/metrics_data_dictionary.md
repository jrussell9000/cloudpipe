# Metrics Data Dictionary

Field-by-field reference for every record cloudpipe writes to `s3://cloudpipe-metrics/metrics/`.
This is the thing to check before trusting a column name — in a query, in
`scripts/export_batch_metrics.py` output, or in a Grafana panel.

**Source of truth is the emitter, not the dataclass.** `src/metrics/schemas.py` defines a
`@dataclass` per table, but three of the nine tables are built as raw Python dicts in the
image scripts rather than constructed through the dataclass, so they can (and do) drift from
it silently — nothing enforces that the dict a script writes matches the dataclass shape. This
file was built by reading the actual `qc = {...}` / `qc[...] = ...` construction in each
emitter, not by copying the dataclass. Where the two disagree, that's called out explicitly
under [Known drift](#known-drift-dataclass-vs-live-emitter) below — trust the emitter.

Every table also includes `pipeline` (str, always `"cloudpipe_minproc"` today) and
`completed_at`/`recorded_at`/`finished_at` (ISO 8601 UTC) unless noted otherwise.

| Table | S3 prefix | Grain | Written by | Constructed via | `export_batch_metrics.py` CSV |
|---|---|---|---|---|---|
| [FuncQC](#funcqc--per-bold-run) | `metrics/func-preproc/` | 1 row per BOLD run | `images/afni/preproc.py` | raw dict — **drifts** | `func_qc.csv` |
| [AnatQC](#anatqc--per-subjectsession) | `metrics/anat-qc/` | 1 row per subject×session | `images/fastsurfer/extract_qc.py` | raw dict — matches dataclass | `anat_qc.csv` |
| [FsqcQC](#fsqcqc--per-subjectsession) | `metrics/fsqc-qc/` | 1 row per subject×session | `images/fsqc/stage_and_run.py` | raw dict — matches dataclass | `fsqc_qc.csv` |
| [RegistrationQC](#registrationqc--per-registration-step) | `metrics/registration/` | 1 row per registration | `images/fireANTs/scripts/fst1w_to_mni.py`, `images/freesurfer/bold_to_t1w.py` | raw dict — **drifts heavily** | `registration_qc.csv` |
| [WorkflowRun](#workflowrun--per-argo-workflow) | `metrics/workflow-runs/` | 1 row per Argo workflow | `src/metrics/exit_handler.py` | dataclass — no drift possible | `workflow_runs.csv` |
| [CostAllocation](#costallocation--per-workflow-per-scrape-date) | `metrics/costs/` | 1 row per workflow×scrape-date | `src/metrics/kubecost_scraper.py` | dataclass — no drift possible | `costs_raw.csv` (raw), `costs_by_subject.csv` (summed per subject) |
| [PodCost](#podcost--per-pod-per-report-date) | `metrics/pod-costs/` | 1 row per pod×report-date | `src/metrics/kubecost_scraper.py` | dataclass — no drift possible | *not exported* |
| [StepOutcome](#stepoutcome--per-step-per-scan-unit) | `metrics/step-outcomes/` | 1 row per (workflow, step, subject, session, task, run) | `src/metrics/outcome_recorder.py` | dataclass — no drift possible | *not exported* |
| [SubjectManifest](#subjectmanifest--per-workflow-per-subject) | `metrics/subject-manifests/` | 1 row per (workflow, subject) | `src/metrics/exit_handler.py` | dataclass — no drift possible | *not exported* |

All nine are partitioned `dt=YYYY-MM-DD/` (the write date, except `CostAllocation`/`PodCost`
where `dt` equals the cost *report* date — see those tables' notes) and queryable via
`metrics.athena.CloudpipeMetrics` / `metrics.duckdb_query.CloudpipeMetrics`
(`func_qc`/`anat_qc`/`fsqc_qc`/`registration_qc`/`workflow_runs`/`costs`/`pod_costs`), or in bulk via
`scripts/export_batch_metrics.py`. `StepOutcome` and `SubjectManifest` are queryable but have no
CSV export today — `export_batch_metrics.py` only pulls the five tables in the rightmost column
above. Three more S3 prefixes exist under `metrics/` that are **not** part of this queryable set
— see [Non-schema objects](#non-schema-s3-objects) at the bottom.

> **Row counts and cohort statistics in this document are historical, not reproducible.** The
> metrics corpus was flushed on **2026-08-10**, so every figure below that quotes a row count, a
> cohort mean, or a measured distribution describes the corpus as of the date attached to it and
> **cannot be re-derived by querying today**. The figures are retained because they document real
> behaviour (FastSurfer non-determinism, the duplication factor, the drift episodes) that a fresh
> corpus will reproduce in kind but not in value. Field names, types, sentinels, schema versions
> and gating thresholds are all verified against the current code and *are* authoritative.

---

## `FuncQC` — per BOLD run

**`export_batch_metrics.py` output file: `func_qc.csv`**

Emitted by `compute_func_qc_summary()` in `images/afni/preproc.py`, at the end of functional
preprocessing for one BOLD run. Dataclass: `src/metrics/schemas.py:51`.

S3 key: `metrics/func-preproc/dt={dt}/{subject}_{session}_{task}_{run}_qc.json`

| Field | Type | Description |
|---|---|---|
| `subject`, `session`, `task`, `run` | str | Run identity |
| `n_frames` | int | Total frames in the BOLD volume |
| `n_nss_frames` | int | Non-steady-state frames dropped |
| `tr_seconds` | float | Repetition time (s) |
| `mean_fd`, `median_fd`, `max_fd` | float | Framewise displacement stats (mm) |
| `n_fd_above_0p2`, `n_fd_above_0p5` | int | Frame counts above FD thresholds |
| `pct_fd_above_0p5` | float | % frames with FD > 0.5 mm — primary motion exclusion criterion |
| `mean_dvars` | float | Mean DVARS (RMS of the frame-to-frame difference in masked BOLD intensity) |
| `dvars_std` | float | **Schema 1.1.** DVARS standardized as percent signal change (Power et al. 2012): `mean_dvars / mean_global_signal * 100`. Comparable across subjects with different global intensity scaling; `mean_dvars` alone is not |
| `mean_global_signal` | float | Mean global signal |
| `tsnr_median` | float | Median temporal SNR, over in-brain voxels of the volumetric BOLD |
| `gcor` | float | **Schema 1.1.** Global correlation (mriqc/AFNI `gcor`) — mean of every pairwise voxel-timeseries correlation in the brain mask, as the squared length of the average unit-length voxel time series. Sensitive to diffuse motion/physiological contamination that frame-to-frame metrics (FD, DVARS) miss when motion is smooth rather than spiky |
| `aor` | float | **Schema 1.1.** AFNI outlier ratio (mriqc `aor`) — mean fraction of outlier voxels per frame |
| `aqi` | float | **Schema 1.1.** AFNI quality index (mriqc `aqi`) — mean per-frame `1 - Spearman correlation with the median volume`. Lower is better |
| `n_acompcor_wm`, `n_acompcor_csf` | int | aCompCor confound regressor counts (WM and CSF) |
| `n_tcompcor`, `n_cosines` | int | tCompCor + cosine-basis regressor counts |
| `stage_timings_s` | dict | Per-stage wall time: `boldref`, `composite_warp`, `4d_warp`, `mask_warp`, `masking`, `confounds`, and `grayordinates` on runs that emit surface output. Query `4d_warp` as `stage_timings_s."4d_warp"` — unquoted, its leading digit fails the **whole** query with `MALFORMED_QUERY`, not just that column. Pre-2026-08-03 records also carry `stc`, always ~0 — slice timing correction was a no-op on every run and has been removed |
| `total_runtime_s` | float | Total preprocessing wall time (s) |
| `peak_memory_gb` | float | Peak RSS for **this run only** (`RUSAGE_SELF`/`RUSAGE_CHILDREN` max) — resets per run |
| `container_peak_memory_gb` | float | Peak RSS over the **container's lifetime** — climbs across a multi-run session even when runs are identically sized; size pod memory limits against this one, not `peak_memory_gb` |
| `pipeline`, `image_tag` | str | Provenance: pipeline name and image SHA |
| `schema_version` | str | `"1.1"` (`"1.0"` records predate `dvars_std`/`gcor`/`aor`/`aqi` — those come back absent, not zero, on pre-1.1 rows). Schema 1.1 originally shipped with an IQM block that [#119] showed could never have run: `aor`/`aqi`/`gcor` shelled out to AFNI's `3dToutcount`/`3dTqual`/`@compute_gcor`, and the conda-forge `afni` package ships only 71 of AFNI's ~600 programs, so none of the three binaries existed in the image (the calls surfaced as `PermissionError`, not "not found"). All three are now **reimplemented in numpy** at AFNI's default settings, so the values stay comparable with AFNI's own and with MRIQC's, which wrap the same programs. Parity is exact for `aor` and `gcor`; `aqi` matches AFNI only to ~1e-5. The schema version deliberately stayed at 1.1 — the field set didn't change, only whether it was populated. On failure all three record `0.0` (with a WARNING in the pod log), not absent |

**`gcor`/`aor`/`aqi` are computed in-process in `preproc.py`, at the same definitions as AFNI's
`@compute_gcor`, `3dToutcount -fraction` and `3dTqual`** — the tools MRIQC wraps for the same
three metrics, so the values stay comparable with published MRIQC norms. They were originally
shelled out to those binaries, but none of the three ship in the conda-forge `afni` package the
image installs (71 of AFNI's ~600 programs), so the calls never ran ([#119]). `tests/images/afni/
test_bold_iqms.py` pins all three against values the real AFNI binaries produced; agreement is
exact for `gcor`/`aor` and within ~1e-5 for `aqi`, where AFNI's own sequential float32
accumulation is the limiting error.

Recorded but not gated — no thresholds are calibrated yet for any of these four fields. **`0.0`
means "not computed", not "computed as zero"**: these three are descriptive metrics derived after
the run's derivatives are already on disk, so a failure to compute them downgrades to the schema
default and logs a warning rather than costing the run.

[#119]: https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/119

**Present only on runs that also emit grayordinates** (`--emit both`, i.e. surface output was
requested for that run) — merged in via `qc.update(surf_metrics)` in `preproc.py`, absent
(not zero — the key is just missing) on volumetric-only runs:

| Field | Type | Description |
|---|---|---|
| `surf_L_n_vertices`, `surf_R_n_vertices` | int | Vertex count per hemisphere's surface mesh |
| `surf_L_coverage_frac`, `surf_R_coverage_frac` | float | Fraction of vertices whose timeseries is not identically zero — vertices sampling outside the BOLD field of view read as constant zero, so this is the direct measure of how much cortex the acquisition covered |
| `surf_L_nan_frac`, `surf_R_nan_frac` | float | Fraction of NaN samples in the surface timeseries |
| `surf_L_tsnr_median`, `surf_R_tsnr_median` | float | Median tSNR over *covered* vertices only |
| `subcort_n_voxels` | int | Voxel count across all sampled subcortical structures |
| `subcort_n_structures` | int | Distinct subcortical structure labels sampled |
| `subcort_space` | str | Always `"MNI152NLin2009cAsym"` today |

These same fields, plus a provenance envelope (`emit`, `stage_timings_s`, `total_runtime_s`,
`peak_memory_gb`, `container_peak_memory_gb`), are *also* written standalone to
`metrics/surface-sample/` — see [Non-schema objects](#non-schema-s3-objects).

**`pending_duration_s` was removed from this schema in [#147]**: it was always `0.0` — the
`POD_CREATION_TIMESTAMP` env var its docstring described was never set, and *couldn't* be, since
the Kubernetes Downward API's `fieldRef` doesn't expose `metadata.creationTimestamp` (only
`name`/`namespace`/`uid`/`labels`/`annotations`, plus a few `spec`/`status` fields) — no per-pod
queue-wait signal reaches this container from within Kubernetes. `WorkflowRun.pending_duration_s`
(below) is the queue-wait field that still exists; it's measured once per workflow — and **not**
from Argo DAG node timing, but from a marker file a dedicated no-`depends`
`record-workflow-start-dagtask` writes to `metrics/workflow-starts/`, which the exit handler reads
back. Argo's own `workflow.outputs.parameters` can't be used for this: `argo lint --offline`
cannot statically resolve it from an `onExit` template even though it works at runtime.

[#147]: https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/147

---

## Anatomical QC — the two T1w-derived tables

`AnatQC` and `FsqcQC` are two halves of one thing and are documented together for that
reason. Both are derived from the same T1w image, both sit at one row per subject×session,
and **neither is complete on its own**: WM/GM SNR left `AnatQC` at schema 1.2 and now lives
only in `FsqcQC`, while volumes, cortical thickness, and the mriqc-style IQMs live only in
`AnatQC`. Join them on `subject`+`session`.

`CloudpipeMetrics.anatomical_qc()` (both the Athena and the DuckDB engine) returns that join
as one frame — reach for it rather than joining by hand, and read its docstring before
interpreting a half-empty row.

**The two are not written together, and that asymmetry is the thing to know.** `fsqc-metrics`
runs on every workflow (its spec deliberately has no skip-on-existing-output gate), while
`AnatQC` is written only when FastSurfer actually reprocesses. A batch reusing existing
FastSurfer derivatives therefore writes `FsqcQC` rows under today's `dt=` and **no `AnatQC`
rows at all** — the pair for those sessions is under the `dt=` of their original run, days or
weeks earlier. A hand-written single-`dt` query returns one half and no error;
`export_batch_metrics.py` prints an explicit note when it sees that shape.

`anatomical_qc()` handles it by separating *which scans* from *which record*: `dt_from`/`dt_to`
select the scans (a scan is in scope if **either** table wrote in the window), and each scan is
then annotated with its most recent record from each table, whenever that was written. Measured
on the 2026-08-09 pilot: filtering both tables by the batch window paired **0 of 23** scans,
this pairs **23 of 23**. The Grafana combined panel does the same thing, one-sidedly.

**Both tables accumulate a record per re-run, so raw row counts are not scan counts.** As of
2026-08-09 (pre-flush) `anat_qc` held **962 rows for 271 subject×session** — up to 9 records for one scan,
spread across 9 `dt=` partitions, and their values genuinely differ (`sub-T1GJUT9Z`/`ses-02A`
has 7 distinct `total_brain_vol_mm3` across its 9 runs, so this is FastSurfer non-determinism,
not duplicate copies). Deduplicate on `subject`+`session` taking the latest `completed_at`
before averaging or plotting a distribution; cohort means barely move (mean eTIV 1,515,449 raw
vs 1,517,394 deduped, 0.13%) because the duplication is near-uniform, but a distribution is
inflated ~3.5× and a per-scan listing repeats each scan. `anatomical_qc()` and every panel on
the Grafana dashboard already dedupe.

The Grafana dashboard **CloudPipe — Anatomical QC** (`cloudpipe-anatqc`) covers both tables,
with rows grouped by what a metric measures rather than by which table it came from.

---

### `AnatQC` — per subject×session

**`export_batch_metrics.py` output file: `anat_qc.csv`**

Emitted by `images/fastsurfer/extract_qc.py` after FastSurfer parcellation completes. Parses
plain-text `stats/aseg.stats` and `stats/{lh,rh}.aparc.stats` for the volume/morphometry
fields; the T1w image-quality fields (schema 1.1+) load `mri/orig.mgz`, `mri/brainmask.mgz`,
and `mri/aseg.auto.mgz` via nibabel/numpy instead. Matches its dataclass exactly
(`src/metrics/schemas.py:124`) — no drift.

WM/GM SNR is **not** here. It lives in [`FsqcQC`](#fsqcqc--per-subjectsession) as
`wm_snr_orig/norm` and `gm_snr_orig/norm`, computed by `fsqc`; join `anat_qc` to `fsqc_qc` on
`subject`+`session`. This file's own `snr_gm`/`snr_wm` were removed at schema 1.2 because they
used a narrower WM label set with no boundary erosion, which biased the numbers low.

S3 key: `metrics/anat-qc/dt={dt}/{subject}_{session}_anat_qc.json`

| Field | Type | Description |
|---|---|---|
| `subject`, `session` | str | Identity |
| `efc` | float | Entropy Focus Criterion — Shannon entropy of voxel intensities; higher means more ghosting/blur from motion. Approximate mriqc-style IQM (whole image, no air-mask refinement) |
| `fber` | float | Foreground-to-Background Energy Ratio — mean squared intensity inside `brainmask.mgz` over outside. Higher is better |
| `cnr` | float | Contrast-to-Noise Ratio between WM and GM (FastSurfer `aseg.auto.mgz` labels 2/41 and 3/42) — higher means better-separated tissue distributions |
| `cjv` | float | Coefficient of Joint Variation between WM and GM — lower is better (inverse-flavored relative to CNR) |
| `wm2max` | float | WM mean intensity over the 99.95th percentile of all positive voxels — flags hyperintense vessel/artifact voxels pulling the max up |
| `fwhm_x_mm`, `fwhm_y_mm`, `fwhm_z_mm`, `fwhm_avg_mm` | float | Per-axis and averaged image smoothness (mm), via Forman's closed-form adjacent-voxel-variance estimator — coarser than mriqc/AFNI's ACF-fit FWHM (assumes a single stationary Gaussian ACF), but needs no AFNI dependency in the FastSurfer image |
| `etiv_mm3` | float | Estimated total intracranial volume (mm³) — use as the denominator to normalize other volumes across subjects of different head size |
| `total_brain_vol_mm3` | float | Brain segmentation volume (mm³); report `total_brain_vol_mm3 / etiv_mm3 * 100` for ICV-normalized QC — the raw volume is eTIV-confounded |
| `lh_cortex_vol_mm3`, `rh_cortex_vol_mm3` | float | Cortical grey matter volume per hemisphere (mm³) |
| `wm_vol_mm3` | float | Cerebral white matter volume (mm³) |
| `subcort_gm_vol_mm3` | float | Subcortical grey matter volume (mm³) |
| `lh_mean_thickness_mm`, `rh_mean_thickness_mm` | float | Mean cortical thickness per hemisphere (mm); healthy adult/adolescent ≈ 2.2–2.9 mm |
| `lh_surface_area_mm2`, `rh_surface_area_mm2` | float | Pial surface area per hemisphere (mm²) |
| `schema_version` | str | `"1.2"` (`"1.0"` records predate the T1w image-quality fields above — those come back `NULL`/absent, not zero. `"1.1"` records additionally carry `snr_gm`/`snr_wm`, dropped at 1.2 in favor of the `fsqc_qc` table's `wm_snr_norm`/`gm_snr_norm`; the two columns are declared on neither Glue table, so pre-1.2 values are still in S3 but no longer queryable) |

> **Fixed — 1.2 records were invisible in `anat_qc_compacted`.** `AnatQC` has emitted
> `schema_version: "1.2"` since the SNR removal, but `anat_qc_compacted`'s
> `projection.schema_version.values` in `terraform/modules/metrics/main.tf` was left at
> `"1.0,1.1"`. `schema_version` is a *projected partition key* on the compacted tables, so a
> value outside that enum is not a valid partition location and its Parquet returned **nothing
> at all** — silently, with no error. The enum now reads `"1.0,1.1,1.2"`. The raw `anat_qc`
> table was never affected (it partitions on `dt` only), so nothing was lost — the records were
> unreadable through the compacted table, not missing.
>
> This is the failure mode to remember when bumping any `schema_version`: an undeclared
> *column* reads as `NULL`, but an out-of-enum *partition key* returns an empty result set with
> a successful query status. See the checklist in [ADR 011](decisions/011-s3-athena-for-metrics.md).

**All nine image-quality fields are `0.0`, not absent, if `mri/orig.mgz`, `mri/brainmask.mgz`,
or `mri/aseg.auto.mgz` was missing when `extract_qc.py` ran** (logged as a WARNING in the pod's
stdout) — unlike the `schema_version: "1.0"` case above, which omits the fields entirely. Filter
out true zeros with care; a same-session `efc == 0.0` alongside `etiv_mm3 > 0` is the missing-
input case, not a real EFC of zero.

Recorded but not gated — no thresholds are calibrated yet for any of these nine fields.
`docs/decisions/` has no ADR on structural T1w IQM thresholds; treat these as candidates for
cross-subject outlier review, not pass/fail gates, per the same reasoning that kept
`bold_to_t1w`'s `nmi`/`rigid_disp_mean_mm` gate-free below (§`RegistrationQC`).

---

### `FsqcQC` — per subject×session

**`export_batch_metrics.py` output file: `fsqc_qc.csv`**

Emitted by `images/fsqc/stage_and_run.py`, which runs [Deep-MI/fsqc](https://github.com/Deep-MI/fsqc)
2.1.7 against the FastSurfer and subregion derivatives already in S3. **One pod per subject
writes one record per session**: fsqc's `--outlier` module compares each session against the
others in the same invocation, so a per-session fan-out would lose the outlier fields entirely.

Note the vocabulary clash: **fsqc's "subject" is our session.** The driver stages a merged
`$SUBJECTS_DIR` holding one directory per session, so `subject` below is the cloudpipe subject
and each row describes one of its sessions.

Constructed as a raw dict in `build_record()`, but assembled from **three** sources, not one:
`fsqc-results.csv` (the core metrics, rotation, and outlier counts), `outliers/all.regions.stats`
(the hypothalamic volumes — the subregion modules contribute *no* CSV columns at all), and the
per-module `status/{session}/status.txt` files (the four `*_status` codes). Matches `FsqcQC` in
`src/metrics/schemas.py`.

WM/GM SNR lives here and **not** in [`AnatQC`](#anatqc--per-subjectsession), which dropped
`snr_gm`/`snr_wm` at schema 1.2. Volumes and cortical thickness live only there. The two tables
are complementary at the same grain — join on `subject`+`session`.

S3 key: `metrics/fsqc-qc/dt={dt}/{subject}_{session}_fsqc_qc.json`
QC images (not part of the record): `fsqc/{subject}/{session}/*.png` — the hippocampus and
hypothalamus overlays plus a whole-brain `screenshots-{session}.png`. The browsable summary page
covering all of a subject's sessions is `fsqc/{subject}/fsqc-results.html`; its image links are
rewritten at upload time to match this layout, since fsqc emits them relative to its own output
tree (`{module}/{session}/{file}`).

**Every metric field is nullable, and that is deliberate — unlike every other table here, which
uses `0.0` as its missing-value default.** fsqc writes `NaN` for a metric whose inputs are absent
and the driver maps `NaN` to JSON `null`, never `0.0`, because `0` is a legitimate measurement for
`rot_tal_*` and the `n_outlier_*` counts. **Filter on `IS NOT NULL`, never on `> 0`.**

| Field | Type | Description |
|---|---|---|
| `subject`, `session` | str | Identity. `session` is what fsqc itself calls the subject |
| `wm_snr_orig`, `gm_snr_orig` | float? | WM/GM signal-to-noise from `mri/orig.mgz` (uncorrected) |
| `wm_snr_norm`, `gm_snr_norm` | float? | WM/GM SNR from the bias-corrected `mri/norm.mgz` — **prefer these**, and use them in place of `AnatQC`'s removed `snr_wm`/`snr_gm`. Expect them close to the `_orig` pair, not systematically above it: ABCD minimally preprocessed input is already intensity-normalized upstream (Hagler et al. 2019), so the bias correction has little left to remove (`sub-086U18RD`/`ses-00A`: 26.01 orig vs 25.69 norm). Both `_norm` and `_orig` read *much higher* than `AnatQC`'s old `snr_wm` — that gap is the eroded masks and broader WM label set, and makes neither comparable to the pre-1.2 numbers |
| `cc_size` | float? | Corpus callosum size as a fraction of eTIV — fsqc's proxy for a failed or truncated talairach registration |
| `holes_lh`, `holes_rh`, `defects_lh`, `defects_rh`, `topo_lh`, `topo_rh` | float? | Surface topology. **`NULL` on every row today**: FastSurfer writes no `surf/[lr]h.orig.nofix`, and its `scripts/recon-all.log` carries no defect counts (it runs `mris_fix_topology -all-info`, which emits structured output instead). Declared so a future FreeSurfer-based run needs no schema change |
| `con_snr_lh`, `con_snr_rh` | float? | White/gray contrast-to-noise per hemisphere, from `surf/[lr]h.w-g.pct.mgh` |
| `rot_tal_x`, `rot_tal_y`, `rot_tal_z` | float? | Rotation components of `transforms/talairach.lta`, in radians. Large magnitudes mean the head was acquired far off the template orientation. **`0.0` is a real value here** |
| `n_outlier_norms` | float? | Count of aseg/aparc regions falling outside fsqc's built-in normative ranges. A count, but fsqc emits it as a float. Note the **singular** `n_outlier_`: fsqc's own docstring documents these as `n_outliers_*`, but the singular is what it writes to the CSV, and the plural spelling in a schema makes the column `NULL` forever |
| `n_outlier_sample_nonpar`, `n_outlier_sample_param` | float? | Sample-based (cohort-relative) outlier counts. **`NULL` on every row today** — these need a reference cohort the driver does not pass |
| `hypothalamus_whole_left_mm3`, `hypothalamus_whole_right_mm3` | float? | Whole hypothalamus volume per side (mm³), lifted from `outliers/all.regions.stats`. `NULL` when the hypothalamic segmentation did not cover this session, which is **common and not an error**: the tarball is subject-level but ships empty per-session `mri/` dirs for uncovered sessions, so "tarball present" ≠ "session covered" |
| `metrics_status`, `outlier_status`, `hippocampus_status`, `hypothalamus_status` | int? | Per-module exit codes from `status/{session}/status.txt`. `0` = the module ran clean, non-zero = it degraded (missing input, `NaN` output), `NULL` = it never reported. **`0` and `NULL` are not the same thing** — that distinction is the only way to tell "ran and found nothing" from "never ran", so read the relevant status alongside any `NULL` metric |
| `fsqc_version` | str | fsqc version that produced the row (`"2.1.7"`), pinned in `images/fsqc/Dockerfile`. Recorded per record because fsqc's metric *definitions* are version-dependent |
| `schema_version` | str | `"1.0"` |

A session whose subregion derivatives are absent yields a **partial but flagged** record — a
non-zero `*_status` with `NULL` metrics — not a step failure. Module flags are global to the fsqc
invocation rather than per-session, so per-session absence is handled by fsqc's own graceful
degradation (non-zero status, `NaN` fields, exit 0) instead of by conditional flags.

Recorded but not gated: no thresholds are calibrated for any of these fields. Treat them as
candidates for cross-subject outlier review, on the same reasoning as `AnatQC`'s IQMs above.

---

## `RegistrationQC` — per registration step

**`export_batch_metrics.py` output file: `registration_qc.csv`** (both `registration_type`s
below, in one file — filter on that column)

Two independent scripts write to this one prefix under a shared `registration_type`
discriminator, and **each emits a genuinely different, non-overlapping field set** — the
dataclass at `src/metrics/schemas.py:348` is a superset of both, padded with each other's
defaults, and its own field defaults (including `schema_version: "1.2"`) do not reflect what
either live emitter actually writes today. Always filter `registration_qc(registration_type=...)`
and read the section below for the type you asked for, not the dataclass.

### `t1w_to_mni` (schema 2.1) — from `images/fireANTs/scripts/fst1w_to_mni.py` (FireANTs SyN, GPU)

S3 key: `metrics/registration/dt={dt}/{subject}_{session}_t1w_to_mni_reg_qc.json`

| Field | Type | Description |
|---|---|---|
| `subject`, `session` | str | Identity. `task`/`run` are present but always `""` — this is a session-level record |
| `registration_type` | str | `"t1w_to_mni"` |
| `lncc` | float | Mean local normalized cross-correlation in the template brain mask |
| `mask_dice` | float | Dice between the MNI brain mask and the warped T1w mask; healthy ≈ 0.98. **Only present if `--brainmask` was passed** — absent, not zero, otherwise |
| `jac_det_min`, `jac_det_max`, `jac_det_mean`, `jac_det_std` | float | Jacobian determinant of the SyN warp, restricted to the template brain mask (schema 2.1+ — not comparable to pre-2.1 whole-field values); `_mean` ≈ 1 for a healthy warp |
| `jac_det_frac_negative` | float | Fraction of brain voxels with Jacobian det < 0 (folded warp). **Gated: fails above 0.005.** The earlier 0.001 sat *inside* the healthy distribution (10-subject batch: mean 0.0004, sd 0.0002, max 0.0013), so a normal registration tripped it ~17% of the time |
| `log_jac_mean`, `log_jac_std` | float | log(det J) distribution — symmetric about 0 (+0.69 = doubling of local volume, −0.69 = halving), so expansion/compression are directly comparable, unlike raw det J |
| `log_jac_p01`, `log_jac_p99` | float | 1st/99th percentile of log-Jacobian — robust lower/upper edge |
| `log_jac_min`, `log_jac_max` | float | Worst single compressing / expanding voxel |
| `log_jac_frac_beyond_1p5`, `log_jac_frac_beyond_3` | float | Fraction of (non-folded) brain voxels with \|log det J\| beyond 1.5 / 3 — healthy T1w→MNI152 keeps the vast majority within ±1.5; mass near ±3 means tissue is being squashed/ballooned to force an intensity match. **Recorded but not gated** — no threshold calibrated yet |
| `ice_mean_mm`, `ice_p95_mm`, `ice_p99_mm`, `ice_max_mm` | float | Inverse consistency error (mm): displace a voxel by the forward warp then the inverse warp sampled there — the residual from the start point. Reads no image intensities, so it catches a warp that matches intensities well but isn't globally invertible. **All four absent (not zero) if the inverse warp couldn't be computed/saved** — `verdict()` skips absent keys, so this gate fails *open*. Only `ice_mean_mm` is gated (fails **above** 0.5 mm, sub-half-voxel on 1 mm MNI152) |
| `centroid_displacement_mm` | float | Brain-mask centroid displacement after alignment (mm) — one of three `verdict` inputs |
| `verdict` | str | `"pass"` / `"fail"` against `registration_qc._T1W_MNI_THRESHOLDS`, which declares **three** fail bounds: `lncc < 0.65`, `jac_det_frac_negative > 0.005`, `ice_mean_mm > 0.5`. `mask_dice` and `centroid_displacement_mm` are recorded but **not** gated. **There is no warn band** — both threshold tables dropped theirs on 2026-07-30 and `verdict()` can no longer emit one, because a warn exited 0 and promoted outputs exactly as a pass did. Records written before then still carry `"warn"`, so queries spanning historical partitions must handle the value |
| `completed_at`, `schema_version` | str | `schema_version` is `"2.1"` |

### `bold_to_t1w` (schema 2.6) — from `images/freesurfer/bold_to_t1w.py` (SynthMorph, rigid)

S3 key: `metrics/registration/dt={dt}/{subject}_{session}_{task}_{run}_bold_to_t1w_reg_qc.json`

| Field | Type | Description |
|---|---|---|
| `subject`, `session`, `task`, `run` | str | Identity |
| `registration_type` | str | `"bold_to_t1w"` |
| `method` | str | Always `"synthmorph"` |
| `nmi` | float | Studholme normalized MI between the warped BOLD and T1w, within the T1w brain mask. **Not on the textbook 1.0–2.0 scale in practice** — for this cross-contrast EPI↔T1w pairing a *good* registration scores ~1.019 and identity (no registration at all) scores ~1.011; don't read ~1.02 as a failure. Recorded, not itself gated |
| `nmi_identity` | float | NMI of the same BOLD reference resampled with **no** transform — the per-session no-registration baseline (schema 2.4) |
| `nmi_gain` | float | `nmi - nmi_identity` — what the fitted transform actually bought, scored against its own session baseline rather than an absolute NMI cutoff (absolute `nmi` isn't comparable across sessions: the identity baseline alone spans ~0.004, about the size of the gain itself). **This is the only gated metric** (schema 2.4+), and the bound is merely `> 0` — Phase A found no absolute gate supportable (best AUC 0.83 against a 0.95 bar). Measured 0.0046–0.0089 across 18 healthy runs |
| `rigid_disp_mean_mm`, `rigid_disp_max_mm` | float | Mean / worst-case brain-voxel displacement induced by the rigid transform. **Recorded only, not gated** — schema 2.3 gated on these and was wrong: on ABCD minimally-preprocessed input the BOLD is never resampled out of scanner space, so these track BOLD-vs-T1w field-of-view prescription (a session-level constant, ~69× between/within-session SD ratio), not registration quality, and correlate *positively* with `nmi`. The 2.3 gate failed 92/110 runs in the 2026-07-29 batch and passed none of the sessions it should have — see `docs/investigations/2026-07-29-bold-to-t1w-qc-handoff.md` |
| `rigid_rot_deg` | float | Rotation angle of the rigid transform, degrees. Recorded only, same reasoning as above |
| `mhd_mm` | float | Modified Hausdorff distance (mm) between the T1w brain surface and an Otsu skull-strip of the warped BOLD. Recorded only — its floor is set by the soft 2.4mm EPI skull-strip, not the registration, so it's the least trustworthy of these to gate. `-1.0` = could not compute (empty EPI mask) |
| `seg_bbr_contrast`, `seg_bbr_contrast_identity` | float | Boundary-sensitive alignment (schema 2.5+), **recorded only**: `(mean_GM − mean_WM) / mean` in the WM/GM shell, sampling the warped BOLD through FastSurfer's `aseg.auto.mgz` — BBR's premise *measured* rather than optimised, on the fitted and identity-resampled BOLD respectively. Phase A (2026-07-30) found this the strongest of seven candidates against a known 2 mm misregistration (AUC 0.830, d′ 1.31, Spearman −0.90), so it is the metric to build any future relative check on — but **normalised within session**. **Sentinel is `-999.0`, not `0.0`**: a zero contrast is a real measurement meaning the tissues are indistinguishable, so filter on `> -999` the way `nmi` is filtered on `> 0` |
| `ngf`, `ngf_identity` | float | Normalized gradient field (schema 2.5+), **recorded only**: squared cosine between BOLD and T1w intensity-gradient directions on T1w edges, edge-strength weighted. In [0, 1], higher better; the square makes it blind to the EPI↔T1w polarity flip, as MI is. `0.0` is both the natural floor **and** the "no evaluation voxels" sentinel |
| `verdict` | str | `"pass"` / `"fail"` — the sole bound in `_BOLD_T1W_THRESHOLDS` is `nmi_gain <= 0.0` (an *inclusive* below-bound: landing exactly on 0 fails). **There is no warn band and no 0.0005/0.002 threshold** — those were the pre-calibration numbers. A `"fail"` exits the step with code 65 so the driver discards the run's outputs, but the QC record (with `verdict="fail"`) still uploads. `""` on the early-exit failure path |
| `completed_at`, `schema_version` | str | `schema_version` is `"2.6"` |

**Removed in schema 2.6** (emitted by 2.5 records only): `seg_bbr_contrast_gain`, `ngf_gain`, and
`seg_ventricle_ratio`. Every `_gain` variant measured *worse* than its absolute counterpart in
Phase A (0.695 vs 0.830 and 0.693 vs 0.805) — the identity baseline is dominated by the
BOLD-vs-T1w field-of-view prescription, a session-level constant, so subtracting it **injects**
the session variance it was meant to cancel. Both gains stay exactly derivable from the retained
operands, including for historical 2.5 records. `seg_ventricle_ratio` was non-monotone (AUC rose
to 0.806 at 10 mm then fell to 0.585 at 20 mm — grosser misregistration scored *healthier*) and
is not derivable from anything retained, though the values survive in the 2.5 S3 JSON.
`nmi_gain` is deliberately kept despite the same critique: "did the transform buy anything over
identity" is inherently relative, so the gain framing is correct *there* even though it is a
poor absolute quality signal.

**Fields the dataclass has that neither live emitter writes:** `dice`, `mi`, `bbr_cost`,
`bbr_converged`, `bbr_init_used` — all dead. `dice`/`mi` were superseded (by `mask_dice`/`nmi`
respectively); the BBR trio described a registration method (`"synthmorph+bbr"`) removed in
`61ccff7` because ABCD's 2.4mm EPI lacks the gray/white contrast BBR needs. They only appear
in historical pre-2.0/pre-1.2 records still in the bucket, deserialized by the dataclass for
backward compatibility — a query spanning old and new records will see them as `NULL` on
every current row.

> **These four dead fields used to break `registration_qc(compacted=True)` — fixed.**
> `dice`, `bbr_cost`, `bbr_converged` and `bbr_init_used` are declared on
> `registration_compacted` (where historical schema-1.x Parquet needs them) but **not** on the
> raw `registration` table — the only raw/compacted pair in the catalog that differs by anything
> beyond `schema_version`. `_UNION_COLUMNS["registration"]` listed all 45 columns and is applied
> to *both* halves of the `UNION ALL`, so the raw half referenced four columns that don't exist
> and Athena rejected the whole query with `COLUMN_NOT_FOUND`.
>
> `_UNION_COLUMNS["registration"]` is now the **intersection** (41 columns) rather than the
> compacted table's list. The Glue declarations were left as they are: the compactor infers
> Parquet columns from the records, so the extra four are inert, and leaving them declared keeps
> historical values readable by querying `registration_compacted` directly. Do not "resync" the
> two column lists to make them symmetrical — that reintroduces the bug.
>
> Fixing this exposed a second, wider bug underneath it (Athena resolves columns before it
> type-checks, so only one error surfaced at a time): `_query()` built `dt = CURRENT_DATE`, but
> `dt` is a `string` partition on all nine tables, so every `compacted=True` query died on
> `TYPE_MISMATCH: cannot apply operator: varchar = date`. The feature had never worked for any
> table. Both are fixed and covered by tests in `tests/metrics/test_athena.py` and
> `tests/metrics/test_registration_union_columns.py`.

---

## `WorkflowRun` — per Argo workflow

**`export_batch_metrics.py` output file: `workflow_runs.csv`**

Written by `src/metrics/exit_handler.py` on every `onExit` trigger, including failed and
errored workflows. Constructed via the dataclass directly (`src/metrics/schemas.py:175`) — no
drift risk.

S3 key: `metrics/workflow-runs/dt={dt}/{workflow_name}__{subject}_run_summary.json`

| Field | Type | Description |
|---|---|---|
| `workflow_name` | str | Argo workflow name, e.g. `cloudpipe-abc12` — join key to `CostAllocation` |
| `subject` | str | Subject ID from workflow parameters |
| `status` | str | `Succeeded` \| `Failed` \| `Error` |
| `started_at`, `finished_at` | str | ISO 8601 UTC |
| `total_duration_s` | int | Wall time, from `workflow.duration` |
| `pending_duration_s` | float \| null | Seconds from workflow submission (`creationTimestamp`) to the wall-clock time a dedicated `record-workflow-start-dagtask` (no `depends`, starts immediately alongside the real first step) actually ran — queue + node-provision wait. That task writes its own start time to `metrics/workflow-starts/dt={dt}/{workflow_name}.json`, which the exit handler reads back; it isn't threaded through Argo's `workflow.outputs.parameters`, because `argo lint --offline` can't statically resolve that from an `onExit` template even though it works at runtime. **`null` means unmeasured, never a confident `0.0`** ([#147]): the two source timestamps are treated as equal/inverted whenever they can't be trusted, which is exactly what the pre-#147 wiring bug produced on every record |
| `message` | str | Argo failure message; empty on success. In practice usually empty even on failure — Argo has no `{{tasks.<name>.message}}` DAG variable, see `docs/decisions/` — `failed_step`/`failure_category` are the reliable failure signal, not this field |
| `failed_step` | str | Canonical name of the first failed step; `""` on success |
| `failure_category` | str | `infrastructure` \| `algorithm` \| `data` \| `dependency` \| `unknown` \| `""`; see the `StepOutcome` taxonomy below — it's the same classifier |
| `schema_version` | str | `"1.1"` |

---

## `CostAllocation` — per workflow, per scrape date

**`export_batch_metrics.py` output files: `costs_raw.csv`** (this table, unaggregated) **and
`costs_by_subject.csv`** (via `subject_costs()` — summed to one row per subject, see the grain
warning below)

Written nightly (02:00 UTC, or on demand) by `src/metrics/kubecost_scraper.py`, one record per
Argo workflow visible to Kubecost that day. Requires the `subjectid` pod label. Constructed via
the dataclass (`src/metrics/schemas.py:521`) — no drift risk.

S3 key: `metrics/costs/dt={date}/{date}_{workflow_name}_cost_allocation.json`

| Field | Type | Description |
|---|---|---|
| `date` | str | YYYY-MM-DD **scrape date** — the day *after* the workflow ran, since the scraper's default window is "yesterday". Equal to the `dt=` partition value |
| `workflow_name` | str | Argo workflow name — join key to `WorkflowRun`. Absent on schema-1.0 records (pre-dates this field; query with `union_by_name=true` in DuckDB, Athena tolerates it natively) |
| `subject` | str | From the `subjectid` Kubernetes pod label |
| `total_cost_usd` | float | Total Kubernetes compute cost (USD) |
| `cpu_cost_usd`, `memory_cost_usd`, `gpu_cost_usd` | float | Cost components |
| `total_adjustment_usd` | float | **Schema 1.2+.** The portion of `total_cost_usd` that Kubecost's reconciliation against cloud billing (AWS CUR + spot data feed) has already applied for this specific allocation — sum of Kubecost's `cpuCostAdjustment`/`gpuCostAdjustment`/`ramCostAdjustment`/`networkCostAdjustment`/`loadBalancerCostAdjustment`/`pvCostAdjustment`. Absent (NULL, not 0) on schema ≤1.1 records. **Nonzero proves reconciliation has touched this row; zero is ambiguous** — could mean not-yet-reconciled, or reconciled with no correction needed. There is no Kubecost field that disambiguates those two cases directly — see `scrape_age_days` |
| `scrape_age_days` | int | **Schema 1.2+.** Days between `date` (the report date) and when this record was actually written, i.e. `completed_at`'s date minus `date`. The nightly scraper writes `1` (day+1, Kubecost's least-reconciled point — see `kubecost_drift_probe.py`), and the settled re-scrape overwrites the same key with `3` once that date's reconciliation has converged. `3` is therefore the normal steady-state value and `1` means either a date less than 3 days old or one whose re-scrape never landed; a still-higher value is a manual backfill. This is the field to filter/sort on when comparing two records for the same `(date, workflow_name)` — the one with the larger `scrape_age_days` is the more-reconciled read. Defaults to `1` on schema ≤1.1 records read back today (they predate the field, and were in practice all day+1 scrapes) |
| `schema_version` | str | `"1.2"` |

**Grain is one row per workflow per scrape date, not one row per workflow.** A workflow that
spans the UTC-midnight boundary is scraped on two consecutive days and gets two rows (this is
deliberate — see `CostAllocation.__doc__`); a subject reprocessed after a failure gets rows
under each attempt's workflow name. Never `SUM(total_cost_usd)` over a bare `costs()` pull —
use `subject_costs(subjects=..., date_from=..., date_to=...)`, which sums to the right grain
and requires a subject list because `metrics/costs/` accumulates indefinitely across every
batch ever run.

**Cost figures here are Kubecost's allocation, which reaches settled values at age 3.** Kubecost
prices from Prometheus usage × estimated rates and reconciles toward the actual AWS bill. The
nightly day+1 scrape catches a date at its least-reconciled point, where it overstates the
settled total by a **median ~51%** (8–240% across the 10 measured dates — wide enough that it
cannot be corrected for with a constant, which is why the re-scrape exists rather than a
fudge factor); the nightly flow then re-scrapes each report-date once at age 3
(`kubecost_scraper.SETTLED_AGE_DAYS`), overwriting those records with reconciled values.
Reconciliation converges at age 3 and never moves again — measured longitudinally 2026-08-05,
flat through age 13, so there is no multi-week settling window to wait out. Ages 1–2 move in
both directions and should not be treated as final.

Read `scrape_age_days` to tell which read a row came from: `3` is settled, `1` is a date whose
re-scrape has not run yet (yesterday and the day before) or never landed.

The frequently-quoted **~2×** is a different quantity: it is the *raw estimate* vs settled
(`total_cost_usd - total_adjustment_usd` vs `total_cost_usd`, 2.27× on 2026-07-28), not the
error in a stored day+1 total. Keep them distinct — but note that a typical day+1 total is
itself ~1.5× settled, so the two are the same order of magnitude and neither is a reason to
trust an un-re-scraped figure. See `metrics/cost-drift-probe/` under
[Non-schema objects](#non-schema-s3-objects) for the probe that measures all of this, and its
docstring in `kubecost_drift_probe.py` for the full per-date table.

**There is no Kubecost field, on this table or in the live API, that flags "this data point has
been reconciled."** `total_adjustment_usd` and `scrape_age_days` (schema 1.2+, above) are the
closest available signals, captured per-row at scrape time — `subject_costs()` sums and
maxes them respectively (`total_adjustment_usd`, `max_scrape_age_days` in its output), so a
batch-level cost pull can show both the raw figure and how much of it reconciliation has already
touched, without a separate query against `cost-drift-probe/`.

---

## `PodCost` — per pod, per report date

**Not exported by `export_batch_metrics.py`** — query via `CloudpipeMetrics.pod_costs()` or
`CloudpipeMetrics.step_costs()` (both the Athena and DuckDB clients).

Written by the same nightly scraper run as `CostAllocation`, from a second pass over the same
window with `aggregate=pod` instead of `aggregate=label:workflows.argoproj.io/workflow`. This is
the table that answers **"which pipeline component does the money go to"**, which the
workflow-grain table structurally cannot. Constructed via the dataclass
(`src/metrics/schemas.py`) — no drift risk.

S3 key: `metrics/pod-costs/dt={date}/{date}_{workflow_name}_pod_costs.json`

**This key holds many records, unlike every other prefix here.** All of one workflow's pods are
written to a single object as newline-delimited JSON (one compact object per line) rather than
one object per pod — object-per-pod would mean ~10k tiny S3 objects/day at the 300-concurrent
target. Athena's JsonSerDe already parses one object per line and DuckDB's `read_json`
auto-detects it, so only `compactor.read_raw_records` needed to learn about the layout.

| Field | Type | Description |
|---|---|---|
| `date` | str | YYYY-MM-DD report date, same value and meaning as `CostAllocation.date`. Equal to the `dt=` partition value |
| `workflow_name` | str | Argo workflow name — join key to `WorkflowRun` and to `costs` |
| `pod` | str | Argo pod name |
| `step` | str | Pipeline component, from the per-template `cloudpipe.io/step` pod label (e.g. `bold-to-t1w`, `bold-preprocessing`, `long-segmentation`, `t1w-to-mni`). **Empty string, not NULL, when a template sets no such label** — such pods are kept so step-level sums always reconcile to the workflow total. A large `''` group means a template is missing its label, not that cost is unattributable |
| `phase` | str | Coarser grouping from the `cloudpipe.io/phase` pod label (e.g. `functional`) |
| `subject`, `session` | str | From the `subjectid` and `session` pod labels |
| `total_cost_usd` | float | Total cost for this pod over the window |
| `cpu_cost_usd`, `memory_cost_usd`, `gpu_cost_usd`, `pv_cost_usd`, `network_cost_usd` | float | Cost components. `pv_cost_usd` is meaningful here because the master workflow's per-subject `volumeClaimTemplate` distributes PV cost unevenly across steps |
| `total_adjustment_usd` | float | Same meaning as on `CostAllocation`, captured per pod |
| `runtime_minutes` | float | Minutes the pod was allocated within the window |
| `cpu_core_hours`, `ram_gb_hours`, `gpu_hours` | float | Resource consumption. RAM is converted from Kubecost's byte-hours to GB-hours so it is directly comparable to a pod's memory request |
| `cpu_efficiency`, `ram_efficiency` | float | Kubecost's usage/request ratio in [0, 1], averaged over the pod's whole allocation window. Low efficiency on an expensive step *suggests* the cost is reducible by lowering requests rather than by making the code faster — but **do not size a request from this number alone on a step with input artifacts** (see below), and never size a hard memory *limit* from it, because a lifetime average cannot see the peak that OOMs |
| `node`, `node_instance_type` | str | Where the pod landed. Instance type drives the rate, so a step whose cost moves without its resource-hours moving is a placement effect, not a workload change. Both come from node labels Kubecost propagates onto the allocation (`kubernetes.io/hostname`, `node.kubernetes.io/instance-type`) — under `aggregate=pod` the API returns no top-level `node` property. **`node` was empty on every row written before 2026-07-31**; rows older than that cannot answer co-tenancy questions |
| `scrape_age_days` | int | Same meaning as on `CostAllocation` |
| `schema_version` | str | `"1.0"` |

**Grain is one row per pod per report date — one below `costs`.** Summing `total_cost_usd` over
a `(date, workflow_name)` reproduces that workflow's `costs` row; the two passes share
`_resolve_window()` precisely so they cannot drift apart. Note that a subject with eight BOLD
runs contributes eight `bold-to-t1w` pods, so `n_pods` in `step_costs()` is a count of work
units, not of subjects — which is what makes its `mean_cost_usd` the figure that scales when the
batch grows.

**The same reconciliation timing as `CostAllocation` applies to every cost column here** — this
grain is re-scraped in the same settled pass, so rows for a date older than 3 days carry
reconciled values and `scrape_age_days = 3`. Within the first two days, use this table for the
*relative* distribution across components, which the day+1 overstatement affects roughly
uniformly.

**`cpu_efficiency` covers the whole pod lifetime, not just compute (issue #123).** A pod's
scheduling footprint (`max(sum(app containers), max(init containers))`) is reserved for its
*entire lifetime*, and Kubecost averages usage/request across all of it. That window holds
three phases, not two: **artifact staging** (Argo's `init` container), the **main-container
image pull** (after init, before `main` starts), and **compute**. Argo's `init`/`wait`
containers request almost nothing, so throughout the first two the reservation is `main`'s,
sized for compute and held idle.

How much this actually distorts the column depends entirely on how long the step computes,
because staging cost is roughly constant per step. Measured on the 2026-08-03 10-subject batch:

| step | staging | compute | staging % |
|---|---|---|---|
| `long-parcellation` | 29.9 s | 1922 s | 1.5% |
| `template-parcellation` | 27.0 s | 974 s | 2.7% |
| `bold-to-t1w` | 34.9 s | 240 s | 12.7% |
| `hydrate-fastsurfer-template` | 52.6 s | 2.0 s | 96.3% |

So for long-running steps the column is trustworthy, and only steps that stage a lot and
compute briefly are meaningfully diluted. Rank candidates by `idle_core_seconds`
(`cpu_request × staging_seconds`), not by input-artifact count.

> **Do not derive the phase split by subtraction.** `pod duration − compute` also swallows the
> main-container image pull, which is 1–22 s on a warm node but **84–152 s on a cold one** for
> the multi-GB images here. An earlier version of this section reported `bold-to-t1w` as ~50%
> staging from exactly that error, measured on a pinned single-pod probe that provisioned a
> fresh node — and paid a cold pull — on every run. The real figure is 12.7%. Read the
> `initContainerStatuses`/`containerStatuses` timestamps separately, as
> `scripts/collect_pod_phase_split.py` does, and measure on a warm batch rather than a probe.

Note also that several steps project `requests.cpu` into their thread-pool env vars via the
downward API, so a trim is never a pure accounting change — it removes threads.

**`cpu_efficiency` is diluted by artifact staging (issue #123).** Argo stages input artifacts
in the pod's **init** container, and a pod's scheduling footprint
(`max(sum(app containers), max(init containers))`) is reserved for its *entire lifetime*,
init included. Argo's `init`/`wait` containers request almost nothing, so the reservation is
`main`'s — sized for compute, held idle while artifacts download. Kubecost averages over that
whole window, so on any step with input artifacts this column blends two phases that behave
completely differently.

Measured on `bold-to-t1w` (2026-08-03, pinned host, 6-run session): a 7m38s pod was ~3m50s
init + 3m45s compute. The phase split alone predicts 0.49 — and the column read **0.484**,
*while the compute phase was saturating all four cores*. Issue #106 was filed on that number
read as "only ~1.9 of 4 cores are used", which was not what it meant.

So before trimming a request on a step with input artifacts, split the phases with
`scripts/collect_pod_phase_split.py`:

- low **compute-phase** efficiency → the request really is too big; trim it
- saturated compute phase but high **init fraction** → the request is correct, and trimming
  it throttles real work to fix waste that lives in staging; reduce staged bytes instead

Note also that several steps project `requests.cpu` into their thread-pool env vars via the
downward API, so a trim is never a pure accounting change — it removes threads.

Typical query:

```python
m.step_costs(date_from="2026-07-01")[
    ["step", "total_cost_usd", "pct_of_total", "mean_cpu_efficiency"]
]
```

### `run_costs()` — pod cost, EVEN-SPLIT across (subject, session, task, run)

**Not exported by `export_batch_metrics.py`** — query via `CloudpipeMetrics.run_costs()`
directly, same as `pod_costs()`/`step_costs()` and `StepOutcome` itself.

Not a table — a derived query (`CloudpipeMetrics.run_costs()`, both Athena and DuckDB clients)
joining `pod_costs` to `StepOutcome`. It exists because `bold-to-t1w`, `bold-preprocessing`
(func-preproc), and `surface-resample` each run as **one pod per session**, looping over every
BOLD run internally — Kubecost bills at pod granularity, so there is no *measured* cost below
session grain for these three. `run_costs()` divides each such pod's cost evenly across the runs
its `StepOutcome` rows say it processed.

**This is a MODELED split, not a measured one.** Two runs of very different length are charged
equally. Use it to compare subjects/sessions once `step_costs()` has already pointed at one of
the three run-scoped steps as worth investigating — not to rank individual runs' cost.

**Vocabulary mismatch, handled internally:** `pod_costs.step` comes from the `cloudpipe.io/step`
pod label; `StepOutcome.step` is a separate, independently-evolved taxonomy. They agree for
`t1w-to-mni`, `bold-to-t1w`, and `surface-resample`, but the `bold-preprocessing` pod writes
**two** `StepOutcome` rows per run — `func-preproc` (volumetric output) and `surface-sample`
(grayordinate output) — from one execution. `run_costs()` maps `func-preproc` onto the pod-cost
step name `bold-preprocessing` and drops `surface-sample` rows entirely; that derivative's cost
is the same dollars already counted under `bold-preprocessing`, not an additional cost. Returned
`step` values are always the pod-cost vocabulary, never the `StepOutcome` one.

Subject-scoped steps (e.g. `long-segmentation`) never appear — `StepOutcome` records `task`/`run`
as the literal `"na"` for those, which `run_costs()` excludes.

A step retried within one workflow can produce more than one pod for the same
`(workflow_name, step, subject, session)`; their costs are summed before splitting, so a retry's
extra cost folds into the per-run figure rather than duplicating rows.

Inherits `pod_costs`' day+1 reconciliation caveat — the dollar figure being split is provisional
until `scrape_age_days` reaches 3.

---

## `StepOutcome` — per step, per scan unit

**Not exported by `export_batch_metrics.py`** — query via `CloudpipeMetrics` directly.

Written unconditionally by `src/metrics/outcome_recorder.py` after every substantive pipeline
step — the finest-grained record in the system. Constructed via the dataclass
(`src/metrics/schemas.py:220`) — no drift risk.

S3 key: `metrics/step-outcomes/dt={dt}/{workflow_name}__{step}__{subject}__{session}__{task}__{run}_outcome.json`
— absent scan dimensions (e.g. `task`/`run` for a subject-level step) use the literal string `"na"`.

| Field | Type | Description |
|---|---|---|
| `workflow_name` | str | Argo workflow name |
| `step` | str | Canonical step name — one of `anatomical-phase`, `session-phase`, `fastsurfer-template`, `fastsurfer-template-parc`, `fastsurfer-long-seg`, `fastsurfer-long-parc`, `t1w-to-mni`, `bold-to-t1w`, `func-preproc`, `surface-sample` |
| `subject` | str | Subject ID |
| `session`, `task`, `run` | str | Scan-unit identity, or `"na"` if the step is scoped above that level |
| `status` | str | `succeeded` \| `failed` \| `skipped` |
| `failure_category` | str | `infrastructure` \| `algorithm` \| `data` \| `dependency` \| `unknown` \| `""` (see classifier below) |
| `failure_reason` | str | Raw Argo failure message, when available |
| `upstream_failed_step` | str | The step whose failure caused this one to be skipped, if `status="skipped"` |
| `outputs_verified` | list[str] | S3 keys the recorder confirmed actually exist, not just that the step reported success |
| `schema_version` | str | `"1.0"` |
| `recorded_at` | str | ISO 8601 UTC — note this field is named `recorded_at`, not `completed_at`, on this one table |

**Failure taxonomy** (`outcome_recorder.classify_failure`, pattern-matched against the Argo
message, falling back to container exit code since `{{tasks.<name>.message}}` doesn't exist):

| Category | Trigger examples |
|---|---|
| `infrastructure` | `OOMKilled`, exit 137/143, node eviction/cordon, `Unschedulable`, deadline exceeded |
| `data` | `NoSuchKey`/`NoSuchBucket`, missing/corrupt input, `nss_volumes` errors |
| `algorithm` | `AssertionError`/`ValueError`/`RuntimeError`, segfault (incl. exit 139), `CalledProcessError` |
| `dependency` | Upstream DAG task failed or was skipped |
| `unknown` | Failed/skipped with no message and no recognized exit code |

---

## `SubjectManifest` — per workflow, per subject

**Not exported by `export_batch_metrics.py`** — query via `CloudpipeMetrics` directly.

Written by `src/metrics/exit_handler.py` at workflow exit, assembled from every `StepOutcome`
emitted during that run. Constructed via the dataclass (`src/metrics/schemas.py:298`) — no
drift risk.

S3 key: `metrics/subject-manifests/dt={dt}/{workflow_name}__{subject}_manifest.json`

| Field | Type | Description |
|---|---|---|
| `workflow_name`, `subject` | str | Identity |
| `overall_status` | str | `succeeded` \| `partial` \| `failed` — `partial` is the correct, non-error outcome for e.g. a subject with no functional scans in one session; it is not itself a failure signal |
| `steps` | list[`StepSummary`] | One entry per step this workflow ran — see below |
| `outputs_available` | list[str] | Union of `outputs_verified` from every succeeded step |
| `failed_steps`, `skipped_steps` | list[str] | Canonical step names |
| `schema_version` | str | `"1.0"` |

**`StepSummary`** (nested, one per list entry in `steps`): `step`, `session`, `task`, `run`,
`status`, `failure_category`, `failure_reason` — same meaning as the identically-named
`StepOutcome` fields, just folded into the manifest rather than queried separately.

---

## Known drift: dataclass vs. live emitter

Kept as a standalone checklist since it's easy to trust `schemas.py` as authoritative and be
wrong in a way that's hard to notice (the dataclass silently accepts and defaults any field it
doesn't recognize via `from_dict`'s `known` filter — a stale reader doesn't error, it just
drops columns):

1. **`FuncQC`** — the dataclass has no `surf_*`/`subcort_*` fields at all; the live emitter adds
   eleven of them whenever a run also produces grayordinates. A query or export that assumes
   the dataclass's field list is complete will simply never see them.
2. **`RegistrationQC` / `bold_to_t1w`** — dataclass default `schema_version` is `"1.2"`; live
   emitted records are `"2.6"`. 2.4 added `nmi_identity`/`nmi_gain` (the fields the current
   pass/fail gate actually reads), 2.5 added the boundary-sensitive
   `seg_bbr_contrast{,_identity}`/`ngf{,_identity}` family, and 2.6 removed the three `_gain`
   /ventricle-ratio fields Phase A found unusable. Meanwhile `dice`, `mi`, `bbr_cost`,
   `bbr_converged`, `bbr_init_used` are dead — present in the dataclass for backward-compat
   deserialization of old records only.

   The 2.4 episode is the canonical instance of this drift: `nmi_gain` was emitted by
   `bold_to_t1w.py` for weeks while `from_dict()` filtered it out (it filters to
   `__dataclass_fields__`, so an unlisted field is *discarded on read*) and `_UNION_COLUMNS`
   never listed it. The gated metric was being written and silently dropped on ingest. Adding a
   `RegistrationQC` field is therefore a **four-place change**, all four enumerated in the
   dataclass's own docstring: this dataclass, `_UNION_COLUMNS["registration"]`, *both* the
   `registration` and `registration_compacted` `columns` blocks in Terraform, and that
   resource's `projection.schema_version.values` enum.
3. **`RegistrationQC` / `t1w_to_mni`** — live schema is `"2.1"`, matching the dataclass's
   documented 2.1 comments reasonably closely (this one drifted the least).
4. Every other table (`AnatQC`, `WorkflowRun`, `CostAllocation`, `StepOutcome`,
   `SubjectManifest`) is constructed by calling the dataclass constructor directly in its
   writer, so it cannot drift the way the two above can — the dataclass *is* the schema for
   these five.

---

## Non-schema S3 objects

Three more prefixes exist under `s3://cloudpipe-metrics/metrics/` with no dataclass, no entry
in `duckdb_query.py`'s `_PREFIXES`, and (as far as could be confirmed without Glue console
access) no Athena table. `scripts/export_batch_metrics.py` and the `CloudpipeMetrics` query
classes do not read any of these.

- **`metrics/surface-sample/`** — `{subject}_{session}_{task}_{run}_surf_qc.json`, not
  `dt=`-partitioned. The `surf_*`/`subcort_*` fields plus a small provenance envelope (`emit`,
  `stage_timings_s`, `total_runtime_s`, `peak_memory_gb`, `container_peak_memory_gb`). Live
  writer, actively used — **not a deletion candidate** like
  `registration-summary/` was. `preproc.py` has two paths that produce grayordinates: a full
  path that computes volumetric + surface QC together (writes the surface fields to *both*
  here and into that run's `FuncQC` record — genuinely redundant in this case), and a
  grayordinate-only short path used when a run's volumetric output already exists and this
  pass only adds surface data on top (writes *only* here, deliberately leaving `FuncQC`
  untouched rather than overwriting the real volumetric record with a partial one — see the
  comment in `_finish_grayordinate_only`). So this prefix is the durable, always-current
  surface QC record regardless of which path produced it; the copy folded into `FuncQC` is the
  one that's sometimes absent and sometimes stale.
- **`metrics/cost-drift-probe/`** — `{run_ts}.json`, not `dt=`-partitioned. Written by
  `src/metrics/kubecost_drift_probe.py` (invoked from `prefect/flows/cost_scraper.py`). Each
  run snapshots how Kubecost's cost estimate for recent report-dates has moved since the last
  probe: `readings` is a list of `{report_date, n_workflows, total_cost_usd,
  total_adjustment_usd, raw_estimate_usd, age_days, run_ts}` per date in the lookback window.
  This exists specifically to measure the drift noted under `CostAllocation` above, and is kept
  in a separate prefix *on purpose* so its records never enter the `metrics/costs/` glob that
  `subject_costs()` sums — a probe reading must never get counted as a real cost record.
  Because each run re-reads the prior `lookback_days` report-dates, the useful view is
  **longitudinal** (one `report_date` across many snapshot files), not one file on its own;
  that view is what established the age-3 convergence the settled re-scrape is timed on, and
  also caught the partial Kubecost response (`1` workflow / `$0.222` where every other read of
  that date saw `103` / `$42.5`) that `PARTIAL_READ_FRACTION` now guards against. `age_days` is
  day-granular, so two reads of one report-date on the same day are indistinguishable in the
  record even when their totals differ.
- **`metrics/workflow-starts/`** — `{workflow_name}.json`, `dt=`-partitioned by write date.
  Written by `src/metrics/record_workflow_start.py`, a dedicated no-`depends` DAG task that
  starts alongside the real first pipeline step and records nothing but the wall-clock time it
  ran (`{"first_step_started_at": ...}`). `exit_handler.py` reads it back to compute
  `WorkflowRun.pending_duration_s` ([#147]) — a marker file rather than an Argo
  `workflow.outputs.parameters` pass-through, since `argo lint --offline` cannot statically
  resolve the latter from an `onExit` template.

---

## See also

- [`src/metrics/schemas.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/schemas.py) — the dataclasses (authoritative for
  the five tables built via constructor; a useful but sometimes-stale reference for the two
  built as raw dicts — see [Known drift](#known-drift-dataclass-vs-live-emitter))
- [`docs/observability.md`](observability.md) — architecture, Grafana dashboards, querying
  with Python/SQL, the Kubecost scraper, and how to add a new metric
- [`scripts/export_batch_metrics.py`](https://github.com/jrussell9000/cloudpipe/blob/main/scripts/export_batch_metrics.py) — export any batch's
  metrics (including costs) to CSV
