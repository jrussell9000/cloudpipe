# 011 — S3 + Glue + Athena for pipeline metrics storage

**Status**: Accepted

## Context

The pipeline generates three categories of per-subject data that have no queryable home: QC metrics from functional preprocessing (framewise displacement, tSNR, confound counts), anatomical morphometry from FastSurfer (brain volume, cortical thickness), and registration quality scores (Dice, NCC, Jacobian regularity). To identify outliers, monitor pipeline health, and attribute costs, these need to be stored in a way that supports SQL-style queries, grouping, and time-range filtering.

Several storage and query backends were considered:

1. **CloudWatch custom metrics** — simple API, integrates with existing AWS observability. However: per-metric-per-datum billing scales poorly (each QC record has ~25 numeric fields × tens of thousands of runs), resolution is limited to 1-second minimum, no JOIN with non-metric dimensions, and Grafana CloudWatch datasource lacks the flexibility of SQL for multi-field aggregations.

2. **InfluxDB / TimescaleDB** — time-series databases built for this use case. However: require always-on pods and persistent volumes, which add ~$50–100/month in compute cost even when no pipeline is running; schema migrations are manual; operationally complex to back up and restore.

3. **OpenTelemetry → Amazon Managed Grafana / OpenSearch** — full observability platform. Overly heavy for a batch pipeline that produces one record per subject per run; OpenSearch persistent storage costs more than the dataset itself.

4. **S3 (JSON files) + AWS Glue crawlers + Athena** — serverless: Glue crawlers are billed per DPU-hour only when they run (~$0.05/crawler/day at daily cadence), Athena is billed per data scanned (~$0.0003 per full-table scan at ~60 MB), Grafana Athena plugin is first-class. Grafana itself is already deployed in the cluster. DuckDB can read the same S3 JSON files locally for development with no additional infrastructure.

## Decision

Store all pipeline metrics as flat JSON files in `s3://abcd-v7/metrics/`, organized by prefix per metric type. Use AWS Glue crawlers (nightly, 01:00 UTC) to build the `cloudpipe_metrics` catalog database. Expose the data via Athena workgroup `cloudpipe_metrics_workgroup` for Grafana dashboards and ad-hoc queries. Provide a DuckDB-backed Python class with the same API surface for local development.

Metric files are written locally by pipeline scripts and uploaded to S3 by the Argo artifact system — no boto3 dependency in the neuroimaging images.

The file-per-record layout (one JSON per run) is intentional: it makes individual records directly inspectable with `aws s3 cp` or `aws s3 ls`, simplifies the write path (no append-to-file atomicity concerns), and keeps the upload mechanism identical to the existing derivative artifact pattern.

## Consequences

- **No always-on compute cost** for the metrics layer: Glue crawlers and Athena are pay-per-use.
- **Schema evolution** is handled by Glue schema merging — new fields added to a JSON record are picked up automatically on the next crawler run without a migration step.
- **Query latency** is higher than a live database (~2–5 seconds for a full-table Athena query vs. milliseconds). This is acceptable for Grafana dashboards that refresh every 1–5 minutes and for ad-hoc analysis that runs infrequently.
- **Data freshness** is bounded by the crawler schedule — new records appear in Athena the morning after they are written to S3, not immediately. Operators who need fresher data can trigger a crawler manually (`aws glue start-crawler --name ...`).
- **No Hive partitioning** initially. At 30k runs the full dataset is ~60 MB, making per-partition overhead not worth the added complexity. Partitioned Parquet compaction can be added later by an Athena CTAS job if query cost becomes non-trivial.
- **DuckDB local queries** read raw S3 JSON and are not affected by the Glue crawler schedule — they always see the latest files. This makes DuckDB useful during active development and for CI checks.
- **Grafana Pod Identity** gives the Grafana service account least-privilege Athena + Glue + S3 read access without storing AWS credentials in the cluster.
- The S3 file layout is append-only by design: there is no mechanism to update or delete an individual record. If a pipeline step is re-run for the same subject/session/run, a new JSON file overwrites the previous one at the same S3 key, which is the correct behavior.
