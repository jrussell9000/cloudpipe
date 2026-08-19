# Code-health & simplification plan

A staged plan for walking through the cloudpipe codebase, simplifying it where
possible, removing stale or low-value code, and doing so **without regressing
core functionality**. Written 2026-07-23; baseline re-measured 2026-08-10.

This is a living plan, not a one-shot task. Work it in order — each layer is
lower-risk than the next, and later layers depend on the map produced by
earlier ones.

> **Layer 1 is done and enforced.** The "why now" framing below was written when
> no static analysis existed. It does now, `ruff check` and `ruff format --check`
> are a **required CI job** (`python_lint` in `.github/workflows/ci.yaml`), and
> the tree is clean on both. What remains open is Layer 3 (coverage) and the two
> judgment-call items in [Status](#status) — not the tooling install.

## Why now — the original state (2026-07-23)

- **~11.8k lines of Python.** The density is concentrated in one hotspot:
  [`images/afni/preproc.py`](../images/afni/preproc.py) at **1406 lines / 25
  functions**. It is decomposed and named (not spaghetti), but several
  functions are long — `compute_confounds` (~196 lines), `apply_transforms`
  (~120), `sample_cortical_ribbon` (~116) — and it is **numpy + subprocess
  glue** (shelling out to AFNI/FreeSurfer), the hard-to-simplify,
  hard-to-unit-test kind of code.
- **~4.2k lines of Argo YAML**, including a
  [900-line master template](../argo/workflows/cloudpipe_minproc/cloudpipe-long-master-workflow-template.yaml).
  This is a *separate* density problem needing different tools (YAML/schema
  linting, template decomposition — not Python linters).
- **No static-analysis tooling is currently installed.** `pixi.toml` defines
  only a `pytest` task. Adding a lint/type/dead-code layer is the single
  biggest, cheapest win available.

### Where those numbers stand now (2026-08-10)

| Measure | 2026-07-23 | 2026-08-10 |
|---|---|---|
| Python, excluding `tests/` | ~11.8k lines | **~15.9k** |
| Python, including `tests/` | — | **~25.5k** (tests alone ~9.6k) |
| `images/afni/preproc.py` | 1406 lines / 25 functions | **1619 lines / 29 functions** |
| Argo YAML | ~4.2k lines | **~6.2k** |
| Master template | ~900 lines | **844** (issue #73 recorder merge) |
| Static-analysis tooling | none | ruff + mypy + vulture wired; ruff **enforced in CI** |

The hotspot got bigger, not smaller — but note the composition changed. Test
code grew fastest, and the master template is the one file that shrank. Growth
in `preproc.py` came from the surface/grayordinate work, not from the long
functions this plan targeted.

### Key principle: match the tool to the complexity *type*

`preproc.py`'s difficulty is (a) long imperative functions and (b) implicit
data contracts around numpy arrays and file `Path`s handed to subprocess. A
linter fixes style, not that. A **type checker** is what surfaces the hidden
contracts in scientific-Python glue code. Dead-code and dependency tools answer
the "stale code" question. Coverage is the safety net for the "retain
functionality" requirement.

## The layered toolchain

### Layer 1 — Automated hygiene (install first, near-zero risk)

| Tool | Role in this repo | Command |
|------|-------------------|---------|
| **ruff** | Linter + formatter + import sorter in one. `F401`/`F841` flag unused imports/vars — the cheapest stale-code signal. `C901` flags over-complex functions objectively. | `ruff check`, `ruff format` |
| **vulture** | Whole-tree dead-code detector — unreferenced functions/vars. Purpose-built for "remove code providing little benefit." | `vulture src/ images/ scripts/ prefect/` |
| **deptry** | Unused / missing / misplaced *dependencies*. | `deptry .` |
| **mypy** or **pyright** | Type-checks the numpy/`Path`/subprocess glue. Where the real comprehension wins live — forces implicit contracts in `preproc.py` to become explicit. | `mypy images/afni/preproc.py` |

**ruff vs. vulture answer different questions.** ruff finds names unused
*within a file*; vulture finds *definitions* nothing references anywhere. For
"identify stale code" you want vulture — but treat its output as a **lead, not a
verdict**: it cannot see names reached via Argo entrypoints, `getattr`, or
pytest fixtures, so it needs a per-repo allowlist. Verify each candidate against
how the pipeline actually invokes code before deleting.

### Layer 2 — Comprehension ("walk through the code")

- **pyright/mypy in the editor (LSP)** — inline go-to-definition and call
  hierarchy; the fastest way to trace `main()` → stages in `preproc.py`.
  Now also wired as a task: `pixi run -e lint typecheck` (baseline below).
- **`ruff check --statistics`** — ranked histogram of problem *kinds*: a map of
  where to spend effort.
- **radon** or ruff `C901` (mccabe complexity) — objectively ranks the long
  functions so you target `compute_confounds`, not a guess.
- **Argo YAML** (Python linters do not apply): **yamllint** + **`argo lint`**
  (schema validation). Treat the 900-line master template as a candidate for
  decomposition into referenced `WorkflowTemplate`s rather than inline DAGs.

### Layer 3 — Safety net (the "retain core functionality" requirement)

This matters most. Tests under `tests/` are substantial (~2.5k lines), but
`preproc.py`'s subprocess-shelling stages are hard to unit-test.

- **`pytest --cov` (coverage.py)** — see which stages of `preproc.py` are
  actually exercised. Refactoring uncovered code is where regressions hide.
- Any simplification of the AFNI/FreeSurfer glue should be guarded by a
  characterization test or a **real-subject validation run**, consistent with
  how surface-func work already gates on real-subject validation.

## Tooling already in this harness

- **`/simplify`** — reviews *changed* code for reuse/simplification/altitude and
  applies fixes. Use per-PR on the diff, not on the whole 1406-line file at once.
- **`/code-review`** and the **`code-simplifier`** subagent — for larger passes.
- **pre-commit** — ~~wire ruff + vulture + deptry~~ **done for ruff + vulture**
  (`.pre-commit-config.yaml` defines `ruff-lint`, `ruff-format`, `vulture` as
  local hooks). deptry is not hooked — it can't parse `pixi.toml` (see below).
  Note pre-commit is the *local* gate; CI's `python_lint` job is the enforcing
  one, since a contributor can skip hooks but not a required check.

## Recommended sequence

1. **Add ruff + a pre-commit config** → auto-fix imports/formatting tree-wide
   (mechanical, reviewable diff, no logic change).
2. **Run deptry** → prune stale deps from `pixi.toml` (see verified leads below).
3. **Run vulture with an allowlist** → candidate stale-code list; verify each
   against Argo entrypoints before deleting.
4. **Add mypy on `preproc.py` only** → surface hidden contracts; use that
   understanding to break up `compute_confounds`.
5. **Measure coverage before touching any subprocess stage.**

Do **not** batch these. Step 1 reformats repo-wide; steps 3–4 need human
judgment on what is genuinely stale.

## Verified dependency leads (2026-07-23)

Checked which declared `pixi.toml` deps are actually imported:

- **`pipreqs`** — never imported anywhere. It is a CLI dev tool (generates
  `requirements.txt`), not a runtime dependency. Candidate for removal or move
  to a dev-only feature.
- **`hera` vs `hera-workflows`** — both declared. The `hera` *module* (used in
  `prefect/flows/lib/argo.py`) ships from the modern `hera` package, so the
  legacy **`hera-workflows`** declaration is the redundancy to resolve. Confirm
  with `deptry` before removing.
- **`prefect`, `globus-sdk`, `hera`** — actively imported; **not** stale. Keep.

## Layer 1 baseline — first run results (2026-07-23)

Tooling wired into `pixi.toml` (`lint` feature/environment), `ruff.toml`,
`.pre-commit-config.yaml`, and `.vulture-whitelist.py`. Run with
`pixi run -e lint <task>`: `lint` (aggregate: `ruff-check` + `fmt-check`),
`ruff-check`, `fmt-check`, `deadcode`, `complexity`,
`health` (report-only); `format`, `lint-fix` (mutating). **No source code was
changed** — this is a measurement baseline.

### ruff — 171 findings, 60 auto-fixable

> **All resolved. `ruff check .` and `ruff format --check .` both pass clean as of
> 2026-08-10** (109 files formatted), and both run as a required CI job. The table
> below is the historical baseline. Two rows are still worth reading rather than
> deleting, because they encode decisions that would otherwise be re-litigated:
> the **F821 row** (closure captures in `apply_transforms` — still false positives,
> still must not be "fixed") and the **UP017 note** at the end of this document
> (blocked by the Python 3.10 image runtimes, not by ruff config).

| Rule | Count | Notes |
|------|------:|-------|
| E501 line-too-long | 77 | style; deferrable |
| I001 unsorted-imports | 36 | auto-fixable |
| E702 multiple-statements-semicolon | 13 | mostly `scripts/` |
| UP017 datetime-timezone-utc | 12 | auto-fixable modernization |
| **F821 undefined-name** | **8** | **FALSE POSITIVES** — closure captures in `apply_transforms` (`bold_data`, `out_data`, `mean_acc`, `M2_acc`), defined at [preproc.py:347-365](../images/afni/preproc.py#L347-L365). Do **not** "fix." |
| F401 unused-import | 4 | genuine; triage before removing |
| F841 unused-variable | 1 | `n_voxels` at [preproc.py:352](../images/afni/preproc.py#L352) — genuinely dead, safe to drop |
| C901 complex-structure | 1 | `parse_messages` (complexity 21) in `scripts/compileFirstLevelQCmetrics.py` |
| B023 loop-variable-binding | 1 | `scripts/computeNumNSS.ipynb` — worth a real look (possible bug) |

**58 of 67 files would be reformatted** by `ruff format`. (Done — the tree is
formatted and CI keeps it that way; there are 109 Python files now.)

**How each row was closed**, since "0 findings" can mean fixed *or* silenced:

- **E501** and **UP017** are `ignore`d in `ruff.toml`, deliberately. E501 as
  style-not-worth-churn; UP017 for the runtime reason at the end of this doc.
  These are suppressed, not fixed.
- **I001 / E702 / F401 / F841** were genuinely fixed (the import-sort and format
  PRs, plus manual triage).
- **F821** is no longer reported: `apply_transforms` was restructured so
  `bold_data`/`out_data` are ordinary locals rather than closure captures ruff
  couldn't see. Not suppressed — the shape that confused ruff is gone.
- **B018** is scoped off two file classes via `[lint.per-file-ignores]` —
  `.vulture-whitelist.py` (a whitelist *is* a file of bare expressions) and
  `*.ipynb` (a trailing bare expression is how Jupyter displays a value). Both
  are cases where the rule is right about the mechanism and wrong about intent.

### vulture — no genuine dead production code

- Prefect `@flow` entrypoints (`cloudpipe_queue_manager`,
  `first_level_queue_manager`, `kubecost_cost_scraper`) — framework-called;
  whitelisted.
- `CloudpipeMetrics.join_subject` — public API used by docs + tests; whitelisted.
- Remaining hits are `schemas.py` **dataclass fields** — a known vulture blind
  spot (fields serialized via `from_dict`/`to_dict`, not "read" in a way vulture
  sees). **Expected noise, not dead code.** Test-mock names
  (`side_effect`/`Bucket`/`kw`) are tuned out via `--ignore-names`.

This still holds on the 2026-08-10 run. Every remaining hit is either a
`schemas.py` dataclass field (`ice_p95_mm`, `cpu_cost_usd`, `gpu_hours`,
`node_instance_type`, …) or a test-local name — all at vulture's lowest,
60% confidence tier. **Do not act on this tool's output for `schemas.py`
without checking the field against `_UNION_COLUMNS` and Terraform first**: a
metrics field that looks unreferenced in Python is exactly what a *correctly
wired* emitted-and-queried field looks like, since the read happens in SQL.

### deptry — blocked

Cannot parse `pixi.toml` dependencies (needs `pyproject.toml [project]` or
`requirements.txt`). Not wired as a hook. **Decision needed:** add a minimal
`pyproject.toml`, or continue relying on the manual import audit above.

### The one genuine "little benefit" lead

Seven near-identical `to_json()` methods across the metrics schemas are used
**only by tests** — production emitters use `to_dict()` (14 call sites) exclusively.
Candidate for consolidation (single base-class impl) or removal, but it is
tested, so this is a deliberate simplification decision for later — not a bug.

**Still open, and now worse: there are nine.** `FsqcQC` and `PodCosts` each
arrived with their own copy. A `rg -n to_json` outside `tests/` still finds no
production caller. This is the clearest instance in the repo of a
copy-per-schema pattern that a base class would collapse — and every new metrics
table adds another copy, so the cost grows on its own. Note it interacts with the
[five-place field checklist](decisions/011-s3-athena-for-metrics.md): a base-class
`to_json()` would be one fewer place to duplicate per new table.

### Incidental fix applied

`.gitignore` `.pixi/*` was root-anchored, so a nested `images/workbench/.pixi`
env (a full stdlib/site-packages tree) was untracked-but-unignored and polluted
tooling. Added `**/.pixi/`.

## Layer 2 baseline — mypy on `preproc.py` (2026-07-23)

Wired as `pixi run -e lint typecheck` (config: `mypy.ini`), scoped to
`images/afni/preproc.py`, **report-only** — deliberately *not* in the `health`
gate until the baseline below is triaged.

**57 errors**, and the distribution is the useful part:

| Code | N (2026-07-23) | N (2026-08-10) | What it actually means |
|------|---|---|------------------------|
| `attr-defined` | 36 | 36 | `nib.load()` is typed as returning the `FileBasedImage` supertype, so `.affine` / `.get_fdata()` / `.shape` are "missing". Runtime is fine — these are real `Nifti1Image`s. |
| `list-item` | 13 | 13 | `Path` objects in subprocess arg lists inferred as `list[str]`. `subprocess` accepts path-like, so runtime is fine. |
| `arg-type` | 5 | 5 | Mostly the same two causes crossing a function boundary. |
| `assignment` | 0 | **5** | New; same `FileBasedImage`-vs-`Nifti1Image` supertype root cause. |
| `index` | 2 | 1 | Header `__setitem__` on the `FileBasedHeader` supertype. |
| `union-attr` | 1 | **2** | See "correlated guards" below. |
| **Total** | **57** | **62** | |

None of the 57 was a live bug, and the same holds for the 62: the distribution is
essentially unchanged, which is the point — the count moved with file size, not
with new type risk. The `union-attr` count going 1 → 2 is the row to watch, since
that is the category the "correlated guards" finding below lives in. Two settings make this run useful at all:

- `check_untyped_defs = true` — without it mypy skips the body of every
  unannotated function, which is nearly all of `preproc.py`, and reports ~nothing.
- `python_version = 3.12` — **not** the 3.10 image runtime. 3.10 was tried and
  aborts the run: the bundled numpy 2.5 stubs use PEP 695 `type` statements that
  mypy will not parse below 3.12. Consequence: mypy will not catch ≥3.11-only
  stdlib usage in `images/`; that stays a review-time constraint (see UP017 below).

### The finding that matters for follow-up #2 — correlated guards

The two `None` errors are false positives, but they are *load-bearing* false
positives: both are guarded by conditions mypy cannot correlate, across long
distances inside the exact functions slated for decomposition.

Both invariants still hold as of 2026-08-10, at moved line numbers, and the
`ts` guard has been **strengthened** in the interim:

- [`preproc.py:918`](../images/afni/preproc.py#L918) — `ts.shape[1]` on a
  `wm_ts`/`csf_ts` that mypy sees as `Any | None`. The guard is now a
  fail-closed one directly above at
  [`:907-914`](../images/afni/preproc.py#L907-L914): any tissue whose timeseries
  is `None` or empty **raises `ValueError`**, which fails the run rather than
  silently `continue`-ing past it. That is a better invariant than the original
  `if n_vox == 0: continue`, and the accompanying comment says why — an empty WM
  or CSF mask means the BOLD→T1w registration for that run is invalid, so the
  run's output must not be treated as valid. Still one level of indirection from
  the use, hence still a mypy `union-attr`.
- [`preproc.py:1584`](../images/afni/preproc.py#L1584) — `mask_mni` is `None`
  only when `args.emit == "grayordinate"` (set at
  [`:1426`](../images/afni/preproc.py#L1426)), and that path returns early at
  [`:1546`](../images/afni/preproc.py#L1546) — now ~38 lines before the use
  rather than ~150. Shorter, but still not stated in the code.

Neither invariant is expressed in a way a type checker or a reader-in-a-hurry can
see. **Splitting these functions without preserving those guards converts both
into real crashes** (`nib.load(None)`). Make the guards explicit *before*
refactoring — that is the concrete de-risking mypy was brought in for.

## Status

Re-verified 2026-08-10.

- [x] Layer 1 tooling added to `pixi.toml` / pre-commit / ruff.toml (2026-07-23)
- [x] Baseline captured; findings triaged (see above)
- [ ] Decide on `pyproject.toml` for deptry (or keep manual audit) — **deferred** per maintainer
- [x] Apply safe import-sort auto-fix (`I001`) as an isolated PR (`style/ruff-import-sort`)
- [x] Reformat tree (`ruff format`) as its own isolated PR (`style/ruff-format`)
- [x] **ruff enforced in CI** — `python_lint` job runs `ruff check .` + `ruff format --check .`; both pass clean (issue #71, which noted ruff was "configured and wired but unenforced")
- [x] Remove verified-dead `n_voxels` — gone; the only `n_voxels` strings left in `preproc.py` are a docstring and the unrelated `subcort_n_voxels` QC field
- [ ] `to_json` consolidation — **still open, now nine methods** (see above)
- [x] mypy on `preproc.py`; contracts documented (2026-07-23, report-only baseline above)
- [ ] coverage baseline captured before refactors — **the one substantive gap left in this plan.** Layers 1 and 2 are done; Layer 3 (the "retain core functionality" safety net) has not been started, and it is the layer this document called "matters most"

### Argo YAML — Layer 2's other half, not started

The plan called for **yamllint** + `argo lint` alongside the Python tooling. Neither
is wired into `pixi.toml` or CI. The YAML grew from ~4.2k to ~6.2k lines in the
interim, so this gap widened while the Python side closed. One caveat if picking it
up: `argo lint --offline` cannot resolve `workflow.outputs.parameters` references,
so it will report false failures on templates that use them — it needs either a
live cluster or a per-file exclusion, which is likely why it was never wired.

### UP017 deliberately NOT auto-applied

`UP017` (`datetime.timezone.utc` → `datetime.UTC`) is runtime-version-sensitive:
`datetime.UTC` requires Python ≥3.11. Several image scripts run on **Python
3.10** — `images/freesurfer` and `images/fireANTs` install `python3` on Ubuntu
22.04 (3.10). Applying UP017 there would break the container at runtime, so it
is excluded from the auto-fix. `ruff.toml`'s `target-version = "py312"` reflects
only the lint environment, **not** the image runtimes. Revisit per-image if/when
those bases move to ≥3.11.
