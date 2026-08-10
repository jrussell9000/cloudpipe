"""
record_workflow_start.py — Argo DAG task run alongside the very first pipeline
step. Writes the wall-clock time it actually started running to S3, so the
exit handler can compute pending_duration_s (queue + node-provision wait
since workflow submission) without relying on Argo's workflow.outputs.parameters,
which argo lint --offline cannot resolve from an onExit template (#147).

Usage:
  python record_workflow_start.py \
    --workflow-name cloudpipe-abc123 \
    --metrics-bucket my-cloudpipe-metrics
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from writer import emit_to_s3


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Record the workflow's first-step start time to S3")
    p.add_argument("--workflow-name", required=True)
    p.add_argument("--metrics-bucket", required=True)
    p.add_argument("--region", default="<YOUR_AWS_REGION>")
    return p.parse_args()


def s3_key(workflow_name: str, dt: str) -> str:
    return f"metrics/workflow-starts/dt={dt}/{workflow_name}.json"


def main() -> None:
    args = parse_args()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    key = s3_key(args.workflow_name, now[:10])
    print(f"  Recording workflow start marker: {key}", flush=True)
    emit_to_s3(
        data={"first_step_started_at": now},
        bucket=args.metrics_bucket,
        key=key,
        region=args.region,
    )


if __name__ == "__main__":
    main()
