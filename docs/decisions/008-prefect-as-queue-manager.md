# 008 — Prefect flow as Argo submission queue manager

**Status**: Accepted

## Context

The pipeline must process a large batch of subjects (hundreds to thousands) against the Argo Workflows API. Submitting all workflows at once is not viable: the Argo controller's `resourceRateLimit` caps pod creates (50/second, burst 90 — see the note below), and the Globus semaphore caps concurrent transfers at 8. Submitting hundreds of workflows simultaneously fills the Argo queue with pending pods, exhausts the semaphore immediately, and makes it difficult to monitor progress or pause mid-batch.

> **Correction (2026-08-10, #206).** This paragraph originally cited a 20/second, burst-35 rate limit as an existing constraint. No such limit was in effect. `resourceRateLimit` was set in the Helm chart's `values.yaml`, but the chart renders it only into the controller ConfigMap, and this deployment sets `controller.configMap.create: false` because Terraform owns that ConfigMap — so the value went nowhere and pod creation ran at Argo's unlimited default. The same was true of `parallelism`. Both now live in `terraform/modules/argo-workflows/main.tf` and are actually in effect.

Several approaches were considered:

1. **Argo CronWorkflow** — schedules periodic workflow submissions but does not support stateful queue management (tracking which subjects have been submitted, respecting a dynamic concurrency limit)
2. **Argo Events** — event-driven architecture that can trigger workflows from an SQS queue or other source; adds significant configuration complexity and requires an SQS queue to be populated before running
3. **Shell script with `argo submit` loop** — simple but brittle; no retry on submission failure, no persistent state, dies if the shell session ends
4. **Prefect flow** — Python code running as a Kubernetes pod on the `cloudpipe-k8s-pool` work pool; durable execution, parameter-driven, observable via the Prefect UI, can be paused and resumed

## Decision

Use a Prefect flow (`cloudpipe_queue_manager.py`) as a long-running drip-feed controller. The flow:
- Reads a CSV of subject IDs from S3 or a local path
- Tracks position via `start_index`/`end_index` parameters for explicit resume
- Gates concurrency in-process: calls `ConcurrencyGate.count()` (Argo API `list_workflows` with `fields=items.metadata.name`) and waits until the count drops below the live `cloudpipe-max-concurrent` Prefect Variable (read via `Variable.get()` on every poll cycle, not passed as a flow parameter) before submitting the next subject
- Submits each subject via the Argo `submitWorkflow` API (not `kubectl` or `argo` CLI)

The same pattern is replicated for `first_level_queue_manager.py`, which additionally checks S3 for existing outputs before submitting.

The gate counts **all** active Argo workflows in the namespace, not just those from a specific pipeline. If both pipelines run simultaneously, the `cloudpipe-max-concurrent` / `first-level-max-concurrent` Variables must account for the combined load.

### The client gate is advisory; the controller is authoritative

A client-side gate polls state it does not own, so it is only ever as strong as the propagation delay of whatever it polls. #206 is the concrete failure: the gate filtered on `pipeline=cloudpipe,workflows.argoproj.io/phase in (Running,Pending)`, but **both** labels are stamped by the workflow controller asynchronously, after the create call has already returned. Mid-burst, 64 of 100 workflows had no labels and no `status` block at all, so both halves of a positive selector missed, the count read `0` on every iteration, and 100 workflows went out under a cap of 50 in ~31 seconds.

The gate is therefore layered:

1. **`namespaceParallelism` in the controller ConfigMap** is the real guarantee. The controller owns the state it counts, so no client burst can race it; excess workflows are held `Pending` until a slot frees. It is set to `400` — deliberately above the sum of both pipelines' Variables (cloudpipe's 300 target + first-level's 25, plus headroom), because it is namespace-wide: setting it to either pipeline's cap would silently hold the *other* pipeline's workflows Pending whenever the first was at capacity. It is a runaway backstop, not the working cap. It was `100` through the 100-concurrent pilots; at that value a 300-wide submission would run 100 and leave 200 `Pending`.
2. **The client gate** stays the working cap, since it is the only layer that is hot-reconfigurable per pipeline. Two changes make it race-free in practice: it selects on `workflows.argoproj.io/completed!=true` (a *negative* match, which Kubernetes evaluates as true when the key is absent — so an unreconciled workflow counts immediately), and it adds names it has itself submitted but not yet seen in a list response.

Choosing the negative selector also inverts the failure direction, which is the more important property: the old gate under-counted and overshot the cap, whereas this one can at worst briefly over-count a workflow that finished before its label updated — which only makes the gate wait.

## Consequences

- The queue manager is itself a long-running Kubernetes pod; it can run for hours or days without a persistent shell session
- Pausing requires cancelling the Prefect flow run (via the Prefect UI) and resuming with an adjusted `start_index`
- The gate is coarse — it counts workflows, not individual pipeline steps; a subject with 10 sessions contributes the same count as one with 2 sessions, so actual cluster load can vary significantly at the same concurrency cap. The equivalent read by hand is `kubectl -n argo-workflows get wf -l 'workflows.argoproj.io/completed!=true'`
- Concurrency is enforced in two places that must be kept consistent: the per-pipeline Prefect Variables and the namespace-wide `namespaceParallelism`. Raising a Variable above `namespaceParallelism` does not raise the effective cap — the surplus workflows are simply submitted and left `Pending`, which looks like a stalled batch rather than a rejected submission
- Only the in-process half of the gate knows about a submission the API has not listed yet, so it does not survive a flow restart. A restarted flow can briefly overshoot by however many workflows were submitted in the last poll window; `namespaceParallelism` is what bounds that
- No per-subject completion tracking: if the flow is restarted from index 0, it will resubmit all subjects; for first-level, the `is_completed()` S3 check prevents duplicate work; for cloudpipe, the inventory step's skip logic handles duplicate submissions gracefully
- The flow runs as a Prefect deployment on the `cloudpipe-k8s-pool` Kubernetes work pool; code changes require a new Docker image build and `prefect deploy --all` (see `docs/images.md`)
- Changing `max_concurrent` is hot-reconfigurable: `prefect variable set cloudpipe-max-concurrent <N>` (or `first-level-max-concurrent`) takes effect on the flow's next poll cycle, no restart needed. Changing `poll_interval`, however, is a true flow parameter and does require cancelling the current run and resubmitting with a new value.
