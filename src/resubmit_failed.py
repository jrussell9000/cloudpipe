"""Find terminally-failed cloudpipe workflows and re-drive the ones worth re-driving.

Issue #234 asked where a batch-level resubmit sweep for `Error` workflows should
live. This is it. It exists because the pipeline's retry machinery is deliberately
*not* a safety net for everything: `retryStrategy` covers a failure inside a
workflow, but a workflow that has exhausted its budget, tripped
`activeDeadlineSeconds`, or failed its expression is terminal, and nothing today
re-drives it. Over a 6-hour test batch that is a rounding error you re-run by hand;
over the ~13-day full-ABCD run it is tens of subjects that no one is watching for.

Resubmitting is cheap because it is idempotent. Every expensive stage gates on a
`_complete.json` marker (ADR 017), so a re-driven subject skips anatomical,
subregion, and registration work that already published and picks up where it
stopped. The cost of a wrong resubmit is a Globus transfer and an inventory pass.

The cost of a *pointless* resubmit is higher than that, though, which is what most
of this module is about. A deterministic failure reproduces identically on every
attempt — re-driving `sub-GNV5ZKU4` after a HISTOalloc blowup burns a full k=4
FastSurfer run to fail in the same place. So the sweep classifies before it acts,
and the classifier's central rule is that **determinism is a property of
repetition, not of the exit code**:

    9 x exit 75  ->  deterministic, skip   (the #248/#249 regression shape)
    1 x exit 75  ->  transient, retry      (the EX_TEMPFAIL guard doing its job)

Keying off the code alone would have gotten both of those wrong, in opposite
directions. Two things sit outside the repetition rule in each direction. Skipped
on a single sighting: an OOMKill and a HISTOalloc blowup (both properties of the
input, which a fresh workflow does not change) and a QC gate, which is a correct
rejection rather than a failure. Exempt from the rule entirely: spot reclaims,
which repeat *without* being deterministic — the templates budget 8 attempts
precisely because back-to-back reclaims on one long step are routine, so counting
two of them as a deterministic pair strands a healthy subject.

That last case is not hypothetical. It is what this classifier got wrong on its
first live run against the 2026-08-14 batch, calling `sub-6LMU82AJ` deterministic
on two SIGTERMs.

Usage
-----
    PYTHONPATH=src python -m resubmit_failed --since 2026-08-14T20:23:36Z
    PYTHONPATH=src python -m resubmit_failed --since ... --apply

`PYTHONPATH=src` is required: `pythonpath` in `pytest.ini` applies under pytest
only. Without `--apply` the sweep only prints its plan.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime

from validate_test_batch import (
    ARGO_NAMESPACE,
    _node_step,
    _workflow_started_at,
    parse_since,
    read_subjects,
)

# Phases a workflow never leaves on its own. `Error` and `Failed` are not
# synonyms in Argo — `Failed` is a pod that ran and exited non-zero, `Error` is a
# pod that never got to run (deleted, deadline, expression false). Both are
# terminal and both are in scope; the distinction matters for classification, not
# for selection.
TERMINAL_PHASES = frozenset({"Failed", "Error"})

# Messages that make a failure deterministic on a single sighting, no repetition
# needed. Matched case-insensitively against the node message.
#
# Both are properties of the *input*, which a fresh workflow does not change: a
# 2.1e9-bin histogram is 17-25 GB regardless of pod size, and `sub-GNV5ZKU4`
# ses-02A blew up identically on 2026-08-12 and 2026-08-14.
#
# Note what is deliberately NOT here: `specified key does not exist`. The
# WorkflowTemplates' retry expressions exclude it, and this module does not,
# because the two operate at different layers. An in-workflow retry of a consumer
# does not re-run its producer, so the key stays absent — but a *resubmission*
# re-runs the producer from the top. The 2026-08-14 batch had exactly this shape:
# a 143 spot kill orphaned a FastSurfer tree, and the downstream init failures
# were a symptom of that, not a cause. See `_absent_key_verdict`.
DETERMINISTIC_MESSAGES: tuple[tuple[str, str], ...] = (
    ("oomkilled", "OOMKilled — deterministic at this pod size (#235)"),
    ("histoalloc", "HISTOalloc runaway — subject-deterministic (#235)"),
)

# An artifact that could not be staged because it was never published. Retryable
# at this layer, but reported distinctly so an operator can see the difference.
ABSENT_KEY_MESSAGE = "specified key does not exist"

# Messages and codes that mean "something external stopped this pod" rather than
# "this pod failed". They are exempt from the repetition rule below, because on
# spot-only nodepools they repeat *by design* — the templates size their retry
# budget at 8 precisely because consecutive reclaims on one long step are routine
# (2 back-to-back observed 2026-08-03). Counting two spot kills as a
# deterministic pair strands a subject that had nothing wrong with it.
EXTERNAL_KILL_MESSAGES = ("imminent node shutdown", "pod deleted")
EXTERNAL_KILL_EXIT_CODE = "143"  # 128+15 (SIGTERM): always an external stop

# `exit 65` is the QC gate exiting before it promotes outputs. It is a correct
# true-negative rejection of a bad registration, not a failure to retry — see
# docs/pipelines.md. Kept separate from DETERMINISTIC_MESSAGES so the plan can
# say "rejected" rather than "broken".
QC_GATE_EXIT_CODE = "65"

# How many identical failures make a step deterministic. Two is enough: the
# retryStrategy already gave it a fresh pod on a (usually) different node, so a
# second identical exit is evidence the input, not the infrastructure, is at
# fault.
DETERMINISTIC_REPEAT_THRESHOLD = 2


@dataclass(frozen=True)
class Attempt:
    """One failed pod attempt, as recorded in `status.nodes`."""

    step: str
    exit_code: str
    message: str


@dataclass(frozen=True)
class Verdict:
    """What the sweep decided about one workflow, and why."""

    action: str  # "retry" | "skip"
    reason: str
    detail: str = ""

    @property
    def retryable(self) -> bool:
        return self.action == "retry"


@dataclass
class PlanRow:
    """A workflow, its verdict, and enough context to act on it."""

    name: str
    subject: str
    phase: str
    verdict: Verdict
    attempts: list[Attempt] = field(default_factory=list)
    submitted_as: str = ""


def failed_attempts(workflow: dict) -> list[Attempt]:
    """Every failed pod attempt in a workflow, one entry per attempt.

    `retryStrategy` leaves each attempt in `status.nodes` as its own node, which
    is the only place a retried-then-recovered failure is visible — the workflow
    phase and the StepOutcome record both describe the final state. That is
    precisely what the repetition rule needs to count.

    A pod killed before its wait sidecar could record a code has no `exitCode`;
    it is kept with an empty code rather than dropped, since its *message* is
    usually the whole story (init-container artifact failures land here — #277).
    """
    attempts: list[Attempt] = []
    for node in (workflow.get("status") or {}).get("nodes", {}).values():
        if node.get("type") != "Pod" or node.get("phase") not in TERMINAL_PHASES:
            continue
        attempts.append(
            Attempt(
                step=_node_step(node),
                exit_code=str((node.get("outputs") or {}).get("exitCode", "")),
                message=node.get("message") or "",
            )
        )
    return attempts


def _deterministic_message(attempt: Attempt) -> str | None:
    """The reason this attempt is deterministic on its own, if it is."""
    lowered = attempt.message.lower()
    for needle, reason in DETERMINISTIC_MESSAGES:
        if needle in lowered:
            return reason
    return None


def _is_external_kill(attempt: Attempt) -> bool:
    """Whether something stopped this pod, as opposed to the pod failing.

    Spot reclaims repeat by design, so they must never feed the repetition rule.
    """
    lowered = attempt.message.lower()
    return attempt.exit_code == EXTERNAL_KILL_EXIT_CODE or any(
        needle in lowered for needle in EXTERNAL_KILL_MESSAGES
    )


def _absent_key_verdict(attempts: list[Attempt]) -> Verdict | None:
    """A verdict for the "artifact key does not exist" family, if it applies.

    Retryable, and the reasoning is the layer distinction: the key is absent
    because a producer never published, and a resubmission re-runs that producer.
    The 2026-08-14 batch's `sub-C9139NWM` and `sub-R4PV7WZ3` are the archetype —
    a spot kill orphaned a FastSurfer tree, so every downstream init failed on a
    genuine 404 with zero retries. Nothing was wrong with either subject.

    The risk this leaves is the opposite case: a producer that *deliberately*
    rejected a session (the #248 HISTOalloc guard exits 0 having pruned it), where
    a re-drive reproduces the same rejection. That is invisible here — the guard
    logs to the pod, not to `status.nodes` — so it is handled by name via
    `--exclude-subjects` rather than guessed at.
    """
    for attempt in attempts:
        if ABSENT_KEY_MESSAGE in attempt.message.lower():
            return Verdict(
                "retry",
                "absent artifact key — producer never published",
                f"{attempt.step} could not stage its input; a resubmission re-runs the producer",
            )
    return None


def _hit_deadline(workflow: dict) -> bool:
    """Whether the workflow tripped `activeDeadlineSeconds` rather than failing."""
    message = ((workflow.get("status") or {}).get("message") or "").lower()
    return "deadline" in message


def classify(workflow: dict, attempts: list[Attempt] | None = None) -> Verdict:
    """Decide whether re-driving this workflow can plausibly succeed.

    Order matters. Deterministic causes are checked before the deadline case,
    because a workflow that spent 12h retrying one deterministic failure trips
    the deadline *as a symptom* — treating it as a timeout would re-drive it
    into the same wall.
    """
    attempts = failed_attempts(workflow) if attempts is None else attempts

    if not attempts:
        # Terminal with no failed pod: the controller never scheduled one. A
        # throttled control plane looks like this (#269), and it is transient.
        return Verdict(
            "retry",
            "no failed pod",
            "terminal without a pod failure — control-plane rejection or expression on an empty node set",
        )

    for attempt in attempts:
        if (reason := _deterministic_message(attempt)) is not None:
            return Verdict("skip", reason, f"{attempt.step} exited {attempt.exit_code or '?'}")

    # The repetition rule. Group by (step, code) so a step that failed the same
    # way twice is caught even if other steps failed differently in between.
    tally: dict[tuple[str, str], int] = {}
    for attempt in attempts:
        if not attempt.exit_code:
            # No code means no evidence of *identical* repetition; an absent code
            # is the #277 init-container shape, which is transient by nature.
            continue
        if _is_external_kill(attempt):
            # Spot churn repeats without being deterministic — see
            # EXTERNAL_KILL_MESSAGES.
            continue
        key = (attempt.step, attempt.exit_code)
        tally[key] = tally.get(key, 0) + 1

    for (step, code), count in tally.items():
        if count >= DETERMINISTIC_REPEAT_THRESHOLD:
            return Verdict(
                "skip",
                f"deterministic: {count} identical failures",
                f"{step} exited {code} on {count} separate attempts",
            )

    # Checked after the repetition rule so a workflow with both a real repeated
    # failure and its downstream 404s is judged on the cause, not the symptom.
    if (verdict := _absent_key_verdict(attempts)) is not None:
        return verdict

    if _hit_deadline(workflow):
        return Verdict(
            "retry",
            "hit activeDeadlineSeconds",
            "re-drive skips completed stages off _complete.json, so it resumes rather than restarts",
        )

    # Checked last, and only when a QC gate is the *whole* story. A workflow is
    # subject-grain while the gate is session-grain, so a k=4 subject can hold one
    # permanently-rejected session and three recoverable ones. Skipping on the
    # first 65 would discard the recoverable sessions with it; re-driving merely
    # re-gates the bad one, which is cheap and already accounted for in the
    # coverage denominator.
    if all(a.exit_code == QC_GATE_EXIT_CODE for a in attempts):
        return Verdict(
            "skip",
            "QC gate rejected the input",
            f"{attempts[0].step} exited 65 — a correct rejection, not a failure",
        )

    return Verdict(
        "retry",
        "transient",
        f"{len(attempts)} failed attempt(s), none repeating identically",
    )


def latest_per_subject(workflows: list[dict]) -> list[dict]:
    """Keep only the newest workflow per subject.

    A sweep must never re-drive a subject that a previous sweep already fixed.
    Without this, running the sweep twice submits the same subject twice — the
    older failed workflow is still inside its 24h TTL and still reads `Error`.
    """
    latest: dict[str, dict] = {}
    for workflow in workflows:
        subject = ((workflow.get("metadata") or {}).get("labels") or {}).get("subjectid")
        if not subject:
            continue
        incumbent = latest.get(subject)
        if incumbent is None:
            latest[subject] = workflow
            continue
        challenger_at = _workflow_started_at(workflow)
        incumbent_at = _workflow_started_at(incumbent)
        if challenger_at is not None and (incumbent_at is None or challenger_at > incumbent_at):
            latest[subject] = workflow
    return list(latest.values())


def terminal_failures(
    workflows: list[dict], since: datetime | None = None, subjects: list[str] | None = None
) -> list[dict]:
    """The workflows a sweep should consider: newest per subject, terminal, in window."""
    candidates = workflows
    if subjects is not None:
        wanted = set(subjects)
        candidates = [
            wf
            for wf in candidates
            if ((wf.get("metadata") or {}).get("labels") or {}).get("subjectid") in wanted
        ]
    if since is not None:
        candidates = [
            wf
            for wf in candidates
            if (started := _workflow_started_at(wf)) is not None and started >= since
        ]
    return [
        wf
        for wf in latest_per_subject(candidates)
        if (wf.get("status") or {}).get("phase") in TERMINAL_PHASES
    ]


def build_plan(
    workflows: list[dict],
    since: datetime | None = None,
    subjects: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[PlanRow]:
    """Classify every terminal workflow, newest first.

    `exclude` names subjects never to re-drive, whatever the classifier decides.
    It exists because some determinism is invisible in `status.nodes`: the #248
    guard rejects a bad session and exits 0, so a subject that will fail the same
    way every run leaves no failed node to classify on.
    """
    excluded = set(exclude or ())
    rows = []
    for workflow in terminal_failures(workflows, since=since, subjects=subjects):
        metadata = workflow.get("metadata") or {}
        subject = (metadata.get("labels") or {}).get("subjectid", "(unlabelled)")
        attempts = failed_attempts(workflow)
        verdict = (
            Verdict(
                "skip", "excluded by name", "known-deterministic; passed via --exclude-subjects"
            )
            if subject in excluded
            else classify(workflow, attempts)
        )
        rows.append(
            PlanRow(
                name=metadata.get("name", "(unnamed)"),
                subject=subject,
                phase=(workflow.get("status") or {}).get("phase", "?"),
                verdict=verdict,
                attempts=attempts,
            )
        )
    rows.sort(key=lambda r: (r.verdict.action, r.subject))
    return rows


def submit_args(workflow: dict) -> list[str]:
    """The `argo submit` command that re-drives this workflow.

    Parameters are copied from the failed workflow's own
    `spec.arguments.parameters` rather than rebuilt: they carry the Globus
    collection IDs and base paths resolved from SSM at original submission time,
    and re-deriving them here would be a second source of truth that can drift.

    `submit --from workflowtemplate/` — never `resubmit`, which snapshots the
    templates from the prior run and would re-drive the subject against exactly
    the code that failed it.
    """
    template = (workflow.get("spec") or {}).get("workflowTemplateRef", {}).get("name", "")
    args = ["argo", "-n", ARGO_NAMESPACE, "submit", "--from", f"workflowtemplate/{template}"]
    for parameter in ((workflow.get("spec") or {}).get("arguments") or {}).get("parameters", []):
        args += ["-p", f"{parameter['name']}={parameter['value']}"]
    return args + ["-o", "name"]


def fetch_workflows(namespace: str = ARGO_NAMESPACE) -> list[dict] | None:
    """Every workflow in the namespace, or None if Argo could not be read.

    None is distinct from `[]`: the first means the sweep is blind and must not
    report "nothing to do", the second means it looked and the batch is clean.
    """
    try:
        proc = subprocess.run(
            ["argo", "-n", namespace, "list", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        workflows = json.loads(proc.stdout)
    except ValueError:
        return None
    return workflows if isinstance(workflows, list) else None


def count_active(workflows: list[dict]) -> int:
    """Workflows that have not completed.

    Counts the absence of the completed label as active: the controller writes
    it asynchronously, so a positive selector misses just-submitted workflows
    (#206). Namespace-wide, matching the queue manager's gate.
    """
    return sum(
        1
        for wf in workflows
        if ((wf.get("metadata") or {}).get("labels") or {}).get("workflows.argoproj.io/completed")
        != "true"
    )


def print_plan(rows: list[PlanRow], *, applied: bool) -> None:
    retry = [r for r in rows if r.verdict.retryable]
    skip = [r for r in rows if not r.verdict.retryable]

    print(f"\n{len(rows)} terminal workflow(s): {len(retry)} to re-drive, {len(skip)} to leave\n")

    if skip:
        print("LEAVE — re-driving these would fail identically:")
        for row in skip:
            print(f"  {row.subject:<20} {row.name:<24} {row.phase:<7} {row.verdict.reason}")
            if row.verdict.detail:
                print(f"  {'':<20} {'':<24} {'':<7} {row.verdict.detail}")
        print()

    if retry:
        header = "RE-DRIVEN:" if applied else "WOULD RE-DRIVE (pass --apply to submit):"
        print(header)
        for row in retry:
            print(f"  {row.subject:<20} {row.name:<24} {row.phase:<7} {row.verdict.reason}")
            if row.submitted_as:
                print(f"  {'':<20} -> {row.submitted_as}")
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-drive terminally-failed cloudpipe workflows that can plausibly succeed."
    )
    parser.add_argument(
        "--since",
        help="RFC3339 instant; consider only workflows started at or after it. "
        "Strongly recommended — without it the sweep sees every workflow still "
        "inside its 24h TTL, which on a continuous run is several batches.",
    )
    parser.add_argument("--subjects", help="CSV with a subject_id column, to narrow the sweep")
    parser.add_argument(
        "--exclude-subjects",
        help="CSV of subjects never to re-drive, for determinism the classifier cannot see "
        "(e.g. a session the #248 guard rejects on every run)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="actually submit; otherwise print the plan and exit"
    )
    parser.add_argument(
        "--max-active",
        type=int,
        default=0,
        help="skip submitting once this many workflows are active (0 = no gate). "
        "Set it to the live cloudpipe-max-concurrent value when sweeping mid-run.",
    )
    parser.add_argument("--namespace", default=ARGO_NAMESPACE)
    args = parser.parse_args(argv)

    since = parse_since(args.since) if args.since else None
    subjects = read_subjects(args.subjects) if args.subjects else None
    exclude = read_subjects(args.exclude_subjects) if args.exclude_subjects else None

    workflows = fetch_workflows(args.namespace)
    if workflows is None:
        print("could not read workflows from Argo — not reporting a clean sweep", file=sys.stderr)
        return 2

    print(f"Scope: {len(workflows)} workflow(s) visible", end="")
    print(f", since {args.since}" if since else ", no time window (all within TTL)", end="")
    print(f", {len(subjects)} subject(s)" if subjects else "")

    rows = build_plan(workflows, since=since, subjects=subjects, exclude=exclude)
    by_name = {(wf.get("metadata") or {}).get("name"): wf for wf in workflows}

    if args.apply:
        active = count_active(workflows)
        for row in rows:
            if not row.verdict.retryable:
                continue
            if args.max_active and active >= args.max_active:
                row.submitted_as = f"(deferred: {active} active >= {args.max_active})"
                continue
            proc = subprocess.run(
                submit_args(by_name[row.name]), capture_output=True, text=True, timeout=120
            )
            if proc.returncode != 0:
                row.submitted_as = f"(submit failed: {proc.stderr.strip()})"
                continue
            row.submitted_as = proc.stdout.strip()
            active += 1

    print_plan(rows, applied=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
