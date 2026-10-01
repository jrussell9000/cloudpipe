"""Deployment values the flows need at run time, and where each one comes from.

Nothing here carries a default. A fork that forgot a value should fail on the
first line that needs it, naming what is missing, rather than quietly read or
write another deployment's bucket.

- The **region** comes from the pod. EKS Pod Identity injects ``AWS_REGION`` into
  every pod on the ``prefect-worker`` service account, which is the account every
  flow-run pod uses (``service_account_name`` in prefect.yaml), so it needs no
  configuration of its own.
- The **buckets** are flow parameters, stored on each deployment from the
  ``cloudpipe-config`` ConfigMap when ``prefect/deploy.sh`` registers it.
"""

import os


def deployment_region() -> str:
    """The AWS region this deployment runs in, from the pod's own environment."""
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        raise RuntimeError(
            "AWS_REGION is not set. In the cluster, EKS Pod Identity injects it into "
            "every pod on the prefect-worker service account — check the pod runs as "
            "that account. Outside the cluster, export AWS_REGION yourself."
        )
    return region
