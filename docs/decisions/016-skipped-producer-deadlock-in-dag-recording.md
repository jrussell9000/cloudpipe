# 016 — Never let a DAG task reference a skipped producer's outputs or exit code

**Status**: Accepted

## Context

`cloudpipe-long-master-workflow-template.yaml` records a `StepOutcome` for
most substantive pipeline steps via a sibling DAG task:

```yaml
depends: X || X.Failed || X.Skipped
templateRef: {name: outcome-recorder, template: record-step-outcome}
```

The bare `X` in Argo's `depends` expression already means
`X.Succeeded || X.Skipped || X.Daemoned`, so this pattern runs the recorder
after `X` regardless of how it terminated, including when `X` was skipped
entirely (e.g. a rerun where every output already exists).

On 2026-07-22 (`dagval-330e63gh-tarerr`), a recorder wired to read
`{{tasks.X.status}}` verbatim recorded the wrong thing in both directions: a
partial failure inside a session pod (some runs failed, the pod itself
survived) recorded every run — including the ones that succeeded — as
whatever the pod's aggregate status was. The fix was to derive per-run status
from each run's own output rather than trust the aggregate
(`resolve_run_status` in `src/metrics/outcome_recorder.py`).

The natural next step — have the *producer* task expose its own per-run
failure list as an output parameter, and have the recorder read
`{{tasks.X.outputs.parameters.failed-runs}}` — turned out to be unsafe. On
2026-07-23 (`dagval-330e63gh-tarerr2`), wiring a recorder task's arguments to
reference a producer's output parameter caused the parent DAG to hang
indefinitely whenever the producer was **skipped**: a skipped task has no
outputs, Argo never instantiates a downstream task whose arguments cannot be
resolved, and the DAG then waits forever for a task that will never exist.
`depends: X || X.Failed || X.Skipped` looks like it handles the skipped case,
but it only controls *when* the downstream task is considered — it does not
change the fact that the task's *arguments* fail to resolve for a producer
with no outputs.

The same hazard applies to `{{tasks.X.exitCode}}`: Argo has no
`{{tasks.X.message}}` variable at all, and exit code has the identical
omission problem for a skipped producer.

This rationale was previously recorded only in scattered YAML comments across
the master and registration templates, and in the `outcome_recorder.py`
module docstring. (Those comments still exist and now cite this ADR by
number; line numbers are deliberately omitted here because they move with
every refactor.) Comments in a
file about to be refactored are easy to delete along with the code around
them, which is exactly the failure mode this ADR exists to prevent.

## Decision

**A DAG task's `arguments` must never reference
`{{tasks.<name>.outputs.parameters.*}}` or `{{tasks.<name>.exitCode}}` for a
producer task that can legitimately be skipped**, regardless of how that
task's `depends` clause is written. `depends` gates instantiation; it does
not guarantee the referenced fields exist.

Concretely:

1. The only Argo task-status field safe to pass to a downstream task
   unconditionally is `{{tasks.<name>.status}}` — it is always defined, even
   for a skipped task (`Skipped`).
2. Any finer-grained detail than the aggregate status — per-run failure
   lists, exit codes, failure messages — must be derived either:
   - **inside the producer pod itself**, and recorded directly by that pod
     (preferred — this is now the shipped design; see issue #66, closed), or
   - **from evidence external to the DAG graph**, such as verifying expected
     S3 output keys exist (`resolve_run_status`'s current approach) — which
     works precisely because it does not depend on Argo exposing anything
     about the producer task at all.
3. Every DAG task that fires unconditionally after a step it does not
   control (`X || X.Failed || X.Skipped`) must be tested against a
   **fully-complete rerun** — one where every upstream step is skipped
   because its output already exists — as a standing regression case. A
   redesign that passes on a fresh subject can still deadlock on a rerun,
   which is the shape both incidents above took.

## Consequences

- This constraint, not cost or template-size concerns, is why per-run
  outcome fan-outs historically re-derived status from S3 (`head_object`)
  rather than reading the producer's own output parameters — the design in
  `outcome_recorder.py` predates this ADR but is the reference
  implementation of point 2 above.
- It directly enabled moving outcome recording into worker pods, which has
  **since shipped** (issue #66, closed): a pod recording its own outcome
  before it exits is not a *DAG task referencing another task's outputs* — it
  is a task reporting facts it computed itself — so this constraint does not
  apply to it. `functional-preprocessing-`, `registration-` and
  `surface-resample-workflow-template.yaml` each define an inline
  `write_outcome()` and a `step-outcomes` output artifact writing straight to
  `metrics/step-outcomes/dt=…/`, so the per-run recorder pods are gone.
- The standalone `outcome-recorder` WorkflowTemplate is retained for
  aggregate, phase-level records and for the cases a worker pod cannot cover.
  Both gate shapes are in use and both are legal under this ADR, because
  neither references outputs or exit codes:
  - the four phase-level recorders in the master DAG
    (`fsqc-metrics`, `subregion-segmentation`, `anatomical-phase`,
    `session-phase`) fire on the explicit
    `X.Succeeded || X.Failed || X.Skipped` triple and pass only
    `{{tasks.X.status}}`
  - the per-run recorders that survive
    (`record-outcome-func-preproc-dagtask`,
    `record-outcome-surface-sample-dagtask`,
    `record-outcome-surface-resample-dagtask`) fire on
    `X.Failed || X.Skipped` only — never on bare `X` — because the pod
    already recorded its own successes
- The master-template refactor (issue #73, closed) found a further
  constraint worth recording here: **Argo resolves a DAG task's `depends`
  statically, before `withItems`/`withParam` expansion**, so a looped
  recorder can carry only one shared `depends` string. Collapsing recorder
  blocks into a loop is therefore safe only where the producers are already a
  serial chain (the four `record-outcome-fastsurfer-*` blocks were merged on
  that basis), never where they run in parallel — merging parallel producers
  would delay the fast branch's outcome record until the slow branch also
  terminates, destroying early-failure visibility. The master template is now
  844 lines.
- Any future refactor must preserve this rule rather than "simplify" it back
  into a direct output/exitCode reference — that simplification is exactly
  what caused `dagval-330e63gh-tarerr2`.
