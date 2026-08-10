# 011 — S3 + Glue + Athena for pipeline metrics storage

**Status**: Accepted, with the Glue-crawler half reversed (see correction)

> **Correction (2026-08-10) — the crawler mechanism described below is gone; the S3+Athena core stands.**
>
> The record below is preserved as written. Four of its claims no longer describe the system:
>
> 1. **Bucket.** Metrics are in `s3://cloudpipe-metrics/metrics/`, not `s3://<YOUR_S3_BUCKET>/metrics/`. The metrics bucket is versioned and deliberately separate, so QC records survive the derivative flushes that precede each test batch. Writes to `<YOUR_S3_BUCKET>/metrics/*` are now actively **denied** by bucket policy (`terraform/abcd_v7_metrics_retire.tf`).
> 2. **No crawlers.** The nightly Glue crawlers were **removed on 2026-07-30** after generating 5,227 junk tables. Every table and every column is now hand-declared in `terraform/modules/metrics/main.tf`.
> 3. **Schema evolution is manual, and this is the trap.** The "picked up automatically on the next crawler run" consequence below is now **inverted**. Adding a field to a `schemas.py` dataclass writes it into the JSON and leaves it *silently unqueryable* — Athena never sees a column nobody declared. Every place that must change is listed below; miss one and the data is written but invisible.
>
>    | Place | What breaks if skipped |
>    |---|---|
>    | the emitter (e.g. `registration_qc.py`, `preproc.py`) | the field is never written at all |
>    | the dataclass in `src/metrics/schemas.py` | `from_dict()` filters to `__dataclass_fields__`, so the field is **discarded on read** |
>    | `src/metrics/athena.py::_UNION_COLUMNS[<table>]` | the raw/compacted `UNION ALL` doesn't select it |
>    | the raw table's `columns` block in `terraform/modules/metrics/main.tf` | no crawler discovers it; the column never appears |
>    | the `_compacted` table's `columns` block, same file | queries work on raw and return `NULL` on compacted |
>    | this doc set (`docs/metrics_data_dictionary.md`) | the field exists but nobody knows what it means |
>
>    A **`schema_version` bump** adds a seventh, and it fails differently and worse: `projection.schema_version.values` on the compacted table is a *projected partition key* enum, so a version outside it returns **zero rows, silently** — not `NULL` columns. An undeclared *column* degrades to `NULL` via name-based Parquet matching; an undeclared *schema_version* makes the whole partition invisible.
>
>    Note that `RegistrationQC`'s own docstring in `schemas.py` enumerates **four** of these — it counts only the code/infra places under a developer's nose, excluding the emitter and the docs, and folds both Terraform `columns` blocks into one item. That list and this one do not disagree; they are scoped differently.
> 4. **Freshness is immediate, not next-morning.** Every table uses Athena partition projection over `dt=`, so a just-written partition is queryable with no crawl and no `MSCK REPAIR TABLE`. The "no Hive partitioning initially" consequence was also resolved: `dt=` partitioning and Parquet `{table}_compacted` tables both shipped (issue #65).
>
> The decision's actual core — flat JSON per record on S3, catalogued in Glue, queried by Athena, mirrored by DuckDB locally — was correct and is unchanged. What failed was specifically crawler-based schema discovery. See [observability.md](../observability.md) for the current mechanism.

## Context

The pipeline generates three categories of per-subject data that have no queryable home: QC metrics from functional preprocessing (framewise displacement, tSNR, confound counts), anatomical morphometry from FastSurfer (brain volume, cortical thickness), and registration quality scores (Dice, NCC, Jacobian regularity). To identify outliers, monitor pipeline health, and attribute costs, these need to be stored in a way that supports SQL-style queries, grouping, and time-range filtering.

Several storage and query backends were considered:

1. **CloudWatch custom metrics** — simple API, integrates with existing AWS observability. However: per-metric-per-datum billing scales poorly (each QC record has ~25 numeric fields × tens of thousands of runs), resolution is limited to 1-second minimum, no JOIN with non-metric dimensions, and Grafana CloudWatch datasource lacks the flexibility of SQL for multi-field aggregations.

2. **InfluxDB / TimescaleDB** — time-series databases built for this use case. However: require always-on pods and persistent volumes, which add ~$50–100/month in compute cost even when no pipeline is running; schema migrations are manual; operationally complex to back up and restore.

3. **OpenTelemetry → Amazon Managed Grafana / OpenSearch** — full observability platform. Overly heavy for a batch pipeline that produces one record per subject per run; OpenSearch persistent storage costs more than the dataset itself.

4. **S3 (JSON files) + AWS Glue crawlers + Athena** — serverless: Glue crawlers are billed per DPU-hour only when they run (~$0.05/crawler/day at daily cadence), Athena is billed per data scanned (~$0.0003 per full-table scan at ~60 MB), Grafana Athena plugin is first-class. Grafana itself is already deployed in the cluster. DuckDB can read the same S3 JSON files locally for development with no additional infrastructure.

## Decision

Store all pipeline metrics as flat JSON files in `s3://<YOUR_S3_BUCKET>/metrics/`, organized by prefix per metric type. Use AWS Glue crawlers (nightly, 01:00 UTC) to build the `cloudpipe_metrics` catalog database. Expose the data via Athena workgroup `cloudpipe_metrics_workgroup` for Grafana dashboards and ad-hoc queries. Provide a DuckDB-backed Python class with the same API surface for local development.

Metric files are written locally by pipeline scripts and uploaded to S3 by the Argo artifact system — no boto3 dependency in the neuroimaging images.

The file-per-record layout (one JSON per run) is intentional: it makes individual records directly inspectable with `aws s3 cp` or `aws s3 ls`, simplifies the write path (no append-to-file atomicity concerns), and keeps the upload mechanism identical to the existing derivative artifact pattern.

## Consequences

- **No always-on compute cost** for the metrics layer: Athena is pay-per-scan and Glue is charged only for catalog storage now that the crawlers are gone (the ~$0.05/crawler/day line in the Context section no longer applies).
- ~~**Schema evolution** is handled by Glue schema merging — new fields added to a JSON record are picked up automatically on the next crawler run without a migration step.~~ **Reversed — see correction item 3. This is now the single most important thing to get right about the metrics layer.**
- **Query latency** is higher than a live database (~2–5 seconds for a full-table Athena query vs. milliseconds). This is acceptable for Grafana dashboards that refresh every 1–5 minutes and for ad-hoc analysis that runs infrequently.
- ~~**Data freshness** is bounded by the crawler schedule — new records appear in Athena the morning after they are written to S3, not immediately. Operators who need fresher data can trigger a crawler manually (`aws glue start-crawler --name ...`).~~ **Reversed — see correction item 4.** Freshness is immediate under partition projection; there is no crawler to trigger.
- ~~**No Hive partitioning** initially. At 30k runs the full dataset is ~60 MB, making per-partition overhead not worth the added complexity. Partitioned Parquet compaction can be added later by an Athena CTAS job if query cost becomes non-trivial.~~ **Resolved (issue #65).** Both halves shipped: `dt=` Hive partitioning on every prefix, and nightly Parquet `{table}_compacted` tables. The compaction is a Prefect flow (`prefect/flows/metrics_compactor_flow.py`), not an Athena CTAS job as anticipated here.
- **DuckDB local queries** (`src/metrics/duckdb_query.py`) read raw S3 JSON directly, bypassing Glue entirely. This is now doubly valuable: DuckDB infers columns from the JSON itself, so it is the one query path that *can* see a field whose Terraform column declaration was forgotten. If a field looks present in DuckDB and absent in Athena, correction item 3 is the reason.
- **Grafana Pod Identity** gives the Grafana service account least-privilege Athena + Glue + S3 read access without storing AWS credentials in the cluster.
- The S3 file layout is append-only by design: there is no mechanism to update or delete an individual record. If a pipeline step is re-run for the same subject/session/run, a new JSON file overwrites the previous one at the same S3 key, which is the correct behavior.
