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


# 3. Architecture-Sensitive Download
ENV MAMBA_ROOT_PREFIX=/opt/conda

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
ARG AFNI_VERSION=AFNI_26.3.02
ARG AFNI_SHA256_AMD64=b2f834925818d1b8b38a4ae2a337af067bd8ad160d05d39c642ecc7661840d85
ARG AFNI_SHA256_ARM64=09fc3ffbe279606bd0f4e75d1203290ac5c20a5e3bb1e2acf1d1b2288e86700f

RUN set -eu; \
    if [ "${TARGETARCH}" = "arm64" ]; then \
        AFNI_URL="https://afni.nimh.nih.gov/pub/dist/tgz/linux_ubuntu_24_ARM.tgz"; \
        AFNI_SHA256="${AFNI_SHA256_ARM64}"; \
        MAMBA_ARCH="aarch64"; \
    else \
        AFNI_URL="https://afni.nimh.nih.gov/pub/dist/tgz/linux_ubuntu_24_64.tgz"; \
        AFNI_SHA256="${AFNI_SHA256_AMD64}"; \
        MAMBA_ARCH="64"; \
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
    rm -f /tmp/afni.tgz; \
    ver=$(sed -n 1p /opt/afni/AFNI_version.txt); \
    if [ "${ver}" != "${AFNI_VERSION}" ]; then \
        echo "FATAL: tarball reports ${ver} but AFNI_VERSION says ${AFNI_VERSION}"; \
        exit 1; \
    fi; \
    echo "AFNI ${ver} (${TARGETARCH}): $(ls /opt/afni | wc -l) files extracted"; \
    curl -fsL "https://micro.mamba.pm/api/micromamba/linux-${MAMBA_ARCH}/latest" \
        | tar -xvj -C /usr/local/bin --strip-components=1 bin/micromamba

ENV PATH="/opt/afni:/opt/conda/envs/ABCD_fmri_orchestrator_S3/bin:$PATH"

# 4. App Setup
WORKDIR /app

COPY environment.yaml /app/environment.yaml
COPY ABCD_fmri_orchestrator_S3/ /app/ABCD_fmri_orchestrator_S3/
COPY fmri-first-level-proc/ /tmp/fmri-first-level-proc/
WORKDIR /app/ABCD_fmri_orchestrator_S3

RUN --mount=type=cache,target=/opt/conda/pkgs \
    --mount=type=cache,target=/root/.cache/pip \
    micromamba env create --yes -f /app/environment.yaml && \
    pip install /tmp/fmri-first-level-proc/ && \
    rm -rf /tmp/fmri-first-level-proc/

# Atlas and configs
COPY Schaefer2018_400Parcels_7Networks_order_Tian_Subcortex_S4_3T_MNI152NLin2009cAsym_2mm.nii.gz \
     orch_config_final.yaml \
     proc_config_final.yaml \
     ./

# Point directly to the python in the env
ENTRYPOINT ["python3", "orchestrate_first_level.py"]