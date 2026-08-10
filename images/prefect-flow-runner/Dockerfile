# Target: linux/arm64 (EKS Graviton nodes)
# Build: ./images/prefect-flow-runner/build.sh  (must use repo root as context for COPY prefect/flows/)
FROM prefecthq/prefect:3-python3.12

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

RUN uv pip install --system --no-cache hera boto3 pyarrow

WORKDIR /opt/prefect
COPY prefect/flows/ flows/
COPY src/metrics/ metrics/
ENV PYTHONPATH=/opt/prefect/flows:/opt/prefect/metrics
