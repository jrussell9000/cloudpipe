#!/usr/bin/env python3
"""Post-run validation for cloudpipe test batch runs.

Checks that all subjects in a batch CSV have:
  - SubjectManifest records (cloudpipe_metrics.subject_manifests)
  - StepOutcome records for all expected steps (cloudpipe_metrics.step_outcomes)
  - WorkflowRun records at schema_version 1.1 (cloudpipe_metrics.workflow_runs)
  - no pod attempts that exited non-zero, read live from Argo — this is the only
    check that sees a step which OOMed and then succeeded on retry, since every
    Athena-backed record above describes the post-retry state only
  - [optional --cost] Kubecost API cost attribution > 0 per subject
  - [optional --cost] Athena cost rows in cloudpipe_metrics.costs

Usage:
    # Outcome tracking only (run after all workflows complete)
    # --since scopes the pod-attempt check to one batch: Argo retains completed
    # workflows for 24h, so without it an earlier same-day run of the same
    # subject list is counted too (defaults to the latest workflow per subject)
    python src/validate_test_batch.py \\
        --subjects tools/cloudpipe_test_sample.csv \\
        --since 2026-06-29T21:59:00Z

    # Include cost validation (run after nightly kubecost-cost-scraper fires at 02:00 UTC)
    # --window-start and --window-end are required with --cost (RFC3339 UTC)
    python src/validate_test_batch.py \\
        --subjects tools/cloudpipe_test_sample.csv \\
        --cost \\
        --window-start 2026-06-29T00:00:00Z \\
        --window-end 2026-06-30T23:59:59Z
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import subprocess
import sys
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
from metrics.athena import CloudpipeMetrics, cost_scope_clause

# Step names emitted by outcome-recorder tasks in the Argo WorkflowTemplates.
# Source: `name: step` / `value:` pairs in argo/workflows/cloudpipe_minproc/*.yaml
EXPECTED_STEP_NAMES: list[str] = [
    "anatomical-phase",
    "session-phase",
    # Covers template creation AND segmentation — they share one pod since the
    # anatomical phase moved off the shared EFS volume. The retired
    # `fastsurfer-template-seg` step will still appear in records predating that.
    "fastsurfer-template",
    "fastsurfer-template-parc",
    "fastsurfer-long-seg",
    "fastsurfer-long-parc",
    "t1w-to-mni",
    "bold-to-t1w",
    "func-preproc",
]

# Steps that only run when a subject has BOLD runs in the requested scan types.
# A subject with none of these recorded had no functional data to process, so
# absent func-preproc records are correct rather than a coverage gap. Includes
# the surface steps so a subject whose surface stage recorded but whose
# volumetric stage did not is still treated as in scope.
FUNCTIONAL_PHASE_STEPS: tuple[str, ...] = (
    "func-preproc",
    "bold-to-t1w",
    "surface-sample",
    "surface-resample",
)

# `bold_to_t1w.py` exits 65 — and only 65 — when its relative-NMI sanity floor
# rejects a run as degenerate; the session driver then deletes that run's output
# directory, so the transform genuinely does not exist and func-preproc correctly
# fails on the missing input. The driver records the exit code verbatim
# ("bold_to_t1w.py exited 65"), which is the only thing distinguishing that
# expected outcome from a bold-to-t1w failure that IS a defect — an OOMKill, a
# spot preemption, or "pod terminated before this run's result was recorded".
# Matching on the code rather than on `status != "succeeded"` is what keeps the
# issue #114 OOM regression from being explained away as a QC rejection.
B2T_QC_REJECTION_RE = re.compile(r"exited\s+65\b")


def per_run_bold_to_t1w_rows(df: pd.DataFrame) -> dict[tuple[str, str, str, str], dict]:
    """Index each run's `bold-to-t1w` outcome by (subject, session, task, run).

    Per-run rows only: the session-level aggregate (task == run == "na") is the
    fallback recorder's row for a skipped or dead pod and says nothing about an
    individual run.

    `failure_reason` is guarded rather than assumed, because records written
    before that column existed — and hand-built frames in tests — may not carry
    it. A missing reason never matches the QC pattern, which is the safe
    direction: report the gap rather than explain it away.
    """
    b2t = df[(df["step"] == "bold-to-t1w") & (df["task"] != "na") & (df["run"] != "na")]
    if "failure_reason" not in b2t.columns:
        b2t = b2t.assign(failure_reason="")
    return (
        b2t.assign(failure_reason=b2t["failure_reason"].fillna(""))
        .groupby(["subject", "session", "task", "run"])[["status", "failure_reason"]]
        .last()
        .to_dict("index")
    )


def classify_bold_to_t1w_evidence(b2t_row: dict | None) -> tuple[bool, str]:
    """Explain a missing func-preproc run against the per-run bold-to-t1w gate.

    Takes that run's `bold-to-t1w` step_outcomes row (None when it has none) and
    returns `(qc_rejected, note)`:

    - `qc_rejected` — the registration was rejected by the relative-NMI floor and
      its transform discarded, so the absent func-preproc output is the correct
      result rather than a defect. Reported in `details`, not `missing`.
    - `note` — a suffix for the `missing` entry when bold-to-t1w failed for some
      OTHER reason, which IS a defect. Carrying the recorded reason here is what
      turns "unexplained gap" into a diagnosis without an S3 log dig.

    Both empty means bold-to-t1w has nothing to say about this run: it succeeded,
    or never recorded a per-run row at all.
    """
    if not b2t_row or b2t_row["status"] == "succeeded":
        return False, ""
    reason = b2t_row.get("failure_reason") or ""
    if B2T_QC_REJECTION_RE.search(reason):
        return True, ""
    return False, f" — bold-to-t1w {b2t_row['status']}: {reason or 'no reason recorded'}"


KUBECOST_BASE_URL = "https://kubecost.<YOUR_DOMAIN>"

ARGO_NAMESPACE = "argo-workflows"

# Fraction of a step's pod attempts that may exit non-zero before the batch is
# failed rather than warned about. Any non-zero attempt at all raises a warning,
# so the threshold only decides how bad it has to get to be fatal.
#
# Calibrated on the two 2026-08-01 10-subject batches (issue #114): the 01:58Z
# batch retried 4 of 25 bold-to-t1w attempts (0.16) and every step still
# succeeded; the 15:20Z batch retried 27 of 46 (0.59) and failed outright. A
# fraction rather than an absolute count so the rule does not tighten as batch
# size grows.
POD_ATTEMPT_FAIL_FRACTION = 0.25

# Below this many attempts the fraction is too noisy to fail on — one spot
# interruption on a step that only ran twice is 50% and means nothing. Such
# steps still warn, which is the part that matters.
POD_ATTEMPT_FAIL_MIN = 4

# The exit code alone does NOT identify the cause, and guessing from it sends
# triage the wrong way. Both an OOM kill and a spot reclaim are SIGKILL/137;
# observed live on 2026-08-03, the same code carried both
# `main: OOMKilled (exit code 137)` and `Pod was terminated in response to
# imminent node shutdown`. Only the first is a memory-sizing problem — the
# second is capacity, and "raise the memory request" would be wasted work.
#
# Argo's node `message` is what separates them, so causes are matched on it
# first and the code is only a fallback label. Patterns follow the same
# vocabulary as the retryStrategy expressions in the WorkflowTemplates and the
# failure taxonomy in metrics/outcome_recorder.py.
ATTEMPT_CAUSES: tuple[tuple[str, str], ...] = (
    ("oomkilled", "OOMKilled"),
    ("imminent node shutdown", "spot interruption"),
    ("pod deleted", "pod deleted"),
)

# Fallback labels when the message says nothing useful. 137 is 128+9 (SIGKILL),
# 143 is 128+15 (SIGTERM) — a pod told to stop, usually a draining node.
NOTABLE_EXIT_CODES = {"137": "SIGKILL", "143": "SIGTERM"}


@dataclass
class ValidationResult:
    check: str
    passed: bool
    found: int
    expected: int
    missing: list[str] = field(default_factory=list)
    triage: str = ""
    details: list[str] = field(default_factory=list)
    # A check that passed but must not be reported as clean. Used for signals
    # that are leading indicators rather than failures (retried-but-recovered
    # pod attempts) and for checks whose input data could not be read at all —
    # in both cases an unqualified PASS would be a lie.
    warning: bool = False


def read_subjects(csv_path: str) -> list[str]:
    """Read subject IDs from a CSV with a subject_id column."""
    subjects = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            s = row["subject_id"].strip()
            if s:
                subjects.append(s)
    return subjects


def cost_date_window(
    window_start: str | None, window_end: str | None
) -> tuple[str | None, str | None]:
    """Map the Kubecost run window to the CostAllocation `date` (scrape date) range.

    The scraper fires at 02:00 UTC and writes the *previous* day's costs, so a
    workflow that ran on day D is attributed a scrape date of D or D+1. The
    upper bound is therefore widened by one day. Returns (None, None) when no
    window was supplied, which leaves cost queries unscoped by date.
    """
    if not window_start or not window_end:
        return None, None
    start = date.fromisoformat(window_start[:10])
    end = date.fromisoformat(window_end[:10]) + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def parse_since(value: str) -> datetime:
    """Parse an RFC3339 `--since` bound into an aware UTC datetime.

    Accepts the `Z` suffix and minute precision (`2026-08-03T21:59Z`), which is
    how `docs/operations.md` has the operator record a submission time. A naive
    value is read as UTC — every timestamp in this pipeline is UTC, and Argo
    emits `Z`-suffixed ones, so comparing against local time would be wrong.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _workflow_started_at(wf: dict) -> datetime | None:
    """When a workflow began, from `status.startedAt` or its creation stamp."""
    raw = (wf.get("status") or {}).get("startedAt") or (wf.get("metadata") or {}).get(
        "creationTimestamp"
    )
    if not raw:
        return None
    try:
        return parse_since(raw)
    except ValueError:
        return None


def select_batch_workflows(
    workflows: list[dict], subjects: list[str], since: datetime | None = None
) -> list[dict]:
    """Narrow an Argo workflow list to the one batch under test.

    The `subjectid` label alone does not identify a batch. `ttlStrategy` retains
    completed workflows for 24h and rerunning the same subject CSV the same day
    is routine, so a label-only match pulls in every earlier run of the day —
    issue #140, where 20 prior workflows contributed 7 OOM kills on a template
    the batch under test no longer even runs, and the validator reported a fixed
    regression as still failing.

    With `since`, every matching workflow started at or after that instant is
    kept: a batch may legitimately contain a resubmitted subject. Without it,
    only the most recent workflow per subject is kept, which is the correct
    scope for the normal one-workflow-per-subject-per-batch case and needs no
    timestamp from the operator.

    Workflows with no readable start time are kept only in the no-`since` case,
    where they are ordered last; under an explicit window an untimestamped
    workflow cannot be shown to belong to it.
    """
    wanted = set(subjects)
    matched = [
        wf
        for wf in workflows
        if ((wf.get("metadata") or {}).get("labels") or {}).get("subjectid") in wanted
    ]

    if since is not None:
        return [
            wf
            for wf in matched
            if (started := _workflow_started_at(wf)) is not None and started >= since
        ]

    latest: dict[str, dict] = {}
    for wf in matched:
        subject = wf["metadata"]["labels"]["subjectid"]
        incumbent = latest.get(subject)
        if incumbent is None:
            latest[subject] = wf
            continue
        challenger_at = _workflow_started_at(wf)
        incumbent_at = _workflow_started_at(incumbent)
        if challenger_at is not None and (incumbent_at is None or challenger_at > incumbent_at):
            latest[subject] = wf
    return list(latest.values())


def fetch_batch_workflows(
    subjects: list[str], namespace: str = ARGO_NAMESPACE, since: datetime | None = None
) -> list[dict] | None:
    """Return the Argo Workflow objects for `subjects`, or None if unreadable.

    Workflows carry a `subjectid` label (the same one Kubecost aggregates on),
    so one list call scopes the whole batch without having to resolve workflow
    names through Athena first. `select_batch_workflows` then narrows the label
    match down to a single batch — see there for why the label is not enough.

    `argo list -o json` returns each workflow's full `status.nodes`, which is
    where per-attempt pod records live — one node per attempt, retries included.
    None of that survives `ttlStrategy.secondsAfterCompletion` (86400s on every
    cloudpipe template), and the archive that would outlive it is only reachable
    through the Argo *server* API — this CLI runs in kubectl mode. So attempt
    data is available for roughly a day after a batch finishes and then is gone,
    which is why the caller reports its absence rather than passing over it.

    Returns None when the data could not be read at all (no `argo` binary, no
    cluster, unparseable output) — distinct from `[]`, which means the query
    worked and matched nothing.
    """
    try:
        proc = subprocess.run(
            ["argo", "-n", namespace, "list", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        workflows = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(workflows, list):
        return None

    return select_batch_workflows(workflows, subjects, since)


def _node_step(node: dict) -> str:
    """Readable step name for a pod node.

    Steps invoked through a WorkflowTemplate carry `templateRef.template`;
    steps defined inline in the calling template carry `templateName`.
    """
    template_ref = node.get("templateRef") or {}
    return node.get("templateName") or template_ref.get("template") or "(unknown)"


def _attempt_label(exit_code: str, message: str) -> str:
    """Label a failed attempt with its cause, falling back to the exit code.

    The message is authoritative — see ATTEMPT_CAUSES for why the code is not.
    """
    lowered = (message or "").lower()
    for needle, cause in ATTEMPT_CAUSES:
        if needle in lowered:
            return f"{exit_code} {cause}"
    fallback = NOTABLE_EXIT_CODES.get(exit_code)
    return f"{exit_code} {fallback}" if fallback else exit_code


def count_pod_attempts(workflows: list[dict]) -> dict[str, collections.Counter]:
    """Count pod attempts per step, keyed by `"0"` or by a failure label.

    One entry per *attempt*, not per step: `retryStrategy` leaves every attempt
    in `status.nodes`, and an attempt that OOMed before a retry succeeded is
    only visible here. Both the StepOutcome record and the workflow phase
    describe the final state and show nothing.

    Clean attempts are counted under `"0"`; failed ones under a label carrying
    both the code and, where the node message gives it, the cause — so an
    OOMKill and a spot reclaim do not collapse into a single "137" tally.

    A pod that failed without producing an exit code (killed before the wait
    sidecar could record one) is counted under `"?"` rather than dropped.
    Attempts still running have no terminal state and are not counted at all.
    """
    per_step: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for wf in workflows:
        for node in ((wf.get("status") or {}).get("nodes") or {}).values():
            if node.get("type") != "Pod":
                continue
            exit_code = (node.get("outputs") or {}).get("exitCode")
            if exit_code is None:
                if node.get("phase") not in ("Failed", "Error"):
                    continue  # running, pending, or skipped — no terminal state yet
                exit_code = "?"
            exit_code = str(exit_code)
            key = "0" if exit_code == "0" else _attempt_label(exit_code, node.get("message", ""))
            per_step[_node_step(node)][key] += 1
    return dict(per_step)


def split_bold_evidence(df: pd.DataFrame, subjects: list[str]) -> tuple[list[str], list[str]]:
    """Split `subjects` into (has BOLD evidence, has none) given functional outcomes.

    Pure and shared on purpose. check_func_preproc_coverage EXCLUDES the second
    group from its denominator, and check_anat_only_delivery is the check that
    then holds that group to account — if the two computed the set separately
    they could drift, and a subject could fall through the gap between them.

    Positive evidence of BOLD is either a per-run row or any functional-phase row
    that is not a skip; see check_func_preproc_coverage for why presence alone is
    insufficient.
    """
    in_scope = set(df["subject"].tolist())
    per_run = df[(df["task"] != "na") & (df["run"] != "na")]
    functional_non_skip = df[df["step"].isin(FUNCTIONAL_PHASE_STEPS) & (df["status"] != "skipped")]
    has_bold = set(per_run["subject"].tolist()) | set(functional_non_skip["subject"].tolist())
    eligible = [s for s in subjects if s in in_scope and s in has_bold]
    no_bold = [s for s in subjects if s not in in_scope or s not in has_bold]
    return eligible, no_bold


class TestBatchValidator:
    """Run pass/fail validation checks against cloudpipe_metrics Athena tables."""

    # Class-level defaults so an instance built without __init__ still works.
    # tests/test_validate_test_batch.py constructs via __new__ to inject a mock
    # metrics client, and every check has to stay reachable that way.
    _func_df: pd.DataFrame | None = None
    _no_bold: list[str] | None = None
    _s3 = None
    _data_bucket = "<YOUR_S3_BUCKET>"
    _region = "<YOUR_AWS_REGION>"

    def __init__(
        self,
        subjects: list[str],
        bucket: str = "cloudpipe-metrics",
        region: str = "<YOUR_AWS_REGION>",
        cost_date_from: str | None = None,
        cost_date_to: str | None = None,
        data_bucket: str = "<YOUR_S3_BUCKET>",
    ):
        self.subjects = subjects
        self._m = CloudpipeMetrics(bucket=bucket, region=region)
        self._subject_in = ", ".join(f"'{s}'" for s in subjects)
        # metrics/costs/ accumulates across batches, so cost queries are scoped
        # by scrape date as well as subject — see check_cost_summary.
        self._cost_date_from = cost_date_from
        self._cost_date_to = cost_date_to
        # Derivatives live in the data bucket, not the metrics bucket. Only
        # check_anat_only_delivery reads it, so the client is built on demand —
        # every other check is Athena-only and must not require S3 credentials.
        self._data_bucket = data_bucket
        self._region = region
        self._s3 = None
        # Shared between check_func_preproc_coverage and
        # check_anat_only_delivery so the two cannot disagree about which
        # subjects have BOLD evidence. Order-independent: whichever runs first
        # populates them.
        self._func_df: pd.DataFrame | None = None
        self._no_bold: list[str] | None = None

    def _s3_client(self):
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client("s3", region_name=self._region)
        return self._s3

    def _functional_outcomes(self) -> pd.DataFrame:
        """Functional-phase (plus t1w-to-mni) step outcomes for the batch, cached."""
        if self._func_df is None:
            steps_sql = ", ".join(f"'{s}'" for s in (*FUNCTIONAL_PHASE_STEPS, "t1w-to-mni"))
            self._func_df = self._m._run_sql(f"""
                SELECT subject, session, task, run, status, step, failure_reason
                FROM cloudpipe_metrics.step_outcomes
                WHERE subject IN ({self._subject_in})
                  AND step IN ({steps_sql})
                ORDER BY subject, session, task, run
            """)
        return self._func_df

    # ------------------------------------------------------------------
    # Outcome tracking checks
    # ------------------------------------------------------------------

    def check_subject_manifests(self) -> ValidationResult:
        """All subjects must have a SubjectManifest record."""
        df: pd.DataFrame = self._m._run_sql(f"""
            SELECT DISTINCT subject
            FROM cloudpipe_metrics.subject_manifests
            WHERE subject IN ({self._subject_in})
        """)
        found_set = set(df["subject"].tolist()) if not df.empty else set()
        missing = [s for s in self.subjects if s not in found_set]
        return ValidationResult(
            check="SubjectManifest",
            passed=len(missing) == 0,
            found=len(self.subjects) - len(missing),
            expected=len(self.subjects),
            missing=missing,
            triage="Check exit_handler.py pod logs for these subjects in Argo UI.",
        )

    def check_step_outcomes(self) -> ValidationResult:
        """All expected step names must appear in StepOutcome records."""
        df: pd.DataFrame = self._m._run_sql(f"""
            SELECT step, COUNT(DISTINCT subject) AS subject_count
            FROM cloudpipe_metrics.step_outcomes
            WHERE subject IN ({self._subject_in})
            GROUP BY step
        """)
        found_steps = set(df["step"].tolist()) if not df.empty else set()
        missing = [s for s in EXPECTED_STEP_NAMES if s not in found_steps]
        return ValidationResult(
            check="StepOutcome steps",
            passed=len(missing) == 0,
            found=len(EXPECTED_STEP_NAMES) - len(missing),
            expected=len(EXPECTED_STEP_NAMES),
            missing=missing,
            triage="Missing step names indicate outcome-recorder tasks not firing for those steps.",
        )

    def check_workflow_run_schema(self) -> ValidationResult:
        """Most-recent WorkflowRun per subject must be at schema_version 1.1.

        Subjects may have older records from prior runs (workflow-runs/ S3 keys are
        workflow-name-keyed and cannot be flushed per-subject). Only the latest run counts.
        """
        df: pd.DataFrame = self._m._run_sql(f"""
            SELECT schema_version, COUNT(*) AS cnt
            FROM (
                SELECT subject, schema_version,
                       ROW_NUMBER() OVER (
                           PARTITION BY subject ORDER BY completed_at DESC
                       ) AS rn
                FROM cloudpipe_metrics.workflow_runs
                WHERE subject IN ({self._subject_in})
            ) t
            WHERE rn = 1
            GROUP BY schema_version
        """)
        if df.empty:
            return ValidationResult(
                check="WorkflowRun schema v1.1",
                passed=True,
                found=0,
                expected=0,
                missing=[],
                triage="",
            )
        wrong = df[df["schema_version"] != "1.1"]
        correct = (
            int(df[df["schema_version"] == "1.1"]["cnt"].astype(int).sum()) if not df.empty else 0
        )
        total = int(df["cnt"].astype(int).sum())
        missing = [
            f"schema_version={row['schema_version']} ({row['cnt']} rows)"
            for _, row in wrong.iterrows()
        ]
        return ValidationResult(
            check="WorkflowRun schema v1.1",
            passed=len(missing) == 0,
            found=correct,
            expected=total,
            missing=missing,
            triage=(
                "Rows at older schema versions mean the workflow ran with a stale exit_handler "
                "image. Check the image SHA in the metrics-exit-handler WorkflowTemplate."
            ),
        )

    def check_func_preproc_coverage(self) -> ValidationResult:
        """Every func-preproc (session, task, run) for each subject must have succeeded.

        Reports at run granularity (issue #146): the denominator is expected
        (session, task, run) triples, not subjects. A subject-level denominator
        undercounts loss 5:1 whenever a multi-session subject has one bad session
        among otherwise-good ones — the subject still "has func-preproc records
        somewhere" and reads as covered while a whole session's runs are missing.
        The 2026-08-04 100-subject pilot read `98/99 subjects` for what was really
        `1438/1467 runs` (29 runs across 4 subjects, 3 of which had another
        healthy session masking the gap).

        Subjects with no BOLD runs in the requested scan types are excluded from the
        denominator rather than counted as gaps. Not every ABCD subject has usable
        rest/nback data — sub-107UCJ69 has no minimally preprocessed BOLD at all
        (it is absent from mmps_mproc/), so subject-data-inventory returned
        `runs:[]` and every BOLD-dependent step was correctly skipped. Counting that
        as a coverage failure made a healthy batch report FAIL and buried a real
        per-run failure in the same list.

        A subject is in scope if it has a per-run functional outcome, or any
        functional-phase outcome that is not a skip. Merely having functional-phase
        rows is NOT sufficient: the master template emits session-level `skipped`
        rows for a subject with no BOLD, so that weaker test re-created the very
        false failure this exclusion exists to prevent (observed 2026-07-31). A
        subject that registered BOLD but produced no func-preproc records still has
        non-skip rows, so it remains a genuine gap in the denominator.

        A missing run whose session failed the t1w-to-mni QC gate is a known,
        explainable gap (func-preproc is correctly skipped downstream of a QC
        rejection) — it is reported in `details`, not `missing`, so it does not
        read the same as an unexplained recording failure. See #146.

        There are TWO such QC gates, and until #221 this only knew about the
        session-level one. `bold-to-t1w` rejects individual runs on its
        relative-NMI sanity floor (exit 65) and the session driver discards the
        output, so func-preproc then fails on a transform that genuinely does not
        exist — for that one run, in a session whose t1w-to-mni succeeded. Those
        runs are reported alongside the session-gated ones instead of as
        unexplained gaps; the 2026-08-10 200-subject batch read `RESULT: FAIL` on
        six of them while having zero actual defects. A bold-to-t1w failure with
        any other cause (OOMKill, spot preemption, a mid-loop pod death) is still
        an unexplained gap, annotated with the recorded reason.

        Since #222 the func-preproc driver records such a run as `skipped` with
        `upstream_failed_step="bold-to-t1w"` rather than letting it fail on the
        absent transform. That is deliberately NOT what this keys off — any
        non-`succeeded` status routes through the same bold-to-t1w evidence
        check, so records from either side of that change are explained
        identically and a driver that regressed to `failed` would not silently
        become an unexplained gap.

        Limitation: if outcome recording failed for *every* functional step of a
        subject that did have BOLD, it is indistinguishable here from a subject with
        no BOLD, and would be silently excluded. Exclusions are listed in `details`
        for exactly that reason — check them against the batch you expected.
        `check_anat_only_delivery` now holds every excluded subject to account
        against its anatomical derivatives, so an exclusion that is really a lost
        subject fails there rather than passing silently here (plan 009 §4).
        """
        df: pd.DataFrame = self._functional_outcomes()

        # An entirely empty result is NOT evidence that every subject is func-less —
        # it is also exactly what a broken outcome recorder looks like, and passing
        # here would make "recorded nothing" indistinguishable from "nothing to
        # record". Excluding a subject requires positive proof that recording worked
        # somewhere in this batch, so an empty result stays a failure.
        if df.empty:
            return ValidationResult(
                check="func-preproc per-run coverage",
                passed=False,
                found=0,
                expected=len(self.subjects),
                missing=[f"{s} (no func-preproc records)" for s in self.subjects],
                triage=(
                    "No func-preproc step_outcomes found. "
                    "Check that outcome-recorder tasks are firing."
                ),
            )

        # df is non-empty, so at least one subject recorded a functional step and
        # `eligible` cannot be empty.
        #
        # Presence in df is NOT evidence the subject has BOLD. A subject with no
        # usable functional scans still produces functional-phase rows: the master
        # template records one session-level `skipped` /
        # failure_category=dependency row per BOLD-dependent step, all with
        # task == run == "na". Those rows postdate the docstring's original
        # premise (that such a subject wrote no records at all), and because the
        # per-run filter below drops every "na" row, treating them as in-scope
        # makes the subject an unfixable coverage gap.
        #
        # So require positive evidence of BOLD: either a per-run row, or any row
        # that is not a skip. A skip alone proves the recorder fired — which is
        # what the empty-df guard above needs — but says nothing about whether
        # there was data to process. A subject whose BOLD steps were skipped
        # because an upstream step FAILED still has a non-skip (failed) row and
        # stays in the denominator, which is the case worth catching.
        #
        # This must be restricted to FUNCTIONAL_PHASE_STEPS rows. `df` also
        # carries `t1w-to-mni` rows (added below for QC gating), and
        # t1w-to-mni runs unconditionally on every subject regardless of BOLD
        # presence — a BOLD-less subject's `t1w-to-mni succeeded` row is not
        # evidence of BOLD and must not promote it into `has_bold` (#153).
        per_run = df[(df["task"] != "na") & (df["run"] != "na")]
        eligible, no_func_data = split_bold_evidence(df, self.subjects)
        self._no_bold = no_func_data

        details = []
        if no_func_data:
            details.append(
                f"{len(no_func_data)} subject(s) excluded — no BOLD runs in the "
                f"requested scan types: {', '.join(no_func_data)} "
                "(held to account by the 'anat-only delivery' check)"
            )

        # Per-session t1w-to-mni QC status. A session that failed this gate
        # correctly produces no func-preproc records downstream — the coverage
        # gap is real but explained, not a recording defect. See #146.
        t1w_mni = df[df["step"] == "t1w-to-mni"]
        t1w_mni_status = t1w_mni.groupby(["subject", "session"])["status"].last().to_dict()

        # Any per-run row, from any functional-phase step, is positive evidence
        # that a (subject, session, task, run) triple was attempted — bold-to-t1w
        # in particular runs in parallel with t1w-to-mni and unconditionally on
        # BOLD presence, so it records per-run triples even for sessions whose
        # t1w-to-mni failed. That is the run-level denominator: expected triples,
        # not subjects.
        #
        # Exclude session-level aggregate rows (task == "na", run == "na") from
        # this set. The master WorkflowTemplate's outcome-recorder emits one of
        # these per session when the whole func-preproc phase is skipped
        # (func_exists already True on a resubmit) — same subject/session/step
        # as the real per-run rows, but with no run-level detail. Only per-run
        # rows carry coverage evidence; aggregate rows carry none either way.
        attempted = per_run[per_run["subject"].isin(eligible)]
        triples = attempted[["subject", "session", "task", "run"]].drop_duplicates()
        subjects_with_per_run_data = set(attempted["subject"].tolist())

        # Eligible subjects with zero per-run rows in ANY functional-phase step
        # (only session-level skip/failure aggregates) can't be evaluated at run
        # granularity — there is no run-level detail to look up. Fall back to a
        # subject-level gap, same as before #146.
        no_records = [s for s in eligible if s not in subjects_with_per_run_data]

        func = df[(df["step"] == "func-preproc") & (df["task"] != "na") & (df["run"] != "na")]
        func_status = func.groupby(["subject", "session", "task", "run"])["status"].last()

        b2t_rows = per_run_bold_to_t1w_rows(df)

        unexplained_missing: list[str] = []
        subjects_with_unexplained_gaps: set[str] = set()
        gated: list[tuple[str, str]] = []  # (subject, session) pairs, deduped below
        run_gated: list[tuple[str, str, str, str]] = []  # (subject, session, task, run)
        found = 0
        for row in triples.itertuples(index=False):
            key = (row.subject, row.session, row.task, row.run)
            status = func_status.get(key)
            if status == "succeeded":
                found += 1
                continue

            # The session gate takes precedence over the per-run one: in a
            # session whose t1w-to-mni failed, bold-to-t1w's own outcome is not
            # meaningful evidence about why func-preproc has no record.
            session_qc = t1w_mni_status.get((row.subject, row.session))
            if session_qc is not None and session_qc != "succeeded":
                gated.append((row.subject, row.session))
                continue

            qc_rejected, b2t_note = classify_bold_to_t1w_evidence(b2t_rows.get(key))
            if qc_rejected:
                run_gated.append(key)
                continue

            subjects_with_unexplained_gaps.add(row.subject)
            if status is None:
                unexplained_missing.append(
                    f"{row.subject} {row.session} {row.task} {row.run} "
                    f"(no func-preproc records){b2t_note}"
                )
            else:
                unexplained_missing.append(
                    f"{row.subject} {row.session} {row.task} {row.run} [{status}]{b2t_note}"
                )

        missing = [f"{s} (no func-preproc records)" for s in no_records] + unexplained_missing

        if no_records:
            details.append(
                f"{len(no_records)} eligible subject(s) with zero per-run records "
                f"(subject-level gap, excluded from the run denominator below): "
                + ", ".join(sorted(no_records))
            )

        gated_sessions = sorted(set(gated))
        if gated_sessions:
            details.append(
                f"sessions gated by t1w-to-mni QC: {len(gated_sessions)} ({len(gated)} runs) "
                "— func-preproc correctly skipped, not a recording gap: "
                + ", ".join(f"{s} {ses}" for s, ses in gated_sessions)
            )

        gated_runs = sorted(set(run_gated))
        if gated_runs:
            details.append(
                f"runs gated by bold-to-t1w QC: {len(gated_runs)} — the registration "
                "was rejected as degenerate and its transform discarded, so "
                "func-preproc has no output for the run (recorded `skipped` since "
                "issue #222, `failed` before it), not a recording gap: "
                + ", ".join(f"{s} {ses} {t} {r}" for s, ses, t, r in gated_runs)
            )

        # Subject-level figure kept for continuity with the old headline number,
        # but it is not the number the batch is judged on — a subject with one
        # bad session among good ones is NOT fully covered even though it has
        # func-preproc records somewhere.
        if subjects_with_per_run_data:
            subjects_fully_covered = len(subjects_with_per_run_data) - len(
                subjects_with_unexplained_gaps
            )
            details.append(
                f"subject-level (context only): {subjects_fully_covered}/"
                f"{len(subjects_with_per_run_data)} subjects with zero unexplained gaps"
            )

        # Run-only denominator (#153): `no_records` are subjects, `triples` are
        # (subject, session, task, run) tuples — mixing them counts one
        # no-records subject as exactly one missing run, which is only right
        # by coincidence. no_records subjects are surfaced separately above
        # and remain in `missing`, so they still fail the check; they just
        # aren't folded into a run-unit ratio they don't belong in.
        expected = len(triples)

        return ValidationResult(
            check="func-preproc per-run coverage",
            passed=len(missing) == 0,
            found=found,
            expected=expected,
            missing=missing,
            details=details,
            triage=(
                "Check bold-to-t1w and func-preproc step_outcomes for failed subjects. "
                "Re-submit the workflow for affected subjects. Runs listed under "
                "'gated by t1w-to-mni QC' or 'gated by bold-to-t1w QC' in details "
                "need no action — they are the correct result of a QC rejection, not "
                "a recording defect."
            ),
        )

    def check_anat_only_delivery(self) -> ValidationResult:
        """Subjects excluded from func coverage must still have delivered anatomy.

        The blind spot this closes (plan 009 §4): coverage is counted in
        functional runs, so a subject with zero expected runs contributes 0 to
        both the numerator and the denominator and is indistinguishable from a
        subject that completed. An anat-only subject can therefore be lost
        outright — its anatomical phase hung or failed — while the batch reads
        clean. `sub-6LMU82AJ` was lost that way in the 2026-08-14 batch and did
        not appear in that batch's FAIL list; the FAIL came from unrelated causes,
        so had those been absent the loss would have gone unreported.

        The existence test is the ADR 017 `_complete.json` marker and nothing
        else. A prefix listing, an object count, or a probe for any single file
        all answer "yes" for a half-uploaded tree, and reaching that state needs
        no bug — a spot reclaim mid-upload produces it. The marker is written
        only after every other object in the tree, so it is the one object whose
        presence implies the rest. `src/inventory.py` gates on it identically.

        Expected sessions come from the batch's own StepOutcome records rather
        than from a static roster: the question is whether what the pipeline
        actually saw for this subject was delivered.

        Note on `long-template/`: it is reported but does NOT decide pass/fail.
        The longitudinal template's presence for a single-session subject was not
        verifiable when this was written (`derivatives/` had been flushed), and a
        validator that fails a healthy batch on an unverified assumption is the
        recurring defect this file's history is mostly about — see the
        false-FAIL notes in check_func_preproc_coverage. Session markers decide;
        a missing template surfaces as a warning to investigate.
        """
        if self._no_bold is None:
            df = self._functional_outcomes()
            if df.empty:
                # An empty result is already a hard failure in the coverage
                # check; do not also claim anything about anatomy here.
                return ValidationResult(
                    check="anat-only delivery",
                    passed=True,
                    warning=True,
                    found=0,
                    expected=0,
                    details=[
                        "No functional step_outcomes at all — anat-only delivery NOT "
                        "checked. See the func-preproc coverage failure."
                    ],
                    triage="Fix outcome recording first, then re-run.",
                )
            _, self._no_bold = split_bold_evidence(df, self.subjects)

        excluded = self._no_bold
        if not excluded:
            return ValidationResult(
                check="anat-only delivery",
                passed=True,
                found=0,
                expected=0,
                details=["No subjects were excluded from func-preproc coverage."],
            )

        subject_in = ", ".join(f"'{s}'" for s in excluded)
        ses_df: pd.DataFrame = self._m._run_sql(f"""
            SELECT DISTINCT subject, session
            FROM cloudpipe_metrics.step_outcomes
            WHERE subject IN ({subject_in})
              AND session <> 'na'
        """)
        sessions: dict[str, list[str]] = {}
        for row in ses_df.itertuples(index=False):
            sessions.setdefault(row.subject, []).append(row.session)

        s3 = self._s3_client()

        def marker_exists(key: str) -> bool:
            try:
                s3.head_object(Bucket=self._data_bucket, Key=key)
                return True
            except Exception:  # noqa: BLE001 — absent marker is the answer, not an error
                return False

        missing: list[str] = []
        details: list[str] = []
        template_warnings: list[str] = []
        delivered = 0
        for subj in excluded:
            ses = sorted(sessions.get(subj, []))
            if not ses:
                # No session-level record at all: the subject did not get far
                # enough to enumerate its sessions, which is itself the loss.
                missing.append(f"{subj} (no session-level StepOutcome records — subject lost?)")
                continue

            gaps = [
                s
                for s in ses
                if not marker_exists(f"derivatives/fastsurfer/{subj}/{s}/_complete.json")
            ]
            if gaps:
                missing.append(
                    f"{subj} ({len(gaps)}/{len(ses)} session(s) without "
                    f"_complete.json: {', '.join(gaps)})"
                )
                continue

            delivered += 1
            if not marker_exists(f"derivatives/fastsurfer/{subj}/long-template/_complete.json"):
                template_warnings.append(f"{subj} ({len(ses)} session(s))")

        details.append(
            f"{len(excluded)} subject(s) had no BOLD evidence and were excluded from "
            f"func-preproc coverage; {delivered}/{len(excluded)} have complete "
            "anatomical derivatives for every session the pipeline recorded."
        )
        if template_warnings:
            details.append(
                f"{len(template_warnings)} of those lack "
                "`long-template/_complete.json` despite complete per-session trees "
                "(reported, not failed — see docstring): " + ", ".join(template_warnings)
            )

        return ValidationResult(
            check="anat-only delivery",
            passed=len(missing) == 0,
            warning=bool(template_warnings),
            found=delivered,
            expected=len(excluded),
            missing=missing,
            details=details,
            triage=(
                "These subjects contribute no functional runs, so they are invisible "
                "to the coverage check by construction. A missing _complete.json means "
                "the anatomical phase did not deliver — check the fastsurfer steps in "
                "the Argo UI for a hang or a failure, and re-submit the subject. "
                "Do not substitute a prefix listing for the marker: a half-uploaded "
                "tree lists non-empty."
            ),
        )

    def check_terminal_workflow_phase(self, workflows: list[dict] | None) -> ValidationResult:
        """A workflow that ends `Error` is a lost subject even if no count moved.

        Plan 009 §4's second half. Every other check reads records the pipeline
        *wrote*; a workflow that died before writing them moves no numerator and
        no denominator, so it can vanish from the report entirely — which is the
        same shape of blind spot as the anat-only one.

        `Error` fails; `Failed` is reported but not failed here. The distinction
        is deliberate: `Failed` is the ordinary outcome of a step that ran and
        returned non-zero, and the QC gates make some of those correct results,
        already classified by the coverage check. Failing on `Failed` here would
        re-fail runs that check just explained — and gating on
        `status != succeeded` rather than on a specific exit code is a
        false-alarm generator this file has been bitten by before.

        Scoped per subject, not per workflow: under `--since` a batch may contain
        a resubmission, so an `Error` workflow whose subject also has a
        `Succeeded` one is a recovered subject and is reported without failing.
        """
        if not workflows:
            return ValidationResult(
                check="Terminal workflow phase",
                passed=True,
                warning=True,
                found=0,
                expected=0,
                details=[
                    "No Argo workflows readable — terminal phase NOT checked. "
                    "Workflow objects expire 24h after completion (ttlStrategy)."
                ],
                triage="Run within a day of the batch, with cluster access.",
            )

        by_subject: dict[str, set[str]] = {}
        for wf in workflows:
            subject = ((wf.get("metadata") or {}).get("labels") or {}).get("subjectid")
            phase = (wf.get("status") or {}).get("phase") or "Unknown"
            if subject:
                by_subject.setdefault(subject, set()).add(phase)

        unrecovered: list[str] = []
        recovered: list[str] = []
        failed_subjects: list[str] = []
        for subject, phases in sorted(by_subject.items()):
            if "Error" in phases:
                if "Succeeded" in phases:
                    recovered.append(subject)
                else:
                    unrecovered.append(f"{subject} (workflow phase Error, no Succeeded run)")
            elif "Failed" in phases:
                failed_subjects.append(subject)

        details = [f"Scope: {len(workflows)} workflows over {len(by_subject)} subjects."]
        if recovered:
            details.append(
                f"{len(recovered)} subject(s) had an Error workflow but also a "
                "Succeeded one (resubmitted and recovered): " + ", ".join(recovered)
            )
        if failed_subjects:
            details.append(
                f"{len(failed_subjects)} subject(s) ended `Failed` — reported here, "
                "classified by the coverage check, not failed on phase alone: "
                + ", ".join(failed_subjects)
            )

        return ValidationResult(
            check="Terminal workflow phase",
            passed=not unrecovered,
            warning=bool(failed_subjects) or bool(recovered),
            found=len(by_subject) - len(unrecovered),
            expected=len(by_subject),
            missing=unrecovered,
            details=details,
            triage=(
                "Phase `Error` means the workflow itself died — a controller error, a "
                "deleted pod, or an exhausted retry on a node-level kill — rather than "
                "a step returning non-zero. It writes no outcome records, so no "
                "coverage number moves. Check the Argo UI for the workflow's message "
                "and re-submit the subject."
            ),
        )

    def check_pod_attempts(
        self, workflows: list[dict] | None, since: datetime | None = None
    ) -> ValidationResult:
        """Surface pod attempts that exited non-zero, whatever the step's final status.

        Every other check in this file reads records that describe a step
        *after* `retryStrategy` has finished with it. A step that OOMed and then
        succeeded on retry leaves a `succeeded` StepOutcome, complete outputs
        and a `Succeeded` workflow — so it passes all of them, silently. That is
        how the bold-to-t1w memory regression survived a validation gate on the
        2026-08-01 01:58Z batch (issue #114): the validator said PASS while 4 of
        25 bold-to-t1w attempts were being OOMKilled.

        It matters beyond bookkeeping because the retry budget is shared with
        spot interruptions: a step that only passes because it had retries left
        is one interruption away from failing outright. Recovered OOMs are a
        leading indicator of the failure that eventually lands.

        Any non-zero attempt warns. A step is failed only once non-zero attempts
        reach POD_ATTEMPT_FAIL_FRACTION of at least POD_ATTEMPT_FAIL_MIN
        attempts, because the point is to make the signal visible, not to fail
        every batch that ever retried a pod.

        The scope it counted over is always printed. Argo retains completed
        workflows for a day, so the set this reads is a function of what else
        ran recently; if it silently widens, a fixed regression reads as live
        (issue #140). `since` is reported only — the caller has already applied
        it in `select_batch_workflows`.
        """
        if workflows is None:
            return ValidationResult(
                check="Pod attempt exit codes",
                passed=True,
                warning=True,
                found=0,
                expected=0,
                details=["Argo workflow status unreadable — retried failures NOT checked."],
                triage="Needs `argo` on PATH and cluster access.",
            )
        if not workflows:
            return ValidationResult(
                check="Pod attempt exit codes",
                passed=True,
                warning=True,
                found=0,
                expected=0,
                details=[
                    "No workflows found for these subjects — retried failures NOT checked. "
                    "Per-attempt data expires with the workflow (ttlStrategy: 24h)."
                    + (
                        f" Scoped to workflows started at or after {since.isoformat()}."
                        if since
                        else ""
                    )
                ],
                triage=(
                    "Run this check within a day of the batch completing, and "
                    "check --since is not later than the batch submission time."
                ),
            )

        per_step = count_pod_attempts(workflows)
        total = sum(sum(c.values()) for c in per_step.values())
        clean = sum(c.get("0", 0) for c in per_step.values())

        scope = (
            f"submitted at or after {since.isoformat().replace('+00:00', 'Z')}"
            if since is not None
            else "most recent workflow per subject"
        )
        scope_line = (
            f"Scope: {len(workflows)} workflows over {len(self.subjects)} subjects ({scope})."
        )

        # Kept apart from the scope line: only these carry the warning. The
        # scope line is context and is printed on a clean batch too.
        findings: list[str] = []
        failing_steps: list[str] = []
        for step, counter in sorted(per_step.items()):
            attempts = sum(counter.values())
            bad = attempts - counter.get("0", 0)
            if not bad:
                continue
            breakdown = ", ".join(
                f"{label}×{n}" for label, n in sorted(counter.items()) if label != "0"
            )
            line = f"{step}: {bad}/{attempts} attempts non-zero ({breakdown})"
            findings.append(line)
            if attempts >= POD_ATTEMPT_FAIL_MIN and bad / attempts >= POD_ATTEMPT_FAIL_FRACTION:
                failing_steps.append(line)

        return ValidationResult(
            check="Pod attempt exit codes",
            passed=not failing_steps,
            warning=bool(findings),
            found=clean,
            expected=total,
            missing=failing_steps,
            details=[scope_line, *findings],
            triage=(
                "Steps above the failure threshold are being retried into success. "
                "Read the cause, not the code: OOMKilled is a memory-request problem, "
                "spot interruption is a capacity one. Resolve before treating this "
                "batch as a baseline."
            ),
        )

    # ------------------------------------------------------------------
    # Cost data checks
    # ------------------------------------------------------------------

    def check_kubecost_api(self, window_start: str, window_end: str) -> ValidationResult:
        """All subjects must have totalCost > 0 in Kubecost for the run window."""
        url = (
            f"{KUBECOST_BASE_URL}/model/allocation"
            f"?window={window_start},{window_end}"
            f"&aggregate=label:subjectid"
            f"&filterNamespaces=argo-workflows"
            f"&accumulate=true"
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # suppress InsecureRequestWarning
            resp = requests.get(url, verify=False, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        found_subjects: set[str] = set()
        for allocation_set in data.get("data", []):
            for key, alloc in allocation_set.items():
                if key.startswith("__"):
                    continue
                if alloc.get("totalCost", 0) > 0:
                    found_subjects.add(key)

        missing = [s for s in self.subjects if s not in found_subjects]
        return ValidationResult(
            check="Kubecost API cost attribution",
            passed=len(missing) == 0,
            found=len(self.subjects) - len(missing),
            expected=len(self.subjects),
            missing=missing,
            triage=(
                "Check that workflows completed within the query window. "
                "Re-run with adjusted --window-end if needed. "
                "Confirm the subjectid pod label is set in the master WorkflowTemplate."
            ),
        )

    def check_cost_athena(self) -> ValidationResult:
        """All subjects must have a cost record written by the kubecost-cost-scraper."""
        where = cost_scope_clause(
            self.subjects,
            self._cost_date_from,
            self._cost_date_to,
            extra=["total_cost_usd > 0"],
        )
        df: pd.DataFrame = self._m._run_sql(f"""
            SELECT DISTINCT subject
            FROM cloudpipe_metrics.costs
            {where}
        """)
        found_set = set(df["subject"].tolist()) if not df.empty else set()
        missing = [s for s in self.subjects if s not in found_set]
        return ValidationResult(
            check="Athena cost rows (total_cost_usd)",
            passed=len(missing) == 0,
            found=len(self.subjects) - len(missing),
            expected=len(self.subjects),
            missing=missing,
            triage=(
                "Trigger the kubecost-cost-scraper Prefect flow if it hasn't run yet "
                "(runs nightly at 02:00 UTC), then re-validate."
            ),
        )

    def check_cost_summary(self) -> ValidationResult:
        """Per-subject cost statistics for the batch (informational — always passes).

        CostAllocation records are one row per Argo workflow per scrape date, and
        `metrics/costs/` retains records from earlier batches, so averaging raw
        rows understates cost per subject (a retried or multi-day subject
        contributes several small rows) and mixes in unrelated history. Costs are
        therefore summed per subject inside a subquery, scoped by the batch
        subject list and — when --window-start/--window-end are given — by the
        scrape-date window, before the distribution is computed.
        """
        where = cost_scope_clause(
            self.subjects,
            self._cost_date_from,
            self._cost_date_to,
            extra=["total_cost_usd > 0"],
        )
        df: pd.DataFrame = self._m._run_sql(f"""
            SELECT
                COUNT(*)                                          AS n_subjects,
                ROUND(SUM(subject_cost), 2)                       AS total_cost,
                ROUND(AVG(subject_cost), 3)                       AS mean_cost,
                ROUND(approx_percentile(subject_cost, 0.5), 3)    AS median_cost,
                ROUND(MIN(subject_cost), 3)                       AS min_cost,
                ROUND(MAX(subject_cost), 3)                       AS max_cost,
                SUM(n_records)                                    AS n_rows
            FROM (
                SELECT subject,
                       SUM(total_cost_usd) AS subject_cost,
                       COUNT(*)            AS n_records
                FROM cloudpipe_metrics.costs
                {where}
                GROUP BY subject
            )
        """)
        if df.empty or int(df.iloc[0]["n_subjects"]) == 0:
            return ValidationResult(
                check="Batch cost statistics",
                passed=True,
                found=0,
                expected=len(self.subjects),
            )
        row = df.iloc[0]
        # found is a subject count now — comparable to expected.
        n_subjects = int(row["n_subjects"])
        n_rows = int(row["n_rows"])
        details = [
            f"Total: ${float(row['total_cost']):.2f}  "
            f"Mean: ${float(row['mean_cost']):.3f}  "
            f"Median: ${float(row['median_cost']):.3f}  "
            f"Range: ${float(row['min_cost']):.3f}–${float(row['max_cost']):.3f}",
            f"Per-subject totals over {n_rows} workflow-day cost records",
        ]
        if not (self._cost_date_from or self._cost_date_to):
            details.append(
                "WARNING: no scrape-date window — totals may include costs from "
                "earlier runs of these subjects."
            )
        return ValidationResult(
            check="Batch cost statistics",
            passed=True,
            found=n_subjects,
            expected=len(self.subjects),
            details=details,
        )


# ------------------------------------------------------------------
# Reporting
# ------------------------------------------------------------------


def print_report(results: list[ValidationResult], run_date: str) -> bool:
    """Print a structured pass/fail report. Returns True if all checks passed."""
    print(f"\n=== cloudpipe test batch validation — {run_date} ===\n")

    outcome_header_printed = False
    cost_header_printed = False

    for r in results:
        is_cost = r.check.startswith(("Kubecost", "Athena cost", "Batch cost"))
        if not is_cost and not outcome_header_printed:
            print("OUTCOME TRACKING")
            outcome_header_printed = True
        elif is_cost and not cost_header_printed:
            print("\nCOST DATA")
            cost_header_printed = True

        status = "✗" if not r.passed else ("⚠" if r.warning else "✓")
        print(f"  {r.check:<38} {r.found}/{r.expected} {status}")
        for detail in r.details:
            print(f"    {detail}")

    overall = all(r.passed for r in results)
    warned = [r for r in results if r.passed and r.warning]
    if not overall:
        verdict = "FAIL"
    elif warned:
        # Never an unqualified PASS while something is warning: a batch that
        # only passed because retries absorbed its failures is not a clean
        # baseline, and reporting it as one is what issue #114 was about.
        verdict = f"PASS WITH WARNINGS ({len(warned)})"
    else:
        verdict = "PASS"
    print(f"\nRESULT: {verdict}\n")

    for r in results:
        if (not r.passed or r.warning) and r.missing:
            print(f"Missing from {r.check}:")
            for item in r.missing[:20]:
                print(f"  {item}")
            if len(r.missing) > 20:
                print(f"  ... and {len(r.missing) - 20} more")
            print(f"Action: {r.triage}\n")

    return overall


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate cloudpipe test batch run — outcome tracking and cost data."
    )
    parser.add_argument(
        "--subjects", required=True, help="Path to subjects CSV (subject_id column)"
    )
    parser.add_argument("--bucket", default="cloudpipe-metrics")
    parser.add_argument(
        "--data-bucket",
        default="<YOUR_S3_BUCKET>",
        help=(
            "Bucket holding derivatives/, read by the anat-only delivery check "
            "(the `bucket` key in the cloudpipe-config ConfigMap)"
        ),
    )
    parser.add_argument("--region", default="<YOUR_AWS_REGION>")
    parser.add_argument(
        "--namespace",
        default=ARGO_NAMESPACE,
        help="Argo namespace to read per-attempt pod records from",
    )
    parser.add_argument(
        "--since",
        help=(
            "Only count pod attempts from workflows submitted at or after this "
            "RFC3339 UTC time (the batch submission timestamp). Defaults to the "
            "most recent workflow per subject."
        ),
    )
    parser.add_argument("--cost", action="store_true", help="Also validate Kubecost cost data")
    parser.add_argument(
        "--window-start",
        help="Kubecost window start, RFC3339 UTC — required with --cost",
    )
    parser.add_argument(
        "--window-end",
        help="Kubecost window end, RFC3339 UTC — required with --cost",
    )
    args = parser.parse_args()

    if args.cost and not (args.window_start and args.window_end):
        parser.error("--window-start and --window-end are required when --cost is specified")

    # Default --since to the cost window start when one was given: they are the
    # same instant — the batch submission time — and the operator has already
    # recorded it (docs/operations.md).
    since_raw = args.since or (args.window_start if args.cost else None)
    since = None
    if since_raw:
        try:
            since = parse_since(since_raw)
        except ValueError:
            parser.error(f"--since must be RFC3339 UTC, got {since_raw!r}")

    subjects = read_subjects(args.subjects)
    print(f"Loaded {len(subjects)} subjects from {args.subjects}")

    cost_from, cost_to = cost_date_window(args.window_start, args.window_end)
    validator = TestBatchValidator(
        subjects=subjects,
        bucket=args.bucket,
        region=args.region,
        cost_date_from=cost_from,
        cost_date_to=cost_to,
        data_bucket=args.data_bucket,
    )

    # Fetched once and shared: each call shells out to `argo list -o json`, which
    # returns every workflow's full status.nodes for the whole namespace.
    workflows = fetch_batch_workflows(subjects, args.namespace, since)

    results: list[ValidationResult] = [
        validator.check_subject_manifests(),
        validator.check_step_outcomes(),
        validator.check_func_preproc_coverage(),
        # Must follow the coverage check: it holds that check's exclusions to
        # account. It recomputes them if run alone, so the order is an
        # optimisation, not a correctness requirement.
        validator.check_anat_only_delivery(),
        validator.check_workflow_run_schema(),
        validator.check_terminal_workflow_phase(workflows),
        validator.check_pod_attempts(workflows, since),
    ]

    if args.cost:
        results += [
            validator.check_kubecost_api(args.window_start, args.window_end),
            validator.check_cost_athena(),
            validator.check_cost_summary(),
        ]

    passed = print_report(results, date.today().isoformat())
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
