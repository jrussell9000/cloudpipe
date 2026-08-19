FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

# Same-release security upgrade (NIST SP 800-171 SI-2, finding N1). `upgrade`, not
# `dist-upgrade`: it only moves already-installed packages to a newer revision of the
# SAME Debian release (bookworm) and never installs or removes one, so it collects
# backported CVE fixes without a version jump. This is a standalone layer only because
# the image has no apt install of its own to fold it into; the apt index it fetches is
# dropped in the same layer so nothing is left behind.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --disable-pip-version-check --root-user-action=ignore --no-cache-dir "globus-sdk>=4,<5"

RUN groupadd --gid 1000 cloudpipe && \
    useradd --uid 1000 --gid 1000 --no-create-home cloudpipe

COPY images/globus/transfer.py /app/transfer.py

USER 1000
