# T1w→MNI Registration Failure Investigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Diagnose why some `cloudpipe_minproc` T1w→MNI registrations failed QC in the recent test batch, and produce a findings report classifying each failure by root cause with remediation recommendations.

**Architecture:** Build three small reusable analysis tools under `scripts/investigations/t1w_mni_qc/` (pull QC metrics from S3 → tidy CSV; plot metric distributions; render NIfTI/MGZ overlay PNGs), then execute a six-phase investigation (locate → population table → categorize → visually inspect → GPU re-run experiments → report) using those tools. Tooling tasks are test-driven; investigation-execution tasks are procedural with concrete commands and observable expected outputs.

**Tech Stack:** Python 3 (invoke as `python`, the Homebrew interpreter that carries the deps), pytest 9.x, boto3 (S3, mocked in tests per `tests/metrics/test_outcome_recorder.py`), pandas, nibabel + numpy + matplotlib (overlays — **no nilearn**, it is not installed), Argo/kubectl for GPU re-runs on `gpu-nodepool`.

## Global Constraints

- **Scope is diagnose-only.** Do NOT modify `images/fireANTs/scripts/fst1w_to_mni.py`, `images/shared/registration_qc.py` thresholds, or any workflow template. Remediation is a separate follow-up spec. (Re-runs in Phase 5 pass tuning via CLI args / a throwaway manifest — they do not edit the committed script.)
- **S3 bucket:** `<YOUR_S3_BUCKET>` (ConfigMap `cloudpipe-config`, key `bucket`, namespace `argo-workflows`). Pass as `--bucket <YOUR_S3_BUCKET>`.
- **QC metrics S3 prefix:** `metrics/registration/`, per-session file `{subj}_{ses}_t1w_to_mni_reg_qc.json`.
- **Derivatives S3 prefix:** `derivatives/registration/{subj}/{ses}/t1w_to_mni/` (warped/orig/warp/affine/qc), `derivatives/fastsurfer/{subj}/{subj}_{ses}_templated.tar.gz` (orig.mgz, brainmask.mgz).
- **QC fail thresholds (verbatim from `registration_qc.py`):** `dice` fail < 0.82 / warn < 0.90; `jac_det_frac_negative` fail > 0.01 / warn > 0.001; `centroid_displacement_mm` fail > 15.0 / warn > 5.0.
- **Interpreter:** run everything with `python` and `pytest` (Homebrew). `python3` lacks the deps.
- **Test placement:** `tests/investigations/t1w_mni_qc/`; add `scripts/investigations` and `scripts/investigations/t1w_mni_qc` to `pytest.ini` `pythonpath` so imports resolve like the existing `src`/`images/shared` entries.
- **Region:** `<YOUR_AWS_REGION>`.

---

## File Structure

- `scripts/investigations/t1w_mni_qc/pull_qc.py` — S3 → tidy per-session QC CSV. Owns all S3 listing/reading + row assembly.
- `scripts/investigations/t1w_mni_qc/plot_distributions.py` — CSV → distribution PNGs with thresholds overlaid. Owns Phase-1 plotting only.
- `scripts/investigations/t1w_mni_qc/make_overlays.py` — NIfTI/MGZ → overlay PNGs (nibabel+matplotlib). Owns Phase-3 rendering only.
- `scripts/investigations/t1w_mni_qc/README.md` — how to run the toolkit end to end.
- `scripts/investigations/t1w_mni_qc/experiments.md` — Phase-5 re-run log (created empty in Task 6).
- `scripts/investigations/t1w_mni_qc/rerun-job.yaml` — throwaway k8s Job manifest template for GPU re-runs (Task 6).
- `tests/investigations/t1w_mni_qc/test_pull_qc.py`, `test_plot_distributions.py`, `test_make_overlays.py` — unit/smoke tests.
- `docs/investigations/2026-07-02-t1w-mni-registration-failures.md` — the deliverable findings report.

---

## Task 1: `pull_qc.py` — assemble the batch QC table

**Files:**
- Create: `scripts/investigations/t1w_mni_qc/pull_qc.py`
- Create: `tests/investigations/t1w_mni_qc/__init__.py` (empty)
- Create: `tests/investigations/t1w_mni_qc/test_pull_qc.py`
- Modify: `pytest.ini` (add pythonpath entries)

**Interfaces:**
- Produces:
  - `rows_from_qc_objects(objects: list[dict]) -> list[dict]` where each input dict is a parsed QC JSON; each output row has keys `subject, session, verdict, dice, jac_det_frac_negative, jac_det_min, centroid_displacement_mm, driving_metrics, completed_at`.
  - `driving_metrics(qc: dict) -> str` — comma-joined subset of `{dice,jac_det_frac_negative,centroid_displacement_mm}` whose value crosses its fail threshold; `""` if none. Uses the Global-Constraints thresholds.
  - CLI: `python scripts/investigations/t1w_mni_qc/pull_qc.py --bucket <YOUR_S3_BUCKET> --out qc_table.csv [--region <YOUR_AWS_REGION>]` → writes CSV, prints pass/warn/fail counts.

- [ ] **Step 1: Add pythonpath entries so the module imports under pytest**

Edit `pytest.ini`, appending two lines under the existing `pythonpath =` block (keep existing entries):

```ini
pythonpath =
    src
    src/metrics
    images/shared
    images/fastsurfer
    images/freesurfer
    tests/metrics
    scripts/investigations
    scripts/investigations/t1w_mni_qc
```

- [ ] **Step 2: Write the failing test**

Create `tests/investigations/t1w_mni_qc/__init__.py` (empty) and `tests/investigations/t1w_mni_qc/test_pull_qc.py`:

```python
import pull_qc


def _qc(**over):
    base = {
        "subject": "sub-X", "session": "ses-00A", "verdict": "pass",
        "dice": 0.93, "jac_det_frac_negative": 0.0, "jac_det_min": 0.4,
        "centroid_displacement_mm": 2.0, "completed_at": "2026-06-01T00:00:00Z",
    }
    base.update(over)
    return base


def test_driving_metrics_flags_dice_fail():
    assert pull_qc.driving_metrics(_qc(dice=0.70)) == "dice"


def test_driving_metrics_flags_multiple():
    got = pull_qc.driving_metrics(_qc(dice=0.70, centroid_displacement_mm=20.0))
    assert set(got.split(",")) == {"dice", "centroid_displacement_mm"}


def test_driving_metrics_empty_when_pass():
    assert pull_qc.driving_metrics(_qc()) == ""


def test_driving_metrics_warn_not_flagged():
    # 0.85 is below warn (0.90) but above fail (0.82) -> not a driving fail metric
    assert pull_qc.driving_metrics(_qc(dice=0.85)) == ""


def test_rows_from_qc_objects_shape():
    rows = pull_qc.rows_from_qc_objects([_qc(verdict="fail", dice=0.70)])
    assert rows[0]["subject"] == "sub-X"
    assert rows[0]["verdict"] == "fail"
    assert rows[0]["driving_metrics"] == "dice"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/investigations/t1w_mni_qc/test_pull_qc.py -v`
Expected: FAIL / ERROR with `ModuleNotFoundError: No module named 'pull_qc'`.

- [ ] **Step 4: Write minimal implementation**

Create `scripts/investigations/t1w_mni_qc/pull_qc.py`:

```python
#!/usr/bin/env python
"""pull_qc.py — pull all T1w->MNI QC JSONs for a batch from S3 into a CSV.

Reads   metrics/registration/*_t1w_to_mni_reg_qc.json   and emits one tidy row
per session so failures can be triaged by driving metric (see design spec
docs/superpowers/specs/2026-07-02-t1w-mni-registration-failures-investigation-design.md).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys

log = logging.getLogger(__name__)

# Verbatim from images/shared/registration_qc.py _T1W_MNI_THRESHOLDS.
_FAIL = {
    "dice": (0.82, "below"),
    "jac_det_frac_negative": (0.01, "above"),
    "centroid_displacement_mm": (15.0, "above"),
}


def driving_metrics(qc: dict) -> str:
    """Comma-joined metrics whose value crosses its fail threshold."""
    hit = []
    for key, (thr, direction) in _FAIL.items():
        if key not in qc:
            continue
        val = qc[key]
        if (direction == "below" and val < thr) or (direction == "above" and val > thr):
            hit.append(key)
    return ",".join(hit)


_FIELDS = [
    "subject", "session", "verdict", "dice", "jac_det_frac_negative",
    "jac_det_min", "centroid_displacement_mm", "driving_metrics", "completed_at",
]


def rows_from_qc_objects(objects: list[dict]) -> list[dict]:
    rows = []
    for qc in objects:
        row = {k: qc.get(k, "") for k in _FIELDS}
        row["driving_metrics"] = driving_metrics(qc)
        rows.append(row)
    return rows


def _fetch(bucket: str, region: str) -> list[dict]:
    import boto3
    s3 = boto3.client("s3", region_name=region)
    paginator = s3.get_paginator("list_objects_v2")
    objs = []
    for page in paginator.paginate(Bucket=bucket, Prefix="metrics/registration/"):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("_t1w_to_mni_reg_qc.json"):
                body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
                objs.append(json.loads(body))
    return objs


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    p = argparse.ArgumentParser()
    p.add_argument("--bucket", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--region", default="<YOUR_AWS_REGION>")
    args = p.parse_args()

    rows = rows_from_qc_objects(_fetch(args.bucket, args.region))
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_FIELDS)
        w.writeheader()
        w.writerows(rows)

    counts = {"pass": 0, "warn": 0, "fail": 0}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    log.info(f"Wrote {len(rows)} rows to {args.out}  counts={counts}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/investigations/t1w_mni_qc/test_pull_qc.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add pytest.ini scripts/investigations/t1w_mni_qc/pull_qc.py tests/investigations/t1w_mni_qc/
git commit -m "feat(investigation): pull_qc.py to assemble batch T1w->MNI QC table"
```

---

## Task 2: `plot_distributions.py` — Phase-1 distribution plots

**Files:**
- Create: `scripts/investigations/t1w_mni_qc/plot_distributions.py`
- Create: `tests/investigations/t1w_mni_qc/test_plot_distributions.py`

**Interfaces:**
- Consumes: CSV produced by `pull_qc.py` (columns per `pull_qc._FIELDS`).
- Produces:
  - `plot_metric(values: list[float], metric: str, out_path: str) -> None` — writes a histogram PNG with that metric's warn+fail thresholds drawn as vertical lines. Recognizes `dice`, `jac_det_frac_negative`, `centroid_displacement_mm`.
  - CLI: `python .../plot_distributions.py --csv qc_table.csv --out-dir plots/` → one PNG per metric.

- [ ] **Step 1: Write the failing smoke test**

Create `tests/investigations/t1w_mni_qc/test_plot_distributions.py`:

```python
import os

import matplotlib
matplotlib.use("Agg")

import plot_distributions


def test_plot_metric_writes_png(tmp_path):
    out = tmp_path / "dice.png"
    plot_distributions.plot_metric([0.93, 0.70, 0.85, 0.5], "dice", str(out))
    assert out.exists() and out.stat().st_size > 0


def test_plot_metric_unknown_metric_raises(tmp_path):
    try:
        plot_distributions.plot_metric([1.0], "bogus", str(tmp_path / "x.png"))
        assert False, "expected ValueError"
    except ValueError:
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/investigations/t1w_mni_qc/test_plot_distributions.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'plot_distributions'`.

- [ ] **Step 3: Write minimal implementation**

Create `scripts/investigations/t1w_mni_qc/plot_distributions.py`:

```python
#!/usr/bin/env python
"""plot_distributions.py — histogram each QC metric with warn/fail thresholds.

Answers Phase 1: are failures borderline (piled near a threshold) or catastrophic
(in the far tail)?
"""
from __future__ import annotations

import argparse
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# (warn, fail) verbatim from registration_qc.py.
_THRESHOLDS = {
    "dice": (0.90, 0.82),
    "jac_det_frac_negative": (0.001, 0.01),
    "centroid_displacement_mm": (5.0, 15.0),
}


def plot_metric(values: list[float], metric: str, out_path: str) -> None:
    if metric not in _THRESHOLDS:
        raise ValueError(f"unknown metric: {metric}")
    warn, fail = _THRESHOLDS[metric]
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.hist([v for v in values if v is not None], bins=30)
    ax.axvline(warn, color="orange", linestyle="--", label=f"warn={warn}")
    ax.axvline(fail, color="red", linestyle="--", label=f"fail={fail}")
    ax.set_title(metric)
    ax.set_xlabel(metric)
    ax.set_ylabel("sessions")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True)
    p.add_argument("--out-dir", required=True)
    args = p.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    cols = {m: [] for m in _THRESHOLDS}
    with open(args.csv) as fh:
        for row in csv.DictReader(fh):
            for m in _THRESHOLDS:
                try:
                    cols[m].append(float(row[m]))
                except (KeyError, ValueError):
                    pass
    for m, vals in cols.items():
        plot_metric(vals, m, os.path.join(args.out_dir, f"{m}.png"))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/investigations/t1w_mni_qc/test_plot_distributions.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/investigations/t1w_mni_qc/plot_distributions.py tests/investigations/t1w_mni_qc/test_plot_distributions.py
git commit -m "feat(investigation): plot_distributions.py for Phase-1 metric histograms"
```

---

## Task 3: `make_overlays.py` — Phase-3 overlay PNGs

**Files:**
- Create: `scripts/investigations/t1w_mni_qc/make_overlays.py`
- Create: `tests/investigations/t1w_mni_qc/test_make_overlays.py`

**Interfaces:**
- Consumes: any nibabel-readable volumes (`.nii`, `.nii.gz`, `.mgz`).
- Produces:
  - `tri_planar(base_path: str, out_path: str, overlay_path: str | None = None) -> None` — renders axial+coronal+sagittal mid-slices of `base_path` (grayscale); if `overlay_path` given, draws its `>0` region as a red contour on top. Writes a PNG. Used two ways: warped-over-template (alignment) and orig with brainmask contour (skull-strip quality).
  - CLI: `python .../make_overlays.py --base warped.nii.gz --overlay template.nii --out o.png`.

- [ ] **Step 1: Write the failing smoke test (synthetic NIfTIs, no network)**

Create `tests/investigations/t1w_mni_qc/test_make_overlays.py`:

```python
import numpy as np
import nibabel as nib

import matplotlib
matplotlib.use("Agg")

import make_overlays


def _nii(tmp_path, name, fill):
    arr = np.zeros((16, 16, 16), dtype=np.float32)
    arr[4:12, 4:12, 4:12] = fill
    path = tmp_path / name
    nib.save(nib.Nifti1Image(arr, np.eye(4)), str(path))
    return str(path)


def test_tri_planar_base_only(tmp_path):
    base = _nii(tmp_path, "base.nii.gz", 100.0)
    out = tmp_path / "o.png"
    make_overlays.tri_planar(base, str(out))
    assert out.exists() and out.stat().st_size > 0


def test_tri_planar_with_overlay(tmp_path):
    base = _nii(tmp_path, "base.nii.gz", 100.0)
    ov = _nii(tmp_path, "ov.nii.gz", 1.0)
    out = tmp_path / "o.png"
    make_overlays.tri_planar(base, str(out), overlay_path=ov)
    assert out.exists() and out.stat().st_size > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/investigations/t1w_mni_qc/test_make_overlays.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'make_overlays'`.

- [ ] **Step 3: Write minimal implementation**

Create `scripts/investigations/t1w_mni_qc/make_overlays.py`:

```python
#!/usr/bin/env python
"""make_overlays.py — tri-planar overlay PNGs for T1w->MNI visual QC.

nibabel + matplotlib only (nilearn is not installed). Two uses:
  * warped T1w (base) over MNI template (overlay contour) -> alignment quality
  * orig.mgz (base) with brainmask.mgz (overlay contour)  -> skull-strip quality
"""
from __future__ import annotations

import argparse

import numpy as np
import nibabel as nib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def _mid_slices(arr: np.ndarray):
    x, y, z = (s // 2 for s in arr.shape[:3])
    return [arr[x, :, :], arr[:, y, :], arr[:, :, z]]


def tri_planar(base_path: str, out_path: str, overlay_path: str | None = None) -> None:
    base = np.asarray(nib.load(base_path).get_fdata(), dtype=np.float32)
    base_slices = _mid_slices(base)
    ov_slices = None
    if overlay_path is not None:
        ov = np.asarray(nib.load(overlay_path).get_fdata(), dtype=np.float32)
        ov_slices = _mid_slices(ov)

    fig, axes = plt.subplots(1, 3, figsize=(12, 4))
    for i, ax in enumerate(axes):
        ax.imshow(np.rot90(base_slices[i]), cmap="gray")
        if ov_slices is not None:
            ax.contour(np.rot90(ov_slices[i]) > 0, levels=[0.5], colors="red", linewidths=0.6)
        ax.axis("off")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--overlay", default=None)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    tri_planar(args.base, args.out, overlay_path=args.overlay)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/investigations/t1w_mni_qc/test_make_overlays.py -v`
Expected: 2 passed.

- [ ] **Step 5: Full toolkit test sweep + commit**

Run: `python -m pytest tests/investigations/t1w_mni_qc/ -v`
Expected: all tests pass (9 total across Tasks 1–3).

```bash
git add scripts/investigations/t1w_mni_qc/make_overlays.py tests/investigations/t1w_mni_qc/test_make_overlays.py
git commit -m "feat(investigation): make_overlays.py tri-planar overlays for visual QC"
```

---

## Task 4: Phases 0–2 — locate failures, population table, categorize

This is an **execution** task: run the tools against the real batch and record what comes back. Verification is observing the CLI output and committing the artifacts, not pytest.

**Files:**
- Create: `scripts/investigations/t1w_mni_qc/README.md`
- Create (generated, committed): `docs/investigations/artifacts/qc_table.csv`, `docs/investigations/artifacts/plots/{dice,jac_det_frac_negative,centroid_displacement_mm}.png`

**Prereqs:** AWS SSO session active (if any AWS call returns an auth/expired-token error, STOP and ask the user to re-login before continuing — do not retry blindly).

- [ ] **Step 1: Pull the batch QC table**

Run:
```bash
python scripts/investigations/t1w_mni_qc/pull_qc.py --bucket <YOUR_S3_BUCKET> --out docs/investigations/artifacts/qc_table.csv
```
Expected: log line `Wrote N rows ... counts={'pass': .., 'warn': .., 'fail': ..}`. Record N and the counts — this is the batch size and the fail count.

- [ ] **Step 2: Confirm the failing sessions**

Run:
```bash
python - <<'PY'
import csv
fails=[r for r in csv.DictReader(open("docs/investigations/artifacts/qc_table.csv")) if r["verdict"]=="fail"]
for r in fails:
    print(r["subject"], r["session"], "->", r["driving_metrics"] or "(verdict fail, no single metric past fail thr)")
print("total fails:", len(fails))
PY
```
Expected: an explicit list of `subject session -> driving_metrics`. Save this list — it is the Phase-0 deliverable and the input set for Tasks 5–6.

> Note: a session can be `fail` with empty `driving_metrics` only if verdict logic and thresholds disagree; if that happens, flag it in the report as a QC-logic discrepancy to inspect.

- [ ] **Step 3: Generate distribution plots**

Run:
```bash
python scripts/investigations/t1w_mni_qc/plot_distributions.py --csv docs/investigations/artifacts/qc_table.csv --out-dir docs/investigations/artifacts/plots
```
Expected: three PNGs written. Open each and note (for the report) whether the fail cases sit just past the red line (borderline) or far into the tail (catastrophic), per metric.

- [ ] **Step 4: Categorize fails by driving metric**

Run:
```bash
python - <<'PY'
import collections, csv
c=collections.Counter()
for r in csv.DictReader(open("docs/investigations/artifacts/qc_table.csv")):
    if r["verdict"]=="fail":
        c[r["driving_metrics"] or "(none)"]+=1
for k,v in c.most_common(): print(f"{v:3d}  {k}")
PY
```
Expected: counts per driving-metric combination (e.g. `dice`, `dice,centroid_displacement_mm`, `jac_det_frac_negative`). These are the failure clusters carried into Phase 3.

- [ ] **Step 5: Write the toolkit README and commit**

Create `scripts/investigations/t1w_mni_qc/README.md` documenting the three commands above in order (pull → plot → overlays) with the `--bucket <YOUR_S3_BUCKET>` example, and a one-line purpose per script.

```bash
git add scripts/investigations/t1w_mni_qc/README.md docs/investigations/artifacts/
git commit -m "chore(investigation): batch QC table, distribution plots, failure clusters"
```

---

## Task 5: Phase 3 — visual inspection & classification

**Files:**
- Create (generated, committed): `docs/investigations/artifacts/overlays/{subj}_{ses}_align.png`, `..._skullstrip.png` for each failing session + up to 3 passing controls.
- Create: `docs/investigations/artifacts/classification.csv` (columns: `subject, session, driving_metrics, category, notes`; `category` from the failure-mode vocabulary in the spec).

**Prereqs:** MNI template available locally (download once): `aws s3 cp s3://<YOUR_S3_BUCKET>/config/MNI152NLin2009cAsym_T1w_brain_res-2_RAI.nii /tmp/mni.nii`.

- [ ] **Step 1: Fetch warped + input volumes for one failing session (verify the flow)**

Pick the first failing `{subj}/{ses}` from Task 4 Step 2. Run:
```bash
S=<subj>; SE=<ses>
mkdir -p /tmp/insp/$S_$SE
aws s3 cp s3://<YOUR_S3_BUCKET>/derivatives/registration/$S/$SE/t1w_to_mni/${S}_${SE}_desc-t1w2mni_warped.nii.gz /tmp/insp/
aws s3 cp s3://<YOUR_S3_BUCKET>/derivatives/fastsurfer/$S/${S}_${SE}_templated.tar.gz /tmp/insp/
tar -xzf /tmp/insp/${S}_${SE}_templated.tar.gz -C /tmp/insp/   # yields mri/orig.mgz, mri/brainmask.mgz
```
Expected: `warped.nii.gz`, `mri/orig.mgz`, `mri/brainmask.mgz` present in `/tmp/insp/`.

- [ ] **Step 2: Render the two overlays for that session**

Run:
```bash
python scripts/investigations/t1w_mni_qc/make_overlays.py --base /tmp/insp/${S}_${SE}_desc-t1w2mni_warped.nii.gz --overlay /tmp/mni.nii --out docs/investigations/artifacts/overlays/${S}_${SE}_align.png
python scripts/investigations/t1w_mni_qc/make_overlays.py --base /tmp/insp/mri/orig.mgz --overlay /tmp/insp/mri/brainmask.mgz --out docs/investigations/artifacts/overlays/${S}_${SE}_skullstrip.png
```
Expected: two PNGs. Inspect: `_align` shows warped brain vs MNI template contour (are they registered?); `_skullstrip` shows orig with the brainmask contour (does the mask clip brain or leak skull/dura?).

- [ ] **Step 3: Repeat for all remaining failing sessions + up to 3 passing controls**

Loop Steps 1–2 over every failing session, plus 2–3 `pass` sessions as controls (to calibrate what "good" looks like). Keep controls' PNGs in the same `overlays/` dir with a `_CONTROL` suffix.

- [ ] **Step 4: Classify each failure into the vocabulary**

Create `docs/investigations/artifacts/classification.csv`. For each failing session write one row, `category` ∈ {`skull_strip_error`, `gross_affine_init_failure`, `anatomical_outlier`, `syn_overwarp_folding`, `fov_neck_inclusion`, `qc_false_alarm`} (multiple allowed, `;`-separated), with a short `notes` justification tied to what the PNGs show. Cross-reference each category against the driving metric from Task 4 (e.g. does every `jac_det_frac_negative` fail visually show folding?).

- [ ] **Step 5: Commit**

```bash
git add docs/investigations/artifacts/overlays/ docs/investigations/artifacts/classification.csv
git commit -m "chore(investigation): visual overlays and failure classification"
```

---

## Task 6: Phase 5 — controlled GPU re-run experiments

**Files:**
- Create: `scripts/investigations/t1w_mni_qc/rerun-job.yaml` (throwaway k8s Job template, mirrors the `t1w-to-mni-template` container in `registration-workflow-template.yaml`).
- Create/append: `scripts/investigations/t1w_mni_qc/experiments.md` (the re-run log).

**Method:** one factor at a time. For each representative failing case (≥1 per category from Task 5 that is NOT a `qc_false_alarm`), run the baseline (reproduce the fail) then vary a single knob and record the resulting metrics/verdict. The container prints `QC: dice=.. jac_frac_neg=.. centroid_disp=.. verdict=..` — capture that line.

**Knobs (fst1w_to_mni.py CLI args, one per run):** `--affine-iterations`, `--syn-iterations`, `--affine-scales`/`--syn-scales`, `--learning-rate`, and brainmask on/off (drop `--brainmask` to test whether skull-strip errors are the cause). Do NOT edit the committed script.

- [ ] **Step 1: Write the re-run Job manifest template**

Create `scripts/investigations/t1w_mni_qc/rerun-job.yaml`. Re-read `argo/workflows/cloudpipe_minproc/registration-workflow-template.yaml` `t1w-to-mni-template` at execution time and copy the current pinned image tag (below is the tag as of writing). The `initContainer` fetches the three inputs; the main container runs `fst1w_to_mni.py` with a per-experiment `__EXTRA_ARGS__` line the operator substitutes (e.g. `--syn-iterations 200 150 100`). Substitute `__SUBJ__`, `__SES__`, `__EXTRA_ARGS__`, and `__JOBNAME__` per run (envsubst or manual edit).

```yaml
# Throwaway debug Job for T1w->MNI re-run experiments. NOT managed by ArgoCD.
# Delete after each run: kubectl -n argo-workflows delete job __JOBNAME__
apiVersion: batch/v1
kind: Job
metadata:
  name: __JOBNAME__
  namespace: argo-workflows
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      nodeSelector:
        karpenter.sh/nodepool: gpu-nodepool
      serviceAccountName: default   # must have S3 read via IRSA; use the SA the pipeline pods use if different
      volumes:
        - name: work
          emptyDir: {}
      initContainers:
        - name: fetch
          image: public.ecr.aws/aws-cli/aws-cli:latest
          command: [sh, -c]
          args:
            - |
              set -e
              B=<YOUR_S3_BUCKET>; S=__SUBJ__; SE=__SES__
              aws s3 cp s3://$B/derivatives/fastsurfer/$S/${S}_${SE}_templated.tar.gz /work/fs.tgz
              mkdir -p /work/fs && tar -xzf /work/fs.tgz -C /work/fs
              aws s3 cp s3://$B/config/MNI152NLin2009cAsym_T1w_brain_res-2_RAI.nii /work/mni.nii
          volumeMounts: [{name: work, mountPath: /work}]
      containers:
        - name: rerun
          image: public.ecr.aws/l9e7l1h1/cloudpipe/fireants:sha-9620b80d0a343bc97ef27044764697c3c59a3702
          command: [python, /app/fst1w_to_mni.py]
          args:
            - --t1w
            - /work/fs/__SUBJ___/__SES__/mri/orig.mgz   # adjust to actual extracted path
            - --brainmask
            - /work/fs/__SUBJ___/__SES__/mri/brainmask.mgz
            - --template
            - /work/mni.nii
            - --out-dir
            - /work/out
            - --prefix
            - rerun
            - --subj
            - __SUBJ__
            - --ses
            - __SES__
            # __EXTRA_ARGS__  (one knob per experiment, e.g. --syn-iterations 200 150 100)
          resources:
            requests: {memory: 4G, cpu: "2"}
            limits: {nvidia.com/gpu: "1"}
          volumeMounts: [{name: work, mountPath: /work}]
```

> Verify the extracted FastSurfer path after the first run (`kubectl exec`/logs) — the tarball's internal layout determines the exact `orig.mgz` path; fix the `--t1w`/`--brainmask` args if they differ. If the Job plumbing proves fiddly, the fallback is `kubectl run` an interactive pod from the fireANTs image on `gpu-nodepool`, `aws s3 cp` the inputs by hand, and invoke `fst1w_to_mni.py` directly. Record whichever mechanism you used in `experiments.md`.

- [ ] **Step 2: Establish the baseline for the first case**

Submit the Job with default args for one failing `{subj}/{ses}`, wait for completion, and capture the `QC:` stdout line:
```bash
kubectl -n argo-workflows apply -f /tmp/rerun-baseline.yaml
kubectl -n argo-workflows logs -f job/<job-name> | grep -E 'QC:|verdict'
kubectl -n argo-workflows delete job <job-name>
```
Expected: a `verdict=fail` reproducing the batch result (confirms the case is reproducible before tuning). If it does NOT reproduce, note that — non-determinism is itself a finding.

- [ ] **Step 3: Run one-factor-at-a-time variations**

For that case, submit one Job per knob change (e.g. `--syn-iterations 200 150 100`, then separately `--affine-iterations 400 300 200 100`, then brainmask-off, etc.). Capture each `QC:` line. Stop early for a case once a single change flips it to `pass`/`warn` — record which knob.

- [ ] **Step 4: Log every run to experiments.md**

Append to `scripts/investigations/t1w_mni_qc/experiments.md` a table: `case | knob changed | dice | jac_frac_neg | centroid_mm | verdict | notes`. One row per Job. Include the baseline row per case.

- [ ] **Step 5: Repeat Steps 2–4 for one case per remaining non-false-alarm category**

- [ ] **Step 6: Commit**

```bash
git add scripts/investigations/t1w_mni_qc/rerun-job.yaml scripts/investigations/t1w_mni_qc/experiments.md
git commit -m "chore(investigation): GPU re-run experiments log for failing T1w->MNI cases"
```

---

## Task 7: Phase 6 — findings report (deliverable)

**Files:**
- Create: `docs/investigations/2026-07-02-t1w-mni-registration-failures.md`

- [ ] **Step 1: Write the report**

Create `docs/investigations/2026-07-02-t1w-mni-registration-failures.md` with these sections, filled from Tasks 4–6 artifacts (link to the committed CSVs/PNGs/experiments.md):

1. **Summary** — batch size, pass/warn/fail counts, the headline root-cause breakdown.
2. **Population picture** — are fails borderline or catastrophic, per metric (reference the three distribution PNGs).
3. **Failure catalogue** — one row per failing session: driving metric(s), category, link to its two overlay PNGs, one-line diagnosis.
4. **Root causes** — grouped by category, each with: how many sessions, visual evidence, and the Phase-5 experiment result (which knob recovered it, or that none did).
5. **Recommendations** — per root cause, one of: (a) parameter change worth adopting (with the exact arg/value that worked in experiments), (b) backup registration method warranted (which method — e.g. SynthMorph or an ANTs affine fallback — and for which failure modes), or (c) QC threshold/metric recalibration (which metric, what evidence it false-alarms). Explicitly list which items belong in the remediation follow-up spec.
6. **Confidence & limitations** — call out any category with N=1, and any case that did not reproduce.

- [ ] **Step 2: Verify report completeness against success criteria**

Confirm every failing session from Task 4 Step 2 appears in the Failure catalogue with a category, and every category has a recommendation. If any fail lacks visual evidence or an experiment, say so explicitly rather than leaving it blank.

- [ ] **Step 3: Commit**

```bash
git add docs/investigations/2026-07-02-t1w-mni-registration-failures.md
git commit -m "docs(investigation): T1w->MNI registration failure findings report"
```

---

## Self-Review Notes

- **Spec coverage:** Phase 0 → Task 4 Steps 1–2; Phase 1 → Task 4 Step 3 + Task 2; Phase 2 → Task 4 Step 4; Phase 3 → Task 5 + Task 3; Phase 4 (hypotheses) → folded into Task 5 Step 4 classification + Task 6 case selection; Phase 5 → Task 6; Phase 6 → Task 7. Failure-mode vocabulary appears verbatim in Task 5 Step 4. Success criteria checked in Task 7 Step 2. Artifact locations match the spec (`scripts/investigations/t1w_mni_qc/`, `docs/investigations/2026-07-02-...md`).
- **Deviation from spec:** overlays use nibabel+matplotlib, not nilearn (nilearn is not installed) — same deliverable, one fewer dependency.
- **Interpreter:** all Python/pytest commands use `python` (Homebrew) which carries the deps; `python3` does not.
