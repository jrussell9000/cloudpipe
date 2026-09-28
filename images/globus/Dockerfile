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

# The operator CLI's error translation, shared rather than copied: the pod log and
# `pixi run globus` must say the same thing about the same Globus error. All three
# files are standard-library only, so the image stays globus-sdk-only. Listing them
# here is also what makes CI rebuild this image when they change —
# .github/scripts/image_deps.py enrols an image by the exact files it COPYs.
COPY src/globus_admin/__init__.py src/globus_admin/exits.py src/globus_admin/errors.py /app/globus_admin/

USER 1000
