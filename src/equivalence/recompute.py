"""One ruler: a FuncQC-shaped record computed from any pipeline's outputs.

Takes `(MNI BOLD, brain mask, confounds TSV)` and returns the record. The arm
identifier labels the output and nothing else — it selects no code path, no mask,
no grid and no threshold (spec, `pipeline-equivalence-benchmark`), which is what
makes a difference between two records a pipeline difference rather than a
measurement difference.

The measurement itself is `images/shared/func_iqm.py`, the same module
`images/afni/preproc.py` calls in-pod to write cloudpipe's own record (design
D2). Imported, never reimplemented: a second implementation here would be
exactly the drift the calibration gate exists to detect.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import nibabel as nib
import pandas as pd

from . import confound_map

# images/shared is on pytest's pythonpath and is COPYed beside preproc.py in the
# afni image, but a plain `PYTHONPATH=src` consumer has neither. Add it here so
# the one ruler stays one file rather than becoming a vendored copy.
_SHARED = Path(__file__).resolve().parents[2] / "images" / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from func_iqm import func_qc_measures  # noqa: E402

#: The fields the calibration gate compares against the in-pod record. Nine, per
#: the spec; the rest of the schema is identity, provenance or runtime, none of
#: which a recompute can reproduce.
CALIBRATED_FIELDS: tuple[str, ...] = (
    "mean_fd",
    "median_fd",
    "max_fd",
    "mean_dvars",
    "mean_global_signal",
    "tsnr_median",
    "gcor",
    "aor",
    "aqi",
)


@dataclass(frozen=True)
class RunInputs:
    """One run's outputs, as the harness reads them.

    `vocabulary` says which confound naming the TSV uses, so the mapping table can
    be applied. It is a property of the file, not of the arm: it tells the harness
    how to read a column, never what to compute from it.
    """

    mni_bold: Path
    brain_mask: Path
    confounds_tsv: Path
    subject: str
    session: str
    task: str
    run: str
    vocabulary: str = confound_map.CLOUDPIPE
    n_nss_frames: int = 0


def recompute_func_qc(inputs: RunInputs, arm: str) -> dict:
    """A FuncQC-shaped dict for one run, measured from its own outputs.

    `arm` is written to the record's `pipeline` field and used nowhere else.

    `n_frames` and `tr_seconds` are read from the MNI BOLD header rather than
    from a native-space image: neither pipeline resamples in time, and
    `preproc.py` writes the native TR into the MNI header's `pixdim[4]`
    deliberately, so the two agree. Reading the output also keeps this function's
    inputs to the three files the spec names.
    """
    confounds = pd.read_csv(inputs.confounds_tsv, sep="\t", na_values="n/a")
    canonical = confound_map.to_canonical(confounds, inputs.vocabulary)

    bold_img = nib.load(inputs.mni_bold)
    measures = func_qc_measures(
        confounds=canonical,
        n_frames=int(bold_img.shape[-1]),
        tr=float(bold_img.header.get_zooms()[3]),
        n_nss_frames=inputs.n_nss_frames,
        mni_bold=inputs.mni_bold,
        mask_mni=inputs.brain_mask,
    )

    return {
        "schema_version": "1.1",
        "pipeline": arm,
        "image_tag": "",
        "subject": inputs.subject,
        "session": inputs.session,
        "task": inputs.task,
        "run": inputs.run,
        **measures,
        "stage_timings_s": {},
        "total_runtime_s": 0.0,
        "peak_memory_gb": 0.0,
        "container_peak_memory_gb": 0.0,
        "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@dataclass(frozen=True)
class FieldMismatch:
    """One calibrated field whose recomputed value differs from the in-pod one."""

    field: str
    in_pod: object
    recomputed: object

    def __str__(self) -> str:
        return f"{self.field}: in-pod {self.in_pod!r}, recomputed {self.recomputed!r}"


def calibration_mismatches(in_pod: dict, recomputed: dict) -> list[FieldMismatch]:
    """The calibrated fields on which the two records disagree.

    Exact equality, not a tolerance. Both sides come from the same code reading
    the same files, and every one of these fields is stored rounded, so the only
    thing a tolerance could absorb is the drift this gate is for. A field absent
    from either record counts as a mismatch rather than being skipped.
    """
    mismatches = []
    for field in CALIBRATED_FIELDS:
        theirs = in_pod.get(field, _MISSING)
        ours = recomputed.get(field, _MISSING)
        if theirs != ours:
            mismatches.append(FieldMismatch(field, theirs, ours))
    return mismatches


class _Missing:
    def __repr__(self) -> str:
        return "<absent>"

    def __eq__(self, other: object) -> bool:
        return False

    __hash__ = None  # type: ignore[assignment]


_MISSING = _Missing()
