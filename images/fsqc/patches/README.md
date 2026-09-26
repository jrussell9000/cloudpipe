# Vendored fsqc module patches

Two files copied verbatim from **Deep-MI/fsqc @ `9fcd40cf76ec8de9d8239528cb35e3b2124dd40b`**
(branch `dev`, the merge of [PR #110](https://github.com/Deep-MI/fsqc/pull/110), 2026-09-01).
MIT licensed, same as the rest of fsqc.

| File | Replaces |
|---|---|
| `evaluateHypothalamicSegmentation.py` | the 2.1.7 copy in `site-packages/fsqc/` |
| `evaluateHippocampalSegmentation.py` | the 2.1.7 copy in `site-packages/fsqc/` |

## Why these are vendored rather than installed

[Deep-MI/fsqc#105](https://github.com/Deep-MI/fsqc/issues/105) is a positional-indexing
bug: both modules built their centroid array from the labels actually **present** in the
segmentation, then indexed that array by fixed position. PR #110 rebuilds both against a
fixed expected-label array, so a label with zero voxels yields a `NaN` centroid (backfilled
from the overall segmentation centroid) instead of shifting every later row.

Measured on this cohort (10,274 sessions, Athena `cloudpipe_metrics.fsqc_qc`, 2026-09-02):

- **hypothalamus** — 1,286 of the 3,610 covered sessions (36%), volumes intact (they are
  read from `outliers/all.regions.stats`, which is written before the crash)
- **hippocampus** — all 82 failures, every one on a session with *both* hemispheres of
  `{l,r}h.hippoAmygLabels.long.FSvoxelSpace.mgz` staged, i.e. genuine module failures
  rather than missing input

Together that is ~100% of the rows in the anatomical-QC triage panel.

### The two halves fail for different reasons — do not collapse them

Both were A/B'd against real production data, stock 2.1.7 vs patched, in this image:

| | data | stock 2.1.7 | patched |
|---|---|---|---|
| hypothalamus | `sub-00BD7VDC/ses-06A` | `IndexError: index 9 is out of bounds for axis 0 with size 9` | completes, warns about the missing label |
| hippocampus | `sub-021403LF/ses-02A` | `IndexError: too many indices for array: array is 2-dimensional, but 3 were indexed` | completes, both hemispheres |

The hypothalamus case is the issue as written: label 806 is genuinely absent from the
segmentation, so the centroid array is short and the positional index runs off the end.

**The hippocampus case is not.** That session has all 28 expected labels present in both
hemispheres — nothing is missing — and `lh` succeeds while `rh` raises. So the issue title
("when a subunit label is absent") does not describe our 82 failures, even though PR #110
demonstrably fixes them. Do not use "a label was missing" to triage a hippocampus failure,
and do not assume a session with a complete label set is therefore unaffected.

**The fix exists only on `dev`.** Both `stable` and the newest release (v.2.1.7,
2026-03-18) still carry the bug, so there is nothing on PyPI to bump to.

## Why not just pin the `dev` commit

`dev` also adds a `checkMotion` module whose call site in `fsqcMain.py` is **not gated on
any flag** — the import sits in the main run function's import block and the call runs
unconditionally inside the core-metrics loop. `checkMotion.py` has only `import os` at
module level, so it imports fine without `mriqc`/`scikit-learn`; the failure surfaces at
*call* time, is swallowed by fsqc's per-module `try/except`, and sets `metrics_status = 1`.

That means a `dev` pin would set `metrics_status = 1` on **every session** while all three
build-time smoke tests still pass. `metrics_status` is currently `0` on all 10,274 rows,
and the triage panel keys on `metrics_status <> 0` — so the panel would go from 1,356 rows
to 10,274. Installing `mriqc` to avoid this is not an option: it pulls nipype, niworkflows
and templateflow into an image that is deliberately ~930 MB.

## Why overlaying only these two files is safe

Verified against `stable` before vendoring:

- `evaluateHypothalamicSegmentation.py` differs from the 2.1.7 copy by exactly the PR #110
  hunk (44 changed lines) and nothing else — no unrelated dev-era drift
- neither file has a module-level import; their function-local imports are `logging`,
  `os`, `nibabel`, `numpy`, `scipy.ndimage`, `fsqc.createScreenshots` and
  `fsqc.fsqcUtils`, all already present under the 2.1.7 install
- PR #110's third file, `createScreenshots.py`, only *deletes* a fallback LUT-augmentation
  block (dev's `returnFreeSurferColorLUT()` already carries labels 231–246 and 801–810).
  It changes no signature, so 2.1.7's copy stays compatible and is deliberately not
  vendored.

## Retiring this directory

When **v2.1.8** lands on PyPI (dev's `VERSION` already reads `2.1.8-dev`):

1. bump `ARG FSQC_VERSION` in `images/fsqc/Dockerfile` to `2.1.8`
2. delete this directory and the `COPY`/patch-assert block that references it
3. re-check `checkMotion` — if it is still ungated in the release, the trimmed dependency
   set has to grow `mriqc`/`scikit-learn` or `metrics_status` will go to 1 cohort-wide

## Identifying patched rows in Athena

An overlay does not change what `fsqc.get_version()` returns — it reads the installed
2.1.7 `VERSION` file and is unaware of the swapped modules. Without help, patched and
unpatched rows would both record `fsqc_version = "2.1.7"` and be distinguishable only by
`completed_at`, which is exactly the wrong thing to rely on when the point of the change is
to measure whether the fix worked.

So the Dockerfile writes the patch ref to `/opt/fsqc-patch-ref`, and
`stage_and_run.py::_fsqc_version()` appends it as a local version segment when that file is
present. Patched records therefore read:

```
fsqc_version = "2.1.7+p9fcd40cf"
```

which the triage dashboard and any Athena query can filter on directly. Deleting this
directory (see above) removes the marker file, and `fsqc_version` reverts to a bare
release string on its own.
