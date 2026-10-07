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
import deployment_env
from schemas import workflow_tag
from writer import emit_to_s3


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Record the workflow's first-step start time to S3")
    p.add_argument("--workflow-name", required=True)
    # {{workflow.uid}} — Argo reuses names, so a name-only marker could be read
    # back by a later workflow that got the same name (#638).
    p.add_argument("--workflow-uid", default="")
    p.add_argument("--metrics-bucket", required=True)
    p.add_argument("--region", default=None, help="default: the pod's own AWS_REGION")
    args = p.parse_args()
    args.region = args.region or deployment_env.region()
    return args


def s3_key(workflow_name: str, dt: str, workflow_uid: str = "") -> str:
    return f"metrics/workflow-starts/dt={dt}/{workflow_tag(workflow_name, workflow_uid)}.json"


def main() -> None:
    args = parse_args()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    key = s3_key(args.workflow_name, now[:10], args.workflow_uid)
    print(f"  Recording workflow start marker: {key}", flush=True)
    emit_to_s3(
        data={"first_step_started_at": now},
        bucket=args.metrics_bucket,
        key=key,
        region=args.region,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # The marker only feeds pending_duration_s, and this task is a leaf of
        # the master DAG outside its retry expression: a transient S3 error
        # here must not fail the subject. A missing marker reads back as null.
        print(f"WARNING: record_workflow_start failed (non-fatal): {exc}", flush=True)
