FROM public.ecr.aws/docker/library/python:3.14.2-alpine3.23

# Install Python packages
RUN pip install --disable-pip-version-check --root-user-action=ignore --no-cache-dir boto3 botocore

# Metrics scripts — copied from tools/metrics/ (keep in sync when editing tools/metrics/*.py)
COPY exit_handler.py writer.py schemas.py /app/
