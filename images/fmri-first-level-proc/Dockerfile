# 0. Where AFNI comes from
#
# AFNI_SOURCE names the image /opt/afni is copied from. The default, afni-download,
# is the stage below: NIMH's tarball, pinned by digest and version. Cloudpipe's build
# passes the first-level image already in production instead
# (build-fmri-first-level-proc.yaml), because NIMH rolls the unversioned tarball in
# place and keeps no archive: on 2026-11-11 26.3.07 became 26.3.11, and every
# published first-level session ran 26.3.07. In this image AFNI does the statistics
# (3dDeconvolve, 3dLSS, 3dTproject), so its version is the operator's decision, not a
# side effect of a rebuild. The version assertion below applies to either source.
# To move AFNI deliberately: set AFNI_VERSION and both digests to a tarball you have
# checked, and build once with AFNI_SOURCE=afni-download.
ARG AFNI_SOURCE=afni-download
ARG AFNI_VERSION=AFNI_26.3.07
ARG AFNI_SHA256_AMD64=a65d5dd466830de2e222bc305020e23b29de45dc677b26a1e6ce2a0f0c44ea6d
ARG AFNI_SHA256_ARM64=9391701ed9fe81d5ddf2c19ab250eef53f301767150289c8d3914ab60dfc97c6

# AFNI's upstream tarballs are UNVERSIONED and roll in place, so they are pinned
# the same two ways as images/afni/Dockerfile — see the longer note there for why
# there is no third option (NIMH publishes no versioned artifact and keeps no
# archive of previous builds):
#
#   AFNI_SHA256_*  byte-pins the exact tarball, per architecture, because the
#                  amd64 and ARM builds are different files with different
#                  digests. When NIMH rolls one, the build fails loudly with the
#                  observed digest printed instead of silently changing AFNI
#                  version on whatever unrelated PR triggered a rebuild.
#   AFNI_VERSION   asserted against the tarball's own AFNI_version.txt, so a
#                  digest bump cannot quietly land a different release. Only the
#                  FIRST line is compared: line 2 is the platform string, which
#                  differs per architecture (linux_ubuntu_24_64 against
#                  linux_ubuntu_24_ARM) while the version does not.
#
# This image keeps the whole distribution at /opt/afni rather than an allow-list
# of programs — the orchestrator calls 3dDeconvolve, 3dLSS, 3dmerge and
# 3dBlurToFWHM among others — so there is no per-program check to make here, and
# the version assertion is what guards the contents.
#
# `--build-arg AFNI_SHA256_ARM64=` (empty) skips the digest check for an
# emergency build; the version assertion still applies.
FROM ubuntu:26.04 AS afni-download
ARG TARGETARCH
ARG AFNI_SHA256_AMD64
ARG AFNI_SHA256_ARM64
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    if [ "${TARGETARCH}" = "arm64" ]; then \
        AFNI_URL="https://afni.nimh.nih.gov/pub/dist/tgz/linux_ubuntu_24_ARM.tgz"; \
        AFNI_SHA256="${AFNI_SHA256_ARM64}"; \
    else \
        AFNI_URL="https://afni.nimh.nih.gov/pub/dist/tgz/linux_ubuntu_24_64.tgz"; \
        AFNI_SHA256="${AFNI_SHA256_AMD64}"; \
    fi; \
    curl -fsSL -o /tmp/afni.tgz "${AFNI_URL}"; \
    if [ -n "${AFNI_SHA256}" ]; then \
        got=$(sha256sum /tmp/afni.tgz | cut -d' ' -f1); \
        if [ "${got}" != "${AFNI_SHA256}" ]; then \
            echo "FATAL: AFNI tarball digest mismatch (${TARGETARCH})."; \
            echo "  expected ${AFNI_SHA256}"; \
            echo "  observed ${got}"; \
            echo "Upstream rolled the unversioned tarball. Verify the new one, then set"; \
            echo "the matching AFNI_SHA256_* to the observed digest and AFNI_VERSION to match."; \
            exit 1; \
        fi; \
    fi; \
    mkdir -p /opt/afni; \
    tar xzf /tmp/afni.tgz -C /opt/afni --strip-components=1; \
    rm -f /tmp/afni.tgz

FROM ${AFNI_SOURCE} AS afni-source

# 1. Base Operating System
FROM ubuntu:26.04

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Required for the conditional logic below
ARG TARGETARCH

# Use bash with pipefail for all RUN instructions
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# 2. Install System Dependencies
# Added libfontconfig1 and libxrender1 for AFNI GUI/Core stability
#
# `apt-get upgrade` at the end is the same-release security upgrade (NIST SP 800-171
# SI-2, finding N1) — not `dist-upgrade`, so it only moves already-installed packages to
# a newer revision of Ubuntu 26.04 and never installs or removes one. It applies to the
# apt set only; the AFNI tarball and the micromamba environment fetched below are
# vendored and invisible to apt.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bzip2 \
    curl \
    wget \
    git \
    tcsh \
    bc \
    gsl-bin \
    netpbm \
    libglib2.0-0 \
    libgomp1 \
    libxm4 \
    libxpm4 \
    libxt6 \
    libjpeg62 \
    libglu1-mesa \
    libglw1-mesa-dev \
    libssl-dev \
    libcurl4-openssl-dev \
    libxml2-dev \
    ca-certificates \
    libfontconfig1 \
    libxrender1 \
    && apt-get upgrade -y \
    && apt-get clean && rm -rf /var/lib/apt/lists/*


# 3. AFNI (from AFNI_SOURCE, see section 0) and micromamba
ENV MAMBA_ROOT_PREFIX=/opt/conda
ARG AFNI_VERSION

COPY --from=afni-source /opt/afni /opt/afni
RUN set -eu; \
    ver=$(sed -n 1p /opt/afni/AFNI_version.txt); \
    if [ "${ver}" != "${AFNI_VERSION}" ]; then \
        echo "FATAL: /opt/afni reports ${ver} but AFNI_VERSION says ${AFNI_VERSION}"; \
        exit 1; \
    fi; \
    echo "AFNI ${ver} (${TARGETARCH}): $(ls /opt/afni | wc -l) files"; \
    if [ "${TARGETARCH}" = "arm64" ]; then MAMBA_ARCH="aarch64"; else MAMBA_ARCH="64"; fi; \
    curl -fsL "https://micro.mamba.pm/api/micromamba/linux-${MAMBA_ARCH}/latest" \
        | tar -xvj -C /usr/local/bin --strip-components=1 bin/micromamba

ENV PATH="/opt/afni:/opt/conda/envs/first_level/bin:$PATH"

# 4. App Setup
#
# The build context is the repo root (build-fmri-first-level-proc.yaml), so the
# image can take the shared publish modules from images/shared/. The
# fmri-first-level-proc package is checked out by that workflow at a pinned commit.
WORKDIR /app

COPY images/fmri-first-level-proc/environment.yaml /app/environment.yaml
COPY images/fmri-first-level-proc/fmri-first-level-proc/ /tmp/fmri-first-level-proc/

RUN --mount=type=cache,target=/opt/conda/pkgs \
    --mount=type=cache,target=/root/.cache/pip \
    micromamba env create --yes -f /app/environment.yaml && \
    pip install /tmp/fmri-first-level-proc/ && \
    rm -rf /tmp/fmri-first-level-proc/ && \
    pip install --disable-pip-version-check --root-user-action=ignore \
        boto3==1.43.72 \
        botocore==1.43.72 \
        urllib3==2.7.0
# boto3/botocore/urllib3 are pinned, and in step with every other image that ships
# fs_derivatives.py (#268; tests/images/test_publish_pin_parity.py): bump all at once.

# The runner (OpenSpec first-level-direct-inputs, D12) and what it imports: the
# exploded publish (ADR 017; fs_derivatives needs restore_links' sidecar names).
# Staging and the analysis template live in fmri-first-level-proc.
COPY images/shared/fs_derivatives.py images/shared/restore_links.py ./
COPY images/fmri-first-level-proc/stage_first_level.py ./
# The one-off staged-run backfill of pre-cp.11 trees (staged-backfill WorkflowTemplate).
COPY images/fmri-first-level-proc/staged_backfill.py ./
# The atlas ships inside the fmri-first-level-proc package, with its licences (4.0.0+cp.4).

# Point directly to the python in the env
ENTRYPOINT ["python3", "stage_first_level.py"]