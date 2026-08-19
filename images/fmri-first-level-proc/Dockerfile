# 1. Base Operating System
FROM ubuntu:24.04

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
# a newer revision of Ubuntu 24.04 and never installs or removes one. It applies to the
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

RUN if [ "${TARGETARCH}" = "arm64" ]; then \
        AFNI_URL="https://afni.nimh.nih.gov/pub/dist/tgz/linux_ubuntu_24_ARM.tgz" ; \
        MAMBA_ARCH="aarch64"; \
    else \
        AFNI_URL="https://afni.nimh.nih.gov/pub/dist/tgz/linux_ubuntu_24_64.tgz" ; \
        MAMBA_ARCH="64" ; \
    fi && \
    mkdir -p /opt/afni && \
    curl -fsL "${AFNI_URL}" | tar -xzf - -C /opt/afni --strip-components=1 && \
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