FROM public.ecr.aws/docker/library/python:3.15-slim-bookworm

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

# HTTPS ingress (https_ingress.py, openspec add-https-ingress-mode) reads the
# session time from SSM and lists the bucket for its size check, so this image
# gains boto3. Pinned to the same three versions as the fastsurfer, freesurfer
# and fsqc images, with urllib3 named rather than left to boto3's resolver — see
# the comment in images/fastsurfer/Dockerfile for why the trio is pinned and
# kept in step.
RUN pip install --disable-pip-version-check --root-user-action=ignore --no-cache-dir \
        boto3==1.43.72 \
        botocore==1.43.72 \
        urllib3==2.7.0

# rclone, for HTTPS ingress's copy. A pinned release whose SHA-256 is checked in
# the same layer: a mismatch fails the build, so a changed archive cannot slip
# into an image Inspector then scans as if it were the reviewed one. The checksum
# is the one rclone publishes in its release SHA256SUMS. Fetched with Python
# because the slim base has no curl or unzip, and adding them for one download
# would only widen what gets scanned. downloads.rclone.org answers 403 to
# Python's default `Python-urllib` user agent (observed 2026-10-12), so the
# request names itself. amd64 only: the ingress node pool is amd64, and this
# image is built for nothing else.
ARG RCLONE_VERSION=1.75.2
ARG RCLONE_SHA256=349ac8fba6ff65d6247043f1750cdcb518ec5d500ef91463a10d37c0ccdf3702
ARG TARGETARCH=amd64
RUN test "${TARGETARCH}" = "amd64" || { echo "rclone is pinned for amd64 only, not ${TARGETARCH}" >&2; exit 1; }; \
    python -c "import hashlib, io, sys, urllib.request, zipfile; \
url = 'https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-amd64.zip'; \
request = urllib.request.Request(url, headers={'User-Agent': 'cloudpipe-image-build'}); \
data = urllib.request.urlopen(request, timeout=300).read(); \
digest = hashlib.sha256(data).hexdigest(); \
digest == '${RCLONE_SHA256}' or sys.exit(f'rclone archive checksum {digest} != pinned ${RCLONE_SHA256}'); \
open('/usr/local/bin/rclone', 'wb').write(zipfile.ZipFile(io.BytesIO(data)).read('rclone-v${RCLONE_VERSION}-linux-amd64/rclone'))" \
    && chmod 0755 /usr/local/bin/rclone \
    && rclone version | head -1

RUN groupadd --gid 1000 cloudpipe && \
    useradd --uid 1000 --gid 1000 --no-create-home cloudpipe

COPY images/globus/listing.py /app/listing.py
COPY images/globus/https_ingress.py /app/https_ingress.py

# The operator CLI's error translation, shared rather than copied: the pod log and
# `pixi run globus` must say the same thing about the same Globus error. All three
# files are standard-library only, so the image adds no package for them. Listing them
# here is also what makes CI rebuild this image when they change —
# .github/scripts/image_deps.py enrols an image by the exact files it COPYs.
COPY src/globus_admin/__init__.py src/globus_admin/exits.py src/globus_admin/errors.py /app/globus_admin/

USER 1000
