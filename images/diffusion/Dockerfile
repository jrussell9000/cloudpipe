# Dockerfile for FSL and MRtrix3 on Ubuntu 24.04

# Set up pixi: from https://pixi.prefix.dev/latest/deployment/container/#example-usage
FROM ghcr.io/prefix-dev/pixi:0.41.4 AS build

# copy source code, pixi.toml and pixi.lock to the container
WORKDIR /app
COPY images/diffusion/ .
# install dependencies to `/app/.pixi/envs/prod`
# use `--locked` to ensure the lockfile is up to date with pixi.toml
RUN pixi install --locked
# create the shell-hook bash script to activate the environment
RUN pixi shell-hook -s bash > /shell-hook
RUN echo "#!/bin/bash" > /app/entrypoint.sh
RUN cat /shell-hook >> /app/entrypoint.sh
# extend the shell-hook script to run the command passed to the container
RUN echo 'exec "$@"' >> /app/entrypoint.sh

# Use the latest Ubuntu 24.04 as the base image
FROM ubuntu:24.04 AS production

WORKDIR /app
# only copy the production environment into prod container
# please note that the "prefix" (path) needs to stay the same as in the build container
COPY --from=build /app/.pixi/envs/default /app/.pixi/envs/default
COPY --from=build --chmod=0755 /app/entrypoint.sh /app/entrypoint.sh

# Set environment variables to prevent interactive prompts during installation
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

#
# Install dependencies for FSL, MRtrix3, and other essential packages
#
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Essential build tools
    build-essential \
    git \
    wget \
    curl \
    # MRtrix3 dependencies
    g++ \
    libeigen3-dev \
    libz-dev \
    libqt5svg5-dev \
    libqt5opengl5-dev \
    libgl1-mesa-dev \
    # Cleanup
    && rm -rf /var/lib/apt/lists/*

#
# Install pixi and create Python environment
# Note: Using Python 3.12 as it is the latest stable version.
#
ENV PIXI_HOME=/opt/pixi
ENV PATH=$PIXI_HOME/bin:$PATH
RUN curl -fsSL https://pixi.sh/install.sh | bash -s -- -y --no-modify-path
WORKDIR /opt/project
RUN pixi init --quiet && \
    pixi add --quiet python=3.12 pip numpy scipy
ENV PATH=/opt/project/.pixi/envs/default/bin:$PATH

#
# Install FSL
#
ENV FSLDIR=/opt/fsl
ENV PATH=$FSLDIR/bin:$PATH
RUN wget -O /tmp/fslinstaller.py https://fsl.fmrib.ox.ac.uk/fsldownloads/fslinstaller.py && \
    python3 /tmp/fslinstaller.py -d $FSLDIR -q && \
    rm /tmp/fslinstaller.py

#
# Install MRtrix3
#
ENV MRTRIX_DIR=/opt/mrtrix3
RUN git clone https://github.com/MRtrix3/mrtrix3.git $MRTRIX_DIR
WORKDIR $MRTRIX_DIR
RUN ./build
ENV PATH=$MRTRIX_DIR/bin:$PATH

#
# Create a non-root user and set up the working directory
#
RUN useradd --create-home --shell /bin/bash neuro-user
USER neuro-user
WORKDIR /home/neuro-user


ENTRYPOINT ["/app/entrypoint.sh"]
# Set the default command to open a bash shell
CMD ["/bin/bash"]
