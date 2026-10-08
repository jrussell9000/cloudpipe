# Target: linux/arm64 (EKS Graviton nodes)
# Build: ./images/prefect-flow-runner/build.sh  (must use repo root as context for COPY prefect/flows/)
# Digest-pinned, not the floating `3-python3.12` (#648). That tag moves on every
# Prefect 3.x release, so this image's Python stack could change between two
# builds of the same commit — which is also what makes a failure here hard to
# attribute.
#
# A no-op for what is deployed: upstream was last pushed 2026-10-06T16:37Z and
# our flow runner was built 2026-10-08T01:24Z, so this digest is the one the
# running image came from.
#
# Comment on its own line because Dockerfiles have no inline comments — see the
# note in images/fastsurfer/Dockerfile.
# tag: 3-python3.12 (upstream pushed 2026-10-06)
FROM prefecthq/prefect@sha256:57e02b3f63d663e02b06d4127362ea48d92d4b361fbd96c8ba8f01e5702755f4

# Same-release security upgrade (NIST SP 800-171 SI-2, finding N1). `upgrade`, not
# `dist-upgrade`: it only moves already-installed packages to a newer revision of the
# SAME Debian release and never installs or removes one, so it collects backported CVE
# fixes without a version jump. Placed first so it does not invalidate the uv layers,
# and kept apt-only — Prefect's own Python stack in this base is a separate class.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# globus-sdk is here for one call: the queue manager's batch gate lists the
# destination collection before submitting, which is the only way to catch a
# revoked session (lib/globus_session.py). Without it the gate still checks the
# session's age, but says out loud that the live half was skipped.
#
# Every package carries the band pixi.toml declares, so the flows run the major
# version tests/prefect/ ran against. Unpinned, `hera` in particular could jump a
# major release here with nothing in the repo changing (#647).
RUN uv pip install --system --no-cache \
    'hera>=5.18.0,<6' 'boto3>=1.43.49,<2' 'pyarrow>=21,<22' 'globus-sdk>=4.8.1,<5'

WORKDIR /opt/prefect
COPY prefect/flows/ flows/
COPY src/metrics/ metrics/
ENV PYTHONPATH=/opt/prefect/flows:/opt/prefect/metrics
