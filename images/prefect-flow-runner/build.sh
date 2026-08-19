#!/usr/bin/env bash
# Run from anywhere — always builds from the repo root so COPY flows/ works.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# Hand-kept in sync with the IMAGE env in
# .github/workflows/build-prefect-flow-runner.yaml and the four job_variables.image
# refs in prefect/prefect.yaml. Nothing enforces that.
IMAGE="<YOUR_AWS_ACCOUNT_ID>.dkr.ecr.<YOUR_AWS_REGION>.amazonaws.com/cloudpipe/cloudpipe-flow-runner:latest"

docker run --rm --privileged multiarch/qemu-user-static --reset -p yes

docker buildx build \
  --platform linux/arm64 \
  --provenance=false \
  --push \
  -f "$REPO_ROOT/images/prefect-flow-runner/Dockerfile" \
  -t "$IMAGE" \
  "$REPO_ROOT"

cd "$REPO_ROOT/prefect"
PREFECT_API_URL=https://prefect.<YOUR_DOMAIN>/api \
  pixi run --manifest-path "$REPO_ROOT/pixi.toml" prefect deploy --all
