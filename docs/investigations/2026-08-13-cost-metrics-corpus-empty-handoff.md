# Cost metrics corpus is empty — handoff (2026-08-13)

> **STATUS — Cause B resolved, same day.** The premise below is partly wrong: the "21:00 UTC"
> timestamps were **local time misread as UTC** (§1), so the scraper had *not* been silent
> since 08-11. The real fault was a dead work-pool worker that skipped exactly one nightly
> run (§3). Fixed by restarting `deploy/prefect-worker`; the corpus went **7 → 606 objects**
> and the 300-subject batch's cost data is recovered. **Cause A (§2) is untouched and still
> needs a decision.** Corrections are marked inline; original text is struck through rather
> than deleted.

**Purpose:** the `metrics/costs/` corpus holds **7 objects**. The per-run cost figure that
`docs/index.md` publishes (`~$0.304`) cannot be reproduced from it, and the 300-subject
batch of 2026-08-12 has no cost data at all.

There are **two independent causes**, and conflating them will waste the investigation.
One is a design decision working as written; the other is a live failure that started on
~~2026-08-11~~ **2026-08-12T09:18Z** (*corrected — see §3; the 08-11 date came from the
misread timestamps in §1*). Only the second is a bug.

**Do not start by re-running a batch.** ~~§4 shows the history is very likely recoverable
from delete markers, which is hours of work rather than days of reconciliation wait.~~
**Superseded — the nightly settled re-scrape already rebuilt `metrics/costs/` for free
(§3). Read §3 first; §4's delete-marker restore is now only relevant to report-dates
outside Kubecost retention, and to `workflow-runs/`, which has no re-scrape path.**

---

## 0. Bottom line

| | |
|---|---|
| Objects in `metrics/costs/` | ~~**7**~~ → **606** after the §3 fix (`dt=2026-08-09` ×2, `dt=2026-08-10` ×300, `dt=2026-08-12` ×304) |
| `costs_compacted` (Athena) | **empty** |
| Delete markers on `metrics/costs/` | **442** (sampled; versioning is ON, no lifecycle policy) |
| Most recent scrape written | **2026-08-12 02:00:32 UTC** (writing `dt=2026-08-09`) — *corrected, see §1* |
| Scrapes since | **one missed**: the `2026-08-13 02:00 UTC` run never started |
| 300-subject batch (2026-08-12) cost data | ~~**not yet scraped** — still upstream in Kubecost~~ → **recovered** by the §3 fix (`dt=2026-08-12`, 304 workflows, $66.67, `scrape_age_days=1`) |

**Cause A (by design).** `scripts/prep_test_batch.py` calls `flush_costs()`
**unconditionally** — line 543, *not* gated behind `--flush-qc`. Every batch prep deletes
the cost history of that batch's subjects. This is intentional and documented, but it means
no longitudinal cost record can survive routine operation.

**Cause B (live failure).** ~~The Kubecost scraper has produced nothing since 2026-08-11
21:00 UTC.~~ **Superseded — see §3.** The scraper ran and `COMPLETED` every night through
`2026-08-12 02:00 UTC`, and the seven objects present are exactly what a healthy scraper
produces. The real fault is that the **`2026-08-13 02:00 UTC` run never started**, because
the `cloudpipe-k8s-pool` work pool had no live worker. Flushing still does not explain it,
and the 300-subject batch's absence of cost data is still **Cause B, not Cause A** — but the
outage is ~18 hours old, not three days, and nothing was lost.

---

## 1. Measured

Everything in this section was run on 2026-08-13 against live AWS. Commands are given so
they can be re-run rather than trusted.

### Corpus state

```bash
aws s3 ls --recursive s3://cloudpipe-metrics/metrics/costs/
```

```
2026-08-11 21:00:32   335  metrics/costs/dt=2026-08-09/2026-08-09_cloudpipe-4v278_cost_allocation.json
2026-08-11 21:00:32   325  metrics/costs/dt=2026-08-09/2026-08-09_cloudpipe-57k7m_cost_allocation.json
2026-08-10 21:00:08   344  metrics/costs/dt=2026-08-10/2026-08-10_cloudpipe-c6b2d_cost_allocation.json
2026-08-10 21:00:08   344  metrics/costs/dt=2026-08-10/2026-08-10_cloudpipe-cmf94_cost_allocation.json
2026-08-10 21:00:17   342  metrics/costs/dt=2026-08-10/2026-08-10_cloudpipe-pbnkz_cost_allocation.json
2026-08-10 21:00:17   340  metrics/costs/dt=2026-08-10/2026-08-10_cloudpipe-pqqd5_cost_allocation.json
2026-08-10 21:00:23   342  metrics/costs/dt=2026-08-10/2026-08-10_cloudpipe-xn5sb_cost_allocation.json
```

> **CORRECTION (2026-08-13, later the same day).** The timestamps above are **local time,
> not UTC** — `aws s3 ls` renders `LastModified` in the caller's timezone, and this
> workstation is `America/Chicago` (CDT, UTC−5). There is **no schedule discrepancy**;
> `docs/architecture.md:103` is correct. The true UTC times, from `s3api`:
>
> ```bash
> aws s3api list-objects-v2 --bucket cloudpipe-metrics \
>   --prefix metrics/costs/ --query 'Contents[].{K:Key,LM:LastModified}' --output text
> ```
>
> | Objects | true UTC write time |
> |---|---|
> | `dt=2026-08-10` ×5 | `2026-08-11T02:00:08Z` – `02:00:23Z` |
> | `dt=2026-08-09` ×2 | `2026-08-12T02:00:32Z` |
>
> Both land exactly on the `0 2 * * *` cron. Use `s3api` — never `s3 ls` — whenever a
> timestamp is load-bearing for an investigation.

The `dt=2026-08-09` objects were written on 2026-08-12, which is **not** a backfill: each run
scrapes two report-dates (see §3), and `2026-08-12 − SETTLED_AGE_DAYS(3)` is `2026-08-09`.
That is the settled re-scrape working exactly as designed.

### Athena

```sql
SELECT dt, COUNT(*), COUNT(DISTINCT workflow_name),
       ROUND(SUM(total_cost_usd),2)
FROM cloudpipe_metrics.costs GROUP BY dt ORDER BY dt;
```

| dt | rows | workflows | total_usd |
|---|---|---|---|
| 2026-08-09 | 2 | 2 | 0.00 |
| ~~2026-08-10~~ | ~~5~~ | ~~5~~ | ~~0.75~~ |

> **SUPERSEDED (same day).** This is the pre-fix state. After the §3 fix the settled
> re-scrape rebuilt `dt=2026-08-10` to **300 workflows / $46.97**, and `dt=2026-08-12`
> landed at **304 / $66.67**. Do not quote the 5-row / `$0.75` figures — see §3.

`cloudpipe_metrics.costs_compacted` returns **zero rows** — consistent with a
`--flush-qc` run at some point, since that deletes `metrics/compacted/` wholesale.

### The bucket is not the problem

```bash
aws s3api get-bucket-versioning --bucket cloudpipe-metrics          # → Enabled
aws s3api get-bucket-lifecycle-configuration --bucket cloudpipe-metrics
#   → NoSuchLifecycleConfiguration
```

So this is **not** expiry, and **not** the unversioned-bucket data loss that motivated the
metrics bucket split. Objects were deleted, and the deletes are recoverable.

### Contrast: `workflow-runs` survived

`metrics/workflow-runs/` holds `dt=2026-08-10` (5 records) and `dt=2026-08-12` (302
records — the 300-subject batch, intact). So the emitters that run **in-workflow** are
healthy; only the **out-of-band nightly scraper** is silent. That is a useful bisection: the
failure is ~~in the Prefect flow or its credentials~~ in the Prefect layer, not in the
pipeline.

> **REFINED by §3.** The bisection was sound but the next step it implies is not: the flow
> code and its credentials were both fine — the flow **never ran**, because the work pool had
> no live worker. When this bisection lands on "the scraper," check pool status and worker
> heartbeat *before* reading flow code or rotating credentials.

---

## 2. Cause A — `flush_costs()` is unconditional

`scripts/prep_test_batch.py`:

- `METRIC_PREFIXES` (`step-outcomes/`, `workflow-runs/`, `subject-manifests/`) — flushed by
  default, subject-scoped.
- `QC_PREFIXES`, `WORKFLOW_KEYED_PREFIXES`, `COMPACTED_PREFIX` — flushed **only** under
  `--flush-qc`.
- `metrics/costs/` — flushed by `flush_costs()` at **line 543, outside any `--flush-qc`
  guard**.

The module comment (lines 79–82) explains *why* costs need their own function — cost object
keys carry the Argo workflow name and no subject id, so a substring match never fires and
the flush needs an explicit workflow→subject index. That reasoning is about **mechanism**.
It does not argue that costs *should* be flushed by default, and the surrounding design puts
every other hard-to-recover prefix behind the opt-in flag.

**The question for whoever picks this up:** is unconditional cost flushing intended? The
stated rationale for flushing `METRIC_PREFIXES` is that `validate_test_batch.py` reads
`step_outcomes` with no recency window, so stale records fail validation spuriously. **That
rationale does not apply to costs** — nothing validates against them. If it does not apply,
`flush_costs()` belongs behind `--flush-qc` with the other destructive-and-unvalidated
prefixes, and the fix is a one-line move.

---

## 3. Cause B — RESOLVED: the work pool lost its worker

**Diagnosed and fixed 2026-08-13.** The scraper did not "stop on 08-11". It ran nightly and
`COMPLETED` without a gap through `2026-08-12 02:00 UTC`. The **`2026-08-13 02:00 UTC` run
never started** — it sat in `SCHEDULED` with `start_time: null`, ~18 hours late, because the
`cloudpipe-k8s-pool` work pool was `NOT_READY`.

### The corpus is exactly what a healthy scraper produces

`prefect/flows/cost_scraper.py` writes **two report-dates per run**: yesterday (day+1) and
`today − SETTLED_AGE_DAYS` (3). That fully accounts for all seven objects:

| Run (UTC) | day+1 partition | settled partition |
|---|---|---|
| `2026-08-11 02:00` | `dt=2026-08-10` → **5 objects** ✓ | `dt=2026-08-08` → 0 (no workflows ran) |
| `2026-08-12 02:00` | `dt=2026-08-11` → 0 (no workflows ran; the batch was 08-12) | `dt=2026-08-09` → **2 objects** ✓ |
| `2026-08-13 02:00` | `dt=2026-08-12` ← **the 300-subject batch** | `dt=2026-08-10` |

The missing 300-subject cost data is simply the third row: **never scraped, never deleted.**

### Root cause

- Work pool `cloudpipe-k8s-pool`: `NOT_READY`. Every registered worker `OFFLINE`, newest
  heartbeat `2026-08-12T09:13:57Z`.
- `prefect-server` restarted around `2026-08-12T09:18Z` (the pool's own `updated` field
  carries that timestamp). During that window the worker's boot sequence —
  `worker.start()` → `sync_with_backend()` → `_update_local_work_pool_info()` →
  `read_work_pool()` — got **HTTP 500** from `prefect-server` and raised.
- **The 500 was transient** and had cleared by the time of investigation (`GET
  /api/work_pools/cloudpipe-k8s-pool` → `200`). The worker never recovered anyway.

### Why it never self-healed — the part worth remembering

`kubectl get pods` reported the worker **`1/1 Running`**, 6 restarts, none recent. It looked
healthy. But the container's log *begins* with the crash traceback and its newest lines are
`kopf` watch retries: in Prefect 3.x's Kubernetes worker the **kopf observer thread keeps
PID 1 alive after the polling loop dies**. Container never exits → kubelet never restarts it
→ readiness passes → nothing alerts, indefinitely.

**A `Running` Prefect worker pod is not evidence that the pool has a worker.** The only
trustworthy check is the pool status / worker heartbeat:

```bash
curl -s -X POST "$PREFECT_API/work_pools/filter" \
  -H 'Content-Type: application/json' -d '{"limit":20}'   # want status: READY
curl -s -X POST "$PREFECT_API/work_pools/cloudpipe-k8s-pool/workers/filter" \
  -H 'Content-Type: application/json' -d '{}'             # want a non-stale heartbeat
```

> Note: the `prefect` CLI silently starts a temporary local server and will report
> variables and deployments that do not match the real one. Query the actual server at
> `https://prefect.<YOUR_DOMAIN>/api`, not bare `prefect` commands.

### PR #217 is exonerated

#217 merged `2026-08-11 01:12 UTC`; the scraper ran clean at `02:00` on both 08-11 and
08-12, *after* that merge. The `containerLogs.enabled = false` change is unrelated. The
timing lead was an artifact of the mis-read timezone in §1.

### Fix applied — and verified

```bash
kubectl -n prefect rollout restart deploy/prefect-worker
```

Pool returned to `READY`, the late run went `SCHEDULED → RUNNING → COMPLETED`, and both of
its report-dates landed and are queryable in Athena:

| dt | rows | workflows | total_usd | avg `scrape_age_days` |
|---|---|---|---|---|
| `2026-08-09` | 2 | 2 | 0.00 | 3.0 (settled) |
| `2026-08-10` | **300** | **300** | **46.97** | 3.0 (settled) |
| `2026-08-12` | **304** | **304** | **66.67** | 1.0 (**unsettled**) |

**Kubecost retention was fine** — the 300-subject batch's allocations were still upstream and
are now recovered. The time-sensitive risk did not materialise.

### The settled re-scrape partly repairs Cause A — for free

`dt=2026-08-10` went from **5 objects to 300** without anyone touching a delete marker: the
08-13 run re-fetched that entire report-date from Kubecost and rewrote it. So `flush_costs()`
damage **self-heals for any report-date still inside both `SETTLED_AGE_DAYS` and Kubecost's
retention**, and is permanent for anything older.

This does *not* generalise to the other prefixes. `workflow_runs` is emitted **in-workflow,
once**, and has no re-scrape path — it still holds only **5** records for `dt=2026-08-10`
against `costs`' 300. For that prefix, §4 really is the only recovery route:

| dt | `costs` workflows | `workflow_runs` workflows |
|---|---|---|
| `2026-08-10` | 300 (re-scraped) | 5 (still flushed) |
| `2026-08-12` | 304 | 302 |

---

## 4. The history is probably recoverable — but this is no longer the first move

> **SCOPE NARROWED by §3.** This section was written believing a delete-marker restore was
> the only way back. It is not, for `metrics/costs/`: each nightly run re-fetches
> `today − SETTLED_AGE_DAYS` from Kubecost, which rebuilt `dt=2026-08-10` from 5 objects to
> 300 with no restore at all. What is left for this section is genuinely narrower:
>
> - **`metrics/costs/` report-dates older than Kubecost's retention** — beyond the
>   re-scrape's reach, so a restore is the only route.
> - **`metrics/workflow-runs/`** — emitted in-workflow once, no re-scrape path, still at 5
>   records for `dt=2026-08-10`. This is now the prefix that actually needs §4, and the
>   commands below apply to it with the prefix changed.
>
> Anything inside both windows will repair itself within three days. Check before restoring.

**442 delete markers** were found on `metrics/costs/` in a 400-item sample, and the bucket
is versioned with no lifecycle policy. The flushed cost history is therefore still present
as non-current versions.

Recovery is enumerate-and-restore:

```bash
aws s3api list-object-versions --bucket cloudpipe-metrics \
  --prefix metrics/costs/ --query 'DeleteMarkers[].{K:Key,V:VersionId}'
# then, per key, delete the delete marker to resurrect the prior version:
aws s3api delete-object --bucket cloudpipe-metrics --key "<K>" --version-id "<V>"
```

**Caveats before running this at scale:**

- Restoring resurrects records for subjects across *many* prior batches. Since
  `metrics/costs/` accumulates by workflow name, a restored corpus mixes batches — scope any
  analysis by `dt` and by an explicit workflow or subject list, exactly as
  `subject_costs(date_from=…, date_to=…)` expects.
- Restored records were written under **older pod resource requests**. Anything before
  2026-08-11 predates #217 (`func-preproc` 3→2 CPU, subregion GEMS 4→3.5 CPU) and before
  that, #96. A per-run cost trend across the restored corpus is only meaningful if it is
  segmented by those change dates.
- Check `max_scrape_age_days` on restored rows. Records scraped at day+1 overstate cost by a
  **median ~51%** and reconciliation freezes at age 3; a restored corpus will contain a mix
  of settled and unsettled records and averaging them is meaningless.
- Do this on a **copy or with a dry run first**. Deleting delete markers is itself a
  mutation, and getting it wrong on 442 keys is worse than the current state.

---

## 5. Why this matters beyond the metrics layer

`docs/index.md:22` publishes `~$0.304` per workflow run and `~$30` per 100-subject batch as
settled figures. **Neither can currently be derived from the data.** The same is true of the
`$0.379 → $0.304` right-sizing result and the ~83% anatomical-plus-registration cost share.

That still holds after the §3 fix, for a different reason than when this was written: the
corpus is no longer empty (606 objects), but no partition in it is both **settled** and
**post-#217**, which is what those figures claim to be. `dt=2026-08-10` is settled and
pre-#217; `dt=2026-08-12` is post-#217 and unsettled.

These figures are also being prepared for external publication. ~~Until §3 is fixed and
either §4 or a fresh measurement lands~~ **§3 is fixed; until the `2026-08-15` settled
re-scrape of `dt=2026-08-12` lands and is read by the method below**, they should be treated
as unsourced.

There is a plausible upside worth confirming rather than assuming: #217's CPU trims were
**threshold** changes, not marginal ones (a `c*.2xlarge` exposes 7.21 usable CPU, so a
4-CPU pod packs one per node and a 3.5-CPU pod packs two; at 2 CPU, three fit). Before the
change, 83 of 148 `cpu-heavy` nodes were carrying exactly one workflow pod. Current per-run
cost is therefore **unmeasured and plausibly well below $0.304** — the re-measurement is
likely to improve the number, not endanger it.

### Do not publish a per-run figure straight off the §3 table

The recovered partitions invite the obvious division, and the obvious division is wrong:

- `dt=2026-08-10`: `46.97 / 300` = **$0.157/run** — *settled, but pre-#217.*
- `dt=2026-08-12`: `66.67 / 304` = **$0.219/run** — *post-#217, but `scrape_age_days = 1`,
  which overstates by a median ~51%. It will fall when the 08-15 settled re-scrape lands.*

Both are far below the published `$0.304`, which is suspicious enough to withhold rather
than announce. The likely confound is **partition grain vs workflow duration**: `dt` is a
report-date, so a batch that spans midnight has its *cost* split across two partitions while
its full set of `workflow_name`s appears in **both**. Dividing within one partition then
understates per-run cost, and the 2026-08-12 batch ran 12h33m — long enough to straddle.

Before any figure is published, verify against `pod_costs` or sum a single batch's workflows
across **all** partitions they appear in, rather than dividing within one. The settled
`dt=2026-08-12` re-scrape (due `2026-08-15 02:00 UTC`) is the first clean post-#217 read.

---

## 6. Definition of done

- [x] Root cause of the scraper's silence identified and fixed (§3: dead work-pool worker;
      `rollout restart deploy/prefect-worker`, pool back to `READY`).
- [x] A fresh `dt=` partition lands and is queryable in Athena — `dt=2026-08-12` (304
      workflows, $66.67) and a rebuilt `dt=2026-08-10` (300 workflows, $46.97).
- [ ] Decide whether to alert on work-pool readiness. This outage was invisible to every
      existing signal: pod `Running`, deployment healthy, flow runs not `FAILED` (just never
      started). A check on pool status `!= READY`, or on worker heartbeat age, is the only
      thing that would have caught it.
- [x] Confirm Kubecost retention still covers `2026-08-12` — it did; data recovered.
- [ ] Wait for the `2026-08-15 02:00 UTC` settled re-scrape of `dt=2026-08-12`, then derive
      the post-#217 per-run cost **using the method in §5**, not naive division.
- [ ] Decision recorded on whether `flush_costs()` moves behind `--flush-qc`.
- [ ] Either the delete-marker restore is completed, or a decision is recorded that the
      history is written off and cost is re-measured from a new batch.
- [ ] A settled (age ≥ 3 days) per-run cost figure exists for the **post-#217**
      configuration.
- [ ] `docs/index.md` cost figures updated to match, or marked unsourced.
- [x] ~~The 02:00 UTC vs 21:00 UTC schedule discrepancy in `docs/architecture.md:103`~~ —
      **void, there was no discrepancy.** `aws s3 ls` prints local time; the doc is correct.

## 7. Open questions

1. Why does `dt=2026-08-09` have only 2 workflows and `dt=2026-08-10` only 5, when the
   2026-08-10 batch was reported as 100 subjects?

   **Likely answered — Cause A, and the "100/100" record is probably not in doubt.** Both
   `metrics/costs/` *and* `metrics/workflow-runs/` are flushed by default (the latter via
   `METRIC_PREFIXES`), and both hold **5** records for `dt=2026-08-10`. Two independently
   flushed prefixes agreeing on the same survivor count is what subject-scoped deletion
   looks like: prepping the 2026-08-12 batch flushed the 08-10 records for every subject the
   two batches share, leaving only the non-overlapping remainder. The tell is that
   `dt=2026-08-12` still holds all **302** `workflow-runs` records — nothing has prepped a
   batch *since* 08-12, so nothing has flushed them yet.

   **CONFIRMED — closed.** The 08-13 settled re-scrape rebuilt `dt=2026-08-10` from Kubecost
   and it now holds **300 workflows / $46.97**, not 5. The batches ran as documented; the
   corpus had simply been flushed. No re-run and no delete-marker restore was needed to
   establish this. (`workflow_runs` for that day is still at 5, since it has no re-scrape
   path — see §3.)
2. Was `--flush-qc` used before the 2026-08-12 batch? `costs_compacted` being empty suggests
   yes. If so, per-scan QC for prior batches is also gone (recoverably).
3. ~~Does Kubecost's own retention window still contain the 2026-08-12 allocations?~~
   **Yes — closed.** The 08-13 run scraped them successfully (304 workflows, $66.67).

4. **New:** should `metrics/costs/` even be flushed, given §3 shows the settled re-scrape
   silently rebuilds it? Flushing a prefix that repairs itself three days later produces a
   corpus that is neither reliably present nor reliably absent — which is what made this
   investigation start from a false premise.
