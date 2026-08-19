FROM deepmi/fastsurfer:latest

USER root

# Same-release security upgrade (NIST SP 800-171 SI-2, finding N1). `upgrade`, not
# `dist-upgrade`: it only moves already-installed packages to a newer revision of the
# SAME distro release and never installs or removes one, so it collects backported CVE
# fixes without a version jump. This is the only lever available on an upstream base we
# do not build — the FreeSurfer tree and the /venv torch stack inside it are vendored
# and untouched by apt, so their findings are a separate class.
#
# The NVIDIA apt repo is moved aside for the upgrade in case this base carries one (it
# is a GPU image); with the list file gone the CUDA packages have no upgrade candidate
# and cannot move off the torch ABI. Same reasoning, and the same guard, as
# images/fireANTs/Dockerfile — a no-op if the repo is not present.
RUN set -eu; \
    mkdir -p /tmp/nvrepo; \
    for f in /etc/apt/sources.list.d/cuda*.list /etc/apt/sources.list.d/nvidia*.list; do \
        { [ -e "$f" ] && mv "$f" /tmp/nvrepo/; } || true; \
    done; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y; \
    mv /tmp/nvrepo/* /etc/apt/sources.list.d/ 2>/dev/null || true; \
    rmdir /tmp/nvrepo; \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 nonroot 2>/dev/null || true && \
    useradd --uid 1000 --gid 1000 -m nonroot 2>/dev/null || true && \
    chown -R 1000:1000 /opt/freesurfer && \
    chmod -R 770 /opt/freesurfer

COPY images/fastsurfer/extract_qc.py /app/extract_qc.py

# Exploded-derivative publish (ADR 017). This is the ONE dependency the whole
# change adds: the base image ships a /venv python 3.12 with no boto3 and no
# AWS CLI, and the long-phase pod must write derivatives/fastsurfer/ itself —
# publishing cannot be an Argo artifact, because Argo does not order artifact
# uploads and `_complete.json` must be written last. Mirrors the equivalent
# line in images/freesurfer/Dockerfile.
#
# PINNED, and urllib3 named explicitly rather than left to boto3's resolver.
# Unpinned, the exact botocore/urllib3/OpenSSL combination in this venv is not
# reproducible from the repo and drifts between builds — so when publish
# segfaulted in the 2026-08-14 batch (#268) there was no way to say WHICH
# combination did it, or to tell a fixed image from a lucky one.
#
# These versions are deliberately NOT lock-matched, and cannot be: pixi resolves
# boto3/botocore from conda-forge, which lags PyPI (newest conda-forge 1.43.70 vs
# PyPI 1.43.72 as of 2026-08-15), and pixi.lock holds two different resolutions
# anyway because the `default` environment is solved separately from the
# lint/imaging/docs solve group. No single version matches everything. What
# matters instead is that all three images running fs_derivatives.py's publish
# path — fastsurfer, freesurfer, fsqc — pin the SAME versions, so a crash
# reproduces identically in each. Keep them in step: if you bump one, bump all
# three. Both are inside pixi.toml's declared `>=1.43.49,<2` band.
RUN pip install --disable-pip-version-check --no-cache-dir \
        boto3==1.43.72 \
        botocore==1.43.72 \
        urllib3==2.7.0

COPY images/shared/fs_derivatives.py /app/fs_derivatives.py
COPY images/shared/restore_links.py /app/restore_links.py

# Switch to the non-privileged user to run the application.
USER nonroot


