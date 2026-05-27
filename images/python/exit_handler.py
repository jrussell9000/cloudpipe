"""
exit_handler.py — Argo workflow exit handler for CloudPipe metrics.

Writes a WorkflowRun summary JSON to S3 using boto3.  Runs as an Argo
onExit template in a python+boto3 container — no neuroimaging deps needed.

Usage:
  python exit_handler.py \
    --workflow-name cloudpipe-abc123 \
    --subject sub-NDARABC123 \
    --status Succeeded \
    --started-at 2026-05-24T08:00:00Z \
    --duration-s 23400 \
    --bucket my-cloudpipe-bucket \
    --pipeline cloudpipe_minproc
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# writer.py is copied alongside this file in the Docker image
sys.path.insert(0, str(Path(__file__).parent))
from writer import emit_to_s3


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Emit WorkflowRun metrics to S3")
    p.add_argument("--workflow-name", required=True)
    p.add_argument("--subject",       required=True)
    p.add_argument("--status",        required=True,
                   choices=["Succeeded", "Failed", "Error"])
    p.add_argument("--started-at",    default="")
    p.add_argument("--duration-s",    type=lambda x: int(float(x)), default=0)
    p.add_argument("--message",       default="")
    p.add_argument("--bucket",        required=True)
    p.add_argument("--pipeline",      default="cloudpipe_minproc")
    p.add_argument("--region",        default="<YOUR_AWS_REGION>")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    record = {
        "schema_version":  "1.0",
        "pipeline":        args.pipeline,
        "workflow_name":   args.workflow_name,
        "subject":         args.subject,
        "status":          args.status,
        "started_at":      args.started_at,
        "finished_at":     now,
        "total_duration_s": args.duration_s,
        "message":         args.message,
        "completed_at":    now,
    }

    s3_key = f"metrics/workflow-runs/{args.workflow_name}_run_summary.json"

    print(f"  Emitting workflow run metrics: {s3_key}", flush=True)
    print(f"  status={args.status}  duration={args.duration_s}s", flush=True)

    emit_to_s3(
        data=record,
        bucket=args.bucket,
        key=s3_key,
        region=args.region,
    )
    print("  Done.", flush=True)


if __name__ == "__main__":
    main()
