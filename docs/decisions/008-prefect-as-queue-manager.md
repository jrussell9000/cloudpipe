# 008 — Prefect flow as Argo submission queue manager

**Status**: Accepted

## Context

The pipeline must process a large batch of subjects (hundreds to thousands) against the Argo Workflows API. Submitting all workflows at once is not viable: the Argo controller's `resourceRateLimit` caps pod creates at 20/second with a burst of 35, and the Globus semaphore caps concurrent transfers at 8. Submitting hundreds of workflows simultaneously fills the Argo queue with pending pods, exhausts the semaphore immediately, and makes it difficult to monitor progress or pause mid-batch.

Several approaches were considered:

1. **Argo CronWorkflow** — schedules periodic workflow submissions but does not support stateful queue management (tracking which subjects have been submitted, respecting a dynamic concurrency limit)
2. **Argo Events** — event-driven architecture that can trigger workflows from an SQS queue or other source; adds significant configuration complexity and requires an SQS queue to be populated before running
3. **Shell script with `argo submit` loop** — simple but brittle; no retry on submission failure, no persistent state, dies if the shell session ends
4. **Prefect flow** — Python code running as a Kubernetes pod on the `cloudpipe-k8s-pool` work pool; durable execution, parameter-driven, observable via the Prefect UI, can be paused and resumed

## Decision

Use a Prefect flow (`cloudpipe_queue_manager.py`) as a long-running drip-feed controller. The flow:
- Reads a CSV of subject IDs from S3 or a local path
- Tracks position via `start_index`/`end_index` parameters for explicit resume
- Gates concurrency in-process: calls `count_running()` (Argo API `list_workflows` with `fields=items.metadata.name`) and waits until the count drops below `max_concurrent` before submitting the next subject
- Submits each subject via the Argo `submitWorkflow` API (not `kubectl` or `argo` CLI)

The same pattern is replicated for `first_level_queue_manager.py`, which additionally checks S3 for existing outputs before submitting.

`count_running()` counts **all** active Argo workflows, not just those from a specific pipeline. If both pipelines run simultaneously, `max_concurrent` must account for the combined load.

## Consequences

- The queue manager is itself a long-running Kubernetes pod; it can run for hours or days without a persistent shell session
- Pausing requires cancelling the Prefect flow run (via the Prefect UI) and resuming with an adjusted `start_index`
- `count_running()` is a coarse gate — it counts workflows, not individual pipeline steps; a subject with 10 sessions contributes the same count as one with 2 sessions, so actual cluster load can vary significantly at the same `max_concurrent` value
- No per-subject completion tracking: if the flow is restarted from index 0, it will resubmit all subjects; for first-level, the `is_completed()` S3 check prevents duplicate work; for cloudpipe, the inventory step's skip logic handles duplicate submissions gracefully
- The flow runs as a Prefect deployment on the `cloudpipe-k8s-pool` Kubernetes work pool; code changes require a new Docker image build and `prefect deploy --all` (see `docs/images.md`)
- Changing `max_concurrent` or `poll_interval` requires cancelling the current run and resubmitting with new parameters — there is no hot-reconfiguration
