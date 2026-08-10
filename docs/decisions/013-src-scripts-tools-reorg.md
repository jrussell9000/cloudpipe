# 013 — Split `tools/` into `src/`, `scripts/`, and data-only `tools/`

**Status**: Accepted

## Context

`tools/` had become a grab-bag: importable library code (`metrics/`, `queue_creation/`, `inventory.py`, `validate_test_batch.py`) sat next to one-off scripts, a notebook, k8s debug manifests, and reference data/CSV files. `pytest.ini` worked around this with a `pythonpath` block pointing directly at `tools` and `tools/metrics`, rather than a proper `src/` layout.

Runtime imports already used bare names (`from metrics.athena import …`), so a reorg that kept those names required no import-statement changes — only file moves and build/config path updates.

## Decision

Split by role:

- `src/` — importable library code, tested and imported by images/tests (`metrics/`, `queue_creation/`, `inventory.py`, `validate_test_batch.py`)

  > **Current contents differ.** `src/queue_creation/` was deleted in `357b853` along with the
  > Batch-era job-tracking and NDA scripts; `pullsamplestats.py` and `workflow_steps.py` have since
  > been added. `src/` today is `metrics/`, `inventory.py`, `validate_test_batch.py`,
  > `pullsamplestats.py`, `workflow_steps.py`. The *split by role* this ADR decided is unchanged —
  > only the file list moved on.
- `scripts/` — runnable helpers: shell scripts, one-off Python scripts, the `computeNumNSS.ipynb` notebook, and `scripts/manifests/` for ad-hoc k8s debug manifests
- `tools/` — retained temporarily as a data-only directory (CSVs, NIST assessment doc); moving these to S3 `config/` is deferred

`pytest.ini`'s `pythonpath` block now points at `src` and `src/metrics` instead of `tools` and `tools/metrics`.

## Consequences

- Dockerfiles (`images/python`, `images/prefect-flow-runner`) updated to `COPY` from `src/...`; `prefect-flow-runner` requires a rebuild via `images/prefect-flow-runner/build.sh` since its flow code is baked into the image.
- The mirrored `tools/metrics/schemas.py` path reference in `images/afni/preproc.py` and the ArgoCD-synced `preproc-script-configmap.yaml` was updated to `src/metrics/schemas.py` — configmap changes go through git commit+push, never `kubectl apply` (ArgoCD selfHeal owns `argo/workflows/`). The configmap half of this is now moot: `preproc-script-configmap.yaml` and `scripts/gen-preproc-configmap.sh` were both deleted when [ADR 005](005-preproc-py-in-configmap.md) was superseded.
- Doc references under `docs/` updated to the new paths.
- `tools/` still holds large reference CSVs (e.g. `nss_volumes.csv`, `subjectids_v611.csv`); moving these to S3 is deferred to a follow-up.
