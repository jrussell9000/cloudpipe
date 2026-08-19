# Target: linux/arm64 (EKS Graviton nodes)
# Build: ./images/prefect-flow-runner/build.sh  (must use repo root as context for COPY prefect/flows/)
FROM prefecthq/prefect:3-python3.12

# Same-release security upgrade (NIST SP 800-171 SI-2, finding N1). `upgrade`, not
# `dist-upgrade`: it only moves already-installed packages to a newer revision of the
# SAME Debian release and never installs or removes one, so it collects backported CVE
# fixes without a version jump. Placed first so it does not invalidate the uv layers,
# and kept apt-only — Prefect's own Python stack in this base is a separate class.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

RUN uv pip install --system --no-cache hera boto3 pyarrow

WORKDIR /opt/prefect
COPY prefect/flows/ flows/
COPY src/metrics/ metrics/
ENV PYTHONPATH=/opt/prefect/flows:/opt/prefect/metrics
