# CloudPipe Operations Runbook

Day-2 reference for submitting pipelines, monitoring progress, handling failures, and updating code.

**Prerequisite**: connect to the AWS Client VPN before using any CLI or web UI listed below. The EKS API endpoint is private. If VPN/web-app access is misbehaving (can't connect at all, connects but no internet, web apps unreachable from a Windows browser), see [docs/investigations/2026-07-16-vpn-remote-access-troubleshooting.md](https://github.com/jrussell9000/cloudpipe/blob/main/docs/investigations/2026-07-16-vpn-remote-access-troubleshooting.md) before re-diagnosing from scratch.

---

## Service URLs

| Service | URL |
|---|---|
| Argo Workflows UI | https://argo.<YOUR_DOMAIN> |
| Prefect UI | https://prefect.<YOUR_DOMAIN> |
| ArgoCD UI | https://argocd.<YOUR_DOMAIN> |
| Kubecost | https://kubecost.<YOUR_DOMAIN> |
| Grafana | https://grafana.<YOUR_DOMAIN> |

---

## Submitting a pipeline run

### cloudpipe_minproc (via Prefect — normal path)

Prefect drip-feeds subjects one at a time and blocks when the `cloudpipe-max-concurrent` Prefect Variable's worth of workflows are already active. This is the preferred submission path for batch runs.

```bash
prefect deployment run cloudpipe-queue-manager/cloudpipe-queue-manager \
  -p subjects_file=s3://<YOUR_S3_BUCKET>/subjects.csv
```

Common optional parameters:

| Parameter | Default | Purpose |
|---|---|---|
| `start_index` | `0` | Skip the first N subjects (resume after a pause) |
| `end_index` | (all) | Stop at this index (exclusive) |
| `poll_interval` | `30` | Seconds to wait between count checks when at capacity |
| `globus_scan_types` | `'["T1w","T2w","rest","nback"]'` | BIDS scan types to transfer |
| `globus_source_collection_id` | (from SSM) | Override source Globus collection UUID |
| `globus_source_base_path` | (from SSM) | Override source base path |
| `globus_dest_base_path` | `/mmps_mproc` | Destination path within GCS collection |

`max_concurrent` is **not** a flow parameter — `prefect deployment run -p max_concurrent=N` has no effect. Concurrency is controlled live via the Prefect Variable `cloudpipe-max-concurrent` (code default `50`), checked on every poll cycle:

```bash
prefect variable set cloudpipe-max-concurrent 30
```

This can be changed at any time, including while the flow is already running — no restart needed.

**Check the live value before a batch — the code default only applies when the Variable is unset:**

```bash
prefect variable get cloudpipe-max-concurrent
```

The 2026-08-10 batch ran at `100` while every doc said `50` (#206). The Variable persists across runs, so whatever the last batch set is what the next one inherits.

The Variable is capped server-side by the controller's `namespaceParallelism` (`400`, namespace-wide across both pipelines — see [ADR 008](decisions/008-prefect-as-queue-manager.md)). Raising a Variable above that does **not** raise the effective cap: the surplus workflows are still submitted, then held `Pending` by the controller, which presents as a stalled batch rather than a submission error. To go above 400 concurrent, raise `namespaceParallelism` in `terraform/modules/argo-workflows/main.tf` and apply first.

Globus destination collection UUID is always read from SSM (`/cloudpipe/globus/collection-id`) at runtime, so instance replacements take effect automatically.

### cloudpipe_minproc (single subject, direct Argo submit)

Useful for testing or rerunning a specific subject without touching Prefect.

```bash
DEST_COLL=$(aws ssm get-parameter --name /cloudpipe/globus/collection-id --query Parameter.Value --output text)
SRC_COLL=$(aws ssm get-parameter --name /cloudpipe/globus/source-collection-id --query Parameter.Value --output text)
SRC_PATH=$(aws ssm get-parameter --name /cloudpipe/globus/source-base-path --query Parameter.Value --output text)

argo submit --from workflowtemplate/cloudpipe \
  -n argo-workflows \
  -p subjID=NDARINVXXXXXXXX \
  -p globus-source-collection-id=$SRC_COLL \
  -p globus-source-base-path=$SRC_PATH \
  -p globus-dest-collection-id=$DEST_COLL \
  -p globus-dest-base-path=/mmps_mproc \
  -p 'globus-scan-types=["T1w","T2w","rest","nback"]'
```

Always use `submit --from workflowtemplate/` — never `resubmit` (resubmit snapshots the template from the prior run and ignores any template updates).

`bucket` and `ecr-registry` are read automatically from the `cloudpipe-config` ConfigMap and do not need to be specified. Globus collection UUIDs are read from SSM at submission time.

### fmri-first-level-proc (via Prefect)

```bash
prefect deployment run first-level-queue-manager/first-level-queue-manager \
  -p subjects_file=s3://<YOUR_S3_BUCKET>/first-level-subjects.csv
```

Like `cloudpipe-queue-manager`, `max_concurrent` is not a flow parameter. Concurrency is controlled via the Prefect Variable `first-level-max-concurrent` (default `25`):

```bash
prefect variable set first-level-max-concurrent 15
```

The gate counts all active Argo workflows across both pipelines. If running cloudpipe and first-level simultaneously, set each Variable so the two caps sum to your desired total, and keep that total at or below the controller's `namespaceParallelism` (`400`).

The count is namespace-wide **by design**, not by oversight. `list_active_names()` filters only on `workflows.argoproj.io/completed!=true`, deliberately avoiding the `pipeline` label and `workflows.argoproj.io/phase` — the controller writes both *after* the create call returns, so any positive selector silently misses workflows submitted in the last few seconds. `ConcurrencyGate` additionally counts names it submitted itself until a list response confirms them, closing the informer-cache window ([#206](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/206)).

---

## Monitoring

### Workflow status

```bash
# All running workflows
argo list -n argo-workflows --running

# All workflows for a subject
argo list -n argo-workflows -l subjectid=NDARINVXXXXXXXX

# Detailed status for a single workflow
argo get -n argo-workflows <workflow-name>

# Tail logs for a running workflow
argo logs -n argo-workflows <workflow-name> --follow

# Logs for a specific pod/step
argo logs -n argo-workflows <workflow-name> <pod-name>
```

Pod logs are **not** forwarded to CloudWatch. Use the Argo UI or `argo logs` — there is no CloudWatch log group for pipeline pod output.

**Do not read `status.nodes` through `kubectl`.** `nodeStatusOffLoad: true` is set in the controller's `persistence` config (`terraform/modules/argo-workflows/main.tf`), so once a workflow's packed node status exceeds the controller's size threshold it is written to Postgres and *stripped from the CR*, leaving only `status.offloadNodeStatusVersion` behind. `kubectl get workflow <name> -o json` then reports `status.nodes` as absent — not empty-because-nothing-ran, absent-because-it-moved — and a running workflow with 30 live nodes looks idle. Observed on 2026-08-17 with the controller logging `"Workflow to be dehydrated" "Workflow Size"=168553` for the same workflow whose pods were visibly running.

`argo get` and `argo list -o json` go through the Argo server, which rehydrates from Postgres and drops `offloadNodeStatusVersion`, so they are the only correct readers (this is also why the server's service account is granted read on the `argo-db` secret — without it both fail with `offload node status is not supported`, [#81](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/81)) (verified the same day: 294 of 306 live workflows carried full `status.nodes`, 0 reported as offloaded). `kubectl get workflows` is still the right tool for **phase** across many workflows — that field stays on the CR — just never for per-node detail. Note the volume: that rehydrated listing was ~86 MB for 306 workflows, so redirect it to a file rather than piping it through a terminal.

### Counting active workflows

```bash
# From outside the cluster — matches what the Prefect gate counts, including
# workflows the controller has not labeled yet. Inequality selectors match
# objects where the key is absent, which is the point: a just-submitted
# workflow has no labels at all for its first few seconds (#206).
kubectl -n argo-workflows get wf \
  -l 'workflows.argoproj.io/completed!=true' --no-headers | wc -l
```

`argo list --running` is **not** equivalent: it filters on the controller-stamped phase, so it under-reports during a submission burst. Use it to see what is actually executing, not to verify a concurrency cap.

### Verifying the gate held (after a batch)

The live count above only tells you about *now*. To confirm the gate actually held for a completed batch, reconstruct the concurrency timeline from each workflow's `startedAt`/`finishedAt` as ±1 events. This needs no Prometheus and no port-forward — the workflows carry their own timestamps, so this is the same ground truth the controller acted on. Run it within `ttlStrategy.secondsAfterCompletion` (24h) of the batch finishing, before the objects are reaped.

```bash
kubectl -n argo-workflows get workflows -o json > /tmp/wfs.json

python3 - <<'PY'
import collections, datetime as dt, json
P = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
wfs = [w for w in json.load(open("/tmp/wfs.json"))["items"]
       if w["metadata"]["name"].startswith("cloudpipe-")]

events = []
for w in wfs:
    s = w["status"]
    if s.get("startedAt") and s.get("finishedAt"):
        events += [(P(s["startedAt"]), 1), (P(s["finishedAt"]), -1)]
events.sort()

# Time-weight the histogram: "touched the cap once" and "sat pinned at the cap
# for hours" both report the same peak, but only the latter means it was binding.
hist, cur, prev = collections.Counter(), 0, events[0][0]
peak = 0
for t, delta in events:
    hist[cur] += (t - prev).total_seconds()
    prev, cur = t, cur + delta
    peak = max(peak, cur)

span = sum(hist.values())
submits = sorted(P(w["metadata"]["creationTimestamp"]) for w in wfs)
print(f"submitted:  {len(wfs)} over {(submits[-1] - submits[0]).total_seconds() / 3600:.1f}h")
print(f"peak:       {peak}")
saturated = sum(v for k, v in hist.items() if k >= peak - 1)
print(f"saturated:  {saturated / 3600:.2f}h at >={peak - 1} ({100 * saturated / span:.1f}% of {span / 3600:.1f}h)")
PY
```

Two readings matter, and one is a trap:

- **`peak` never exceeds the cap.** This also recovers the effective cap when the Prefect API is unreachable — the 2026-08-17 run reads back a clean `300`.
- **`submitted` must exceed the cap for this to prove anything.** A batch sized at or below the cap never blocks the gate at all, so `peak == cap` is a tautology on a 300-subject batch at a cap of 300. The 08-17 run submitted **407** over 13.0h and sat saturated for **2.60h (15.6%)** — the plateau is the gate visibly holding, not grazing the cap once.

### Checking S3 outputs for a subject

```bash
# Final MNI-space BOLD outputs
aws s3 ls s3://<YOUR_S3_BUCKET>/derivatives/func/NDARINVXXXXXXXX/ --recursive

# Registration outputs
aws s3 ls s3://<YOUR_S3_BUCKET>/derivatives/registration/NDARINVXXXXXXXX/ --recursive

# FastSurfer derivatives
aws s3 ls s3://<YOUR_S3_BUCKET>/derivatives/fastsurfer/NDARINVXXXXXXXX/ --recursive
```

### Prefect flow run status

Use the Prefect UI or:
```bash
PREFECT_API_URL=https://prefect.<YOUR_DOMAIN>/api \
  prefect flow-run ls
```

---

## Stopping and pausing

### Stop a Prefect queue manager (pause submission)

Cancel the Prefect flow run from the UI, or:
```bash
PREFECT_API_URL=https://prefect.<YOUR_DOMAIN>/api \
  prefect flow-run cancel <flow-run-id>
```

Workflows already submitted continue running. To also stop those, terminate them individually (see below) or use the bulk approach.

### Terminate a single workflow

```bash
argo terminate -n argo-workflows <workflow-name>
```

### Terminate all running workflows (nuclear option)

```bash
argo list -n argo-workflows --running -o json \
  | jq -r '.[].metadata.name' \
  | xargs -I{} argo terminate -n argo-workflows {}
```

### Resume after a pause

Restart Prefect with `start_index` set to the first unprocessed subject:
```bash
prefect deployment run cloudpipe-queue-manager/cloudpipe-queue-manager \
  -p subjects_file=s3://<YOUR_S3_BUCKET>/subjects_v611.csv \
  -p start_index=150 \
  -p batch_label=leg-2
```

Always pass `subjects_file` explicitly. The deployment's stored default is
`s3://<YOUR_S3_BUCKET>/subjects.csv`, which **404s — it has never existed**; the real cohort CSV is
`subjects_v611.csv`.

Pass `batch_label` on anything you will later want to isolate in the metrics. It is stamped onto
every `WorkflowRun` record in the submission, and it is the only reliable way to separate one run
from another: batches whose workflows straddle a UTC midnight cannot be told apart by `dt` at all,
and before this field existed the pre-leg-1 test batches and leg 1 shared the `2026-08-18`
partition. Records written before 2026-09-02 predate the field and read back **`NULL`**, not `''`
— so `batch_label = ''` selects only unlabelled runs from schema 1.2 onward. Use
`COALESCE(batch_label, '') = ''` for "unlabelled", and `started_at` for anything historical.

Resume with the **complete ordered CSV** and a moved `start_index` — never a filtered remainder.
The inventory step checks S3 for existing derivatives, so already-completed subjects cost one
Globus transfer plus an inventory pod and then skip the rest.

---

## Reprocessing subjects and flushing metric data

The inventory step skips any step whose S3 derivative already exists. To force re-processing, delete the relevant derivative prefix first. To keep dashboards clean, also delete the corresponding metric records.

### Force re-run a single subject (all steps)

```bash
SUBJ=NDARINVXXXXXXXX

# Delete derivatives (forces all pipeline steps to re-run)
aws s3 rm s3://<YOUR_S3_BUCKET>/derivatives/fastsurfer/${SUBJ}/ --recursive
aws s3 rm s3://<YOUR_S3_BUCKET>/derivatives/registration/${SUBJ}/ --recursive
aws s3 rm s3://<YOUR_S3_BUCKET>/derivatives/func/${SUBJ}/ --recursive

# Delete workflow-keyed metric records (clears dashboard rows for this subject)
for prefix in workflow-runs step-outcomes subject-manifests; do
  aws s3 rm s3://cloudpipe-metrics/metrics/${prefix}/ --recursive \
    --exclude "*" --include "*${SUBJ}*"
done
```

> **Do not delete `metrics/func-preproc/`, `metrics/registration/`, `metrics/anat-qc/`, or
> `metrics/fsqc-qc/` for a single-subject rerun.** These hold the per-scan motion, tSNR, and
> registration/anatomical QC — the pipeline's only record of output quality, and the evidence
> base for publication. Their keys carry no workflow name, so the re-run overwrites each record
> in place; deleting first gains nothing. `cloudpipe-metrics` *is* versioned, so such a delete is
> recoverable from delete markers, but only by manual enumeration — don't rely on it.
> (`<YOUR_S3_BUCKET>`, which holds the derivatives and `logs/`, is **not** versioned; deletes there are
> unrecoverable without reprocessing.) For a full fresh-start batch, where stale QC from earlier
> batches would pollute the dashboards, use `prep_test_batch.py --flush-qc`.

This does **not** clear `metrics/costs/`. Cost objects are keyed
`dt={date}/{date}_{workflow-name}_cost_allocation.json` and contain no subject ID, so a `--include
"*${SUBJ}*"` glob silently matches nothing. To clear a subject's costs, look up its workflow names
first:

```bash
# Workflow names that ran this subject
aws s3 ls s3://cloudpipe-metrics/metrics/workflow-runs/ \
  | grep "__${SUBJ}_run_summary.json" \
  | sed 's/.* //; s/__.*//' \
  | while read -r WF; do
      # One object per scrape date, so match any date prefix; the second
      # include also catches legacy undated keys written before the change.
      aws s3 rm "s3://cloudpipe-metrics/metrics/costs/" --recursive \
        --exclude '*' \
        --include "*_${WF}_cost_allocation.json" \
        --include "${WF}_cost_allocation.json"
    done
```

Or run `scripts/prep_test_batch.py` with a one-subject CSV, which handles this (plus orphaned cost
records whose run summary is already gone) — use `--dry-run` first.

### Flush all metric data (start fresh dashboards)

> **Destructive.** This deletes every per-scan QC record ever produced, which cannot be
> regenerated without reprocessing every subject. `cloudpipe-metrics` **is** versioned, so a
> delete leaves recoverable non-current versions behind — but recovery is a manual,
> object-by-object job, so treat this as effectively one-way. Prefer clearing only the
> workflow-keyed prefixes:

```bash
# Reset dashboards without destroying QC history
for prefix in workflow-runs step-outcomes subject-manifests; do
  aws s3 rm s3://cloudpipe-metrics/metrics/${prefix}/ --recursive
done
```

Athena reads directly from S3 and resolves `dt=` partitions by projection, so nothing needs to run after a deletion — there are no crawlers and no partition metadata to invalidate. A projected partition whose objects are gone simply returns no rows. Grafana panels reflect the cleared data on the next query (within the dashboard refresh interval).

Note this clears the **raw** prefixes only. The `metrics/compacted/{table}/` Parquet copies are written by the nightly compactor and are what the `*_compacted` tables read, so rows already compacted survive a raw-prefix delete and keep appearing in any query that hits the compacted arm. Delete the matching `metrics/compacted/` partitions too if you want a genuinely clean slate.

### Process a new batch (no flush needed)

For an entirely new set of subjects with no overlap, just submit — metric records accumulate across batches and the dashboards aggregate all of them. Scope dashboard time ranges to the batch window if you want batch-specific views.

### Clear completed Argo workflow history (cosmetic only)

Does not affect reprocessing or metrics:

```bash
argo delete -n argo-workflows --completed
```

---

## Running a test batch

A test batch validates the full observability stack — outcome tracking (StepOutcome,
SubjectManifest, WorkflowRun) and Kubecost cost attribution — before committing to a production
run. The canonical subject list is `tools/cloudpipe_test_sample.csv` (100 subjects);
`tools/cloudpipe_test_sample_200.csv` (200 subjects) is the concurrency-scaling batch.

**Prerequisites:** VPN connected, AWS SSO active, `kubectl` context set to `cloudpipe`.

**Also check the age of the Globus refresh token, before Step 1 and not after Step 2:**

```bash
aws secretsmanager list-secrets --filters Key=name,Values=globus \
  --query 'SecretList[].LastChangedDate' --output text
```

The destination collection's storage gateway is High Assurance with a **1-week**
`--authentication-timeout-mins`, so a token older than 7 days fails every single
`globus-transfer` pod and takes the whole batch with it. Nothing warns you: the token is
still valid, the secret is still synced, and the error Globus returns is
`not_from_allowed_domain`, which reads as a gateway misconfiguration rather than an expiry.
If the token is anywhere near 7 days old, re-run `setup_auth.py` first
([globus.md → Recognising an expired session](globus.md#recognising-an-expired-session)).

This is not hypothetical: the batch submitted 2026-08-17T01:43Z lost 300/300 subjects to a
session that had lapsed 23 hours earlier, and had to be terminated and resubmitted.
After reauthenticating, prove it with a **single-subject** submit
([above](#cloudpipe_minproc-single-subject-direct-argo-submit)) and wait for
`Destination collection pre-flight check passed` before releasing 300.

### Generating a subject list

`scripts/make_test_sample.py` draws a sample stratified by session count k, because cost
and wall clock scale near-linearly in k ($0.096 at k=1 to $0.305 at k=4) — a random draw's
k-mix drifts from the corpus and makes extrapolation to the full 11,628 unreliable.

```bash
pixi run python scripts/make_test_sample.py 200 -o tools/cloudpipe_test_sample_200.csv
```

By default the draw is **seeded from the previous 100-subject sample**, so the smaller batch
is a strict subset and the two runs differ only in concurrency. That requires flushing those
subjects first (Step 1 below) — otherwise their existing `derivatives/fastsurfer/` causes the
inventory step to skip the whole anatomical phase, omitting the GPU-heavy work and
understating load. Pass `--exclude-prior` to draw entirely fresh subjects instead.

The seed is fixed (`--seed`), so the same command reproduces the same list.

---

### Step 1 — Flush prior data

The flush removes any derivatives and subject-keyed metrics from a previous test run so
validation checks start from a clean slate.

```bash
# Preview what will be deleted (no changes):
pixi run python scripts/prep_test_batch.py tools/cloudpipe_test_sample.csv \
  --metrics-bucket cloudpipe-metrics --dry-run

# Execute (parallel flush, prompts for confirmation):
pixi run python scripts/prep_test_batch.py tools/cloudpipe_test_sample.csv \
  --metrics-bucket cloudpipe-metrics
```

`--metrics-bucket` is required and deliberately has no default — metrics are the run
of record, and a defaulted value is how an operator deletes the wrong thing by reflex.
Derivatives are flushed from `--bucket` (default `<YOUR_S3_BUCKET>`); metrics from
`--metrics-bucket`. Both names are readable from the `cloudpipe-config` ConfigMap:

```bash
kubectl get configmap cloudpipe-config -n argo-workflows -o jsonpath='{.data}' | jq .
```

The script:
1. Uploads `cloudpipe_test_sample.csv` to `s3://<YOUR_S3_BUCKET>/config/test_batch_subjects.csv`
2. Deletes derivatives (`fastsurfer/`, `registration/`, `func/`, `func_surf/`,
   `subregions/`, `subregions_mni/`) per subject in parallel
3. Deletes workflow-keyed metrics (`step-outcomes`, `workflow-runs`, `subject-manifests`)
   by matching the subject ID in the object key
4. Deletes `metrics/costs/` records belonging to the batch subjects (see below)
5. Filters the same subjects' rows out of the compacted Parquet of every table it just
   flushed raw records from (see below)
6. Prints cluster node pool status and Globus instance state

`metrics/workflow-runs/` and `metrics/subject-manifests/` filenames embed the subject ID
(`{workflow-name}__{subject}_...json`), so they're flushed per-subject by key matching.

**Per-scan QC is not flushed by default — pass `--flush-qc` to include it.**
`metrics/func-preproc/`, `metrics/registration/`, `metrics/anat-qc/`, `metrics/fsqc-qc/` and
`metrics/surface-sample/` are keyed `{subject}_{session}_{task}_{run}` with no workflow name, so a
re-run overwrites each record in place — flushing first gains nothing on a single-subject
rerun and only destroys history. For a **fresh-start batch** it is the opposite: the QC
dashboards read the raw tables, so records from subjects processed by an earlier batch
survive and mix into every panel. That is what `--flush-qc` is for:

```bash
# fresh-start batch: also flush per-scan QC and pod-costs/workflow-starts
pixi run python scripts/prep_test_batch.py tools/cloudpipe_test_sample.csv \
  --metrics-bucket cloudpipe-metrics --flush-qc
```

`--flush-qc` additionally removes `metrics/pod-costs/` and `metrics/workflow-starts/` (both
workflow-keyed, resolved through the same workflow→subject index as costs).

**Both modes flush compacted too, per subject.** Every table whose raw records are deleted
has its `*_compacted` twin filtered the same way: the batch subjects' rows are removed from
each Parquet file in place (one file packs many subjects, so it is rewritten, never deleted),
and every other subject's history stays. This is what makes a flushed attempt stop counting
everywhere at once
([#382](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/382)) — the nightly
compactor rebuilds only its last four days, so without it an older day's compacted copy kept
the flushed rows while raw and the dashboards did not. (`--flush-qc` used to delete **all**
of `metrics/compacted/` on the theory that the compactor rebuilds it; it rebuilds four days,
so that erased every older day for every subject.) The compacted pass runs last, and if any
file fails it exits 1 and says so: re-run the same command before submitting — the raw and
derivative deletes are already done, so a re-run only finishes the compacted pass.

> **Ordering trap.** `pod-costs` and `workflow-starts` resolve their subject through
> `metrics/workflow-runs/`. If that index is already gone (a prior default flush deleted it),
> these records become permanently unattributable: the script reports them as orphans and
> leaves them in place rather than guessing. Run `--flush-qc` in the *same* invocation as the
> rest of the flush, not as a second pass afterward.

`cloudpipe-metrics` has **versioning enabled**, so deletes here place a delete marker and are
recoverable by enumerating versions — but recovery is manual and tedious, which is why QC
stays opt-in rather than becoming the default.

One consequence to be aware of: a run processed by an earlier batch
but not the current one leaves a stale QC record behind, so **always scope QC analyses by
`completed_at`**, not by subject alone. This is not hypothetical — the 2026-07-31 batch's
10 subjects matched 337 func-preproc, 532 registration and 92 anat-qc records, of which
only 110 / 133 / 23 were from that run.

**The derivative list must stay complete.** `DERIV_PREFIX_TEMPLATES` in the script has to
name every prefix under `derivatives/`. Anything omitted survives the flush, and because
the inventory step skips a step whose output already exists, that step is silently
recorded `skipped` / `failure_category: dependency` on a run you believe is a full
reprocess. `func_surf/` was missing until 2026-07-31, which is why `surface-resample` ran
zero pods in that batch. `tests/test_prep_test_batch.py::test_every_derivative_prefix_is_flushed`
pins the list; if you add a derivative prefix, update both.

**`metrics/costs/` is different.** Cost object keys are `dt={date}/{date}_{workflow-name}_cost_allocation.json` —
they contain the *workflow* name and no subject ID, so a subject-substring match never fires. The
script therefore resolves the mapping explicitly: it lists `metrics/workflow-runs/` once to build a
workflow-name → subject index (LIST only, no object reads), and falls back to reading the `subject`
field out of any cost object the index can't resolve — orphans whose run summary was deleted by an
earlier flush. This runs *before* the workflow-runs flush, which would otherwise destroy the index.

> Before the fix in `flush_costs()`, cost records were never flushed and accumulated across every
> batch. If you have run batches against this bucket historically, the first flush will delete a
> large backlog. Always preview with `--dry-run`, which prints every key it would delete.

---

### Step 2 — Submit

```bash
PREFECT_API_URL=https://prefect.<YOUR_DOMAIN>/api \
prefect deployment run cloudpipe-queue-manager/cloudpipe-queue-manager \
  -p subjects_file=s3://<YOUR_S3_BUCKET>/config/test_batch_subjects.csv
```

`max_concurrent` is not a flow parameter; it comes from the live `cloudpipe-max-concurrent` Prefect Variable. **Read it before submitting** — it persists from the previous batch, and `50` is only the fallback for when it is unset:

```bash
prefect variable get cloudpipe-max-concurrent   # confirm, then override if needed
prefect variable set cloudpipe-max-concurrent <N>
```

Record the value you confirmed alongside the submission timestamp: it is what the batch's cost and runtime baselines are conditioned on.

Note the submission timestamp in **UTC** — it is `--since` in Step 4 and the cost validation
window start in Step 6.

---

### Step 3 — Monitor

```bash
# Active workflow count
argo list -n argo-workflows --running -o json | jq length

# Failed workflows
argo list -n argo-workflows --field-selector=status.phase=Failed -o json \
  | jq -r '.[].metadata.name'

# Kubecost mid-run spot-check — count workflows with cost data so far
# (substitute actual RFC3339 UTC window times)
curl -sk "https://kubecost.<YOUR_DOMAIN>/model/allocation?window=<START>,<END>&aggregate=label:workflows.argoproj.io/workflow&filterNamespaces=argo-workflows&accumulate=true" \
  | jq '[.data[][]] | map(select(.totalCost > 0)) | length'
```

Expected runtime: 1–3 hours for 100 subjects at `max_concurrent=50`. Cost reference:
**~$0.304 per run, settled** (FastSurfer + registration ~83%, func-preproc ~17%). Total ~$30 for 100 subjects.

> Compare against **settled** figures only. A day+1 scrape overstates cost by a median ~51%, so the earlier ~$0.27 reference — read at day+1 — is not comparable to a settled number and reads as a spurious regression. Cost is per **run** (divide by distinct `workflow_name`), never per distinct subject; a fixed subject list inflated an earlier figure ~3×. See [observability.md](observability.md#kubecost-scraper).

---

### Step 4 — Validate outcome tracking

Run immediately after all workflows complete (before the cost scraper fires). The
`Pod attempt exit codes` check reads per-attempt records **live from Argo**, and those are
deleted 24h after a workflow completes (`ttlStrategy`), so a late run silently loses that
check — it warns rather than passing when the data is gone:

```bash
python src/validate_test_batch.py \
  --subjects tools/cloudpipe_test_sample.csv \
  --since <run_start_utc>
```

Expected output:
```
OUTCOME TRACKING
  SubjectManifest                        100/100 ✓
  StepOutcome steps                      9/9 ✓
  func-preproc per-run coverage          1467/1467 ✓
  WorkflowRun schema v1.1                100/100 ✓
  Pod attempt exit codes                 216/216 ✓
    Scope: 100 workflows over 100 subjects (submitted at or after 2026-08-03T21:59:00Z).

RESULT: PASS
```

Pass `--since` (RFC3339 UTC — the submission timestamp you noted in Step 2, also used as
`--window-start` in Step 6). It scopes the `Pod attempt exit codes` check to this batch.
Without it the check falls back to the most recent workflow per subject, which is right for
a plain one-workflow-per-subject batch but will mix runs if any subject was resubmitted.

This scoping matters because that check reads live Argo state rather than the S3 records
`prep_test_batch.py` flushes, so nothing clears the previous batch for it. Argo retains
completed workflows for 24h, and rerunning the same subject CSV twice in a day is routine —
unscoped, the earlier run's failures are counted against the new one (issue #140, where a
batch with zero OOM kills was reported as FAIL on OOMs from a template it no longer runs).
Always read the `Scope:` line: it states how many workflows were counted and under which
rule, so a contaminated count is visible rather than silent.

`func-preproc per-run coverage` counts **runs**, not subjects (issue #146) — the denominator is
expected `(session, task, run)` triples, so a subject with one bad session among otherwise-good
sessions still shows the gap instead of reading as fully covered. A count below the expected
total means some BOLD runs failed or were skipped; check the failure detail printed below the
table and inspect those subjects in Argo UI.

Runs missing because their session failed the `t1w-to-mni` QC gate are a **known, explainable**
gap — `func-preproc` is correctly skipped downstream of the rejection, not a recording defect —
and are listed separately under `details` as `sessions gated by t1w-to-mni QC: N (M runs)` rather
than in the failure list. This is expected residual (the documented outcome of `NONE` sampling's
deterministic bad basins, see ADR 012) and does not by itself fail the check.

There are **two** such QC gates (issue #221). The second is per **run**, not per session:
`bold-to-t1w` rejects an individual run on its relative-NMI sanity floor (exit 65) and the
session driver discards that run's transform, so `func-preproc` has no input to work from — in a
session whose `t1w-to-mni` succeeded. Those runs are listed under `details` as
`runs gated by bold-to-t1w QC: N` and likewise need no action. When both gates apply to the same
run, only the session-level one is reported.

Since issue #222 the func-preproc driver detects the discarded transform up front and records
that run as `skipped` with `upstream_failed_step=bold-to-t1w`, logging one line:

```
[driver] SKIPPED sub-VKLJZA5A_ses-06A_task-rest_run-01 — no bold_to_t1w transform (QC-rejected upstream): ..._desc-bold2t1w_itk.txt
```

Before that it ran `preproc.py` anyway and the run died at the composite-warp stage with an
`antsApplyTransforms` `CalledProcessError` traceback, which reads exactly like an OOM or an ANTs
crash — the 2026-08-10 batch needed two pod logs out of `s3://<YOUR_S3_BUCKET>/logs/` and a
`derivatives/registration/` listing to establish otherwise. The validator keys off the
**bold-to-t1w** row either way, so records written before #222 (`failed`) and after it
(`skipped`) are reported identically. If you see the old traceback in a recent batch, the pod is
running a pre-#222 image — check the pinned SHA.

Only exit 65 counts as that gate. A `bold-to-t1w` failure from any other cause — an OOMKill, a
spot preemption, a mid-loop pod death recorded as `pod terminated before this run's result was
recorded` — is a real defect and stays in the failure list, annotated with the recorded reason.
Any run in the failure list that is *not* under either gated heading is unexplained and needs
investigation.

`Pod attempt exit codes` counts **pod attempts**, not steps. Every other check describes a
step *after* `retryStrategy` has run, so a step that was OOMKilled and then succeeded on
retry passes all of them; only this one sees it:

```
  Pod attempt exit codes                 212/216 ⚠
    bold-to-t1w-session-template: 4/25 attempts non-zero (137 OOMKilled×4)

RESULT: PASS WITH WARNINGS (1)
```

**A `PASS WITH WARNINGS` batch is not a clean baseline.** Retries are a shared budget — the
same one spot interruptions draw on — so a step that only passed because it had retries left
is one interruption away from failing outright.

**Read the cause, not the exit code.** An OOM kill and a spot reclaim are both SIGKILL/137
and only the pod message separates them, so the check labels attempts by cause
(`137 OOMKilled` vs `137 spot interruption`). They need opposite fixes: the first is a
memory request that is too small, the second is spot capacity in the nodepool. Sizing memory
up in response to a reclaim is wasted work.
The check fails (not warns) once a step's non-zero attempts reach 25% of at least 4 attempts.
This exists because the 2026-08-01 01:58Z batch reported an unqualified `PASS` while
OOMKilling `bold-to-t1w` (issue #114, `docs/investigations/2026-08-01-bold-to-t1w-oom-handoff.md`).

Warnings do **not** change the exit code — only a failed check exits non-zero. If you gate
automation on this script, read the verdict line, not just `$?`.

Note the **denominator shrinks** when a subject has no usable BOLD in the requested scan
types (absent from `mmps_mproc/`, or only scan types outside `globus-scan-types`). Such a
subject is excluded rather than counted as a gap, and every exclusion is listed under the
table — read that list and confirm it matches the batch you expected, because a subject
whose outcome recording failed entirely would look the same from here.

---

### Step 5 — Run the cost scraper

The nightly scraper fires at **02:00 UTC** and writes the previous day's costs to S3. Because a
batch submitted in the evening CDT crosses the UTC midnight boundary (e.g. submitted 20:30 CDT =
01:30 UTC next day), most workflow costs land on the *following* UTC day. Wait for 02:00 UTC or
trigger manually:

```bash
PREFECT_API_URL=https://prefect.<YOUR_DOMAIN>/api \
prefect deployment run kubecost-cost-scraper/kubecost-cost-scraper
```

If the batch crossed the UTC midnight boundary and the nightly scraper missed it, backfill with
the explicit date:
```bash
pixi run python src/metrics/kubecost_scraper.py \
  --bucket cloudpipe-metrics \
  --base-url https://kubecost.<YOUR_DOMAIN> \
  --date <YYYY-MM-DD> \
  --insecure
```

`--bucket` here is the *metrics* bucket, not the data bucket — the scraper writes
`metrics/costs/` and `metrics/pod-costs/`, both of which moved in the metrics bucket split.

Nothing further is needed to make the data queryable: Athena reads straight from S3, and
there is no crawler to run. The Glue crawlers were removed after they generated 5,227 junk
tables — Glue tables **and** their columns are now hand-declared in
`terraform/modules/metrics/`, so a new metric field that is not declared there will simply
never appear in Athena.

---

### Step 6 — Validate cost data

```bash
python src/validate_test_batch.py \
  --subjects tools/cloudpipe_test_sample.csv \
  --cost \
  --window-start <run_start_utc> \
  --window-end <run_end_utc>
```

`--window-start` and `--window-end` are RFC3339 UTC timestamps bracketing the full batch run.
When in doubt, use a wide window (e.g. 24-hour span covering the run date).

With `--cost` and no explicit `--since`, `--window-start` also scopes the `Pod attempt exit
codes` check. A deliberately wide cost window therefore widens that scope too — pass
`--since <run_start_utc>` as well when the window was widened past the actual submission time.

Expected output:
```
OUTCOME TRACKING
  SubjectManifest                        100/100 ✓
  StepOutcome steps                      9/9 ✓
  func-preproc per-run coverage          1467/1467 ✓
  WorkflowRun schema v1.1                100/100 ✓
  Pod attempt exit codes                 216/216 ✓
    Scope: 100 workflows over 100 subjects (submitted at or after 2026-08-03T21:59:00Z).

COST DATA
  Kubecost API cost attribution          100/100 ✓
  Athena cost rows (total_cost_usd)      100/100 ✓
  Batch cost statistics                  100/100 ✓
    Total: $30.xx  Mean: $0.30x  Median: $0.29x  Range: $0.1xx–$0.6xx
    Per-subject totals over 137 workflow-day cost records

RESULT: PASS
```

The cost statistics line is informational — it always passes and shows the batch cost
distribution. Compare mean and median to the reference baseline (~$0.304/run, settled). Outliers
more than 2× the mean warrant investigation.

The `n/100` on that line is a count of **subjects** with cost data. Because CostAllocation is one
record per workflow per scrape date, the statistics are computed over per-subject sums (a subject
that was retried, or that ran across two UTC days, contributes several records — the second detail
line reports that raw record count). The costs are scoped to the `--window-start`/`--window-end`
window, widened by one day to cover the 02:00 UTC scrape. Omit those flags and the check falls back
to subject-only scoping and prints a WARNING, because `metrics/costs/` can still hold records from
earlier runs of the same subjects.

---

### Step 7 — Snapshot the derived numbers

**Do this before the next batch's Step 1.** `metrics/` is a scratchpad: the next
`prep_test_batch.py --flush-qc` deletes the records these numbers are derived from, and some of
them cannot be re-derived by re-running — a re-run produces a *different* batch's cost. The
2026-08-05 pilot's settled cost was lost exactly this way.

```bash
pixi run python scripts/snapshot_batch_numbers.py \
  --batch-id 2026-08-12-300 \
  --subjects tools/cloudpipe_test_sample_300.csv \
  --since 2026-08-12T03:28:04Z \
  --until 2026-08-12T16:30:00Z \
  --notes "what this batch was testing; template revision; image pin"
```

Writes `s3://cloudpipe-metrics/snapshots/batches/{batch-id}.json` — outside `metrics/`, so no
flush path reaches it, alongside the per-attempt telemetry the `argo-nodes-snapshot` CronWorkflow
lands under `snapshots/argo-nodes/`. It reads the newest of those snapshots rather than live Argo,
so it still works after the 24h `ttlStrategy` has reaped the workflows.

**Pass `--until` whenever any subject was resubmitted after the batch.** Reruns are created after
`--since`, so a lower bound alone silently counts the repair as a batch member.

Cost is a **second pass**, because reconciliation freezes at scrape age 3 and a day+1 read
overstates settled by a median ~51%:

```bash
# >= 3 days after the batch
pixi run python scripts/snapshot_batch_numbers.py --batch-id 2026-08-12-300 --amend-cost \
  --window-start 2026-08-12T03:28:04Z --window-end 2026-08-12T16:00:52Z
```

`--amend-cost` refuses to write an unsettled read, and refuses to replace a cost block with one
covering fewer subjects — a partial re-scrape once landed $0.222 where the real figure was ~$37.
`--force` overrides both, and should be rare enough to need a reason.

---

### Pass/fail criteria

| Check | Expected | Fail — first thing to check |
|---|---|---|
| SubjectManifest | 100/100 | `exit_handler.py` pod logs in Argo UI for missing subjects |
| StepOutcome steps | 9/9 | outcome-recorder task `depends` expressions in WorkflowTemplate |
| func-preproc per-run coverage | all expected runs | unexplained gaps: Argo UI → subject → session phase → bold-to-t1w / func-preproc pod logs. Gaps listed under "gated by t1w-to-mni QC" or "gated by bold-to-t1w QC" need no action. |
| WorkflowRun schema v1.1 | 100/100 | Image SHA in `metrics-exit-handler` WorkflowTemplate; re-submit affected subjects |
| Kubecost API cost attribution | 100/100 | Widen `--window-end`; confirm `subjectid` pod label is set in master WorkflowTemplate |
| Athena cost rows | 100/100 | Re-trigger scraper against `--bucket cloudpipe-metrics`; confirm the column is declared in `terraform/modules/metrics/` (there is no crawler) |
| Batch cost statistics | always ✓ (n = subjects with cost data) | Mean >> $0.35 → investigate outliers; mean << $0.20 → check cost scraper window, or a stale `metrics/costs/` backlog that the Step 1 flush didn't clear |

Any FAIL on the first six checks requires investigation before proceeding to a production run.

---

## Handling failures

### Retries

All spot-exposed workflow templates retry automatically on spot interruption (`pod deleted`, `imminent node shutdown`, exit codes 64/143) and on exit 75, the `EX_TEMPFAIL` guard the fastsurfer templates raise when a reclaim truncated their output. The budget is 8 attempts with exponential backoff from 1 minute, each delay capped at 5 minutes.

**The budget is sized for infrastructure, not for the workload.** Every nodepool is spot-only, so consecutive reclaims on a single long step are routine and 8 attempts exist to absorb them. Exit **137 is deliberately not retried**: a genuine OOMKill in this pipeline has always been deterministic (#120, #129, #134 each needed a memory or partitioning change, and no number of retries would have helped), so retrying it would burn the budget re-running an identical failure. If you find yourself wanting 137 back in the expression, the real fix is almost certainly a memory request or a step split. See issue #115.

The short `cpu-light` steps (`globus-transfer`, `inventory`) are the exception — they have no expression at all and a limit of 2–3, so they retry on *any* failure. They used `retryPolicy: OnFailure`, which silently dropped every reclaimed pod (phase `Error`, not `Failed`) and cost 4 subjects zero retries; [#269](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/269) moved them to `Always`.

**The codes are matched in the node message as well as in `exitCode`, and that is not belt-and-braces — it is load-bearing.** Argo populates `outputs.exitCode` from the **main** container only. A pod that dies in its **init** container (where argoexec's artifact loader runs) has no `exitCode` at all, and one that dies in its `wait` container records `"0"`. Neither is in `["64","75","143"]`, so before [#277](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/277) every artifact-staging failure got exactly one attempt regardless of `limit: "8"` — which is how [#274](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/274) turned a transient staging fault into the permanent loss of all three sessions of one subject. The tell in `status.nodes` is a message reading `init: Error (exit code 64): …` next to an absent `exitCode`; the plain-text `64` in the message is exactly what made the dead arm look live.

**One staging failure is excluded from that widening**: `The specified key does not exist`. An artifact key is absent because its *producer* was skipped or failed, S3 is read-after-write consistent, and retrying the *consumer* never re-runs the producer — so those 8 attempts are ~32 minutes of backoff spent re-failing identically. This is the same rule that keeps 137 out, and the same rule as the exit-66 case in [pipelines.md](pipelines.md). Note the guard is keyed on `key`, not on `does not exist`: a wrong *bucket* is a config fault and still retries.

Verified against the live controller on 2026-08-15 with probe workflows whose input artifact pointed at a bad S3 key or a bad S3 bucket, `limit: "2"`:

| expression | probe | attempts |
|---|---|---|
| old | bad key | **1** (the bug: no retry on any staging failure) |
| new | bad key | **1** (the guard, deliberately) |
| new | bad bucket | **3** (initial + 2 — the retry path works) |

When editing the expression, keep the code alternation explicit — a looser `matches "exit code"` re-admits the 137 above, since a real OOMKill surfaces as `main: Error (exit code 137)` — and keep the parentheses around the `||` group, or the missing-key guard binds to the `exitCode` arm alone and stops applying. `tests/argo/test_retry_expression.py` pins all 10 copies to one canonical string and classifies 15 failure shapes captured from real payloads, translating the YAML expression to Python so operands *and* boolean structure come from the file; a reworded Argo message breaks the test rather than the retry path.

Failed workflows are not auto-resubmitted. Resubmit a failed workflow manually:
```bash
DEST_COLL=$(aws ssm get-parameter --name /cloudpipe/globus/collection-id --query Parameter.Value --output text)
SRC_COLL=$(aws ssm get-parameter --name /cloudpipe/globus/source-collection-id --query Parameter.Value --output text)
SRC_PATH=$(aws ssm get-parameter --name /cloudpipe/globus/source-base-path --query Parameter.Value --output text)

argo submit --from workflowtemplate/cloudpipe \
  -n argo-workflows \
  -p subjID=NDARINVXXXXXXXX \
  -p globus-source-collection-id=$SRC_COLL \
  -p globus-source-base-path=$SRC_PATH \
  -p globus-dest-collection-id=$DEST_COLL \
  -p globus-dest-base-path=/mmps_mproc \
  -p 'globus-scan-types=["T1w","T2w","rest","nback"]'
```

The inventory step will set `b2t_exists`, `func_exists`, and `fastsurfer-exists` flags, so only incomplete steps run.

### Sweeping a whole batch for resubmittable failures

Doing the above by hand is fine for one subject. For a batch, `src/resubmit_failed.py` ([#234](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/234)) finds every terminally-failed workflow, classifies each one, and re-drives only the ones that can plausibly succeed:

```bash
# Always dry-run first — it prints the plan and exits.
PYTHONPATH=src pixi run python -m resubmit_failed --since 2026-08-17T01:43:43Z \
  --subjects tools/cloudpipe_test_sample_300_fresh.csv

# Then apply. --max-active gates submission against the live concurrency ceiling.
PYTHONPATH=src pixi run python -m resubmit_failed --since ... --apply --max-active 300
```

`PYTHONPATH=src` is required — the `pythonpath` in `pytest.ini` applies under pytest only.

Two things about it are worth knowing before you trust the plan:

- **It classifies on repetition, not on the exit code.** Nine `exit 75`s are deterministic and get skipped; one `exit 75` is the `EX_TEMPFAIL` guard working and gets retried. Keying off the code alone gets both wrong, in opposite directions. Spot kills (`143`, `pod deleted`, `imminent node shutdown`) are exempt from the repetition rule entirely, because on spot-only nodepools they repeat *without* being deterministic — that exemption exists because the classifier called a healthy subject deterministic on two SIGTERMs on its first live run.
- **`The specified key does not exist` is retryable here even though the WorkflowTemplates' retry expression excludes it.** The two operate at different layers: an in-workflow retry of a consumer does not re-run its producer, so the key stays absent, but a *resubmission* re-runs the producer from the top. That was the real shape of [#274](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/274) — a spot kill orphaned a FastSurfer tree and the staging failures downstream were the symptom.

Resubmitting is cheap because every expensive stage gates on a `_complete.json` marker ([ADR 017](decisions/017-exploded-derivatives-over-tarballs.md)), so a re-driven subject skips whatever already published. Use `--exclude-subjects` for determinism the classifier cannot see, such as a session the [#248](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/248) guard rejects on every run.

The sweep reads node status via `argo list -o json`, not `kubectl` — see the warning under [Workflow status](#workflow-status).

### Diagnosing a failure

```bash
# See which step failed
argo get -n argo-workflows <workflow-name>

# Get logs from the failed pod
argo logs -n argo-workflows <workflow-name> <failed-pod-name>

# Get the full workflow event history
argo get -n argo-workflows <workflow-name> -o json | jq '.status.nodes[] | select(.phase=="Failed")'
```

---

## Updating code

### Running tests and CI checks

`.github/workflows/ci.yaml` runs on every pull request (and push to `main`) that touches `**.py`, `src/`, `tests/`, `images/shared/`, `images/fastsurfer/`, `images/freesurfer/`, `pytest.ini`, `pixi.toml`/`pixi.lock`, `terraform/`, `argo/workflows/`, `docs/`, or `mkdocs.yml`. Five independent jobs:

- **`python_lint`** — `ruff check .` and `ruff format --check .` over the **whole repo**.
- **`pytest`** — installs the [pixi](https://pixi.sh) environment (`pixi.toml`/`pixi.lock`) and runs the full `tests/` suite via `pytest.ini`.
- **`docs_build`** — `mkdocs build --strict` in the `docs` pixi environment. The only check that reads `docs/` as a *linked graph* rather than as prose, so it catches the drift a human reviewer misses: a renamed doc or reworded heading leaving live links pointing nowhere.
- **`terraform_lint`** — `terraform fmt -check -recursive`, `terraform init -backend=false -lockfile=readonly` + `terraform validate`, then `tflint --recursive` using `terraform/.tflint.hcl`. Provider binaries come from a cached `TF_PLUGIN_CACHE_DIR` keyed on `terraform/.terraform.lock.hcl`; see [Terraform provider pinning](#terraform-provider-pinning) for why that lock file is committed.
- **`argo-lint`** — `argo lint --offline argo/workflows/`, catching WorkflowTemplate schema errors before ArgoCD syncs them.

The `**.py` trigger is load-bearing for `python_lint`: because that job lints the whole repo, the trigger has to match any `.py` file anywhere. When it was a directory list instead, an unformatted file under an unfiltered path merged green and then failed the *next* PR that happened to touch a filtered path — blaming an innocent change for a break it never went near.

Run the same checks locally before pushing:

```bash
# Python lint — runs BOTH `ruff check .` and `ruff format --check .`, matching
# the two steps in CI's python_lint job. (It used to be `ruff check` alone, which
# is how 14 of the 18 CI failures in the five weeks to 2026-08-13 happened: all
# of them were the format check, with `ruff check` passing.) The granular tasks
# `ruff-check` and `fmt-check` still exist if you want one without the other.
pixi run lint

# Python test suite
pixi run test

# Terraform (from the terraform/ directory)
cd terraform
terraform fmt -check -recursive
terraform validate
tflint --init && tflint --recursive

# Argo templates (from the repo root)
argo lint --offline argo/workflows/

# Docs site — strict build (fails on broken links / stale anchors)
pixi run -e docs docs-build

# Docs site — live preview on http://localhost:8000
pixi run -e docs docs-serve
```

`--strict` on its own is not enough to catch a broken link: MkDocs' *default* severity for an unresolvable relative link is `INFO`, so a plain strict build exits 0 over a tree full of dead links. The `validation:` block in `mkdocs.yml` promotes those to warnings, and `--strict` then turns warnings into a failure. Both settings are required; changing either one silently disables the check.

Two consequences worth knowing before you edit a doc:

- **In-page anchors follow GitHub's slug algorithm, not MkDocs'.** These docs are read on GitHub as well as on the site, and the two slugify headings differently (GitHub turns an em dash into a *double* hyphen; MkDocs' default collapses it to one). `mkdocs.yml` sets `toc.slugify` to `pymdownx.slugs.slugify(case="lower")`, which reproduces GitHub's algorithm so one setting keeps both renderings valid. Don't "fix" an anchor link by hand — that fixes the site and breaks GitHub.
- **Links out of `docs/` must be absolute GitHub URLs.** A relative `../terraform/...` link resolves on GitHub but 404s on the published site, which is rooted at `docs/`. Point at `https://github.com/jrussell9000/cloudpipe/blob/main/...` (or `/tree/main/` for a directory) instead. The same applies to the internal-only docs listed in `exclude_docs` — they are still synced to the public repo, so a published page linking to one needs the absolute form.

Neither job installs pip/conda dependencies outside `pixi.toml` — if a test needs a new package, add it to `pixi.toml` (and regenerate `pixi.lock` with `pixi install`) rather than installing ad hoc.

### Terraform provider pinning

`terraform/.terraform.lock.hcl` **is committed**, and CI runs `terraform init -lockfile=readonly` so a stale lock fails the build instead of being silently rewritten inside the runner.

It was previously gitignored ("don't need terraform locks"). The cost of that showed up on 2026-08-12, when `terraform_lint` went red on `main` with:

```
Error while installing gavinbunney/kubectl v1.19.0: could not query provider registry
  ... failed to retrieve authentication checksums for provider:
  Get ".../terraform-provider-kubectl_1.19.0_SHA256SUMS": context deadline exceeded
```

With no lock file, every `init` re-resolves versions from the registry and re-fetches each provider's `SHA256SUMS` to establish trust — a live network dependency on ten providers' release hosting. `gavinbunney/kubectl` is the fragile one: it is a community provider served from **GitHub Releases** rather than the HashiCorp CDN, so it adds GitHub's availability to the critical path. With the lock committed, `init` verifies cached binaries against recorded hashes and never makes that call.

A plugin cache alone would not have fixed this — the cache saves the provider *zip*, not the metadata round-trip. Both halves are needed.

**Bumping a provider** is now explicit. From `terraform/`:

```bash
terraform init -upgrade
terraform providers lock \
  -platform=linux_amd64 -platform=linux_arm64 \
  -platform=darwin_arm64 -platform=darwin_amd64
```

Then commit the updated `.terraform.lock.hcl`. The four platforms matter: a lock generated for only your machine makes `init` fail for everyone on a different OS/arch, CI included.

### Editing preproc.py (AFNI functional preprocessing)

`preproc.py` lives in `images/afni/preproc.py` and is baked into the `afni` image at build time — there is no runtime ConfigMap override. Editing it triggers a normal image rebuild (GitHub Actions rebuilds on changes to `.py` files under `images/afni/`, and the WorkflowTemplate's SHA reference is updated automatically).

```bash
# 1. Edit images/afni/preproc.py

# 2. Commit and push
git add images/afni/preproc.py
git commit -m "update preproc.py: ..."
git push
# GitHub Actions rebuilds the afni image and updates the SHA-pinned reference
```

### Updating a WorkflowTemplate

WorkflowTemplates are managed by ArgoCD (`selfHeal: true`). Manual `kubectl apply` will be reverted within seconds.

**If no workflows are currently running using the template:**

```bash
# Just commit and push — ArgoCD applies it automatically
git add argo/workflows/cloudpipe_minproc/<template>.yaml
git commit -m "update template: ..."
git push
```

**If workflows are running:**

```bash
# 1. Find running workflows using the template
argo list -n argo-workflows --running

# 2. Terminate them (they can be resubmitted after the update)
argo terminate -n argo-workflows <workflow-name>

# 3. Commit and push the template change
git add argo/workflows/cloudpipe_minproc/<template>.yaml
git commit && git push

# 4. Resubmit
DEST_COLL=$(aws ssm get-parameter --name /cloudpipe/globus/collection-id --query Parameter.Value --output text)
SRC_COLL=$(aws ssm get-parameter --name /cloudpipe/globus/source-collection-id --query Parameter.Value --output text)
SRC_PATH=$(aws ssm get-parameter --name /cloudpipe/globus/source-base-path --query Parameter.Value --output text)

argo submit --from workflowtemplate/cloudpipe \
  -n argo-workflows \
  -p subjID=NDARINVXXXXXXXX \
  -p globus-source-collection-id=$SRC_COLL \
  -p globus-source-base-path=$SRC_PATH \
  -p globus-dest-collection-id=$DEST_COLL \
  -p globus-dest-base-path=/mmps_mproc \
  -p 'globus-scan-types=["T1w","T2w","rest","nback"]'
```

### Updating Prefect flow code

Flow code is baked into the `cloudpipe-flow-runner` Docker image.

```bash
# Option A: push to main (triggers GitHub Actions automatically)
git push
# GitHub Actions builds and pushes public.ecr.aws/l9e7l1h1/cloudpipe/cloudpipe-flow-runner:latest
# If prefect.yaml also changed, the job summary will warn that prefect deploy --all is needed.

# Option B: manual local build + deploy (from repo root)
bash images/prefect-flow-runner/build.sh
# This builds, pushes, and runs prefect deploy --all in one step.
```

`prefect deploy --all` is only needed when `prefect/prefect.yaml` changes (adding deployments, changing parameters, etc.). Changing flow logic in `.py` files only requires a new image push.

### Updating infrastructure (Terraform)

```bash
cd terraform
terraform plan
terraform apply
```

Never use `-chdir=terraform` — run from within the `terraform/` directory.

PRs touching `terraform/` are checked by CI (`terraform fmt`, `terraform validate`, `tflint`) — see [Running tests and CI checks](#running-tests-and-ci-checks) above.

### Syncing to the public repo

[jrussell9000/cloudpipe](https://github.com/jrussell9000/cloudpipe) is the public, institution-neutral version of this repo. It contains the same pipeline code with all deployment-specific values replaced by `<YOUR_*>` placeholders (account IDs, bucket names, domain names, Globus UUIDs, etc.).

**Automated sync (normal path)**

The `.github/workflows/sync-public.yaml` workflow runs automatically on every push to `main` that touches a synced path. It opens a PR on the public repo from the `sync/from-internal` branch. Review and merge the PR to publish the changes.

Synced paths: `argo/`, `gitops/`, `images/`, `prefect/`, `terraform/modules/`, `docs/`, `src/`, `scripts/`, `README.md`, `LICENSE`, `pixi.toml`, `pixi.lock`, `mkdocs.yml`.

Not synced: root-level Terraform files (contain account-specific resource definitions), `.github/`, `packer/`, `tools/` (data & reference files only), `CLAUDE.md`. `scripts/sync-public.sh` is also excluded — it's the sync tool itself. Local build artifacts and caches (`.terraform/`, `.pixi/`, `.venv/`, `__pycache__/`, …) and secret material (`.env`, `*.tfvars`, `*.tfstate`, `keys/`) are excluded even if present in the working tree.

**Deleting is not automatic outside a synced directory.** `rsync --delete` prunes only *within* each `SYNC_DIRS` entry, so removing a file from `docs/` does propagate, but removing a whole directory from `SYNC_DIRS` leaves the public copy in place forever. `tools/` was exactly that: ADR 013 moved its contents into `scripts/` and `scripts/manifests/`, and the public repo carried both copies until 2026-08-10, the stale one indistinguishable from current code to an outside reader.

So dropping a path from `SYNC_DIRS` is a **two-step** change: remove it there *and* add it to `SYNC_ORPHANS`, which is force-deleted from the public tree on every run. (Entries are validated against absolute paths and `..` traversal before deletion — the loop runs `rm -rf` unattended in CI with a write token.)

Names in `SYNC_FILES` that don't exist locally now log a `WARNING` instead of being skipped silently. That silent skip is why the public repo had **no front page for ~75 days**: `README.md` was correctly listed, the file simply didn't exist internally yet, and nothing reported it. Note also that `pixi.lock` must always travel with `pixi.toml` — publishing a current manifest beside a stale lockfile makes `pixi install` resolve to something nobody tested.

**The sync fails closed.** After scrubbing, two gates run before anything is published; either one exiting non-zero aborts the sync (and, in CI, fails the job before the PR is opened):

1. **Pattern verifier** (in `sync-public.sh`): scans the staged tree for known-sensitive patterns — the AWS account ID, the institution domain, data-bucket names (`abcd-v*`), any Globus UUID, and personal (`@gmail.com`) emails. A surviving match prints `file:line:match` and the fix hint, then exits 1.
2. **Secret scanner** (CI): `gitleaks detect --no-git` over the staged tree, as a backstop for keys/tokens/high-entropy strings the pattern list doesn't anticipate.

**The published documentation site**

The public repo builds `docs/` into a GitHub Pages site with MkDocs Material. `mkdocs.yml` is synced, so the site's structure and its curation boundary (`exclude_docs`) are maintained here, in this repo, alongside the docs themselves — there is no second copy to keep in step.

The Pages workflow itself is **not** synced, and can't be. Two reasons: `.github/` is outside `SYNC_DIRS` on purpose (the internal CI needs AWS credentials and the public repo needs none of it), and GitHub rejects a PAT-authenticated push that touches `.github/workflows/` unless the token carries the `workflow` scope — so auto-syncing it would break the entire sync job, not just itself. It is therefore installed once, by hand, from a template kept in `scripts/`:

```bash
cp scripts/public-pages-workflow.yaml /tmp/cloudpipe-public/.github/workflows/pages.yaml
# then in the public repo: Settings → Pages → Source: "GitHub Actions"
```

After that it is self-maintaining — any sync touching `docs/` or `mkdocs.yml` rebuilds the site on merge. It runs the *same* `pixi run -e docs docs-build` as the internal `docs_build` CI job, so the published site can't be produced by a different toolchain than the one that gated it, and a docs problem should always surface internally first.

**Manual sync**

```bash
# Clone the public repo if you don't have it locally
git clone https://github.com/jrussell9000/cloudpipe /tmp/cloudpipe-public

# Run the sync script from the internal repo root. It scrubs, then runs the
# fail-closed verifier; a non-zero exit means a sensitive value survived and
# the public clone was left in a partially-updated state — do not commit it.
bash scripts/sync-public.sh /tmp/cloudpipe-public

# Review, commit, and push (only if the script exited 0)
cd /tmp/cloudpipe-public
git diff --stat
git add -A && git commit -m "sync from internal"
git push
```

**Adding a new deployment-specific value**

If you add a new hardcoded value (account ID, domain, email, bucket, UUID, etc.) to any synced file:

1. Prefer removing it at the source — lift it to a Terraform `variable`/`local` or a Terraform-published ConfigMap so the public copy carries a reference, not a literal. Nothing to scrub is the strongest guarantee.
2. If it must stay a literal, add a `"literal|<YOUR_PLACEHOLDER>"` entry to the ordered `REPLACEMENTS` array in `scripts/sync-public.sh`. **Order matters** — put any pattern that is a substring of a more general one *before* that general one.
3. If the value is genuinely sensitive (not just deployment-specific), also add a matching pattern to `VERIFY_PATTERNS` in the same script so the fail-closed gate catches future omissions.

**A gate fired — what now?** The script names the offending `file:line`. Apply step 1 or 2 above for that value, then re-run. The public repo is not updated until both gates pass.

---

## Step names: which one to use where

One pipeline step goes by four different names. Using the wrong one does **not**
error — the filter matches nothing and the analysis quietly comes back empty or short.

| Where | `long-segmentation` appears as |
|---|---|
| Argo template `name:` (workflow YAML, `tests/argo/`) | `fastsurfer-long-segmentation-template` |
| `cloudpipe.io/step` label = pod-costs `step` = Athena | `long-segmentation` |
| Prose in issues, handoffs, ADRs | `fastsurfer-long-segmentation` |
| Pod name in logs and the S3 log archive | `cloudpipe-<id>-fastsurfer-long-segmentation-template-NN` |

**`cloudpipe.io/step` is canonical.** The Kubecost scraper stamps it onto every
pod-cost record, so it is the key all metrics, Athena queries and cost comparisons
are written against. Prefer it in prose too. Do **not** rename these labels to match
the template names — they are what the historical metrics corpus is keyed by, and
renaming silently destroys comparability with every prior batch.

There is no string rule between the template name and the step label: **10 of 17**
templates disagree with "strip the `-template` suffix" (`workflow-exit-handler` ->
`workflow-run-metrics` shares no words at all). Derive it instead:

```python
from workflow_steps import step_for_template, step_to_template
step_for_template("fastsurfer-long-segmentation-template")  # -> "long-segmentation"
step_to_template()["long-segmentation"]  # -> "fastsurfer-long-segmentation-template"
```

`step_for_template` raises on an unknown name rather than returning `None`, because
a silent miss is the whole failure mode.

### Querying Argo node status: the `templateRef` trap

Argo records a pod's template name in **one of two fields** depending on how the
step was invoked — `templateName` for a template in the same WorkflowTemplate, and
`templateRef.template` when it is called across WorkflowTemplates. Most steps here
are cross-referenced, so filtering on `.templateName` alone drops them silently: in
the 2026-08-01 batch that was **25 of 31 pods**, returning zero rows without error.

```bash
# WRONG — silently misses every cross-WorkflowTemplate step
argo -n argo-workflows get <wf> -o json \
  | jq -r '.status.nodes[] | select(.templateName=="fastsurfer-long-segmentation-template")'

# RIGHT
argo -n argo-workflows get <wf> -o json \
  | jq -r '.status.nodes[] | select((.templateName // .templateRef.template)=="fastsurfer-long-segmentation-template")'
```

Use Argo node status (not pod-costs) when you need per-pod **timing**: pod-cost
records carry `completed_at`, but that is the *scrape* timestamp and is identical
across every pod in a scrape — it cannot be used to reconstruct concurrency.

---

## Viewing QC metrics

### Grafana dashboards

Open **https://grafana.<YOUR_DOMAIN>**. Eight dashboards are provisioned — six Athena-backed and two Prometheus-backed:

| Dashboard | Datasource | What to check |
|-----------|-----------|--------------|
| Pipeline Throughput | Athena | Success rate, failure count, mean duration — use to assess batch health |
| Functional QC | Athena | Flag runs with `pct_fd_above_0p5 > 10` (high-motion, in % of frames) or low tSNR |
| Anatomical QC | Athena | Outlier brain volumes, extreme cortical thickness, and fsqc anatomical QC |
| Registration QC | Athena | T1w→MNI folding and round-trip gates; BOLD→T1w `nmi_gain` |
| Failure Triage | Athena | Per-step failure counts and exit codes |
| Cost Overview | Athena | Daily spend trends, per-run cost distribution |
| Infra Health | Prometheus | Node/pod health, resource saturation |
| Karpenter | Prometheus | Provisioning latency, node churn, spot reclaims |

All dashboards default to a 30-day time range; adjust the top-right time picker as needed.

Dashboard JSONs live in `gitops/apps/grafana/dashboards/` and are provisioned as ConfigMaps by ArgoCD — **edit them in git, not in the Grafana UI**, or the next sync reverts your change.

### Python (Athena)

```python
import csv

from metrics.athena import CloudpipeMetrics

# The metrics bucket, not the derivatives bucket — metrics/ moved to its own
# versioned bucket so QC survives a derivative flush.
m = CloudpipeMetrics(bucket="cloudpipe-metrics")

# High-motion runs
df = m.func_qc(task="task-rest")
bad = df[df["pct_fd_above_0p5"].astype(float) > 10][["subject", "session", "run", "pct_fd_above_0p5", "mean_fd"]]

# Registration outliers.
#
# NOT `dice`: that field is a dead 0.0 for both registration types since schema
# 2.0, so a `dice < 0.85` filter matches every row. Its successor `mask_dice` is
# recorded but deliberately NOT gated — whole-brain overlap saturates for
# affine+SyN to a template (mean 0.9818, sd 0.0019), so any threshold that can
# fire at all sits ~17 sd below the mean. Gate on the failure modes instead:
reg = m.registration_qc(registration_type="t1w_to_mni")
folded = reg[reg["jac_det_frac_negative"].astype(float) > 0.005]   # deformation folding
poor_lncc = reg[reg["lncc"].astype(float) < 0.65]                  # intensity agreement
# NOT `ice_mean_mm`: it is never populated (fireants cannot invert a SyN fit), so
# the column is empty on every partition and any filter on it matches nothing.

# For bold_to_t1w the single gate is nmi_gain — how much NMI the fitted transform
# buys over identity. Note raw nmi ~1.02 is a GOOD score here (identity ~1.011),
# so do not threshold on nmi itself.
b2t = m.registration_qc(registration_type="bold_to_t1w")
no_gain = b2t[b2t["nmi_gain"].astype(float) <= 0]

# Per-subject cost for one batch.
#
# CostAllocation is one record per Argo workflow per scrape date — NOT one per
# subject — and metrics/costs/ retains records from earlier batches. m.costs()
# alone therefore returns a mix of runs at the wrong grain. Scope by the batch
# subject list AND the scrape-date window, and let subject_costs() aggregate:
batch = [r["subject_id"] for r in csv.DictReader(open("tools/cloudpipe_test_sample.csv"))]
costs = m.subject_costs(subjects=batch, date_from="2026-06-29", date_to="2026-07-02")
print(costs["total_cost_usd"].describe())   # one row per subject

# Raw workflow-day records, if you need the per-workflow breakdown:
raw = m.costs()
```

The scraper runs at 02:00 UTC and writes the *previous* day's costs, so a workflow that ran late on
day D is usually attributed scrape date D+1 — set `date_to` one day past the end of the run window.
`subject_costs()` exists on both `metrics.athena` and `metrics.duckdb_query` with the same
signature.

See [observability.md](observability.md) for the full querying guide, schema reference, and annotated SQL examples.

---

## Cost monitoring

### Grafana Cost Overview dashboard

The **Cost Overview** dashboard at https://grafana.<YOUR_DOMAIN> shows daily spend, mean cost per subject, and a cost-by-subject table. Data is populated nightly by the Kubecost scraper (Prefect flow `kubecost-cost-scraper`, 02:00 UTC) once the `subjectid` pod labeling work is confirmed.

### Live Kubecost UI

For real-time or intra-day cost breakdowns, use the Kubecost UI at https://kubecost.<YOUR_DOMAIN> (Allocations → Group by `subjectid` or namespace).

---

## Globus EC2 instance

The Globus Connect Server runs on an EC2 instance that is started automatically by the `start-globus-instance-template` step at the beginning of each workflow. It stays running until manually stopped.

```bash
# Get the instance ID
aws ssm get-parameter --name /cloudpipe/globus/instance-id --query Parameter.Value --output text

# Stop the instance when no transfers are running
aws ec2 stop-instances --instance-ids <instance-id>
```

If the instance is replaced, update SSM:
```bash
aws ssm put-parameter \
  --name /cloudpipe/globus/instance-id \
  --value <new-instance-id> \
  --overwrite

# Also update the collection UUID if it changed
aws ssm put-parameter \
  --name /cloudpipe/globus/collection-id \
  --value <new-collection-uuid> \
  --overwrite
```

The Prefect queue manager reads the collection UUID from SSM at runtime, so in-flight flow runs pick up the new value automatically on the next subject submission.

---

## Checking cluster health

```bash
# Node pool status
kubectl get nodes -L karpenter.sh/nodepool

# ArgoCD sync status
kubectl get applications -n argocd

# Pending pods (scheduling issues)
kubectl get pods -n argo-workflows --field-selector=status.phase=Pending

# Recent workflow events
kubectl get events -n argo-workflows --sort-by='.lastTimestamp' | tail -20
```

### SSM session (privileged access to a node)

```bash
aws ssm start-session --target <instance-id>
# Once connected:
sudo -i
# Then run privileged commands
```

Always run `sudo -i` first before any privileged command in an SSM session.
