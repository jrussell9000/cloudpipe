# 1. Base Operating System
FROM ubuntu:24.04

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Required for the conditional logic below
ARG TARGETARCH

# 2. Install System Dependencies
# Added libfontconfig1 and libxrender1 for AFNI GUI/Core stability
RUN apt-get update && apt-get install -y --no-install-recommends \
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
    libglw1-mesa \
    libssl-dev \
    libcurl4-openssl-dev \
    libxml2-dev \
    ca-certificates \
    libfontconfig1 \
    libxrender1 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 3. Architecture-Sensitive Download
ENV CONDA_DIR=/opt/conda
RUN if [ "$TARGETARCH" = "arm64" ]; then \
        AFNI_URL="https://afni.nimh.nih.gov/pub/dist/tgz/linux_ubuntu_24_ARM.tgz" ; \
        CONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-aarch64.sh" ; \
    else \
        AFNI_URL="https://afni.nimh.nih.gov/pub/dist/tgz/linux_ubuntu_24_64.tgz" ; \
        CONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh" ; \
    fi && \
    mkdir -p /opt/afni && \
    curl -sL $AFNI_URL | tar -xzf - -C /opt/afni --strip-components=1 && \
    wget --quiet $CONDA_URL -O ~/miniconda.sh && \
    /bin/bash ~/miniconda.sh -b -p ${CONDA_DIR} && \
    rm ~/miniconda.sh

ENV PATH="/opt/afni:${CONDA_DIR}/bin:$PATH"

# 4. Switch to libmamba solver for faster dependency resolution
RUN CONDA_PLUGINS_AUTO_ACCEPT_TOS=true conda install -n base -c conda-forge conda-libmamba-solver -y && \
    conda config --set solver libmamba

# 5. App Setup
WORKDIR /app

# Copy environment.yaml before the git clone so this layer is cached
# independently — only reruns when environment.yaml itself changes.
COPY environment.yaml .
RUN --mount=type=cache,target=/opt/conda/pkgs \
    CONDA_PLUGINS_AUTO_ACCEPT_TOS=true conda env create -f environment.yaml && \
    conda clean -afy

# Clone repo for scripts after env build so repo changes don't bust the env cache
RUN git clone https://github.com/tjkeding/ABCD_fmri_orchestrator_S3
WORKDIR /app/ABCD_fmri_orchestrator_S3

# Set environment path
ENV CONDA_DEFAULT_ENV=ABCD_fmri_orchestrator_S3
ENV PATH="${CONDA_DIR}/envs/ABCD_fmri_orchestrator_S3/bin:$PATH"

# Atlas
COPY Schaefer2018_400Parcels_7Networks_order_Tian_Subcortex_S4_3T_MNI152NLin2009cAsym_2mm.nii.gz ./

# Config files
COPY orch_config_final.yaml ./
COPY proc_config_final.yaml ./

# Point directly to the python in the env
ENTRYPOINT ["python3", "orchestrate_first_level.py"]