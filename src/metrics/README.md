# CloudPipe Metrics

Unified observability layer for the cloudpipe neuroimaging pipeline.
Collects structured QC metrics from every pipeline step, stores them in S3,
and makes them queryable via Athena SQL or DuckDB.

---

## Architecture

```
preproc.py            fst1w_to_mni.py       bold_to_t1w.py
  → _qc.json            → _t1w_mni_reg_qc     → _bold_t1w_reg_qc
                                                        ↓
FastSurfer container                           Argo exit handler
  → _anat_qc.json                               → _run_summary.json (boto3)
        ↓
        All uploaded via Argo output artifacts
        ↓
s3://cloudpipe-metrics/metrics/      (versioned; NOT the <YOUR_S3_BUCKET> data bucket —
        ↓                             writes to <YOUR_S3_BUCKET>/metrics/* are denied)
        ↓
nightly compaction (prefect/flows/metrics_compactor_flow.py)
  → {table}_compacted  (Parquet, partitioned on dt + schema_version)
        ↓
cloudpipe_metrics Glue database (tables declared in Terraform;
  dt= partitions resolved by partition projection, no crawler)
        ↓
Athena (cloudpipe_metrics_workgroup) — SQL queries
DuckDB — local Python queries (no billing)
Grafana — dashboards via Athena datasource
```

---

## S3 Layout

All metrics live under `s3://cloudpipe-metrics/metrics/` — a dedicated,
versioned bucket, not the `<YOUR_S3_BUCKET>` data bucket. The table name is always the
prefix's last segment with hyphens → underscores.

| Prefix | Table | Grain |
|--------|-------|-------|
| `metrics/func-preproc/` | `func_preproc` | Per BOLD run |
| `metrics/anat-qc/` | `anat_qc` | Per subject × session |
| `metrics/fsqc-qc/` | `fsqc_qc` | Per subject × session (the other half of anatomical QC) |
| `metrics/registration/` | `registration` | Per registration step |
| `metrics/workflow-runs/` | `workflow_runs` | Per Argo workflow |
| `metrics/costs/` | `costs` | Per workflow per report date — **divide by `COUNT(DISTINCT workflow_name)`, never by subject** |
| `metrics/pod-costs/` | `pod_costs` | Per pod per report date (cost by pipeline component) |
| `metrics/step-outcomes/` | `step_outcomes` | Per step (per run where the step is per-run) |
| `metrics/subject-manifests/` | `subject_manifests` | Per subject per workflow |

That is **nine** prefixes — the authoritative list is `local.metric_prefixes` in
`terraform/modules/metrics/main.tf`. Each has a Parquet `{table}_compacted`
counterpart built nightly.

File naming (every prefix is now partitioned by write date under `dt=YYYY-MM-DD/`):
```
metrics/func-preproc/dt={date}/{subj}_{ses}_{task}_{run}_qc.json
metrics/anat-qc/dt={date}/{subj}_{ses}_anat_qc.json
metrics/fsqc-qc/dt={date}/{subj}_{ses}_fsqc_qc.json
metrics/registration/dt={date}/{subj}_{ses}_t1w_to_mni_reg_qc.json
metrics/registration/dt={date}/{subj}_{ses}_{task}_{run}_bold_to_t1w_reg_qc.json
metrics/workflow-runs/dt={date}/{workflow-name}__{subj}_run_summary.json
metrics/subject-manifests/dt={date}/{workflow-name}__{subj}_manifest.json
metrics/costs/dt={date}/{date}_{workflow-name}_cost_allocation.json
metrics/pod-costs/dt={date}/{date}_{workflow-name}_pod_costs.json
metrics/step-outcomes/dt={date}/{workflow-name}__{step}__{subj}__{ses}__{task}__{run}_outcome.json
```

`pod-costs/` is the one prefix where a key holds **many** records: all of a workflow's pods, as
newline-delimited JSON (one compact object per line). Athena and DuckDB both read that natively;
`compactor.read_raw_records` handles it by falling back to line-wise parsing when a whole-body
`json.loads` fails.

---

## Schemas

See [schemas.py](schemas.py) for the full field definitions.

### `FuncQC` — Functional preprocessing QC
Key fields: `n_frames`, `mean_fd`, `pct_fd_above_0p5`, `tsnr_median`, `n_acompcor_wm/csf`, `stage_timings_s`

### `AnatQC` — FastSurfer anatomical QC
Key fields: `etiv_mm3`, `total_brain_vol_mm3`, `lh/rh_mean_thickness_mm`, `lh/rh_surface_area_mm2`

### `FsqcQC` — Deep-MI/fsqc anatomical QC
The other half of `AnatQC`: same T1w image, same subject × session grain, complementary
fields. Key fields: `wm_snr_norm`, `gm_snr_norm`, `con_snr_lh/rh`, `cc_size`, `rot_tal_x/y/z`,
`n_outlier_norms`, and the four `*_status` module exit codes. **Every metric is nullable** —
filter on `IS NOT NULL`, never `> 0` (`0` is a real value for `rot_tal_*` and the outlier
counts), and read the relevant `*_status` alongside a null metric: `0` = ran clean, non-zero =
degraded, `NULL` = never reported. WM/GM SNR lives here rather than in `AnatQC`, which dropped
`snr_wm`/`snr_gm` at schema 1.2; the two are not comparable across that boundary. Use
`anatomical_qc()` to get both tables as one frame.

### `RegistrationQC` — Registration quality
- `registration_type="t1w_to_mni"`: **gates the run** on `lncc`, `jac_det_frac_negative` and `ice_mean_mm` — one bound per failure family (intensity agreement / local folding / global invertibility), fail bounds only, so this type never emits `warn`. The bounds live in `_T1W_MNI_THRESHOLDS` in `images/shared/registration_qc.py` and are retuned as batch distributions accumulate, so they are deliberately not duplicated here. `mask_dice`, `centroid_displacement_mm` and the log-Jacobian distribution (`log_jac_*`) are recorded but ungated — a `pass` does not imply they are good. A gated metric absent from the record (e.g. `ice_*` when the inverse warp could not be saved) is skipped, not failed. A `fail` exits 65 **before** promoting outputs, so no completion marker reaches S3 and a resubmit re-registers the session. Filter on `schema_version` before pooling — `jac_det_*` changed meaning at 2.0 → 2.1 (restricted to brain voxels).
- `registration_type="bold_to_t1w"`: live schema is **2.6**. The **only gated metric is `nmi_gain`**, and the bound is merely `> 0` — i.e. the registration must beat the same session's identity baseline. `_BOLD_T1W_THRESHOLDS` in `images/shared/registration_qc.py` is exactly `{'nmi_gain': {'fail': 0.0, 'direction': 'below', 'inclusive': True}}`. Everything else is recorded-only.
  - **Mind the scale.** `nmi` for a *good* cross-contrast EPI↔T1w registration is ≈ **1.019**, against an identity baseline of ≈ **1.011**. **~1.02 is a good score, not a failure.** Gate calibration (2026-07-30) found no absolute bound supportable — the best candidate reached AUC 0.83 against a 0.95 bar — which is why the gate is relative-to-identity rather than absolute. The earlier schema-2.3 rigid gate (`rigid_rot_deg`/`rigid_disp_max_mm` warn > 15, fail > 20) **has been removed**: the ABCD BOLD is never resampled out of native scanner space, so the BOLD↔T1w offset is field-of-view prescription, routinely **16–97 mm**, and transform magnitude is *not* a quality signal. Do not reintroduce a magnitude gate. This type no longer emits `warn` at all.
  - Recorded-only: `nmi`, `nmi_identity`, `rigid_disp_mean_mm` / `rigid_disp_max_mm` / `rigid_rot_deg`, `mhd_mm` (`-1.0` = could not compute), and the boundary family added at 2.5 — `seg_bbr_contrast{,_identity}` (sentinel **-999.0**; the strongest of seven candidates against a known misregistration, but only *within* session, so normalise before comparing across subjects) and `ngf{,_identity}` (where `0.0` is both the floor and the sentinel).
  - Dead for this type: `dice` (hardcoded `0.0` — EPI↔T1w Dice is not meaningful cross-contrast), `mi` (superseded by `nmi` at 2.1), and `bbr_cost` / `bbr_converged` / `bbr_init_used` (the SynthMorph+BBR two-stage variant was removed in `61ccff7`). Removed at 2.6: `seg_bbr_contrast_gain`, `ngf_gain`, `seg_ventricle_ratio`.
  - Always filter on `schema_version` before pooling.

### `WorkflowRun` — Argo workflow run summary
Key fields: `status`, `started_at`, `finished_at`, `total_duration_s`

### `CostAllocation` — Kubecost cost, per workflow per report date
Key fields: `total_cost_usd`, `cpu_cost_usd`, `memory_cost_usd`, `gpu_cost_usd`

Two things will produce wrong numbers if missed:
- **Grain.** Divide by `COUNT(DISTINCT workflow_name)`, **not** by subject. Dividing a
  batch's cost by a fixed subject list inflated a published figure ~3×.
- **Freshness.** Kubecost's reconciliation *freezes at age 3 days*, and day+1 figures
  overstate settled cost by a **median ~51%**. Use the `--settled` scrape for anything
  quoted. Reference settled figure: **~$0.304 per run**. Component fields are gross
  list price, before any discount/credit adjustment.

### `PodCosts` — same cost data at pod grain
Cost by pipeline component, so per-step attribution is possible. This is the one prefix
whose keys hold **many** records (newline-delimited JSON).

### `StepOutcome` — per-step (and per-run) success/failure
Written mostly **in-pod** now rather than by recorder pods; per-run status comes from
`outputs_verified`, not from the producing task's aggregate status. See
[ADR 016](../../docs/decisions/016-skipped-producer-deadlock-in-dag-recording.md).

### `SubjectManifest` — per subject per workflow
What the workflow found and decided to process (sessions, tasks, runs, skip reasons).

---

## Querying

### Python (Athena — production)

```python
from metrics.athena import CloudpipeMetrics

m = CloudpipeMetrics(bucket="my-cloudpipe-bucket")

# All functional QC for resting-state runs
df = m.func_qc(task="task-rest")

# Flag high-motion runs
bad = df[df["pct_fd_above_0p5"].astype(float) > 20]

# Anatomical QC, both halves on one row (anat_qc volumes/thickness + fsqc SNR),
# joined on subject+session and deduplicated per side. A NULL anat half means
# FastSurfer derivatives were reused, not that the scan is missing metrics.
anat = m.anatomical_qc(dt_from="2026-08-09", dt_to="2026-08-09")

# Registration QC. Note `dice` is DEAD — hardcoded 0.0 for bold_to_t1w and
# superseded by `mask_dice` for t1w_to_mni at schema 2.0. Filtering on it
# silently matches everything.
reg = m.registration_qc(registration_type="t1w_to_mni")
poor = reg[reg["mask_dice"].astype(float) < 0.85]

# bold_to_t1w: the gated metric is nmi_gain, and its bound is just > 0.
# Do NOT filter on `nmi` against an absolute threshold — ~1.02 is a good score.
b2t = m.registration_qc(registration_type="bold_to_t1w")
failed = b2t[b2t["nmi_gain"].astype(float) <= 0]

# Full per-subject view: one row per BOLD run, with the subject's run-of-record
# workflow status/duration/total_cost_usd attached. Those three are
# workflow-grain and repeat on every run row — read one row, don't SUM them.
summary = m.join_subject("sub-NDARABC123")
```

> **Fixed — `compacted=True` used to fail for every table.** Two independent bugs
> were stacked, and the first masked the second because Athena resolves columns
> before it type-checks:
>
> 1. **`registration` only.** `registration_compacted` declares four columns the raw
>    table lacks (`dice`, `bbr_cost`, `bbr_converged`, `bbr_init_used` — all retired
>    with BBR in `61ccff7`). `_query()` applies one `col_list` to both halves of the
>    `UNION ALL`, so the raw half referenced columns that do not exist there:
>    `COLUMN_NOT_FOUND`. Fixed by pinning `_UNION_COLUMNS["registration"]` to the
>    **intersection** of the two declarations. The Terraform lists were deliberately
>    left alone — the compactor infers Parquet columns from the records themselves,
>    so the extra declarations are harmless, and keeping them means historic values
>    stay reachable by querying `registration_compacted` directly.
> 2. **All nine tables.** `dt` is declared `string` everywhere, but `_query()` built
>    `dt = CURRENT_DATE` — varchar = date, which Athena rejects outright with
>    `TYPE_MISMATCH`. So `compacted=True` had never returned a row for *any* table;
>    the registration column bug was just the error Athena happened to report first.
>    Now `dt = date_format(CURRENT_DATE, '%Y-%m-%d')`. Same varchar/date class of bug
>    that broke six Grafana dashboards.
>
> Both are covered by tests, but note what those tests can and cannot do: they assert
> on the *generated SQL string*, because the unit suite never reaches Athena. That is
> why bug 2 survived — the SQL built fine and every existing assertion passed. When
> changing `_query()`, run the generated SQL against Athena once by hand.
>
> **Fixed — `anat_qc_compacted` used to return zero rows.** `AnatQC.schema_version`
> is `"1.2"` but the compacted table's `projection.schema_version.values` enum was
> `"1.0,1.1"`. Because `schema_version` is a *projected partition key* there, an
> out-of-enum value yields **no rows and no error** — unlike an undeclared *column*,
> which reads as `NULL`. The enum now includes `1.2`; requires a Terraform apply to
> take effect on a given deployment.

### Python (DuckDB — local dev, no billing)

```python
from metrics.duckdb_query import CloudpipeMetrics

m = CloudpipeMetrics(bucket="my-cloudpipe-bucket")
df = m.func_qc(task="task-rest")          # same API, reads S3 directly
```

### Athena SQL

See [example_queries.sql](example_queries.sql) for annotated queries covering:
- High-motion run flagging, distribution-by-task, and slowest-run triage
- Brain volume outliers (ICV-normalised)
- Workflow failure rates and currently-failed subjects
- Poor T1w→MNI registrations — filtered on the emitted `verdict`, **not** on
  `dice`/`ncc` (both dead) and not by re-deriving `_T1W_MNI_THRESHOLDS` in SQL,
  since those bounds are retuned as batch distributions accumulate
- Cost attribution
- Joined cross-domain summary

---

## Adding a New Metric

1. Add a dataclass to `schemas.py` following the existing pattern.
2. Write the JSON locally in the pipeline script (no boto3 needed in pipeline images).
3. Add an `archive: none` Argo output artifact pointing to the JSON file, uploading to `metrics/{new-prefix}/`.
4. In `terraform/modules/metrics/main.tf`: add the prefix to `local.metric_prefixes`
   **and** declare an `aws_glue_catalog_table` for it, with every column spelled out.
   There is no crawler — nothing discovers the table or its columns for you.
5. Add example queries to `example_queries.sql`.

Writing a new **column** to an existing schema needs the same treatment: add it to
**both** that table's `columns` block and its `_compacted` counterpart's, and to
`_UNION_COLUMNS` in `athena.py`, in the change that starts emitting it. Athena
returns NULL for a declared column absent from older files, so this is
backward-compatible. Miss the dataclass and it is worse than NULL: `from_dict()`
filters to `__dataclass_fields__`, so an unlisted field is **discarded on read**.

**Bumping `schema_version` is a different and sharper trap.** It is a *projected
partition key* on the compacted tables, so you must also add the new value to that
resource's `projection.schema_version.values` enum. A version outside the enum
returns **zero rows, silently** — not NULL columns. `anat_qc_compacted` shipped that
way for real (emitter `1.2`, enum `"1.0,1.1"`) and nothing surfaced it: the query
status is `ok`, so it reads as "no data for that window" rather than as a fault.

See [ADR 011](../../docs/decisions/011-s3-athena-for-metrics.md) for the full
place-by-place table and what breaks if each is skipped.

Objects must always be written under `dt=YYYY-MM-DD/`. Partition projection is the
only thing publishing partitions, so an object at a prefix root is invisible to both
Athena and DuckDB.

---

## Files

| File | Purpose |
|------|---------|
| `schemas.py` | Dataclasses for all metric types |
| `writer.py` | `emit_to_s3()` helper (used by exit handler + cost scraper) |
| `athena.py` | `CloudpipeMetrics` Athena query class |
| `duckdb_query.py` | Same API over DuckDB (local dev) |
| `example_queries.sql` | Annotated Athena SQL |
| `exit_handler.py` | Standalone script for workflow run summaries |
| `kubecost_scraper.py` | Nightly Kubecost cost allocation scraper |
| `kubecost_drift_probe.py` | Measures Kubecost's day+1-vs-settled overstatement (median ~51%) |
| `compactor.py` | Nightly raw-JSON → Parquet `{table}_compacted` compaction |
| `outcome_recorder.py` | Per-step `StepOutcome` records; `resolve_run_status` derives per-run status from output presence (see ADR 016) |
| `record_workflow_start.py` | Writes the `metrics/workflow-starts/` marker that `pending_duration_s` is computed from |
