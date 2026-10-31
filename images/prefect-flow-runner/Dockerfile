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
# The TAG is part of the pin, not only the comment. Dependabot reads a bare
# `image@sha256:` as tracking `latest`, and `prefecthq/prefect:latest` is the
# legacy Prefect 1.x line. On 2026-10-10 that "bump" (#760) swapped this digest
# for Prefect 1.4.1 on Python 3.7, from 2022, amd64-only. The only thing that
# stopped it shipping was the arm64 build failing on `exec format error`. With
# `:3-python3.12@sha256:` Dependabot follows the tag it should. Restored here:
# the multi-arch index the deployed image was built from.
#
# Comment on its own line because Dockerfiles have no inline comments — see the
# note in images/fastsurfer/Dockerfile.
# tag: 3-python3.12 (upstream pushed 2026-10-06)
FROM prefecthq/prefect:3-python3.12@sha256:5b665ed7b55836b1c50b0733dd7f2812db57f82a748b5fd1bf765842e5501dee

# Same-release security upgrade (NIST SP 800-171 SI-2, finding N1). `upgrade`, not
# `dist-upgrade`: it only moves already-installed packages to a newer revision of the
# SAME Debian release and never installs or removes one, so it collects backported CVE
# fixes without a version jump. Placed first so it does not invalidate the uv layers,
# and kept apt-only — Prefect's own Python stack in this base is a separate class.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# globus-sdk is here for the queue manager's batch gate: before submitting, it
# lists the source collection and refreshes the HTTPS token, which is the only
# way to catch a revoked session or a missing HTTPS grant (lib/globus_session.py).
# Without it the gate still checks the session's age, but says out loud that the
# live half was skipped.
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
