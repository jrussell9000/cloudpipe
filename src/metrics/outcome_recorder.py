"""
outcome_recorder.py — Record StepOutcome records for cloudpipe pipeline steps.

Invoked as an Argo DAG sibling task immediately after each substantive step.
Classifies the Argo failure message into a taxonomy category, verifies which
expected output S3 paths exist, and writes a StepOutcome JSON to S3.

--step is repeatable. Steps that share a producer task and a gate, differing
only by step name, are recorded by a single pod: each gets its own
expected-output verification, per-run status and record, and one step's failure
never suppresses another's.

Usage:
  python outcome_recorder.py \
    --workflow-name cloudpipe-abc123 \
    --step t1w-to-mni [--step surface-sample] \
    --subject sub-XXXXXXXX \
    --session ses-baselineYear1Arm1 \
    --status Succeeded \
    --bucket my-cloudpipe-bucket \
    --metrics-bucket my-cloudpipe-metrics \
    [--session na] [--task na] [--run na] \
    [--message ""] \
    [--upstream-failed-step ""] \
    [--expected-output s3://bucket/key1 --expected-output s3://bucket/key2] \
    [--pipeline cloudpipe_minproc]

Argo passes the aggregate step status via:
  --status  '{{tasks.<step>.status}}'

Per-run status is then DERIVED from that plus this run's own verified output
(see resolve_run_status) — never from a {{tasks.<step>.outputs.parameters.*}}
reference, which deadlocks the DAG when the step is skipped (a skipped task has
no outputs, so the record task is never instantiated and the parent DAG waits
on it forever — dagval-330e63gh-tarerr2, 2026-07-23).

--message is always empty from a DAG (Argo exposes no task-message variable).
--exit-code is passed only by a recorder task whose `depends` admits Failed
and nothing that could be Skipped: referencing exitCode of a skipped task has
the same omission hazard as outputs, so a step that can be skipped gets two
recorder tasks, a failure arm that carries the code and a success/skip arm that
does not. Even on the failure arm the code may be absent (a node that died in
its init container or before main ran records no exitCode), so callers pass
`{{= tasks['<step>'].exitCode ?? '' }}` and an empty --exit-code means "no
code"; the bare `{{tasks.<step>.exitCode}}` tag would requeue the DAG forever.
Without a code, failure_category falls back to the missing-output fact for
DAG-recorded per-run failures.

This module is invoked ONLY by the standalone outcome-recorder WorkflowTemplate
now — the fallback path fired when a step is Skipped or its worker pod died
before it could record its own outcome. func-preproc, bold-to-t1w and
surface-resample now write their own per-run StepOutcome records directly
(see ADR 016), with real failure messages. They and this recorder classify
through ONE taxonomy, images/shared/step_outcome.py (#646) — COPYed into /app
of every image that records an outcome, this one included.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import deployment_env
from schemas import StepOutcome

# The failure taxonomy and its exit-code tables live in step_outcome; they are
# re-exported here under their old names, which callers and tests still use.
# message is usually empty in practice — Argo has no DAG task variable for it
# (see module docstring) — so the exit code is the fallback that keeps the
# taxonomy from collapsing to "unknown" on every real failure.
from step_outcome import (  # noqa: F401 — re-exports
    _EXIT_CODE_CATEGORY,
    _EXIT_CODE_REASON,
    _TAXONOMY,
    QC_REJECTED_EXIT_CODE,
    classify_failure,
)
from writer import emit_to_s3

# ---------------------------------------------------------------------------
# Per-run status resolution
# ---------------------------------------------------------------------------


def resolve_run_status(
    aggregate_status: str, expected_keys: list[str], outputs_verified: list[str]
) -> str:
    """Resolve one run's status from the aggregate status and its own output.

    {{tasks.<name>.status}} is the status of the whole session pod, not of this
    run. Recording it verbatim is wrong in both directions, both observed on
    2026-07-22 (dagval-330e63gh-tarerr):

      - partial failure, pod survives → the broken run records "succeeded"
      - any failure that exits the pod → every healthy sibling records "failed",
        including runs the driver never even touched

    Rather than plumb a per-run failure list from the driver — which deadlocks
    the DAG when the producer is skipped, because a record task that references
    {{tasks.<name>.outputs.parameters.*}} is never instantiated for a skipped
    task (dagval-330e63gh-tarerr2, 2026-07-23) — derive it from what the recorder
    already knows:

      - aggregate Skipped/Omitted → the step never ran; report that verbatim.
        (This also keeps a genuinely skipped step's per-run records as "skipped"
        rather than inventing a success/failure.)
      - the step ran → this run succeeded iff its own expected output is present
        and intact (verify_outputs applies a size floor, so a truncated upload
        counts as absent). A missing/short output is this run's failure,
        independent of any sibling.

    This is skip-safe (the fan-out passes only status, no output reference) and
    subsumes the volumetric/surface split for free: func-preproc checks the
    volumetric tarball, surface-sample checks the components tarball, so a run
    downgraded to volumetric-only reads func=succeeded / surface=failed.
    """
    st = aggregate_status.lower()
    # Step never ran (or has no meaningful status) → no per-run outcome to derive.
    if st in ("skipped", "omitted", ""):
        return aggregate_status
    # Subject/session-level step with no per-run completion key → use aggregate.
    if not expected_keys:
        return aggregate_status
    # The step ran: success is defined by this run's own output existing intact.
    return "Succeeded" if set(expected_keys) <= set(outputs_verified) else "Failed"


# ---------------------------------------------------------------------------
# Expected output paths per canonical step
# ---------------------------------------------------------------------------


def default_expected_keys(step: str, subject: str, session: str, task: str, run: str) -> list[str]:
    """Return the canonical S3 key(s) that indicate step completion."""
    prefix = f"{subject}_{session}_{task}_{run}"
    mapping: dict[str, list[str]] = {
        "t1w-to-mni": [
            f"derivatives/registration/{subject}/{session}/t1w_to_mni/"
            f"{subject}_{session}_desc-t1w2mni_affine.mat",
        ],
        "bold-to-t1w": [
            f"derivatives/registration/{subject}/{session}/"
            f"bold_to_t1w_{task}_{run}/{prefix}_desc-bold2t1w_itk.txt",
        ],
        "func-preproc": [
            f"derivatives/func/{subject}/{session}/{prefix}_space-MNI152NLin2009cAsym_bold.tar.gz",
        ],
        # Produced inside the func-preproc pod, but gated by its own marker so
        # that a run can have one derivative and not the other.
        #
        # This key is TRANSIENT: surface-resample deletes each run's components
        # once the dtseries assembled from them is intact. That is correct for
        # live recording, which runs in-pod at extraction time while the tarball
        # is still there. It does mean a *retrospective* re-verification of an
        # old session will report surface-sample as Failed — the step succeeded,
        # its output was reclaimed. Use the surface-resample record, or
        # inventory's surf_exists (components OR dtseries), to judge that.
        "surface-sample": [
            f"derivatives/func_surf/{subject}/{session}/components/"
            f"{prefix}_desc-grayordcomponents_bold.tar.gz",
        ],
        "surface-resample": [
            f"derivatives/func_surf/{subject}/{session}/fsLR32k/"
            f"{prefix}_space-fsLR32k_bold.dtseries.nii",
        ],
    }
    return mapping.get(step, [])


# ---------------------------------------------------------------------------
# Output verification
# ---------------------------------------------------------------------------

# Smallest plausible size for each step's output, in bytes. Existence alone is
# not evidence of validity: on 2026-07-22 a 1 MB truncation of a 264 MB
# components tarball was recorded as outputs_verified, because verification was
# a bare head_object. ContentLength comes back in that same response, so this
# costs nothing extra.
#
# Floors are set roughly an order of magnitude below the smallest real output
# observed (components ~266 MB, func tarballs ~386 MB, dtseries ~138 MB) so that
# a genuinely short BOLD run never trips them. This catches gross truncation —
# the realistic partial-upload mode — and nothing subtler; it is a backstop
# behind the drivers' own tarball guards, not a substitute for them.
_MIN_OUTPUT_BYTES: dict[str, int] = {
    "func-preproc": 20_000_000,
    "surface-sample": 20_000_000,
    "surface-resample": 10_000_000,
    # Text/matrix outputs, legitimately tiny — presence is all we can assert.
    "t1w-to-mni": 1,
    "bold-to-t1w": 1,
}


def verify_outputs(expected: list[str], bucket: str, region: str, step: str = "") -> list[str]:
    """Return the subset of expected S3 keys that exist and look intact.

    "Intact" is only a size floor (see _MIN_OUTPUT_BYTES) — enough to stop a
    truncated upload from being certified as a verified output, not a real
    integrity check.
    """
    if not expected:
        return []
    min_bytes = _MIN_OUTPUT_BYTES.get(step, 1)
    try:
        import boto3
        from botocore.exceptions import ClientError

        s3 = boto3.client("s3", region_name=region)
    except ImportError:
        return []

    verified = []
    for key in expected:
        # Accept bare keys (no s3:// prefix) or s3://bucket/key form
        if key.startswith("s3://"):
            parts = key[5:].split("/", 1)
            b, k = parts[0], parts[1] if len(parts) > 1 else ""
        else:
            b, k = bucket, key
        try:
            head = s3.head_object(Bucket=b, Key=k)
            size = head.get("ContentLength", 0)
            if size < min_bytes:
                print(
                    f"  WARNING: {k} exists but is {size} B, below the "
                    f"{min_bytes} B floor for step={step} — treating as "
                    f"NOT verified (likely a truncated upload)",
                    flush=True,
                )
                continue
            verified.append(key)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code not in ("404", "NoSuchKey"):
                raise
    return verified


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Record a StepOutcome to S3")
    p.add_argument("--workflow-name", required=True)
    # {{workflow.uid}}. Argo reuses names, so this is what keys the record (#638).
    # Optional only so a template that has not passed it yet still records.
    p.add_argument("--workflow-uid", default="")
    # Repeatable: one pod can record several steps that share a producer task
    # and a gate, differing only by step name. func-preproc and surface-sample
    # both hang off functional-preprocessing-dagtask, so recording them together
    # halves the fallback recorder pods. Each step still gets its own
    # expected-output check, per-run status and S3 record.
    p.add_argument("--step", required=True, action="append")
    p.add_argument("--subject", required=True)
    p.add_argument("--session", default="na")
    p.add_argument("--task", default="na")
    p.add_argument("--run", default="na")
    p.add_argument("--status", required=True)
    p.add_argument("--message", default="")
    p.add_argument("--exit-code", default="")
    p.add_argument("--upstream-failed-step", default="")
    # Two buckets, deliberately distinct. Outputs are verified in the data
    # bucket; the StepOutcome record is written to the versioned metrics
    # bucket, which this role cannot delete from.
    p.add_argument(
        "--bucket",
        required=True,
        help="Data bucket — where expected derivative outputs are verified.",
    )
    p.add_argument(
        "--metrics-bucket",
        required=True,
        help="Metrics bucket — where the StepOutcome record is written.",
    )
    p.add_argument("--pipeline", default="cloudpipe_minproc")
    p.add_argument("--region", default=None, help="default: the pod's own AWS_REGION")
    args = p.parse_args()
    args.region = args.region or deployment_env.region()
    return args


def record_one_step(args: argparse.Namespace, step: str) -> None:
    """Verify outputs for one step and write its StepOutcome record."""
    # Verify this run's own output first — its presence is what defines this
    # run's success once the step has run (see resolve_run_status).
    expected_keys = default_expected_keys(
        step=step,
        subject=args.subject,
        session=args.session,
        task=args.task,
        run=args.run,
    )
    outputs_verified = verify_outputs(expected_keys, args.bucket, args.region, step=step)

    # The pod-level status is only a starting point — resolve this run's own.
    run_status = resolve_run_status(args.status, expected_keys, outputs_verified)
    if run_status.lower() != args.status.lower():
        print(
            f"  Per-run status {run_status} overrides pod aggregate "
            f"{args.status} (output {'present' if outputs_verified else 'missing'})",
            flush=True,
        )

    failure_category = classify_failure(run_status, args.message, args.exit_code)

    # DAG-recorded failures carry no message (Argo has no task-message variable),
    # so fall back to the exit code, then to the fact that defines an
    # output-derived per-run failure: the expected output never landed. The
    # code is preferred because it is the cause; "expected output not found" is
    # only the symptom, and for a QC rejection it pointed triage at S3 (#368).
    failure_reason = args.message
    if not failure_reason and run_status.lower() not in ("succeeded", "skipped"):
        code = (args.exit_code or "").strip()
        if code:
            meaning = _EXIT_CODE_REASON.get(int(code)) if code.isdigit() else None
            failure_reason = f"exit code {code}" + (f": {meaning}" if meaning else "")
        elif expected_keys and not outputs_verified:
            failure_reason = f"expected output not found: {expected_keys[0]}"

    outcome = StepOutcome(
        workflow_name=args.workflow_name,
        workflow_uid=args.workflow_uid,
        step=step,
        subject=args.subject,
        session=args.session,
        task=args.task,
        run=args.run,
        status=run_status.lower(),
        failure_category=failure_category,
        failure_reason=failure_reason,
        upstream_failed_step=args.upstream_failed_step,
        outputs_verified=outputs_verified,
        pipeline=args.pipeline,
    )

    s3_key = StepOutcome.s3_key(
        workflow_name=args.workflow_name,
        step=step,
        subject=args.subject,
        session=args.session,
        task=args.task,
        run=args.run,
        dt=outcome.recorded_at[:10],
        workflow_uid=args.workflow_uid,
    )

    print(f"  Recording step outcome: {s3_key}", flush=True)
    print(f"  step={step}  status={run_status}  category={failure_category}", flush=True)
    if outputs_verified:
        print(f"  outputs_verified={outputs_verified}", flush=True)

    emit_to_s3(
        data=outcome.to_dict(),
        bucket=args.metrics_bucket,
        key=s3_key,
        region=args.region,
    )
    print("  Done.", flush=True)


def main() -> None:
    args = parse_args()

    # Argo always passes every declared arg, so an unused optional step arrives
    # as "" — drop those rather than writing a record with an empty step name.
    steps = [s for s in (s.strip() for s in args.step) if s]
    if not steps:
        print("WARNING: no non-empty --step given; nothing to record", flush=True)
        return

    # Each step is independent: one step's S3 error must not cost the others
    # their record, since a merged invocation is the only recorder pod they get.
    for step in steps:
        try:
            record_one_step(args, step)
        except Exception as exc:
            print(f"WARNING: recording step={step} failed (non-fatal): {exc}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Recording is non-critical observability; log but never fail the workflow.
        print(f"WARNING: outcome_recorder failed (non-fatal): {exc}", flush=True)
