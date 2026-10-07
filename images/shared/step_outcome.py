"""StepOutcome failure taxonomy and the in-pod record writer — one copy (#646).

Every StepOutcome record is classified here, whoever writes it:

- src/metrics/outcome_recorder.py, the standalone fallback recorder (python image);
- the three driver scripts that record their own per-run outcomes in-pod (ADR
  016): functional preprocessing (afni image), bold-to-t1w (freesurfer image) and
  surface resample (workbench image).

The drivers used to carry inline copies of the taxonomy, on the grounds that
their containers could not reach src/metrics. That was true of src/metrics but
never of images/shared: each image COPYs what it needs from here into /app, the
same way restore_links.py travels. The copies had already drifted — a skipped
surface-resample run was pattern-matched while a skipped func-preproc run was
`dependency`, and only bold-to-t1w knew exit 65 meant `qc_rejected`.

STANDARD LIBRARY ONLY, and Python 3.10: the afni and workbench images have
neither boto3 nor a newer interpreter.

The record shape and key layout here must match src/metrics/schemas.py's
StepOutcome (which this module cannot import — schemas.py is not in the driver
images). tests/images/shared/test_step_outcome.py holds the two together.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

# Must equal schemas.StepOutcome's default; the test asserts it.
SCHEMA_VERSION = "1.1"

# Ordered patterns — first match wins.
_TAXONOMY: list[tuple[str, list[str]]] = [
    (
        "infrastructure",
        [
            r"OOMKilled",
            r"exit\s+code\s+137",
            r"evicted",
            r"node\s+cordoned",
            r"deadline\s+exceeded",
            r"pod\s+unschedul",
            r"Unschedulable",
            r"insufficient\s+memory",
        ],
    ),
    (
        "data",
        [
            r"NoSuchKey",
            r"NoSuchBucket",
            r"nss_volumes",
            r"missing\s+input",
            r"corrupt",
            r"Unable\s+to\s+open",
            r"Input\s+file\s+not\s+found",
        ],
    ),
    (
        "algorithm",
        [
            r"AssertionError",
            r"ValueError",
            r"segmentation\s+fault",
            r"Segmentation\s+fault",
            r"SIGSEGV",
            r"RuntimeError",
            r"CalledProcessError",
            r"non-zero\s+exit\s+code",
            r"ERROR:",
        ],
    ),
    (
        "dependency",
        [
            r"depended\s+on\s+task",
            r"upstream\s+task\s+failed",
            r"Skipped",
        ],
    ),
]


# The exit code a registration step uses when its own quality gate rejects the
# transform it just produced: fst1w_to_mni.py and bold_to_t1w.py exit 65, and
# only for that, before promoting any output. The run did not break — it
# worked and said no — so it gets its own category rather than falling through
# to "unknown" alongside real crashes (issue #368). 65 is also excluded from
# every retry expression, since a deterministic registration re-rejects.
QC_REJECTED_EXIT_CODE = 65

# Container exit codes carry the only failure signal a DAG task actually
# exposes, since {{tasks.<name>.message}} does not exist. 128+N is the shell
# convention for "killed by signal N".
_EXIT_CODE_CATEGORY: dict[int, str] = {
    137: "infrastructure",  # 128+9  SIGKILL — OOMKilled or node eviction
    143: "infrastructure",  # 128+15 SIGTERM — pod deleted / node drained
    139: "algorithm",  # 128+11 SIGSEGV
    134: "algorithm",  # 128+6  SIGABRT
    QC_REJECTED_EXIT_CODE: "qc_rejected",
}

# What a known exit code means, spelled out in failure_reason. The panel a
# human reads is failure_reason, not failure_category, and a bare "exit code 65"
# there reads as a crash.
_EXIT_CODE_REASON: dict[int, str] = {
    QC_REJECTED_EXIT_CODE: "QC gate rejected the registration; no outputs promoted",
}


def classify_failure(status: str, message: str, exit_code: object = "") -> str:
    """Return a taxonomy category for a failed or skipped step.

    - succeeded -> "".
    - skipped -> "dependency", by definition: its reason describes an absent
      upstream artifact, not anything this step did. Left to the pattern match,
      a reason naming a missing input would read as `data` — bad subject data —
      rather than an upstream QC rejection (#222).
    - otherwise the message patterns, then the exit code (65 -> qc_rejected,
      #368; signal deaths), then "infrastructure" for an Error with nothing to go
      on (the controller never ran the pod), else "unknown".

    `exit_code` may be an int, a numeric string, "" or None — the fallback
    recorder gets a string from Argo, the drivers hold an int.
    """
    st = (status or "").lower()
    if st == "succeeded":
        return ""
    if st == "skipped":
        return "dependency"
    msg = message or ""
    for category, patterns in _TAXONOMY:
        for pat in patterns:
            if re.search(pat, msg, re.IGNORECASE):
                return category
    code = "" if exit_code is None else str(exit_code).strip()
    if code.isdigit() and int(code) in _EXIT_CODE_CATEGORY:
        return _EXIT_CODE_CATEGORY[int(code)]
    if st == "error" and not msg.strip():
        return "infrastructure"
    return "unknown"


def outcome_filename(
    workflow_name: str,
    workflow_uid: str,
    step: str,
    subject: str,
    session: str,
    task: str,
    run: str,
) -> str:
    """The object name under metrics/step-outcomes/dt=<date>/ — the same layout
    as schemas.StepOutcome.s3_key (`{name}__{uid}__…`, bare name without a UID)."""
    tag = f"{workflow_name}__{workflow_uid}" if workflow_uid else workflow_name
    return f"{tag}__{step}__{subject}__{session}__{task}__{run}_outcome.json"


def write_outcome(
    out_dir: Path,
    *,
    workflow_name: str,
    workflow_uid: str,
    subject: str,
    session: str,
    step: str,
    task: str,
    run: str,
    status: str,
    failure_reason: str = "",
    upstream_failed_step: str = "",
    exit_code: object = None,
    pipeline: str = "cloudpipe_minproc",
) -> Path:
    """Write one StepOutcome record into out_dir and return its path.

    out_dir is the directory the template uploads as an output artifact under
    metrics/step-outcomes/dt=<date>/, so the file name is the object name.
    """
    record = {
        "workflow_name": workflow_name,
        "workflow_uid": workflow_uid,
        "step": step,
        "subject": subject,
        "session": session,
        "task": task,
        "run": run,
        "status": status,
        "failure_category": classify_failure(status, failure_reason, exit_code),
        "failure_reason": failure_reason,
        "upstream_failed_step": upstream_failed_step,
        "outputs_verified": [],
        "pipeline": pipeline,
        "schema_version": SCHEMA_VERSION,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = Path(out_dir) / outcome_filename(
        workflow_name, workflow_uid, step, subject, session, task, run
    )
    path.write_text(json.dumps(record))
    return path
