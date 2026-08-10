FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

RUN pip install --disable-pip-version-check --root-user-action=ignore --no-cache-dir "globus-sdk>=4,<5"

RUN groupadd --gid 1000 cloudpipe && \
    useradd --uid 1000 --gid 1000 --no-create-home cloudpipe

COPY images/globus/transfer.py /app/transfer.py

USER 1000
