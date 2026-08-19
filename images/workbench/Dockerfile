FROM ghcr.io/prefix-dev/pixi:0.41.4 AS build

# Same-release security upgrade (NIST SP 800-171 SI-2, finding N1) — same treatment,
# and for the same reason, as images/afni/Dockerfile: the Ubuntu 24.04 pixi base ships
# whole (no second stage), so its apt layer is what the scanner sees. `upgrade` stays
# inside the release, and the conda-forge side pixi installs below is untouched.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy pixi manifest files
COPY images/workbench/pixi.toml images/workbench/pixi.lock ./

# Install the environment (workbench)
RUN pixi install --locked

# Create a shell-hook script to activate the environment
RUN pixi shell-hook > /shell-hook.sh
RUN echo "#!/bin/bash" > /entrypoint.sh && \
    echo "source /shell-hook.sh" >> /entrypoint.sh && \
    echo "exec \"\$@\"" >> /entrypoint.sh && \
    chmod +x /entrypoint.sh

COPY images/workbench/cifti_assemble.py ./

ENTRYPOINT ["/entrypoint.sh"]
