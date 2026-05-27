# CloudPipe Observability

Structured metrics emitted by every pipeline step, stored in S3, queryable via Athena SQL or DuckDB, and visualized in Grafana.

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
s3://abcd-v7/metrics/
        │
AWS Glue crawlers (daily 01:00 UTC) → cloudpipe_metrics Glue database
        │
Athena workgroup: cloudpipe_metrics_workgroup
DuckDB — same API locally, no billing, no infra
        │
Grafana (https://grafana.<YOUR_DOMAIN>)
  — 4 dashboards: Pipeline Throughput, Functional QC, Anatomical QC, Cost Overview
```

Metrics are written to local disk by the pipeline scripts themselves — no boto3 dependency in the neuroimaging images. Argo uploads the files to S3 as output artifacts using the existing runner service account S3 permissions. The only exceptions are the workflow exit handler and the Kubecost scraper, which use boto3 directly and run in the `python+boto3` image.

---

## S3 layout

All under `s3://abcd-v7/metrics/`:

| Prefix | Athena table | Grain | Key format |
|--------|-------------|-------|-----------|
| `metrics/func-preproc/` | `func_preproc` | Per BOLD run | `{subj}_{ses}_{task}_{run}_qc.json` |
| `metrics/anat-qc/` | `anat_qc` | Per subject × session | `{subj}_{ses}_anat_qc.json` |
| `metrics/registration/` | `registration` | Per registration step | `{subj}_{ses}_t1w_to_mni_reg_qc.json` / `{subj}_{ses}_{task}_{run}_bold_to_t1w_reg_qc.json` |
| `metrics/workflow-runs/` | `workflow_runs` | Per Argo workflow | `{workflow-name}_run_summary.json` |
| `metrics/costs/` | `costs` | Daily per subject | `{date}_{subj}_cost_allocation.json` |

> **Naming convention**: Glue crawlers derive the table name from the last S3 path segment, replacing hyphens with underscores. The prefix name must match the desired table name — e.g. `func-preproc/` → `func_preproc`, `anat-qc/` → `anat_qc`.

No Hive partitioning. At 30k runs × ~2 KB, total storage is ~60 MB and each full-table scan costs ~$0.0003.

---

## Schemas

Full Python dataclasses are in [`tools/metrics/schemas.py`](../tools/metrics/schemas.py). Each schema includes `schema_version`, `pipeline`, and `completed_at` (ISO 8601 UTC).

### `FuncQC` — per BOLD run

Emitted by `preproc.py` at the end of functional preprocessing. Written to `/tmp/{prefix}_qc.json` and uploaded as an Argo output artifact.

| Field | Type | Description |
|-------|------|-------------|
| `subject`, `session`, `task`, `run` | str | Run identity |
| `n_frames` | int | Total frames in BOLD volume |
| `n_nss_frames` | int | Non-steady-state frames dropped |
| `tr_seconds` | float | Repetition time (s) |
| `mean_fd`, `median_fd`, `max_fd` | float | Framewise displacement stats (mm) |
| `n_fd_above_0p2`, `n_fd_above_0p5` | int | Frame counts above FD thresholds |
| `pct_fd_above_0p5` | float | % frames with FD > 0.5 mm — primary motion exclusion criterion |
| `mean_dvars` | float | Mean DVARS (standardized signal variability) |
| `mean_global_signal` | float | Mean global signal |
| `tsnr_median` | float | Median temporal SNR (in-brain mask) |
| `n_acompcor_wm`, `n_acompcor_csf` | int | aCompCor regressors (WM and CSF) |
| `n_tcompcor`, `n_cosines` | int | tCompCor + cosine basis regressor counts |
| `stage_timings_s` | dict | Per-stage wall time: `stc`, `boldref`, `composite_warp`, `4d_warp`, `mask_warp`, `masking`, `confounds` |
| `total_runtime_s` | float | Total preprocessing wall time (s) |
| `peak_memory_gb` | float | Peak RSS memory (GB) |
| `pipeline`, `image_tag` | str | Provenance: pipeline name and image SHA |

### `AnatQC` — per subject × session

Emitted by `extract_qc.py` (inside the FastSurfer image) after parcellation completes. Parses plain-text `stats/aseg.stats` and `stats/{lh,rh}.aparc.stats` — no neuroimaging library dependencies.

| Field | Type | Description |
|-------|------|-------------|
| `subject`, `session` | str | Subject/session identity |
| `etiv_mm3` | float | Estimated total intracranial volume (mm³) |
| `total_brain_vol_mm3` | float | Brain segmentation volume (mm³) |
| `lh_cortex_vol_mm3`, `rh_cortex_vol_mm3` | float | Cortical grey matter volume per hemisphere (mm³) |
| `wm_vol_mm3` | float | Cerebral white matter volume (mm³) |
| `subcort_gm_vol_mm3` | float | Subcortical grey matter volume (mm³) |
| `lh_mean_thickness_mm`, `rh_mean_thickness_mm` | float | Mean cortical thickness per hemisphere (mm); healthy adult ~2.2–2.5 mm |
| `lh_surface_area_mm2`, `rh_surface_area_mm2` | float | Pial surface area per hemisphere (mm²) |

### `RegistrationQC` — per registration step

Emitted by `fst1w_to_mni.py` (FireANTs T1w→MNI, GPU) and `bold_to_t1w.py` (SynthMorph BOLD→T1w). The `registration_type` field distinguishes the two. T1w→MNI records include warp regularity metrics; BOLD→T1w records include only Dice and the run identity.

| Field | Type | Description |
|-------|------|-------------|
| `subject`, `session` | str | Subject/session identity |
| `registration_type` | str | `"t1w_to_mni"` or `"bold_to_t1w"` |
| `dice` | float | Dice coefficient between aligned brain mask and reference mask; `< 0.85` is a quality flag |
| `ncc` | float | Normalized cross-correlation in MNI brain mask (T1w→MNI only) |
| `jac_det_min`, `jac_det_max`, `jac_det_mean`, `jac_det_std` | float | Jacobian determinant distribution of the SyN warp field (T1w→MNI only) |
| `jac_det_frac_negative` | float | Fraction of voxels with Jacobian det < 0 (folded warp); should be `< 0.01` |
| `task`, `run` | str | BOLD run identity (BOLD→T1w only) |

### `WorkflowRun` — per Argo workflow

Written by `tools/metrics/exit_handler.py` on every `onExit` trigger — including failed and errored workflows.

| Field | Type | Description |
|-------|------|-------------|
| `workflow_name` | str | Argo workflow name (e.g. `cloudpipe-abc12`) |
| `subject` | str | Subject ID from workflow parameters |
| `status` | str | `Succeeded`, `Failed`, or `Error` |
| `started_at`, `finished_at` | str | ISO 8601 UTC timestamps |
| `total_duration_s` | int | Wall time from `workflow.duration` |
| `message` | str | Argo failure message (empty on success) |

### `CostAllocation` — daily per subject

Written nightly by `tools/metrics/kubecost_scraper.py`. Requires the `subjectid` pod label to be visible to Kubecost (see [activation gate](#kubecost-scraper) below).

| Field | Type | Description |
|-------|------|-------------|
| `date` | str | YYYY-MM-DD |
| `subject` | str | Subject ID (from the `subjectid` Kubernetes pod label) |
| `total_cost_usd` | float | Total Kubernetes compute cost (USD) |
| `cpu_cost_usd` | float | CPU cost component |
| `memory_cost_usd` | float | Memory cost component |
| `gpu_cost_usd` | float | GPU cost component |

---

## Grafana dashboards

Grafana is at **https://grafana.<YOUR_DOMAIN>**, deployed via ArgoCD (`gitops/apps/grafana/`). All dashboards query `cloudpipe_metrics_workgroup`; results are cached in `s3://cloudpipe-finops/grafana-query-results/`.

| Dashboard | UID | What it shows |
|-----------|-----|--------------|
| Pipeline Throughput | `cloudpipe-throughput` | Submissions/day, success rate %, mean duration, runs-by-status bar chart, recent workflow table |
| Functional QC | `cloudpipe-funcqc` | Mean FD, % high-motion runs, median tSNR, mean runtime; FD and tSNR distributions; high-motion run table |
| Anatomical QC | `cloudpipe-anatqc` | Mean eTIV, mean brain volume, cortical thickness (LH/RH); volume and thickness distributions; full session table |
| Cost Overview | `cloudpipe-costs` | Total cost, mean per subject, subjects processed; daily spend timeseries; cost-by-subject table |

All dashboards use a 30-day default time range and refresh every 1–5 minutes.

---

## Querying with Python

### Athena (production)

```python
from tools.metrics.athena import CloudpipeMetrics

m = CloudpipeMetrics(bucket="abcd-v7")

# Functional QC for all resting-state runs
df = m.func_qc(task="task-rest")
high_motion = df[df["pct_fd_above_0p5"].astype(float) > 20]

# Registration quality — flag low Dice
reg = m.registration_qc(registration_type="t1w_to_mni")
low_dice = reg[reg["dice"].astype(float) < 0.85]

# Folded warp check
folded = reg[reg["jac_det_frac_negative"].astype(float) > 0.01]

# Cross-domain per-subject summary
summary = m.join_subject("sub-NDARABC123")
```

`CloudpipeMetrics` wraps `boto3.client("athena")`, polls until `SUCCEEDED`, and returns a `pandas.DataFrame`. It uses `cloudpipe_metrics_workgroup` automatically.

### DuckDB (local dev, no billing)

```python
from tools.metrics.duckdb_query import CloudpipeMetrics   # same API

m = CloudpipeMetrics(bucket="abcd-v7")
df = m.func_qc(task="task-rest")
```

DuckDB reads JSON files directly from S3 using the default AWS credential chain — no workgroup, no per-query cost. Use this for iterative development and CI.

### Athena SQL

See [`tools/metrics/example_queries.sql`](../tools/metrics/example_queries.sql) for annotated queries covering:

- High-motion run flagging and exclusion lists
- Registration Dice/NCC outlier detection
- Brain volume and cortical thickness distributions
- Workflow failure rates and duration percentiles
- Daily cost per subject with CPU/memory/GPU breakdown
- Joined per-subject QC + cost view

---

## Kubecost scraper

The nightly scraper queries the in-cluster Kubecost Allocation API aggregated by the `subjectid` pod label, then writes one `CostAllocation` JSON per subject.

**Activation gate**: The `subjectid` label must appear in Kubecost's `labelMappingConfigs` and be set on all cloudpipe pipeline pods (`podMetadata.labels` in the master workflow template). Confirm in the Kubecost UI (Allocations → Group by: `subjectid`) before relying on the scraped data.

**Schedule**: Prefect deployment `kubecost-cost-scraper`, nightly at 02:00 UTC. To run manually:

```bash
PREFECT_API_URL=https://prefect.<YOUR_DOMAIN>/api \
  prefect deployment run kubecost-cost-scraper/kubecost-cost-scraper
```

**Direct CLI**:

```bash
python tools/metrics/kubecost_scraper.py --bucket abcd-v7 --region <YOUR_AWS_REGION>
```

**Direct API access**: The Kubecost Allocation API is externally accessible at `https://kubecost.<YOUR_DOMAIN>` (HTTPS only — port 80 times out). Use `accumulate=true` to collapse hourly buckets into a single row per entity over the full query window:

```bash
# Cost for a batch of workflows over a specific window
curl -sk "https://kubecost.<YOUR_DOMAIN>/model/allocation?window=<START_RFC3339>,<END_RFC3339>&aggregate=label:workflows.argoproj.io/workflow&filterNamespaces=argo-workflows&accumulate=true" \
  | jq '[.data[] | to_entries[] | select(.key != "__idle__" and .key != "__unallocated__") | {workflow: .key, totalCost: .value.totalCost}] | sort_by(.totalCost) | reverse'

# Cost by pipeline phase
curl -sk "https://kubecost.<YOUR_DOMAIN>/model/allocation?window=<START_RFC3339>,<END_RFC3339>&aggregate=label:cloudpipe.io/phase&filterNamespaces=argo-workflows&accumulate=true" \
  | jq '[.data[] | to_entries[] | select(.key != "__idle__" and .key != "__unallocated__") | {phase: .key, totalCost: .value.totalCost}]'
```

**Reference costs** (100 subjects, ~11h run, 2026-05-26): $23.65 total (~$0.24/subject). Anatomical (FastSurfer) accounts for 97% of cost; registration 2.3%; functional preprocessing 0.25%.

---

## Infrastructure

Managed by `terraform/modules/metrics/`, invoked from `terraform/metrics.tf`.

| Resource | Details |
|----------|---------|
| Glue catalog database | `cloudpipe_metrics` |
| Glue crawlers (5) | `cloudpipe-metrics-{func-qc,anat-qc,registration-qc,workflow-runs,costs}` — nightly at 01:00 UTC |
| Explicit Glue tables | `anat_qc` and `costs` — defined in Terraform so the table exists before the first record is written. Crawlers update them nightly once data arrives. |
| Athena workgroup | `cloudpipe_metrics_workgroup` — results → `s3://cloudpipe-finops/grafana-query-results/` |
| Glue crawler IAM role | `cloudpipe-metrics-crawler` — S3 read on `abcd-v7/metrics/*` + standard Glue service permissions |
| Grafana Pod Identity | `cloudpipe-grafana` — Athena query + Glue GetTable/GetDatabase + S3 read on `metrics/*` + S3 write on `grafana-query-results/*` |

Glue crawlers run nightly. New JSON files in a prefix are queryable in Athena immediately after the crawler completes. To pick up new data without waiting for the scheduled run:

```bash
aws glue start-crawler --name cloudpipe-metrics-func-qc

# All five at once:
for name in func-qc anat-qc registration-qc workflow-runs costs; do
  aws glue start-crawler --name "cloudpipe-metrics-${name}"
done
```

---

## Adding a new metric

1. Add a `@dataclass` to `tools/metrics/schemas.py` following the existing pattern — include `schema_version`, `pipeline`, and `completed_at`.
2. Write the JSON file locally in the pipeline script (no boto3 needed in pipeline images).
3. Add an `archive: none {}` Argo output artifact in the relevant WorkflowTemplate, pointing the S3 key at `metrics/{new-prefix}/`.
4. Add a new entry to the `crawler_targets` locals map in `terraform/modules/metrics/main.tf` **and** add an explicit `aws_glue_catalog_table` resource for the same prefix — follow the `anat_qc` or `costs` blocks as a template. The explicit table ensures dashboards don't error with `TABLE_NOT_FOUND` before the first record is written. Then apply.
5. Add example queries to `tools/metrics/example_queries.sql`.
6. If adding a Grafana dashboard panel, see [Grafana dashboard authoring](#grafana-dashboard-authoring) below.

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
| [`tools/metrics/schemas.py`](../tools/metrics/schemas.py) | Dataclasses for all 5 metric types |
| [`tools/metrics/writer.py`](../tools/metrics/writer.py) | `emit_to_s3()` with retry (boto3) — used by exit handler + cost scraper |
| [`tools/metrics/athena.py`](../tools/metrics/athena.py) | `CloudpipeMetrics` Athena query class |
| [`tools/metrics/duckdb_query.py`](../tools/metrics/duckdb_query.py) | Same API over DuckDB (local dev) |
| [`tools/metrics/example_queries.sql`](../tools/metrics/example_queries.sql) | Annotated Athena SQL |
| [`tools/metrics/exit_handler.py`](../tools/metrics/exit_handler.py) | Writes `WorkflowRun` on Argo `onExit` |
| [`tools/metrics/kubecost_scraper.py`](../tools/metrics/kubecost_scraper.py) | Kubecost Allocation API client |
| [`images/fastsurfer/extract_qc.py`](../images/fastsurfer/extract_qc.py) | FreeSurfer stats parser — writes `AnatQC` |
| [`gitops/apps/grafana/`](../gitops/apps/grafana/) | Grafana Helm chart wrapper + 4 dashboard JSONs |
| [`terraform/modules/metrics/`](../terraform/modules/metrics/) | Glue database + crawlers, Athena workgroup, IAM |
