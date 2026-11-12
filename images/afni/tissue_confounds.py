#!/usr/bin/env python3
"""tissue_confounds.py: add `csf` and `white_matter` to a func session pod's fresh TSVs.

Run by the functional-preprocessing session pod after export_tissue_masks() and
before publish_outputs() (add-tissue-confound-columns D1). For every run with a fresh
MNI BOLD, brain mask and confounds TSV in --outdir, it adds the two columns
(images/shared/confounds_tissue.py), and fills the run's FuncQC JSON in --qc-dir with
the three schema-1.5 fields, before the pod's func-qc artifact uploads it.

It never fails the session (D5). A session without maps gets n/a columns
(`no-tissue-maps`), and a run that cannot be computed gets n/a columns (`error`, with
the message printed). The only non-zero exit is a usage error. Each TSV and record is
written to a temporary name and renamed, so a killed pod leaves a whole file either way.

  python tissue_confounds.py --outdir OUT --qc-dir QC --subj sub-X --ses ses-00A
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import confounds_tissue as ct  # noqa: E402

BOLD_SUFFIX = f"_{ct.SPACE}_bold.nii.gz"


def label_path(outdir: Path, subj: str, ses: str, tissue: str) -> Path:
    return outdir / f"{subj}_{ses}_{ct.SPACE}_res-1_label-{tissue}_mask.nii.gz"


def session_labels(outdir: Path, subj: str, ses: str) -> dict | None:
    """Each column's resampled label mask, or None if the session has no maps."""
    paths = {c: label_path(outdir, subj, ses, t) for c, t in ct.COLUMNS}
    if not all(p.is_file() for p in paths.values()):
        return None
    return {c: ct.load_label(p) for c, p in paths.items()}


def fresh_runs(outdir: Path, subj: str, ses: str) -> list[str]:
    """The prefixes of the runs this pod wrote an MNI BOLD and a TSV for."""
    out = []
    for bold in sorted(outdir.glob(f"{subj}_{ses}_*{BOLD_SUFFIX}")):
        prefix = bold.name[: -len(BOLD_SUFFIX)]
        if (outdir / f"{prefix}_desc-confounds_timeseries.tsv").is_file():
            out.append(prefix)
    return out


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def columns_for(outdir: Path, prefix: str, labels, labels_error: str, n_frames: int):
    if labels_error:
        return ct.unavailable(n_frames, ct.ERROR, labels_error)
    try:
        return ct.compute(
            outdir / f"{prefix}{BOLD_SUFFIX}",
            outdir / f"{prefix}_{ct.SPACE}_brainmask.nii.gz",
            labels,
        )
    except Exception as exc:  # noqa: BLE001 -- recorded as `error`; the run still publishes
        traceback.print_exc()
        return ct.unavailable(n_frames, ct.ERROR, f"{type(exc).__name__}: {exc}")


def process_run(outdir: Path, qc_dir: Path, prefix: str, labels, labels_error: str) -> dict:
    tsv = outdir / f"{prefix}_desc-confounds_timeseries.tsv"
    text = tsv.read_text()
    if ct.state(text) != "absent":
        # This image writes them, so a TSV already carrying them is not this pod's to redo.
        return {"run": prefix, "status": "already"}
    cols = columns_for(outdir, prefix, labels, labels_error, ct.n_frames(text))
    write_atomic(tsv, ct.add_columns(text, cols))
    qc = qc_dir / f"{prefix}_qc.json"
    if qc.is_file():
        record = json.loads(qc.read_text())
        record.update(cols.qc_fields())
        write_atomic(qc, json.dumps(record))
    result = {"run": prefix, "status": cols.status, **cols.qc_fields(), "qc_record": qc.is_file()}
    if cols.error:
        result["error"] = cols.error[:300]
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--outdir", type=Path, required=True)
    p.add_argument("--qc-dir", type=Path, required=True)
    p.add_argument("--subj", required=True)
    p.add_argument("--ses", required=True)
    args = p.parse_args(argv)

    runs = fresh_runs(args.outdir, args.subj, args.ses)
    if not runs:
        return 0
    labels, labels_error = None, ""
    try:
        labels = session_labels(args.outdir, args.subj, args.ses)
    except Exception as exc:  # noqa: BLE001 -- every run is then `error`, n/a columns
        traceback.print_exc()
        labels_error = f"{type(exc).__name__}: {exc}"
    for prefix in runs:
        try:
            result = process_run(args.outdir, args.qc_dir, prefix, labels, labels_error)
        except Exception as exc:  # noqa: BLE001 -- an unreadable TSV: leave it as preproc wrote it
            traceback.print_exc()
            result = {"run": prefix, "status": "skipped", "error": f"{type(exc).__name__}: {exc}"}
        print(f"[tissue-confounds] {json.dumps(result)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
