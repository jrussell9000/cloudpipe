# How to build a pandas DataFrame of workflow metrics for a time frame

This guide walks through, in detail, how to assemble a pandas DataFrame of
metrics for **every Argo workflow that ran between two dates** — the common
"give me last week's runs" or "the June batch" question — and how to enrich
those runs with their functional QC and cost data.

It is written against the actual query layer in
[`src/metrics/`](https://github.com/jrussell9000/cloudpipe/tree/main/src/metrics/); every code path referenced here exists in
[`athena.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/athena.py) or
[`duckdb_query.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/duckdb_query.py). Read
[observability.md](observability.md) first if you have not seen the metrics
architecture before.

---

## 0. TL;DR

```python
from metrics.duckdb_query import CloudpipeMetrics   # local dev, no billing

m = CloudpipeMetrics(bucket="cloudpipe-metrics")

# Every workflow whose record was WRITTEN in the window, as a typed DataFrame.
runs = m.workflow_runs(dt_from="2026-06-29", dt_to="2026-07-06")
```

That is usually all you need. `dt_from`/`dt_to` filter on the **`dt=` partition**
— the UTC date the record was written — which is not quite the same question as
"finished between these instants". When you need exact instant semantics, add a
`finished_at` predicate on top; §2 explains when the difference bites and §4
shows the raw-SQL form.

The rest of this document explains *why* it looks like that — the partitioned
storage model, the partition-vs-timestamp distinction, the Athena vs. DuckDB
trade-off, the string-dtype trap, and the grain hazards when you join cost and
QC onto the run rows.

---

## 1. What "a workflow run" is, and where the timestamp lives

One Argo workflow processes one subject. When it exits, the exit handler
([`exit_handler.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/exit_handler.py)) writes a single
`WorkflowRun` record to
`s3://cloudpipe-metrics/metrics/workflow-runs/dt={YYYY-MM-DD}/{workflow_name}__{subject}_run_summary.json`
(`WorkflowRun.s3_key`, where `dt` defaults to the current UTC date).

The schema ([`schemas.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/schemas.py), `WorkflowRun`) carries
three time-ish fields:

| Field | Meaning | Use for time-frame filtering? |
|-------|---------|-------------------------------|
| `started_at` | When the first step started running | Yes, if you want *start* semantics |
| `finished_at` | When the workflow reached a terminal state | **Yes — the natural "ran during" field** |
| `completed_at` | When the record was serialized (≈ `finished_at`) | Athena's declared sort key; fine too |

All three are **ISO-8601 UTC strings** (`YYYY-MM-DDTHH:MM:SSZ`). Because they
are zero-padded and lexicographically ordered, a plain string comparison
(`finished_at >= '2026-06-29T00:00:00Z'`) is a correct chronological
comparison — no cast to a timestamp type is required for the filter itself.
That property is what makes the range query simple in both backends.

> **Pick `finished_at` unless you have a reason not to.** A workflow that
> started on the 28th but finished on the 29th "belongs" to the 29th for
> throughput and failure-rate reporting. `example_queries.sql` query #5
> (failure rate over time) buckets on `finished_at` for exactly this reason.

---

## 2. Partition date vs. event timestamp — the one thing to get right

Every query method takes an explicit date window:

```python
# src/metrics/duckdb_query.py
def workflow_runs(self, dt_from: str | None = None, dt_to: str | None = None, **filters):
    return self._query("workflow_runs", filters, dt_from, dt_to)
```

`dt_from`/`dt_to` become `dt >= '…' AND dt <= '…'`, and `**filters` still emits
only **equality** predicates (`m.workflow_runs(status="Failed")` works;
`m.workflow_runs(finished_at="2026-06-29")` compiles to
`finished_at = '2026-06-29'` and matches **nothing**, since the stored values are
full timestamps).

Note the two differences from `finished_at` filtering:

- **`dt` is a bare date** (`'2026-06-29'`), not a timestamp, so no `T00:00:00Z`.
- **The window is inclusive on both ends** (`dt <= dt_to`), not half-open. A
  `dt_to="2026-07-06"` *includes* all of the 6th.

**`dt` is the date the record was written, not the instant the workflow
finished.** For nearly every run those agree, but they diverge at the UTC
boundary: a workflow that finishes at `23:59:50Z` on the 5th whose exit handler
writes at `00:00:10Z` lands in `dt=2026-07-06`. So:

| You want | Use |
|----------|-----|
| "the batch I ran last week" (throughput, failure counts, cost scoping) | `dt_from`/`dt_to` alone — partition pruning, cheapest |
| Exact instant semantics, or a sub-day window | `dt_from`/`dt_to` to prune, **plus** a `finished_at` range predicate in SQL (§4) |

Always pass `dt_from`/`dt_to` even when you also filter on `finished_at`: `dt` is
the partition key, so it is what stops the engine reading every day ever written.
Widen the `dt` window by one day on each side of your `finished_at` range so a
midnight-straddling record isn't pruned away before the timestamp filter sees it.

---

## 3. Choosing a backend: Athena vs. DuckDB

Both modules expose the *same class name and method surface*. The difference
is where the SQL runs and what dtypes come back.

| | `metrics.athena` | `metrics.duckdb_query` |
|--|------------------|------------------------|
| Where SQL runs | AWS Athena (Glue tables) | Locally, DuckDB over S3 JSON |
| Cost | Billed per scan | Free |
| Freshness | New **rows** visible immediately (partition projection); a new **column** needs a Terraform change | Reads S3 live — new rows *and* new fields appear immediately |
| Column dtypes | **Every column is a `str`** | Natively typed (float/int/bool) |
| Auth | boto3 Athena client | boto3 creds injected into a DuckDB S3 secret |
| Best for | Grafana, shared dashboards, big scans | Ad-hoc analysis, recent batches, notebooks |

> **Freshness — rows are immediate; columns are not.** There are **no Glue
> crawlers** (they were removed on 2026-07-30 after generating 5,227 junk
> tables). The Glue tables are partitioned on `dt=` using Athena **partition
> projection**, so a partition is queryable the moment the first object lands in
> it — nothing has to run first, and there is no `MSCK REPAIR TABLE` step.
>
> The catch is **columns**. Every column is hand-declared in
> `terraform/modules/metrics/main.tf`. A field added to a dataclass in
> `src/metrics/schemas.py` is written into the JSON but stays **invisible to
> Athena** until a Terraform apply declares it — indefinitely, not overnight.
> DuckDB has no such limitation: `union_by_name=true` picks new fields up from
> the raw JSON on the next query, which makes it the right backend for
> inspecting a field you just started emitting. See
> [observability.md](observability.md#infrastructure).

---

## 4. The recommended path — raw SQL with a range predicate

### 4a. DuckDB (local, typed, freshest)

`CloudpipeMetrics` holds an open DuckDB connection at `m._con`, already
configured with an S3 secret from your boto3 credential chain. You can run any
SQL against it, and `_s3_glob("workflow_runs")` gives you the correct S3 glob.

```python
from metrics.duckdb_query import CloudpipeMetrics

m = CloudpipeMetrics(bucket="cloudpipe-metrics")

glob = m._s3_glob("workflow_runs")
# s3://cloudpipe-metrics/metrics/workflow-runs/dt=*/*.json

runs = m._con.execute(f"""
    SELECT
        workflow_name,
        subject,
        status,
        started_at,
        finished_at,
        total_duration_s,
        failed_step,
        failure_category
    FROM read_json('{glob}', auto_detect=true,
                   union_by_name=true, sample_size=-1,
                   hive_partitioning=true)
    WHERE dt >= '2026-06-28' AND dt <= '2026-07-06'   -- prune partitions first
      AND finished_at >= '2026-06-29T00:00:00Z'
      AND finished_at <  '2026-07-06T00:00:00Z'       -- half-open interval
    ORDER BY finished_at DESC
""").df()
```

Notes that matter here:

- **The glob has a `dt=*/` segment, and `hive_partitioning=true` is required.**
  That flag is what exposes the `dt=YYYY-MM-DD/` folder name as a queryable `dt`
  column; without it the `WHERE dt >= …` predicate fails with "column not
  found". A flat `workflow-runs/*.json` glob (as older revisions of this guide
  showed) now matches **zero** objects — every record lives under a `dt=`
  partition.
- **Both predicates, deliberately.** The `dt` range prunes which files DuckDB
  opens; the `finished_at` range gives exact instant semantics. `dt_from` is one
  day wide of the `finished_at` start so a midnight-straddling record isn't
  pruned before the timestamp filter can judge it (§2).

- **`union_by_name=true, sample_size=-1`** — the `workflow-runs/` prefix has
  schema drift (records were written at `schema_version` 1.0 and 1.1). These
  flags reconcile records that lack newer columns (they become `NULL` instead
  of raising "unknown key"), and `sample_size=-1` inspects every file so a
  late-appearing column isn't missed. This is the same pattern the library's
  own `_query` uses.
- **Half-open interval `[from, to)`** — using `< to` rather than `<= to`
  avoids double-counting a run exactly at midnight when you page through
  consecutive weeks. Pick one convention and keep it.
- **`.df()`** returns a pandas DataFrame with real dtypes:
  `total_duration_s` is an integer, so `runs["total_duration_s"].mean()`
  works with no cast.

### 4b. Athena (production, shared, billed)

Athena runs against the Glue-catalogued `cloudpipe_metrics.workflow_runs`
table. `_run_sql` submits the query, polls to completion, and paginates the
result into a DataFrame:

```python
from metrics.athena import CloudpipeMetrics

m = CloudpipeMetrics(bucket="cloudpipe-metrics")

runs = m._run_sql("""
    SELECT
        workflow_name, subject, status,
        started_at, finished_at, total_duration_s,
        failed_step, failure_category
    FROM cloudpipe_metrics.workflow_runs
    WHERE dt >= '2026-06-28' AND dt <= '2026-07-06'
      AND finished_at >= '2026-06-29T00:00:00Z'
      AND finished_at <  '2026-07-06T00:00:00Z'
    ORDER BY finished_at DESC
""")
```

The `dt` predicate matters more on Athena than on DuckDB: `dt` is a **projected
partition key**, so it is what bounds the bytes scanned, and Athena bills per
byte. Omitting it scans every day ever written.

**The string-dtype trap.** `_run_sql` builds the DataFrame by reading every
cell out of Athena's `VarCharValue`:

```python
values = [d.get("VarCharValue", None) for d in row["Data"]]
```

So **every column comes back as a Python string**, `total_duration_s`
included. Any numeric work needs an explicit cast — this is why README
snippets read `df["pct_fd_above_0p5"].astype(float)`. For example:

```python
runs["total_duration_s"] = runs["total_duration_s"].astype(int)
runs["finished_at"] = pd.to_datetime(runs["finished_at"])
mean_hours = runs["total_duration_s"].mean() / 3600
```

The *filter* stays a string comparison inside SQL (correct, as §1 explained);
only post-fetch arithmetic in pandas needs the cast. DuckDB does not have this
problem — it types the columns for you.

---

## 5. The simpler path — fetch, then filter in pandas

If you would rather not write SQL, let the convenience method prune partitions
and do the exact-instant slice in pandas. Pass `dt_from`/`dt_to` one day wide, so
the engine reads only the relevant partitions and pandas handles the boundary:

```python
import pandas as pd
from metrics.duckdb_query import CloudpipeMetrics

m = CloudpipeMetrics(bucket="cloudpipe-metrics")
runs = m.workflow_runs(dt_from="2026-06-28", dt_to="2026-07-06")   # typed columns

ts = pd.to_datetime(runs["finished_at"])       # tz-aware from the trailing Z
window = (ts >= "2026-06-29") & (ts < "2026-07-06")
runs = runs.loc[window].sort_values("finished_at", ascending=False)
```

Do **not** call `m.workflow_runs()` with no window "and filter later": that reads
every `dt=` partition ever written, which is the one genuinely expensive mistake
available in this API (and on Athena, a billed one).

With the Athena backend the same code works, but remember `finished_at` is a
string there — `pd.to_datetime` handles that fine, so this pattern actually
papers over the dtype trap for the timestamp column specifically. Other
numeric columns still need casting.

---

## 6. Enriching the runs: cost and QC within the window

A bare list of runs is often step one. The three enrichment tables each sit at
a **different grain**, and joining them carelessly silently inflates every
sum. This is the single most important correctness issue in this codebase's
metrics layer, so it gets its own section.

| Table | Grain | Multiplicity per subject |
|-------|-------|--------------------------|
| `workflow_runs` | per workflow | >1 if a subject was reprocessed |
| `func_preproc` | per BOLD run | = the subject's BOLD-run count |
| `costs` | per workflow **× scrape date** | >1 if a run spanned UTC midnight |

Joining these on `subject` alone fans each run row out by the *product* of the
other tables' multiplicities, so any `SUM(total_cost_usd)` or
`SUM(total_duration_s)` over the joined result overstates the true figure.

### 6a. Per-subject cost for the window — use `subject_costs`

The library already solves cost aggregation. `subject_costs()` sums cost to one
row per subject and takes a **scrape-date window** plus a subject list:

```python
# Suppose `runs` (from §4/§5) is your window of workflows.
subjects = runs["subject"].unique().tolist()

# The cost scraper runs at 02:00 UTC, so a workflow's cost usually lands on the
# UTC day AFTER it ran. Widen date_to by a day past the run window accordingly.
costs = m.subject_costs(
    subjects=subjects,
    date_from="2026-06-29",
    date_to="2026-07-07",     # note: run window ended 07-06; +1 day for scrape lag
)
```

Two things this handles that a naive join would not:

- **Batch accumulation.** `metrics/costs/` accumulates across *all* batches
  ever run. Scoping by `subjects` **and** a date window isolates one batch;
  omit them and you mix in unrelated runs.
- **Midnight-spanning runs.** A workflow scraped on two consecutive days has
  two cost records; `subject_costs` sums them (`GROUP BY subject`) so you get
  the full figure, not a truncated one.

> **Check `max_scrape_age_days` before quoting any cost figure.** Kubecost
> reconciles its estimates over ~3 days. A day+1 snapshot (`max_scrape_age_days`
> = 1) overstates the settled cost by a **median ~51%** (range 8%–240%), so a
> cost pulled the morning after a batch is not the cost of that batch. Records at
> age ≥ 3 are settled. `subject_costs` returns `max_scrape_age_days` and
> `total_adjustment_usd` for exactly this check — if the former is 1, either
> re-scrape after three days or say plainly that the number is unsettled.

> **Grain: divide by workflow, not by subject.** For a per-run figure use
> `n_workflows` (`COUNT(DISTINCT workflow_name)`), not the number of subjects you
> passed in. A reprocessed subject has several workflows, and dividing a batch
> total by a fixed subject list has inflated per-run cost ~3× in practice. Cost
> components are gross list price.

### 6b. Per-run QC with the run-of-record attached — `join_subject`

For a run-level view (one row per BOLD run) with the subject's workflow status,
duration, and total cost attached exactly once, `join_subject(subject)` is the
purpose-built query. It sums cost to workflow grain first, reduces
`workflow_runs` to the subject's **most recent** workflow (the "run of
record") via `ROW_NUMBER() ... ORDER BY completed_at DESC`, and only then
joins — so each run row is annotated once, not fanned out.

```python
frames = [m.join_subject(s) for s in subjects]
per_run = pd.concat(frames, ignore_index=True)
```

> **Do not SUM `status`, `total_duration_s`, or `total_cost_usd` across the
> rows `join_subject` returns.** They are workflow-grain values *repeated* on
> every BOLD-run row for that subject — read any single row for the
> subject-level figure. Summing them multiplies by the subject's run count.
> (This exact bug was fixed in the metrics layer — see commit `c187b5f`.)

### 6c. Assembling the window-scoped summary

Putting it together — a window of workflows, enriched with per-subject cost:

```python
runs   = ...                                   # §4a, your date window
subjects = runs["subject"].unique().tolist()
costs  = m.subject_costs(subjects=subjects,
                         date_from="2026-06-29", date_to="2026-07-07")

summary = runs.merge(costs, on="subject", how="left")
# summary: one row per workflow, with that subject's windowed cost columns.
# `total_cost_usd` here is per-subject (already summed) — safe to describe(),
# but do not sum it again across reprocessed-subject rows.
```

---

## 7. Correctness checklist

Before trusting a window DataFrame, confirm:

- [ ] **`dt_from`/`dt_to` passed** (bare dates, inclusive both ends) so the query
      prunes partitions instead of reading every day ever written.
- [ ] **`dt` window widened one day** past each end of a `finished_at` range, so
      a midnight-straddling record isn't pruned before the timestamp filter.
- [ ] **Filtered on `finished_at` (or `started_at`) with a range** if you need
      exact instant semantics — equality on a bare date matches nothing against
      full timestamps.
- [ ] **Half-open interval** `[from, to)` on `finished_at` so week-over-week
      paging doesn't double-count midnight boundaries.
- [ ] **`hive_partitioning=true`** in any hand-written `read_json` — without it
      there is no `dt` column to filter on.
- [ ] **Backend freshness understood** — both are live for *rows* (partition
      projection, no crawlers); a *new column* needs a Terraform apply before
      Athena can see it, while DuckDB sees it immediately.
- [ ] **Athena dtypes cast** — every Athena column is a string; cast before
      arithmetic. DuckDB is already typed.
- [ ] **Cost scoped by subjects + scrape-date window**, with `date_to` pushed
      ~1 day past the run window to catch the 02:00 UTC scrape lag.
- [ ] **No `SUM` over workflow-grain columns** (`total_cost_usd`,
      `total_duration_s`, `status`) after a run/QC join — they repeat per row.

---

## 8. Reference: files touched

| File | Role in this workflow |
|------|-----------------------|
| [`src/metrics/duckdb_query.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/duckdb_query.py) | `CloudpipeMetrics` over DuckDB; `_con`, `_s3_glob`, `subject_costs`, `join_subject` |
| [`src/metrics/athena.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/athena.py) | Same API over Athena; `_run_sql` (string-dtype source), `cost_scope_clause` |
| [`src/metrics/schemas.py`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/schemas.py) | `WorkflowRun` field definitions and timestamp semantics |
| [`src/metrics/example_queries.sql`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/example_queries.sql) | Query #5 (failure rate over time) is the SQL analogue of §4 |
| [docs/observability.md](observability.md) | Architecture, S3 layout, grain notes |
