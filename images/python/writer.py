"""
S3 metric writer for cloudpipe observability.

Used by the exit handler and cost scraper (boto3 available in those containers).
Pipeline containers (afni, fastsurfer) write JSON locally; Argo uploads as artifacts.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)


def emit_to_s3(
    data: dict[str, Any],
    bucket: str,
    key: str,
    region: str = "<YOUR_AWS_REGION>",
    retries: int = 3,
    retry_delay_s: float = 2.0,
) -> None:
    """Write a dict as JSON to s3://{bucket}/{key}.

    Retries up to `retries` times on transient errors with exponential backoff.
    Raises on final failure so callers can decide whether to propagate.
    """
    import boto3
    from botocore.exceptions import ClientError

    body = json.dumps(data).encode()
    s3 = boto3.client("s3", region_name=region)

    for attempt in range(1, retries + 1):
        try:
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
            )
            log.info("Emitted metric: s3://%s/%s", bucket, key)
            return
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if attempt == retries or code not in {
                "RequestTimeout", "ServiceUnavailable", "InternalError", "SlowDown",
            }:
                raise
            delay = retry_delay_s * (2 ** (attempt - 1))
            log.warning(
                "s3 put_object attempt %d/%d failed (%s), retrying in %.1fs",
                attempt, retries, code, delay,
            )
            time.sleep(delay)
