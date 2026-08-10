"""
exit_handler.py — Argo workflow exit handler for CloudPipe metrics.

Writes a WorkflowRun summary JSON and a SubjectManifest JSON to S3 using boto3.
Runs as an Argo onExit template in a python+boto3 container.

Usage:
  python exit_handler.py \
    --workflow-name cloudpipe-abc123 \
    --subject sub-NDARABC123 \
    --status Succeeded \
    --started-at 2026-05-24T08:00:00Z \
    --duration-s 23400 \
    --metrics-bucket my-cloudpipe-metrics \
    --pipeline cloudpipe_minproc
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from schemas import StepSummary, SubjectManifest, WorkflowRun
from writer import emit_to_s3


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Emit WorkflowRun and SubjectManifest metrics to S3")
    p.add_argument("--workflow-name", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--status", required=True, choices=["Succeeded", "Failed", "Error"])
    p.add_argument("--started-at", default="")
    p.add_argument("--duration-s", type=lambda x: int(float(x)), default=0)
    p.add_argument("--message", default="")
    # Every S3 access in this script is a metrics read or write, so there is
    # only one bucket and it is the versioned metrics bucket.
    p.add_argument("--metrics-bucket", required=True)
    p.add_argument("--pipeline", default="cloudpipe_minproc")
    p.add_argument("--region", default="<YOUR_AWS_REGION>")
    return p.parse_args()


def _pending_duration_s(submitted_at: str, started_at: str) -> float | None:
    """Seconds between workflow submission and first step running, or None if
    unmeasurable.

    Equal (or inverted) timestamps are treated as unmeasurable rather than a
    genuine zero-second wait: the only way this function has ever seen equal
    inputs in practice was the #147 wiring bug, where both arguments were
    fed the same upstream value by construction. A real "no wait" workflow
    would need to reach the first DAG task within the same whole second as
    its own submission, which the two timestamps can't distinguish from that
    bug — so treat non-positive as "not measured" rather than risk silently
    reporting a confident 0.0 again.
    """
    if not submitted_at or not started_at:
        return None
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        delta_s = (
            datetime.strptime(started_at, fmt) - datetime.strptime(submitted_at, fmt)
        ).total_seconds()
    except ValueError:
        return None
    return delta_s if delta_s > 0 else None


def _date_range(start_iso: str, end_iso: str) -> list[str]:
    """Inclusive list of YYYY-MM-DD dates spanning [start_iso, end_iso].

    Falls back to just today if start_iso is missing/malformed (e.g. no
    --started-at was passed) rather than raising, since this only scopes
    which dt= partitions get scanned.
    """
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        start = datetime.strptime(start_iso, fmt).date()
        end = datetime.strptime(end_iso, fmt).date()
    except ValueError:
        today = datetime.now(timezone.utc).date()
        return [today.isoformat()]
    days = max((end - start).days, 0)
    return [(start + timedelta(days=i)).isoformat() for i in range(days + 1)]


def _load_step_outcomes(
    bucket: str, workflow_name: str, region: str, dates: list[str]
) -> list[dict]:
    """Scan metrics/step-outcomes/dt=<date>/{workflow_name}__*.json for each date
    and return all records.

    Step outcomes are written with dt = the write date of each individual
    record (see StepOutcome.s3_key), so a workflow that spans UTC midnight can
    have outcomes under more than one dt= partition. `dates` should cover the
    workflow's started_at through its completion date, inclusive.
    """
    import boto3

    s3 = boto3.client("s3", region_name=region)
    paginator = s3.get_paginator("list_objects_v2")
    needle = f"{workflow_name}__"

    records = []
    for dt in dates:
        prefix = f"metrics/step-outcomes/dt={dt}/"
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key[len(prefix) :].startswith(needle):
                    continue
                try:
                    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                    records.append(json.loads(body))
                except Exception as exc:
                    print(f"  Warning: could not read {key}: {exc}", flush=True)
    return records


def _read_first_step_started_at(
    bucket: str, workflow_name: str, region: str, dates: list[str]
) -> str:
    """Read the record_workflow_start.py marker for this workflow, or "" if
    it was never written (e.g. the DAG failed before that task could run).

    Written to metrics/workflow-starts/dt=<date>/{workflow_name}.json, keyed
    by that record's own write date -- same reasoning as
    _load_step_outcomes, so scan every date the workflow could have touched.
    """
    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3", region_name=region)
    for dt in dates:
        key = f"metrics/workflow-starts/dt={dt}/{workflow_name}.json"
        try:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            return json.loads(body).get("first_step_started_at", "")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("NoSuchKey", "404"):
                print(f"  Warning: could not read {key}: {exc}", flush=True)
        except Exception as exc:
            print(f"  Warning: could not read {key}: {exc}", flush=True)
    return ""


def _build_manifest(
    workflow_name: str,
    subject: str,
    pipeline: str,
    step_outcomes: list[dict],
    workflow_status: str,
) -> SubjectManifest:
    """Assemble a SubjectManifest from StepOutcome records."""
    steps = []
    outputs_available = []
    failed_steps = []
    skipped_steps = []

    for rec in step_outcomes:
        steps.append(
            StepSummary(
                step=rec.get("step", ""),
                session=rec.get("session", "na"),
                task=rec.get("task", "na"),
                run=rec.get("run", "na"),
                status=rec.get("status", ""),
                failure_category=rec.get("failure_category", ""),
                failure_reason=rec.get("failure_reason", ""),
            ).to_dict()
        )
        if rec.get("status") == "succeeded":
            outputs_available.extend(rec.get("outputs_verified", []))
        elif rec.get("status") == "failed":
            failed_steps.append(rec.get("step", ""))
        elif rec.get("status") == "skipped":
            skipped_steps.append(rec.get("step", ""))

    # Derive overall_status
    if not steps and workflow_status != "Succeeded":
        overall_status = "failed"
    elif failed_steps or skipped_steps:
        overall_status = "partial"
    else:
        overall_status = "succeeded"

    return SubjectManifest(
        workflow_name=workflow_name,
        subject=subject,
        overall_status=overall_status,
        steps=steps,
        outputs_available=sorted(set(outputs_available)),
        failed_steps=failed_steps,
        skipped_steps=skipped_steps,
        pipeline=pipeline,
    )


def _first_failed_step(step_outcomes: list[dict]) -> tuple[str, str]:
    """Return (step_name, failure_category) of the earliest-recorded failed step."""
    failed = [r for r in step_outcomes if r.get("status") == "failed"]
    if not failed:
        return "", ""

    # Sort by recorded_at; fall back to list order if timestamp is missing/malformed
    def _ts(r: dict) -> str:
        return r.get("recorded_at", "")

    earliest = min(failed, key=_ts)
    return earliest.get("step", ""), earliest.get("failure_category", "unknown")


def main() -> None:
    args = parse_args()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Load step outcomes written during this workflow run. dt= partitions are
    # keyed by each record's own write date, so scan every date the workflow
    # could have touched (started_at through now) rather than assuming one day.
    dates = _date_range(args.started_at, now)
    print(
        f"  Scanning step outcomes for workflow {args.workflow_name} across dt={dates}...",
        flush=True,
    )
    step_outcomes = _load_step_outcomes(args.metrics_bucket, args.workflow_name, args.region, dates)
    print(f"  Found {len(step_outcomes)} step outcome(s).", flush=True)

    first_step_started_at = _read_first_step_started_at(
        args.metrics_bucket, args.workflow_name, args.region, dates
    )

    # Determine failed_step / failure_category for WorkflowRun
    failed_step, failure_category = _first_failed_step(step_outcomes)
    if args.status == "Succeeded":
        failed_step = ""
        failure_category = ""
    elif not failed_step:
        failure_category = "unknown"

    # Emit WorkflowRun record
    run_record = WorkflowRun(
        workflow_name=args.workflow_name,
        subject=args.subject,
        status=args.status,
        started_at=args.started_at,
        finished_at=now,
        total_duration_s=args.duration_s,
        pending_duration_s=_pending_duration_s(args.started_at, first_step_started_at),
        message=args.message,
        failed_step=failed_step,
        failure_category=failure_category,
        pipeline=args.pipeline,
        completed_at=now,
    )
    run_key = WorkflowRun.s3_key(args.workflow_name, args.subject, dt=now[:10])
    print(f"  Emitting workflow run metrics: {run_key}", flush=True)
    print(
        f"  status={args.status}  duration={args.duration_s}s"
        f"  failed_step={failed_step!r}  failure_category={failure_category!r}",
        flush=True,
    )
    emit_to_s3(
        data=run_record.to_dict(), bucket=args.metrics_bucket, key=run_key, region=args.region
    )

    # Assemble and emit SubjectManifest
    manifest = _build_manifest(
        workflow_name=args.workflow_name,
        subject=args.subject,
        pipeline=args.pipeline,
        step_outcomes=step_outcomes,
        workflow_status=args.status,
    )
    manifest_key = SubjectManifest.s3_key(args.workflow_name, args.subject, dt=now[:10])
    print(f"  Emitting subject manifest: {manifest_key}", flush=True)
    print(
        f"  overall_status={manifest.overall_status}"
        f"  failed={manifest.failed_steps}"
        f"  skipped={manifest.skipped_steps}",
        flush=True,
    )
    emit_to_s3(
        data=manifest.to_dict(), bucket=args.metrics_bucket, key=manifest_key, region=args.region
    )

    print("  Done.", flush=True)


if __name__ == "__main__":
    main()
