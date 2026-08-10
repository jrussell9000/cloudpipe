FROM ghcr.io/prefix-dev/pixi:0.41.4 AS build

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
