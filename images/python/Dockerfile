FROM public.ecr.aws/docker/library/python:3.14.2-alpine3.23

# Install Python packages
RUN pip install --disable-pip-version-check --root-user-action=ignore --no-cache-dir boto3 botocore

# Metrics scripts — sourced directly from src/metrics/ (no manual copies needed)
COPY src/metrics/exit_handler.py src/metrics/writer.py src/metrics/schemas.py src/metrics/outcome_recorder.py src/metrics/record_workflow_start.py /app/
COPY src/inventory.py /app/
