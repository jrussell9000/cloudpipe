# CloudPipe Observability

Structured metrics emitted by every pipeline step, stored in S3, queryable via Athena SQL or DuckDB, and visualized in Grafana.

**Why this exists as a first-class layer rather than log scraping.** On ephemeral infrastructure the pod that knew how a step went is gone minutes later, and its node with it. So each step *emits* what it knows — alignment scores, motion summaries, resource peaks, exit status — as a structured record at the moment it still knows it. The result is that "how well did this run register?" and "what did this run cost?" are both SQL queries over a durable corpus, not archaeology across logs. Ten tables, all schemas hand-declared in Terraform ([ADR 011](decisions/011-s3-athena-for-metrics.md)).

Three things about this corpus are load-bearing and non-obvious; skipping them leads to confidently wrong numbers:

- **Cost data keeps moving for days.** AWS reconciliation means a day-old read overstates by a median ~51%. Only *settled* figures are usable, and the cost grain is **per Argo workflow run**, not per subject — dividing by a subject list has produced a 3× overstatement. See [Cost attribution](#kubecost-scraper).
- **QC tables partition on workflow *start* date; `workflow_runs` partitions on *finish*.** A single-day query window therefore returns only one side of any batch that crossed midnight. [how-to-timeframe-metrics-dataframe.md](how-to-timeframe-metrics-dataframe.md) is the recipe that gets this right.
- **A partition-key mismatch returns zero rows with status `ok`.** Because partitions are resolved by *projection* rather than a crawler, a `schema_version` outside the declared enum is not an error — it is silently empty. An undeclared *column*, by contrast, reads as `NULL`. Both failure modes look like "no data" rather than like a bug.

Metric semantics — every field, its units, and its gating threshold — live in [metrics_data_dictionary.md](metrics_data_dictionary.md). For why `bold_to_t1w` NMI sits near 1.02 rather than 2.0, see [nmi-interpretation.md](nmi-interpretation.md).

---

## Architecture

```
preproc.py              fst1w_to_mni.py         bold_to_t1w.py
  → _qc.json              → _t1w_mni_reg_qc.json  → _bold_t1w_reg_qc.json
                                                          │
FastSurfer container                           Argo exit handler (onExit)
  → _anat_qc.json                               → _run_summary.json (boto3)
        │
        All uploaded as Argo output artifacts (archive: none)
        │
s3://cloudpipe-metrics/metrics/
        │
cloudpipe_metrics Glue database — tables hand-declared in Terraform,
  partitions resolved by projection (no crawler, queryable immediately)
        │
Athena workgroup: cloudpipe_metrics_workgroup
DuckDB — same API locally, no billing, no infra
        │
Grafana (https://grafana.<YOUR_DOMAIN>)
  — 8 dashboards: Pipeline Throughput, Functional QC, Anatomical QC, Registration QC,
    Cost Overview, Infrastructure Health, Karpenter Autoscaler, Failure Triage
```

Metrics are written to local disk by the pipeline scripts themselves — no boto3 dependency in the neuroimaging images. Argo uploads the files to S3 as output artifacts using the existing runner service account S3 permissions. The only exceptions are the workflow exit handler and the Kubecost scraper, which use boto3 directly and run in the `python+boto3` image.

---

## S3 layout

All under `s3://cloudpipe-metrics/metrics/` — a dedicated, **versioned** bucket
separate from the `<YOUR_S3_BUCKET>` data bucket. Metrics are the run of record and must
survive the derivative flushes that precede every test batch; versioning also
makes an in-place overwrite recoverable. The Argo controller and runner roles
have put/get but **no delete** on this bucket. See
`terraform/metrics_bucket.tf` (root-level Terraform; not part of the public
repo, which publishes only `terraform/modules/`).

The Glue table name is always the prefix's last segment with hyphens replaced by
underscores. Adding a prefix whose name differs from its table name produces a
duplicate table — how the historical `anat`/`anat_qc` and
`registration`/`registration_qc` pairs arose.

| Prefix | Athena table | Grain | Key format |
|--------|-------------|-------|-----------|
| `metrics/func-preproc/` | `func_preproc` | Per BOLD run | `dt={date}/{subj}_{ses}_{task}_{run}_qc.json` |
| `metrics/surface-sample/` | `surface_sample` | Per BOLD run | `dt={date}/{subj}_{ses}_{task}_{run}_surf_qc.json` |
| `metrics/anat-qc/` | `anat_qc` | Per subject × session | `dt={date}/{subj}_{ses}_anat_qc.json` |
| `metrics/fsqc-qc/` | `fsqc_qc` | Per subject × session | `dt={date}/{subj}_{ses}_fsqc_qc.json` |
| `metrics/registration/` | `registration` | Per registration step | `dt={date}/{subj}_{ses}_t1w_to_mni_reg_qc.json` / `dt={date}/{subj}_{ses}_{task}_{run}_bold_to_t1w_reg_qc.json` |
| `metrics/workflow-runs/` | `workflow_runs` | Per Argo workflow | `dt={date}/{workflow-name}__{subj}_run_summary.json` |
| `metrics/costs/` | `costs` | Per Argo workflow × scrape date | `dt={date}/{date}_{workflow-name}_cost_allocation.json` |
| `metrics/pod-costs/` | `pod_costs` | Per pod × scrape date | `dt={date}/{date}_{workflow-name}_pod_costs.json` |
| `metrics/step-outcomes/` | `step_outcomes` | Per step × scan unit | `dt={date}/{workflow-name}__{step}__{subj}__{ses}__{task}__{run}_outcome.json` |
| `metrics/subject-manifests/` | `subject_manifests` | Per Argo workflow | `dt={date}/{workflow-name}__{subj}_manifest.json` |

Each of the ten prefixes above also has a compacted Parquet counterpart at
`metrics/compacted/{table}/dt={date}/schema_version={version}/part-0000.parquet`,
written by the nightly `metrics-compactor` flow — see
[How compaction works](#how-compaction-works). Raw JSON is retained
indefinitely alongside it; compaction never deletes.

`dt=` is a Hive-style partition folder holding the record's own write date
(`completed_at`/`recorded_at`, or `date` for costs). Athena and Grafana filter
on it directly so a query prunes to the days it actually needs instead of
scanning the whole prefix — see the partitioning note below.

> The `costs` key carries **no subject ID** — the subject is a field inside the record, not part of
> the key. Two consequences: subject-glob deletes never match cost objects (see
> [operations.md](operations.md#reprocessing-subjects-and-flushing-metric-data)), and cost is at
> workflow-day grain, so a per-subject figure requires `SUM(...) GROUP BY subject` — use
> `subject_costs()` rather than `costs()`.

> **Naming convention**: every table is declared explicitly in `terraform/modules/metrics/main.tf`, and by convention its name is the last S3 path segment with hyphens replaced by underscores (`func-preproc/` → `func_preproc`). The convention is historical — it dates from when Glue crawlers derived names this way and a mismatch would produce two tables at one location — but it is worth keeping, because queries, dashboards and `schemas.py` prefixes all read consistently. This is why the anat prefix is `anat-qc/` (not `anat/`): it yields `anat_qc`. Note the Terraform *resource* name need not match the table name — `resource "aws_glue_catalog_table" "registration_qc"` sets `name = "registration"`.

**Hive-style `dt=` date partitioning** (issue #65). At 30k runs the flat layout this replaced was cheap (~60 MB total, ~$0.0003/full scan), but that assumption doesn't hold at the full-ABCD target of ~11,800 subjects (~900k objects across these 9 prefixes) — a full-table scan there is pathological for Athena's per-file overhead. Every table uses [partition projection](https://docs.aws.amazon.com/athena/latest/ug/partition-projection.html) (`terraform/modules/metrics/main.tf`) rather than discovered partitions: Athena computes valid `dt=` locations from a date-range formula, so a newly-written partition is queryable immediately with no `MSCK REPAIR TABLE`. Query and dashboard code should filter on `dt` directly wherever possible — a filter on `completed_at`/`recorded_at` alone gives Athena nothing to prune on, since those are JSON body fields, not the partition column.

Records written before this change are flat (no `dt=` folder) and are invisible to partition-projected queries. That corpus is entirely test-batch data with no retention requirement, so no migration was written — stale flat objects can simply be deleted once the partitioned layout is confirmed working:
```bash
aws s3 rm s3://<metrics-bucket>/metrics/<prefix>/ --recursive --exclude "dt=*"
```
run once per prefix (the `--exclude "dt=*"` keeps every already-partitioned object and only removes the old flat ones).

---

## Pod log archive

Separate from the structured metrics above, Argo archives **every pod's full stdout/stderr to S3**, and those logs outlive the workflow object. This is a distinct data source with different durability properties from `metrics/`, and it is the only place unstructured run detail (progress bars, `nvidia-smi` samples, library warnings) survives.

| | |
|---|---|
| Location | `s3://<YOUR_S3_BUCKET>/logs/{workflow.name}/{pod.name}/main.log` |
| Bucket | The **`<YOUR_S3_BUCKET>` data bucket** — *not* `cloudpipe-metrics` |
| Enabled by | `archiveLogs: true` in both Terraform-managed ConfigMaps: [`modules/argo-workflows/main.tf`](https://github.com/jrussell9000/cloudpipe/blob/main/terraform/modules/argo-workflows/main.tf) keys `artifactRepository` and `cloudpipe-artifacts` |
| Retention | Indefinite — no lifecycle rule (the only `<YOUR_S3_BUCKET>` rule targets `derivatives/`), and `logs/` is absent from `prep_test_batch.py`'s `METRIC_PREFIXES` |
| Scale | 13,445 workflow prefixes as of 2026-07-26, 1,728 of them `cloudpipe-*` |

Because logs are keyed by workflow and pod name — not by subject — a subject-glob delete can never match them, which is why they survive the derivative flushes that precede every test batch. The tradeoff is the inverse of the [metrics bucket rationale](#s3-layout): `<YOUR_S3_BUCKET>` is **unversioned**, so an accidental recursive delete on this prefix is unrecoverable.

### Retrieving GPU VRAM history

**Archived logs are the only source of GPU memory history.** No metric schema records VRAM: `FuncQC.peak_memory_gb` is host RSS on a CPU-only step, and `RegistrationQC` has no memory field. No DCGM exporter is deployed (only `nvidia-device-plugin`), so the in-cluster Prometheus holds no GPU-memory series, and pod logs are not forwarded to CloudWatch. What makes VRAM recoverable retroactively is the `nvidia-smi ... -l 5` monitor baked into each GPU step, whose samples land in this stream.

Find the pods for a given step, then read one:

```bash
# All archived t1w-to-mni pods, newest last
aws s3api list-objects-v2 --bucket <YOUR_S3_BUCKET> --prefix logs/cloudpipe \
  --query 'Contents[?contains(Key, `t1w-to-mni`)].[LastModified,Key]' --output text | sort

aws s3 cp s3://<YOUR_S3_BUCKET>/logs/<workflow>/<pod>/main.log -
```

Three caveats when parsing:

- **`memory.used` is whole-card, not per-process.** Under GPU time-slicing (see `handoffs/gpu-time-slicing.md`, internal repo only) **three** pods share one physical GPU, so any co-tenant's footprint is included. In practice this shows up as a multi-modal split at ~1×, ~2× and ~3× the solo figure, which is what lets you recover the per-pod working set by differencing the populations. Historical records predating the 2-to-3 raise are bimodal only, so read the slice count from the node's `nvidia.com/gpu` capacity at the time rather than assuming it. DCGM would not improve on this — its fields are all device-scoped, so pod-label enrichment attributes the same whole-card value to every slice.
- **The CSV format widened from 4 columns to 6.** `name` and `memory.total` were added to the monitors so each sample is self-describing about headroom (T4 15 GiB vs A10G/L4 ~23 GiB). Logs archived before that change lack both columns, so a parser spanning the corpus must accept either width — a fixed-field regex will silently skip half the data rather than error.
- **Two streams are interleaved, sometimes on one line.** `nvidia-smi` CSV rows and Python `logging` lines share the file, and `tqdm` writes without newlines, so match both with regexes applied anywhere in the line rather than splitting on position.

---

## Schemas

Full field-by-field reference — every table, every column, units, gating thresholds, and where
the live emitted JSON has drifted from the `src/metrics/schemas.py` dataclasses — lives in the
dedicated **[Metrics Data Dictionary](metrics_data_dictionary.md)**. Check it before trusting
any column name in a query or export.

Quick index: `FuncQC` (per BOLD run), `SurfaceSampleQC` (per BOLD run — grayordinate QC; the
durable copy, and the *only* copy for runs preprocessed by the `--emit grayordinate` short path),
`AnatQC` (per subject×session), `RegistrationQC` (per
registration — `t1w_to_mni` and `bold_to_t1w` emit different field sets, query them
separately), `WorkflowRun` (per Argo workflow), `CostAllocation` (per workflow per scrape
date), `StepOutcome` (per step per scan unit), `SubjectManifest` (per workflow per subject).
Two more S3 prefixes (`cost-drift-probe/`, `workflow-starts/`) exist
outside this queryable set — see the dictionary's [Non-schema
objects](metrics_data_dictionary.md#non-schema-s3-objects) section.

---

## Grafana dashboards

Grafana is at **https://grafana.<YOUR_DOMAIN>**, deployed via ArgoCD (`gitops/apps/grafana/`). The six Athena-backed dashboards (Pipeline Throughput, QC × 3, Cost, Failure Triage) query `cloudpipe_metrics_workgroup`; results are cached in `s3://cloudpipe-finops/grafana-query-results/`. Infrastructure Health and Karpenter Autoscaler query the in-cluster Prometheus (`cloudpipe-prometheus` datasource).

| Dashboard | UID | What it shows |
|-----------|-----|--------------|
| Pipeline Throughput | `cloudpipe-throughput` | Total runs, success rate, mean duration, mean queue wait; runs-by-status bargauge; recent workflow run table |
| Functional QC | `cloudpipe-funcqc` | Header: runs QC'd, mean FD, % high-motion runs, median tSNR. Rows: **Head motion** (max FD, % frames > 0.2 mm, mean DVARS; FD + DVARS histograms), **Signal quality** (GCOR, AOR, AQI; tSNR + global-signal histograms), **Processing cost** (runtime, per-run and per-pod peak memory p95; stage-timing + confound-regressor bargauges), **Needs review** (high-motion run table) |
| Anatomical QC | `cloudpipe-anatqc` | Mean eTIV, brain/eTIV % (ICV-normalized), cortical thickness (LH/RH); brain volume + thickness histograms; full session table |
| Registration QC | `cloudpipe-regqc` | Header: anatomicals + BOLD runs registered, and the rejection rate of each gate. Organised gate-first, twice — for each registration step, what the calibrated gate decided, then the recorded-only metrics behind it. Rows: **T1w→MNI gate** (mean LNCC, % failing LNCC, % over the folding budget — the two gated families; LNCC + folding histograms), **T1w→MNI recorded-only** (mask Dice, mean Jacobian, centroid shift, extreme local volume change; Dice + log-Jacobian histograms; per-metric coverage/distribution table covering all 20 declared `t1w_to_mni` columns), **BOLD→T1w gate** (median `nmi_gain`, % failing, absolute NMI beside its identity baseline; NMI-gain histogram; rejections split by task × run), **BOLD→T1w recorded-only** (`seg_bbr_contrast`, `ngf`, `mhd_mm`, `rigid_disp_max_mm`; contrast + NGF histograms; coverage/distribution table over all 12 declared `bold_to_t1w` columns), **Needs review** (per-step rejection tables naming the bound each record crossed; within-session boundary-contrast outlier table as a descriptive triage aid), **Provenance** (collapsed: records by `registration_type` × `schema_version`). A `schema_version` template variable pins one definition, since `jac_*`/`log_jac_*` changed meaning at 2.1 and `seg_*`/`ngf` only exist from 2.5. Colour bands are set from the measured passing distribution; metrics with no calibrated bound are left uncoloured rather than banded. |
| Cost Overview | `cloudpipe-costs` | Header: cost per run, total spend, runs/participants, settled share of spend, reconciliation applied. Rows: **Spend over time** (daily spend stacked by component; cost-per-run trend), **Where the money goes** (cost per run by component, by pipeline phase, and by step — the last two from `pod_costs`), **Right-sizing signal** (step economics table with cost beside CPU/RAM request efficiency and resource-hours; spend by node instance type), **Per-run and per-participant detail** (cost-per-run distribution, spend by run outcome, spend-by-participant table), **Settlement and data quality** (collapsed: per-report-date scrape age, adjustment, and component residual) |
| Failure Triage | `cloudpipe-failure-triage` | Header: participants with incomplete output (from `subject_manifests`, not workflow status), root-cause vs downstream-casualty failure counts, QC rejections. Rows: **Root causes** (failures by step split root/casualty, normalized `failure_reason` families, hourly failures by step), **QC-gate rejections** (joined to `registration.verdict='fail'` — names the gated metric and the bound it crossed), **Hard failures** (crashed/killed/output-missing detail, QC rejections excluded), **Skipped steps** (per participant × session, with any failure in the same session), **Blast radius** (per-participant `subject_manifests` table) |
| Infrastructure Health | `cloudpipe-infra-health` | Argo workflow phases + queue depth/latency; API server latency; Karpenter node provisioning + disruption summary |
| Karpenter Autoscaler | `cloudpipe-karpenter` | Per-nodepool resource usage vs limits; node claim lifecycle latency; disruption counts; interruption messages |

Default time ranges: 7-day (QC × 3, Throughput, Failure Triage), 30-day (Cost — the
monthly view is the point of that dashboard), 3-hour/6-hour (Infra/Karpenter). Athena
dashboards refresh every 5–60 minutes; Prometheus dashboards refresh every 1 minute.
The QC dashboards were dropped from 30 days to 7 because first paint scans every
partition in range and the raw tables are one small S3 object per record — widen the
picker when you actually want the longer window.

The three QC dashboards (Functional, Anatomical, Registration) have **Subject**, **Session**, and **Task** (Functional only) dropdown variables for drill-down to a specific scan. Setting any variable to "All" includes all values.

### Shared query sources on the QC dashboards

Most panels on the three QC dashboards **do not query Athena**. A handful of *source*
panels run the query; every other panel reads that panel's result through Grafana's
built-in `-- Dashboard --` data source and picks its own column out of the shared frame.
This exists because Athena's latency here is dominated by S3 object count, not bytes
(the raw tables are ~700 B per object, tens of thousands of objects per `dt=`), and
because firing 20–30 concurrent queries made each one roughly 7× slower than the same
query run alone.

| Dashboard | Source panels | Athena queries | Was |
|---|---|---|---|
| Functional QC | 12 (scalars), 5 (distributions), 7 (its own table) | 3 | 20 |
| Anatomical QC | 2 + 10 (scalars, one per table), 16 + 17 (distributions), 25/43/50 (tables) | 7 | 27 |
| Registration QC | 1 (scalars, both `registration_type`s in one pass), 14 + 34 (distributions), 26/35/46/50/51/52/60 (tables) | 10 | 33 |

Rules that keep this working — all three have bitten or nearly bitten:

- **A source panel must live in an expanded row.** Grafana does not run queries for
  panels inside a collapsed row, so a source panel there would leave every consumer
  blank until someone expanded it. This is why `registration-qc`'s collapsed
  Provenance panel (60) keeps its own query instead of feeding others.
- **Removing a column from a source query silently blanks its consumers.** Consumers
  select by field name (`options.reduceOptions.fields`, or a `filterFieldsByName`
  transformation on histograms). A name that matches nothing renders as "No data",
  not as an error. Run `scripts/validate_dashboard_queries.py` before deleting a column.
- **Athena preserves the case of column aliases** (`aCompCor_WM` comes back
  `aCompCor_WM`, not lowercased), and the field selectors are case-sensitive regexes.
- Consumers set `withTransforms: false`, so they read the source panel's **raw** query
  result. A source panel is therefore free to transform its own copy for display —
  panel 5 on Functional QC does exactly that.
- Merging per-panel filters into one query is only safe when the semantics survive:
  `AND <col> IS NOT NULL` is redundant under `AVG`/`APPROX_PERCENTILE` (SQL aggregates
  skip NULLs) and was dropped, but `AND <col> > 0` is a *value* filter and became
  `AVG(CASE WHEN <col> > 0 THEN <col> END)`. Do not drop the latter.

Because `gitops/**` is outside the `pull_request.paths` filter in
`.github/workflows/ci.yaml`, **nothing validates these dashboards on a PR** — a
dashboard-only PR reports no checks at all, which reads identically to "CI hasn't
started yet". Run the wiring check by hand instead:

```bash
python scripts/validate_dashboard_queries.py
```

It resolves every consumer's `panelId`, confirms the target actually queries Athena,
checks each selected field name against the source query's column aliases
(case-sensitively), and flags a source panel buried in a collapsed row. It cannot tell
you whether a *query* is right, so also load the real dashboard after a change, and run
any changed `rawSQL` against Athena with the `$__timeFrom()`/`$__timeTo()` macros
substituted before pushing.

---

## Querying with Python

### Athena (production)

```python
from metrics.athena import CloudpipeMetrics

m = CloudpipeMetrics(bucket="cloudpipe-metrics")

# Functional QC for all resting-state runs
df = m.func_qc(task="task-rest")
high_motion = df[df["pct_fd_above_0p5"].astype(float) > 20]

# Registration quality — flag low Dice (t1w_to_mni uses mask_dice, not dice —
# see the field-drift note above)
reg = m.registration_qc(registration_type="t1w_to_mni")
low_dice = reg[reg["mask_dice"].astype(float) < 0.85]

# Folded warp check
folded = reg[reg["jac_det_frac_negative"].astype(float) > 0.01]

# Cross-domain per-subject summary: one row per BOLD run, with the subject's
# run-of-record workflow status/duration/total_cost_usd attached. Those three
# are workflow-grain and repeat on every run row — read one row, don't SUM them.
summary = m.join_subject("sub-NDARABC123")

# Scope to a known write-date window with dt_from/dt_to whenever you have one
# (a specific batch, "the last week"). This filters on the dt= partition
# column itself, which Athena can prune on — unlike a completed_at/recorded_at
# filter, which is a JSON body field and forces a full-prefix scan regardless
# of how narrow the range is.
recent = m.func_qc(task="task-rest", dt_from="2026-07-20", dt_to="2026-07-27")
```

`CloudpipeMetrics` wraps `boto3.client("athena")`, polls until `SUCCEEDED`, and returns a `pandas.DataFrame`. It uses `cloudpipe_metrics_workgroup` automatically. Every query method (`func_qc`, `anat_qc`, `workflow_runs`, `registration_qc`, `costs`) accepts optional `dt_from`/`dt_to` (inclusive, `YYYY-MM-DD`) for this partition-pruning filter; `subject_costs()`'s existing `date_from`/`date_to` already does the equivalent for cost records, since `dt` equals `date` there.

### DuckDB (local dev, no billing)

```python
from metrics.duckdb_query import CloudpipeMetrics   # same API

m = CloudpipeMetrics(bucket="cloudpipe-metrics")
df = m.func_qc(task="task-rest")
recent = m.func_qc(task="task-rest", dt_from="2026-07-20", dt_to="2026-07-27")
```

DuckDB reads every file under the glob on each query rather than pruning by
partition metadata the way Athena does, so `dt_from`/`dt_to` here narrows the
result set with a `WHERE` clause but doesn't reduce how many files get
scanned — it exists mainly for API symmetry with `athena.py`. `read_json` is
called with `hive_partitioning=true` so the `dt=YYYY-MM-DD/` folder segment
is exposed as a real `dt` column rather than just a path component.

DuckDB reads JSON files directly from S3 using the default AWS credential chain — no workgroup, no per-query cost. Use this for iterative development and CI.

### Exporting a batch to CSV

For a one-shot export of everything recorded for a batch — rather than querying
interactively — use `scripts/export_batch_metrics.py`. It wraps the same
`func_qc`/`anat_qc`/`registration_qc`/`workflow_runs`/`costs` calls shown
above, scopes them to a subject list and `dt_from`/`dt_to` window, and writes
one CSV per table (never a single merged file — see `join_subject()`'s
docstring for why joining these grains naively inflates any SUM):

```bash
pixi run python scripts/export_batch_metrics.py \
  --subjects tools/cloudpipe_test_sample_10.csv \
  --dt-from 2026-07-30 --dt-to 2026-07-30 \
  --window-start 2026-07-30T01:45:00Z --window-end 2026-07-30T05:00:00Z \
  --out-dir metrics_exports/2026-07-30_10subj
```

Defaults to the DuckDB backend; pass `--engine athena` for large date ranges
over the full cohort, where partition pruning matters. That distinction is not a
preference — measured 2026-08-11, a 0-row DuckDB query for a two-day window took
**16.8 s** against **23.7 s** for the same query unscoped over the same 3,047
files, because `sample_size=-1` forces schema inference across the whole glob.
Read cost tracks total corpus size, not window size, so DuckDB cannot be the
full-cohort path (projected ~476k files across the six exported grains at 11,876
subjects).

**Span the batch's start day *through its finish day*.** The QC grains take `dt`
from workflow *start*; `workflow_runs` takes it from workflow *finish*. A batch
crossing UTC midnight therefore splits across two `dt=` partitions, and a
single-day window silently returns a fraction of `workflow_runs` — 15 of 200 on
the 2026-08-10 batch. Because both cost CSVs are scoped to the workflow names
`workflow_runs` returned, a short `workflow_runs` truncates the cost totals too.
`grain_asymmetry_warning()` now catches this by comparing each grain's *subject*
coverage rather than its row count, which only ever saw a total split.

**The export does not deduplicate.** A scan re-processed on another day keeps
every earlier record, and a window spanning both days exports both. Run
`scripts/purge_superseded_metrics.py` (see "How compaction works" below) before
any export meant to describe the processed data.

### Athena SQL

See [`src/metrics/example_queries.sql`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/example_queries.sql) for annotated queries covering:

- High-motion run flagging and exclusion lists
- Registration Dice/NCC outlier detection
- Brain volume and cortical thickness distributions
- Workflow failure rates and duration percentiles
- Daily cost per subject with CPU/memory/GPU breakdown
- Joined per-subject QC + cost view

### Building a DataFrame for a date range

The `CloudpipeMetrics` convenience methods filter by **equality only**, so a
"workflows that ran between two dates" query needs a range predicate on
`finished_at` and (for cost/QC enrichment) careful handling of the differing
table grains. See
[how-to-timeframe-metrics-dataframe.md](how-to-timeframe-metrics-dataframe.md)
for the full walkthrough in both backends.

---

## Kubecost scraper

The nightly scraper queries the in-cluster Kubecost Allocation API aggregated by the `subjectid` pod label, then writes one `CostAllocation` JSON per subject. The scraper calls `kubecost-frontend.kubecost.svc.cluster.local:9090/model/allocation` — the frontend is the API proxy in Kubecost 3.x; the aggregator (port 9004) does not serve this path.

**Activation gate**: The `subjectid` label must appear in Kubecost's `labelMappingConfigs` and be set on all cloudpipe pipeline pods (`podMetadata.labels` in the master workflow template). Confirm in the Kubecost UI (Allocations → Group by: `subjectid`) before relying on the scraped data.

**Schedule**: Prefect deployment `kubecost-cost-scraper`, nightly at 02:00 UTC. To run manually:

```bash
PREFECT_API_URL=https://prefect.<YOUR_DOMAIN>/api \
  prefect deployment run kubecost-cost-scraper/kubecost-cost-scraper
```

**Direct CLI**:

```bash
python src/metrics/kubecost_scraper.py --bucket cloudpipe-metrics --region <YOUR_AWS_REGION>
python src/metrics/kubecost_scraper.py --bucket cloudpipe-metrics --settled   # re-read day-3
```

**Settled re-scrape**: the nightly run reads yesterday at day+1, where Kubecost has not finished
reconciling against the AWS CUR and overstates settled cost by a median ~51% (8–240% over 10
measured dates; see `kubecost_drift_probe.py` for the table). Each run therefore also
re-scrapes the report-date from 3 days back (`kubecost_scraper.SETTLED_AGE_DAYS`) and overwrites
that date's records in place at both grains. Age 3 is where reconciliation converges — measured
longitudinally from `metrics/cost-drift-probe/` and flat from there through age 13 — so that
second read is the last one; there is no multi-week settling window. `scrape_age_days` on the
record distinguishes the two reads (`1` = day+1, `3` = settled).

Overwrites are guarded: Kubecost occasionally answers a window with a near-empty allocation set
(one observed read returned 1 workflow and $0.222 for a date with 103 workflows and $42.5), so
both passes raise `PartialReadError` and write nothing when the response holds fewer than
`PARTIAL_READ_FRACTION` (50%) of the workflows already stored for that date. The re-scrape is
best-effort in the flow — if it fails, that date keeps its day+1 records and can be recovered
with `--date <report_date>`.

The `metrics-compactor` deployment's `lookback_days` **must stay above `SETTLED_AGE_DAYS`** (it
is 4) so the re-scraped day gets re-compacted. Otherwise the Parquet copy keeps the day+1
numbers while the raw JSON holds settled ones, and `compacted=True` queries — which read Parquet
for every closed day — would silently disagree with raw ones.

**Direct API access**: The Kubecost Allocation API is externally accessible at `https://kubecost.<YOUR_DOMAIN>` (HTTPS only — port 80 times out). Use `accumulate=true` to collapse hourly buckets into a single row per entity over the full query window:

```bash
# Cost for a batch of workflows over a specific window
curl -sk "https://kubecost.<YOUR_DOMAIN>/model/allocation?window=<START_RFC3339>,<END_RFC3339>&aggregate=label:workflows.argoproj.io/workflow&filterNamespaces=argo-workflows&accumulate=true" \
  | jq '[.data[] | to_entries[] | select(.key != "__idle__" and .key != "__unallocated__") | {workflow: .key, totalCost: .value.totalCost}] | sort_by(.totalCost) | reverse'

# Cost by pipeline phase
curl -sk "https://kubecost.<YOUR_DOMAIN>/model/allocation?window=<START_RFC3339>,<END_RFC3339>&aggregate=label:cloudpipe.io/phase&filterNamespaces=argo-workflows&accumulate=true" \
  | jq '[.data[] | to_entries[] | select(.key != "__idle__" and .key != "__unallocated__") | {phase: .key, totalCost: .value.totalCost}]'
```

**Reference costs**: use **settled** figures only. The most recent settled measurement is **~$0.304 per run** (100-subject batch, post-rightsizing). Day+1 numbers overstate by a median ~51% — the earlier "$23.65 / ~$0.24 per subject" figure from the 2026-05-26 batch was an unsettled read at the wrong grain and should not be requoted. Note the grain: cost records are **per Argo workflow run**, not per subject (divide by `COUNT(DISTINCT workflow_name)`). Anatomical (FastSurfer) still dominates the split, with registration and functional preprocessing each a low-single-digit percentage.

---

## Infrastructure

Managed by `terraform/modules/metrics/`, invoked from `terraform/metrics.tf`.

| Resource | Details |
|----------|---------|
| Glue catalog database | `cloudpipe_metrics` |
| Raw Glue tables (9) | `func_preproc`, `anat_qc`, `fsqc_qc`, `registration`, `workflow_runs`, `step_outcomes`, `subject_manifests`, `costs`, `pod_costs` — each an explicit `aws_glue_catalog_table` resource. Explicit declaration is required so partition projection (a table property) is configured, and so the table exists before the first record is written. **There are no Glue crawlers**; see the note below. |
| Compacted Glue tables (9) | `{table}_compacted` — Parquet-backed, partitioned on `dt` + `schema_version` (both via partition projection). No crawler: columns are hand-set in Terraform. `registration_compacted` and `costs_compacted` need manual updates to their column list / `projection.schema_version.values` whenever `schemas.py` gains a field or a new `schema_version` starts being emitted — see [How compaction works](#how-compaction-works). |
| Athena workgroup | `cloudpipe_metrics_workgroup` — results → `s3://cloudpipe-finops/grafana-query-results/` |
| Grafana Pod Identity | `cloudpipe-grafana` — Athena query + Glue GetTable/GetDatabase + S3 read on `metrics/*` + S3 write on `grafana-query-results/*` |

**Nothing gates data freshness.** Partition projection (see [S3 layout](#s3-layout)) computes valid `dt=` locations from a formula rather than discovering them, so a new `dt=` partition written by a pipeline step is queryable **immediately** — no `MSCK REPAIR TABLE`. This is why Grafana dashboards reflect new pipeline data within their refresh interval (5–60 min for the Athena dashboards), not once a day. Compaction (see [How compaction works](#how-compaction-works)) never touches today's `dt=` partition — only closed, prior days — so this immediacy guarantee for the raw tables is unaffected by compaction ever running.

> **There are no Glue crawlers, and schema evolution is *not* automatic.** Crawlers were removed after they generated 5,227 junk tables. Every table's **column definitions** are now hand-declared in `terraform/modules/metrics/main.tf`. The practical consequence: **adding a field to a dataclass in `src/metrics/schemas.py` does not make a column appear in Athena.** The field will be written into the JSON records and silently ignored by every query until you also declare the column in Terraform and apply. There is no `aws glue start-crawler` to run and no overnight process that will fix it. See [Adding a new metric](#adding-a-new-metric) for the full checklist — a new or changed field is a five-place change (emitter, dataclass, Athena/query union, Terraform raw **and** compacted table, docs).

---

## Adding a new metric

1. Add a `@dataclass` to `src/metrics/schemas.py` following the existing pattern — include `schema_version`, `pipeline`, and `completed_at`.
2. Write the JSON file locally in the pipeline script (no boto3 needed in pipeline images).
3. Add an `archive: none {}` Argo output artifact in the relevant WorkflowTemplate, pointing the S3 key at `metrics/{new-prefix}/dt={{workflow.creationTimestamp.Y}}-{{workflow.creationTimestamp.m}}-{{workflow.creationTimestamp.d}}/` (or, for a schemas.py-driven writer, use the `dt` param on `s3_key()` the same way the existing schemas do).
4. Add an `aws_glue_catalog_table` resource for the prefix in `terraform/modules/metrics/main.tf` — follow the `anat_qc` or `costs` blocks as a template, including the `partition_keys { name = "dt" ... }` block and `parameters = merge(local.partition_projection.<name>, {...})`. Add the matching `{table}_compacted` resource too. **Declaring the columns here is mandatory, not an optimisation** — there is no crawler to discover them, so an undeclared column is invisible to Athena forever. Then apply (prefer `-target` on the metrics module; a bare plan pulls in unrelated IAM/addon drift).
5. Add example queries to `src/metrics/example_queries.sql`.
6. If adding a Grafana dashboard panel, see [Grafana dashboard authoring](#grafana-dashboard-authoring) below.

---

## How compaction works

At full ABCD scale the metrics corpus is ~900k small JSON objects. Even with
`dt=` partition pruning, a 30-day dashboard query still opens tens of
thousands of tiny files, and Athena's per-file open overhead dominates well
below the ~128 MB sweet spot. The nightly `metrics-compactor` Prefect flow
([`prefect/flows/metrics_compactor_flow.py`](https://github.com/jrussell9000/cloudpipe/blob/main/prefect/flows/metrics_compactor_flow.py),
core logic in [`src/metrics/compactor.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/compactor.py))
compacts each closed day's raw JSON into one Parquet file per
`(table, dt, schema_version)` under `metrics/compacted/{table}/`.

**Schedule**: `30 2 * * *` UTC — 30 minutes after `kubecost-cost-scraper`'s
`0 2 * * *` run, so yesterday's `costs` records exist before that day is ever
compacted. The deployment sets `lookback_days: 4`, which is not a general
safety margin: it exists so the day the cost scraper re-scrapes with settled
values (report_date − `SETTLED_AGE_DAYS`, i.e. 3 days back) is re-compacted
the same night. Lower it below 4 and the Parquet copy of that day keeps the
unreconciled day+1 costs forever.

**Never touches today.** The flow's `date` parameter defaults to yesterday
UTC and `lookback_days` never reaches into today — compaction
only ever reads/writes a "closed" `dt=` partition, so it can never race an
in-flight write and the raw tables' "queryable immediately" freshness
contract holds regardless of whether/when compaction has run for that day.

**Per-`schema_version` Parquet, not hand-built nulls.** Records are read as
plain JSON dicts (never round-tripped through the `schemas.py` dataclasses —
historical schema-1.0 `costs` records lack `workflow_name`, a required field
with no default on the current `CostAllocation` dataclass, so that round-trip
would raise) and grouped by their raw `schema_version` field. Each group
becomes exactly one Parquet file containing only the fields that
schema_version's records actually have — e.g. a `registration` schema-2.0
file has `mask_dice`/`lncc`, not the schema-1.2-only `bbr_cost`. The
`{table}_compacted` Glue table declares the **superset** of columns across
every known schema_version for that table; Athena/Parquet match columns by
name, so a column absent from a given file's own schema_version simply reads
as `NULL` — the same mechanism `duckdb_query.py`'s `union_by_name=true`
already relies on for the raw `costs` table's 1.0/1.1 drift. `registration`
is the table with the most versions in flight (1.1, 1.2, then 2.0 through
2.6 — nine values in `projection.schema_version.values`); its
compacted column list must be kept in sync by hand with the field-by-field
version provenance already documented in `RegistrationQC`'s docstring in
`src/metrics/schemas.py`, since there's no crawler to auto-discover new
columns on `*_compacted` tables.

**Idempotent, no delete needed.** The output key
(`metrics/compacted/{table}/dt={date}/schema_version={version}/part-0000.parquet`)
is fully determined by `(table, dt, schema_version)`, with exactly one file
per group — so every re-run overwrites the same key. This is what lets
compaction run under the existing `prefect-worker` IAM policy (Put/Get, no
Delete) with no new grant: raw JSON is retained forever, and a re-compact
never needs to clean up a stale file.

**Orphan `schema_version=` partitions are emptied, not deleted.** Per-key
idempotency is not per-*partition-set* idempotency. If a date's raw records
are re-emitted under a **different** `schema_version` — a `schemas.py` version
bump landing between a date's day+1 write and its age-3 settled re-scrape, i.e.
a routine deploy inside the 3-day window — the old partition has nobody left to
overwrite it, and since the Glue table unions every `schema_version` under a
`dt=`, that date double-counts from then on. This actually happened
([#180](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/180)):
`costs` `dt=2026-07-29` held a `schema_version=1.1` partition ($5.075) beside a
`schema_version=1.2` one ($2.755) for the *same 15 workflows*, and
`costs_compacted` reported `$7.83` against a raw truth of `$2.755` — a 2.8×
overstatement.

`compact_prefix_dt` now closes this by listing the `schema_version=` partitions
that already exist for the `dt` and reconciling them against the versions it
just derived from raw. Raw JSON is authoritative, so a version present in
compacted but absent from raw has genuinely zero rows for that date; its
partition is **overwritten in place with a zero-row Parquet carrying its own
original column set** (read back from the existing file's footer, so the
superset-column Glue table still matches by name and reads it as no rows).
That is a `PutObject`, not a `DeleteObject` — the key survives as a harmless
empty file, so this needs **no new IAM grant** and preserves the "nothing may
delete from the run-of-record bucket" stance in `terraform/metrics_bucket.tf`.

Repair stands down whenever the evidence for it is incomplete, because
inferring absence from a partial read would empty a live partition:

- **Any unreadable raw key** for that `(table, dt)` — `read_raw_records` skips
  malformed/unreadable objects rather than raising, which makes the parsed
  record set an under-estimate. A transient `GetObject` failure that dropped
  every key of one version would otherwise look exactly like that version
  being retired. The Parquet is still written; only the repair pass is skipped,
  with a warning.
- **A `dt` with no raw keys at all.** The metrics corpus is periodically
  flushed per test batch, so "zero raw keys" is a routine state that does *not*
  mean the date has no data. Treating it as authoritative absence would empty
  every compacted partition for that day.

Both cases err toward leaving a stale partition in place: a double-count is
visible in the dashboards and fixable, whereas emptying a live partition
silently destroys a day. Legitimate multi-version dates are unaffected — a
`dt` whose raw data genuinely spans two versions (as `registration` does on 8
dates, `2.1` alongside `2.4`/`2.6`, from a mid-stream bump with zero row-grain
overlap) has both versions present in raw, so neither is an orphan.

**A flush reaches compacted too, because the compactor cannot.** The nightly
run rebuilds only its `lookback_days` window from raw, so a subject flushed by
`prep_test_batch.py` from an older day used to vanish from raw and stay in
`*_compacted` indefinitely
([#382](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/382): 83
failed step outcomes of reprocessed subjects, visible to
`CloudpipeMetrics(compacted=True)` and invisible to every dashboard). The
decision there was that **a flushed attempt no longer counts**: raw stays
authoritative, and the flush filters the same subjects out of every compacted
partition of each table it deletes raw records from — `drop_rows`, a
conditional `PutObject` rewrite that keeps the file's schema, never a delete.
So after any flush, re-running the compactor over any `dt` is a no-op for
those subjects rather than a silent change. Flushed records are not gone:
the bucket is versioned, and they survive as noncurrent versions. What flushes
before the fix left behind is cleared by `scripts/reconcile_compacted_with_raw.py`,
which keeps only the rows of workflows raw still holds — safe to re-run any time
the two are suspected of drifting.

**A re-processed scan keeps its old record; the purge removes it.** The per-scan
QC tables (`registration`, `anat_qc`, `fsqc_qc`, `func_preproc`, `surface_sample`) key a record by
scan and write *date* — `metrics/registration/dt={day}/{subj}_{ses}_t1w_to_mni_reg_qc.json`
— not by workflow. So re-processing a scan on a later day **adds** a record
instead of replacing it (on the same day, it overwrites raw). The census on
2026-09-11 put ~2.5% of every per-scan table in this state: 826 of 31,965
`t1w_to_mni` rows, 4,290 `bold_to_t1w`, 803 `anat_qc`, 932 `fsqc_qc`, 4,113
`func_preproc`. Every `registration-qc` panel and the `athena.py` joins read
only the latest record per scan, so the dashboards are right. But the export
and ad-hoc SQL see every record, and an exported dataset has to describe the
data that was actually processed. `scripts/purge_superseded_metrics.py` keeps
each scan's final record (latest `completed_at` across raw and compacted) and
removes the rest from both stores. It deletes raw first (the bucket must be
versioned; the delete markers are recorded in the audit) and then applies
`drop_rows` to compacted. It **holds** any scan whose final record is not
compacted yet, whose final fail supersedes an earlier pass, or whose derivative
contradicts the final verdict:

```bash
pixi run python scripts/purge_superseded_metrics.py --metrics-bucket cloudpipe-metrics \
  --data-bucket <YOUR_S3_BUCKET> --registration-type t1w_to_mni            # dry run
pixi run python scripts/purge_superseded_metrics.py --metrics-bucket cloudpipe-metrics \
  --data-bucket <YOUR_S3_BUCKET> --registration-type t1w_to_mni --write
```

Only `registration` is wired up. Each other table needs its own scan key and
derivative check before it can be added. **Step outcomes are never purged**:
they are an event log (a failure *happened*), not a measurement.

**Querying compacted + raw together.** `CloudpipeMetrics` methods that have a
compacted counterpart (`func_qc`, `anat_qc`, `fsqc_qc`, `workflow_runs`,
`registration_qc`, `costs`, `pod_costs`) accept `compacted=True`, opt-in and
off by default so existing dashboards are unaffected until explicitly
migrated:

```python
from metrics.athena import CloudpipeMetrics

m = CloudpipeMetrics(bucket="cloudpipe-metrics")
df = m.func_qc(task="task-rest", compacted=True)
```

This runs today's raw JSON unioned with every prior compacted day:

```sql
SELECT <cols> FROM cloudpipe_metrics.func_preproc           WHERE dt = date_format(CURRENT_DATE, '%Y-%m-%d') AND ...
UNION ALL
SELECT <cols> FROM cloudpipe_metrics.func_preproc_compacted WHERE dt < date_format(CURRENT_DATE, '%Y-%m-%d') AND ...
```

`dt` is compared against a **formatted string**, not a bare `CURRENT_DATE`. `dt`
is declared `string` on all ten tables, so `dt = CURRENT_DATE` is
varchar = date and Athena rejects the entire query with `TYPE_MISMATCH`. (`<`
still orders correctly because `YYYY-MM-DD` sorts lexicographically.) The
Grafana panels solve the same mismatch the other way round, with
`CAST(dt AS DATE)`, because there both operands come from Grafana's
`$__timeFrom()` macros.

Never the same `dt` on both sides, so this never double-counts a record. The
column list is explicit on both sides (not `SELECT *`) because the
`*_compacted` table has one fewer column than the raw table —
`schema_version` is a partition key there, not a data column — so a
positional `UNION ALL` needs identical column lists regardless of each
table's own column order; see `_UNION_COLUMNS` in
[`src/metrics/athena.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/athena.py).

> **Fixed: `compacted=True` never worked for any table.** Two stacked bugs, the
> first masking the second because Athena resolves columns before type-checking.
>
> **`registration` only.** Eight of the nine table pairs differ by exactly the one
> `schema_version` column. `registration` is the exception: `registration_compacted`
> declares **four columns the raw `registration` table does not** — `dice`,
> `bbr_cost`, `bbr_converged`, `bbr_init_used`, all retired with BBR (`61ccff7`) and
> still present in the compacted superset for historical schema-1.x Parquet.
> `_UNION_COLUMNS["registration"]` listed all 45, so the raw half of the union
> selected four columns that don't exist: `COLUMN_NOT_FOUND`. It now lists the
> **intersection** (41). The Glue declarations are unchanged on purpose — the
> compactor infers Parquet columns from the records, so the extra four are inert, and
> historical values stay readable via a direct `registration_compacted` query.
>
> **All nine tables.** With that cleared, every table then failed on
> `dt = CURRENT_DATE` (varchar = date) as described above. Fixed by formatting the
> date to a string. Verified against live Athena on 2026-08-10: the
> `registration_qc(registration_type="bold_to_t1w", compacted=True)` union now
> returns rows (`nmi` ≈ 1.019, the documented good value).
>
> The unit tests assert on the **generated SQL string** — they never reach Athena,
> which is exactly why the `dt` bug survived for so long behind a green suite. After
> changing `_query()`, run its SQL against Athena once by hand.

---

## Grafana dashboard authoring

Dashboard JSON files live in `gitops/apps/grafana/dashboards/`. The sidecar container watches the `grafana` namespace for ConfigMaps labelled `grafana_dashboard=1` and hot-reloads them — no pod restart needed after ArgoCD syncs.

### Required fields in every Athena panel target

Every `targets[]` entry must include `connectionArgs`, or the Athena plugin throws *"can't access property 'region', t.connectionArgs is undefined"* and renders No data:

```json
{
  "refId": "A",
  "queryType": "table",
  "format": 1,
  "connectionArgs": { "catalog": "__default", "database": "__default", "region": "__default" },
  "rawSQL": "SELECT ..."
}
```

`format: 1` = Table (returns rows). `format: 0` = Time Series (requires a time column — use only for time-series panels).

### Time macro patterns

`$__timeFrom()` and `$__timeTo()` expand to **bare TIMESTAMP literals** (e.g. `TIMESTAMP '2026-05-19 00:00:00'`). Never wrap them in single quotes — `'$__timeFrom()'` produces broken SQL like `'TIMESTAMP '2026-05-19 ...'`.

| Column type | Correct WHERE clause |
|-------------|---------------------|
| ISO 8601 string (`completed_at`, `started_at`) | `from_iso8601_timestamp(col) BETWEEN $__timeFrom() AND $__timeTo()` |
| Date string (`YYYY-MM-DD`) | `CAST(col AS DATE) BETWEEN DATE($__timeFrom()) AND DATE($__timeTo())` |
| Native TIMESTAMP | `col BETWEEN $__timeFrom() AND $__timeTo()` |

### Template variables

The QC dashboards expose **Subject**, **Session**, and **Task** (Functional QC only) dropdowns. Variable values are populated from distinct values in the underlying Athena table on each dashboard load (Grafana `type: query`, `refresh: 2`).

All panel WHERE clauses use the guard pattern:

```sql
WHERE ('${subject}' = 'All' OR subject = '${subject}')
  AND ('${session}' = 'All' OR session = '${session}')
  AND ...
```

When adding a new panel to a QC dashboard, follow this pattern for any WHERE clause that should respect the dropdowns.

### Cross-dashboard links

All dashboards include a `links` array that renders as buttons at the top of the page. The pattern is:

```json
"links": [
  { "title": "Pipeline Throughput", "type": "link", "url": "/d/cloudpipe-throughput", "targetBlank": false }
]
```

Use the UID-only URL (`/d/<uid>`) — Grafana redirects to the current slug automatically, so links survive dashboard renames.

### Deploying changes

Commit dashboard JSON files, push, then sync the `grafana` ArgoCD app. The sidecar reloads within ~10 seconds — check the Grafana UI to confirm panels render. If a panel still shows stale behaviour after sync, verify the file on disk in the pod:

```bash
kubectl exec -n grafana deployment/grafana -c grafana-sc-dashboard -- \
  cat /tmp/dashboards/<dashboard-file>.json | python3 -m json.tool | grep rawSQL
```

---

## Files

| Path | Purpose |
|------|---------|
| [`src/metrics/schemas.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/schemas.py) | Dataclasses for all 5 metric types |
| [`src/metrics/writer.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/writer.py) | `emit_to_s3()` with retry (boto3) — used by exit handler + cost scraper |
| [`src/metrics/athena.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/athena.py) | `CloudpipeMetrics` Athena query class |
| [`src/metrics/duckdb_query.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/duckdb_query.py) | Same API over DuckDB (local dev) |
| [`src/metrics/example_queries.sql`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/example_queries.sql) | Annotated Athena SQL |
| [`src/metrics/exit_handler.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/exit_handler.py) | Writes `WorkflowRun` on Argo `onExit` |
| [`src/metrics/kubecost_scraper.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/kubecost_scraper.py) | Kubecost Allocation API client |
| [`src/metrics/compactor.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/compactor.py) | Compacts raw JSON into per-`schema_version` Parquet under `metrics/compacted/` |
| [`prefect/flows/metrics_compactor_flow.py`](https://github.com/jrussell9000/cloudpipe/blob/main/prefect/flows/metrics_compactor_flow.py) | Nightly Prefect flow wrapping `compactor.py` |
| [`images/fastsurfer/extract_qc.py`](https://github.com/jrussell9000/cloudpipe/blob/main/images/fastsurfer/extract_qc.py) | FreeSurfer stats parser — writes `AnatQC` |
| [`gitops/apps/grafana/`](https://github.com/jrussell9000/cloudpipe/tree/main/gitops/apps/grafana/) | Grafana Helm chart wrapper + 8 dashboard JSONs (Pipeline Throughput, Functional QC, Anatomical QC, Registration QC, Cost Overview, Failure Triage, Infrastructure Health, Karpenter) |
| [`terraform/modules/metrics/`](https://github.com/jrussell9000/cloudpipe/tree/main/terraform/modules/metrics/) | Glue database + hand-declared catalog tables, Athena workgroup, IAM |
