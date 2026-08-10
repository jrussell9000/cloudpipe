FROM ghcr.io/prefix-dev/pixi:0.41.4 AS build

WORKDIR /app

# Copy pixi manifest files
COPY images/afni/pixi.toml images/afni/pixi.lock ./

# Install the environment (afni)
RUN pixi install --locked

# Create a shell-hook script to actiave the environment
RUN pixi shell-hook > /shell-hook.sh
RUN echo "#!/bin/bash" > /entrypoint.sh && \
    echo "source /shell-hook.sh" >> /entrypoint.sh && \
    echo "exec \"\$@\"" >> /entrypoint.sh && \
    chmod +x /entrypoint.sh

COPY images/afni/preproc.py images/afni/surf_geometry.py ./

RUN echo "python preproc.py" >> /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
