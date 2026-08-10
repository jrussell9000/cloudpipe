# Cost Reduction Handoff — cloudpipe_minproc test batch (2026-07-17)

**Purpose:** starting point for a cost-reduction investigation. Everything below was measured
against real data on 2026-07-18; nothing here is estimated or extrapolated unless labelled as a
hypothesis.

---

## 1. Headline numbers

100-subject `cloudpipe_minproc` test batch, scrape date `2026-07-17`:

| Metric | Value |
|---|---|
| Batch total | **$39.62** |
| Mean / subject | **$0.3962** |
| Median / subject | **$0.3039** |
| Std dev | $0.2884 |
| Min / max | $0.00006 / $1.3274 |
| 25th / 75th pct | $0.1940 / $0.5832 |

Component means per subject:

| Component | Mean | Share |
|---|---|---|
| GPU | $0.1964 | ~50% |
| CPU | $0.1954 | ~49% |
| Memory | $0.0409 | ~10% |

(Components sum above `total_cost_usd`; Kubecost's `totalCost` is not a plain sum of these
columns. Treat shares as indicative, not exact.)

### Against the documented baseline

| Source | Baseline | This batch |
|---|---|---|
| `docs/operations.md` | ~$0.27/subject, ~$27/100 | $0.396/subject, $39.62/100 |
| `docs/observability.md` | $0.24/subject ($23.65, 2026-05-26) | — |

**This batch is ~47% over the operations.md baseline** and trips that doc's own pass/fail
criterion: *"Mean >> $0.35 → investigate outliers."*

> ⚠️ **The two docs disagree on where the money goes** and both conflict with the measurement:
> `operations.md` says FastSurfer + registration ~83% / func-preproc ~17%; `observability.md` says
> anatomical 97% / registration 2.3% / func-preproc 0.25%. Measured GPU share here is ~50%.
> Do not trust either doc's component split — re-derive it before optimizing against it.

---

## 2. Distribution shape — this is the important part

Mean ($0.396) sits well above median ($0.304): **right-skewed, driven by a tail, not a uniform
shift.** Top 5 subjects:

| Subject | Total | GPU | n_workflows |
|---|---|---|---|
| sub-BKN88GVE | $1.3274 | $0.3880 | 1 |
| sub-FA0DV854 | $1.1587 | $0.4459 | 1 |
| sub-T1GJUT9Z | $1.0859 | $0.3666 | 1 |
| sub-LW127JFT | $1.0842 | $0.4239 | 1 |
| sub-H37LMD3K | $0.9897 | $0.3810 | 1 |

All are **>3× the mean** and all have `n_workflows: 1` — **this is not retry overhead.** These are
genuinely expensive single runs. Per `CLAUDE.md`, runtime variance is driven by BOLD run count and
length per subject, so the leading hypothesis is that these subjects have more sessions/runs.

**Unverified but high-value:** normalize cost by session count and by BOLD run count. If cost per
*run* is flat, the tail is just data volume and the per-subject mean is the wrong planning unit —
budget on runs, not subjects.

The `min` of **$0.00006** is equally informative in the other direction: some subjects cost
essentially nothing, which almost certainly means failed or skipped workflows. Those drag the mean
*down*, so **the true cost of a fully successful subject is higher than $0.396.**

---

## 3. How to reproduce these numbers

Requires VPN + `AWS_PROFILE=<YOUR_NETID>` (the `[default]` profile has no SSO config and cannot
authenticate).

```bash
AWS_PROFILE=<YOUR_NETID> pixi run python - <<'PY'
import csv, sys
from pathlib import Path
sys.path.insert(0, "src")
import pandas as pd
from metrics.duckdb_query import CloudpipeMetrics

with open("tools/cloudpipe_test_sample.csv", newline="") as fh:
    batch = sorted({r["subject_id"].strip() for r in csv.DictReader(fh)})

m = CloudpipeMetrics(bucket="<YOUR_S3_BUCKET>")
df = m.subject_costs(subjects=batch, date_from="2026-07-17", date_to="2026-07-17")
print(df[["total_cost_usd","cpu_cost_usd","memory_cost_usd","gpu_cost_usd"]].describe())
print(df.nlargest(5, "total_cost_usd").to_string(index=False))
PY
```

`subject_costs()` requires PR #23 (`fix/cost-flush-and-batch-scoping`). If unmerged, work from that
branch. It depends functionally on PR #21 — without #21's credential fix, DuckDB cannot read S3
at all (403).

### Scoping is mandatory, not optional

`metrics/costs/` accumulates across every batch ever run and is **not** subject-keyed
(`{workflow-name}_cost_allocation.json`). The same 100 subjects have been processed four times:

| scrape date | rows | subjects | cost |
|---|---|---|---|
| 2026-07-02 | 138 | 100 | $13.56 |
| 2026-07-05 | 149 | 100 | $36.75 |
| 2026-07-06 | 151 | 100 | $16.45 |
| 2026-07-16 | 5 | 2 | $0.19 |
| **2026-07-17** | **100** | **100** | **$39.62** |

Two ways to get this wrong, both encountered:
- Unscoped per-subject sum → **$1.066/subject** (silently sums all four runs).
- Mean over raw cost rows → **$0.196/subject** (workflow-day grain dilutes it).

Always scope by subject list **and** date window.

**Note the trend:** 07-02 $13.56 → 07-05 $36.75 → 07-06 $16.45 → 07-17 $39.62. Cost per run of the
same cohort is volatile. Worth understanding *why* before optimizing — it may indicate spot pricing
variance, node-provisioning waste, or differing completion rates rather than pipeline cost itself.

---

## 4. Supporting batch metrics (same batch, may help correlate)

| Table | n | Notable |
|---|---|---|
| `func_qc` | 1406 BOLD runs | `total_runtime_s` mean 71 s; **`peak_memory_gb` mean 2.61, max 2.99** |
| `anat_qc` | 271 subject×session | — |
| `registration` (t1w_to_mni) | 269 | `mask_dice` mean 0.982 — healthy |
| `registration` (bold_to_t1w) | 1467 | QC fields all 0.0 — see caveats |
| `workflow_runs` | 151 for 100 subjects | `total_duration_s` mean 20,538 s (5.7 h), max 35,487 s (9.9 h) |

**Strong lead — possible memory over-provisioning:** `func-preproc` runs on `cpu-heavy-nodepool`
(32 GB / 8 CPU per `CLAUDE.md`), but measured `peak_memory_gb` never exceeds **2.99 GB** across all
1406 runs. If pod memory *requests* are sized near the node spec rather than near actual usage,
that is a large amount of paid-for-but-unused memory, and it also reduces bin-packing density.
**Verify the actual pod `resources.requests` in the WorkflowTemplate before acting** — the 32G/8cpu
figure is the nodepool spec, not necessarily the pod request.

Also note **151 workflow runs for 100 subjects** — ~50% more runs than subjects, indicating
retries/resubmissions somewhere in the batch's history. Each failed-then-retried run is spend with
no output.

---

## 5. Caveats that limit cost analysis right now

These are known-broken and will distort any analysis that leans on them:

1. **`pending_duration_s` is 0.0 for every workflow run.** This field is supposed to measure queue +
   node-provisioning wait. It is not being recorded, so **you currently cannot measure provisioning
   waste** — likely a meaningful cost component given Karpenter spin-up and GPU spot scarcity.
   Fixing this is probably a prerequisite for serious cost work.
2. **`bold_to_t1w` QC metrics are all 0.0** (`dice`, `ncc`, all `jac_det_*`; `bbr_cost` null). These
   fields are hardcoded literals in `images/freesurfer/bold_to_t1w.py:307-327`, not a computation
   failure. You cannot correlate cost with BOLD registration quality. See PR #22.
3. **The deployed `images/freesurfer` image is ~3 weeks stale** (predates `c8a14ef`, 2026-06-29).
   This batch ran on old code, so its cost profile may not reflect current `main`.
4. **T1w→MNI metrics are invisible to Athena/Grafana.** `terraform/modules/metrics/main.tf:363-460`
   declares `registration_qc` with a `dice` column, but the script emits `mask_dice`/`lncc`/
   `verdict`. Grafana panels read null. DuckDB reads raw JSON and is unaffected — **use DuckDB, not
   the dashboards, for anything quantitative** until this is fixed.
5. **Component columns don't sum to `total_cost_usd`** — see §1.

---

## 6. Suggested lines of attack

Roughly in expected-value order. None of these have been investigated.

1. **Fix `pending_duration_s`**, then quantify node-provisioning and queue waste. Without it you are
   optimizing blind.
2. **Normalize cost by session and BOLD-run count.** Determines whether the expensive tail is a
   pipeline problem or just bigger subjects — changes whether the fix is technical or budgetary.
3. **Audit pod resource requests vs. measured usage** (`peak_memory_gb` max 2.99 GB). Potential
   over-provisioning on `cpu-heavy-nodepool`.
4. **Quantify failed/partial runs.** 151 workflows for 100 subjects, and a $0.00006 minimum. Spend
   on runs that produce nothing is pure waste and may be the cheapest thing to eliminate.
5. **Investigate GPU spend (~50%).** FastSurfer/anatomy is the single biggest lever. Fractional
   GPUs (`g6f` via NodeOverlay) were enabled in commit `8a9d65e` — determine whether this batch
   actually used them, and whether anatomy is GPU-bound or just GPU-scheduled. See
   `docs/investigations/` for the existing GPU nodepool spot-scarcity notes.
6. **Explain the 07-02 → 07-17 cost volatility** ($13.56 → $39.62 for the same cohort).
7. **Re-derive the true component split** and correct both docs.

---

## 7. Related PRs (open as of 2026-07-18)

| PR | Branch | Relevance |
|---|---|---|
| #21 | `fix/observability-duckdb-schema-drift` | DuckDB SSO credentials + cost schema-drift tolerance. **Required to query S3 at all.** |
| #22 | `fix/registration-qc-schema-fields` | Documents the dead bold_to_t1w QC fields; notes the stale image + Glue schema gap |
| #23 | `fix/cost-flush-and-batch-scoping` | Adds `subject_costs()`; fixes cost flush; corrects `validate_test_batch.py` cost stats. **Stacked on #21.** |

⚠️ PR #23 changes code that deletes S3 data. The first real flush will remove a large historical
cost backlog (~12,235 rows). `--dry-run` enumerates keys; review before executing.

---

## 8. Reference

- Bucket `<YOUR_S3_BUCKET>`, region `<YOUR_AWS_REGION>`, Kubecost at `https://kubecost.<YOUR_DOMAIN>`
- Cost scraper: Prefect `kubecost-cost-scraper`, nightly 02:00 UTC. Scrape `date` is typically the
  day *after* the workflow ran — widen date windows by a day.
- Query helpers: `src/metrics/duckdb_query.py` (no billing, reads raw JSON — preferred) and
  `src/metrics/athena.py` (same API, subject to the Glue schema gaps in §5).
- Live per-phase cost breakdown via the Kubecost Allocation API — see
  `docs/observability.md` § "Direct API access" for `aggregate=label:cloudpipe.io/phase`.
