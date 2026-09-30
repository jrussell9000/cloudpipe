"""Deployment values read from the environment, with an error that names what is missing.

Nothing here carries a default. A value that is not set must fail on the first
line that needs it rather than quietly reach another deployment's bucket or
region (ADR 020, design D3).

Where each value comes from:

- **Region** — ``AWS_REGION``, then ``AWS_DEFAULT_REGION``, then the region of
  the active AWS profile. In a pod, EKS Pod Identity injects the first two into
  every pod on a service account with an association (every workflow pod runs as
  ``argo-workflows-runner``, which has one). On a workstation, the AWS CLI
  profile supplies it.
- **Everything else** — a ``CLOUDPIPE_*`` environment variable. On a
  workstation, ``pixi run -e ops …`` sets them from the cloudpipe-config
  ConfigMap (scripts/cloudpipe-env.sh). In a pod, the caller passes them as
  arguments or the WorkflowTemplate sets them.

Standard library only apart from a lazy boto3 import, so it can be COPYed into
any image beside the scripts that use it.
"""

from __future__ import annotations

import os


def region() -> str:
    """The AWS region this deployment runs in."""
    found = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if found:
        return found
    try:
        import boto3

        found = boto3.session.Session().region_name
    except ImportError:
        found = None
    if found:
        return found
    raise RuntimeError(
        "No AWS region: set AWS_REGION, or configure a region on the active AWS "
        "profile. In a pod, EKS Pod Identity injects AWS_REGION on a service "
        "account with an association — check the pod runs as one."
    )


def required(name: str, what: str) -> str:
    """A non-empty environment variable, or an error naming it and what it is for."""
    found = os.environ.get(name, "")
    if not found:
        raise RuntimeError(
            f"{name} is not set ({what}). On a workstation, run through "
            "`pixi run -e ops …`, which sets it from the cloudpipe-config ConfigMap."
        )
    return found
