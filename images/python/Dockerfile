FROM public.ecr.aws/docker/library/python:3.14.2-alpine3.23

# Same-release security upgrade (NIST SP 800-171 SI-2, finding N1). `apk upgrade` is
# Alpine's equivalent of `apt-get upgrade`: with no repository pinned beyond the base
# image's own v3.23 branch, it can only move packages to a newer -rNN build of that
# branch (openssl 3.5.5 → 3.5.6-r0), which is a backported-fix bump, not a release
# upgrade. Every OS-class finding in this image is of that form — it ships no compiled
# extension of its own, only boto3's pure-Python tree, so there is no ABI to break.
RUN apk upgrade --no-cache

# Install Python packages
RUN pip install --disable-pip-version-check --root-user-action=ignore --no-cache-dir boto3 botocore

# Metrics scripts — sourced directly from src/metrics/ (no manual copies needed)
COPY src/metrics/exit_handler.py src/metrics/writer.py src/metrics/schemas.py src/metrics/outcome_recorder.py src/metrics/record_workflow_start.py /app/
COPY src/inventory.py /app/
