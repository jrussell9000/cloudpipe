#!/usr/bin/env bash
# Run from anywhere — always builds from the repo root so COPY flows/ works.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="<YOUR_AWS_ACCOUNT_ID>.dkr.ecr.<YOUR_AWS_REGION>.amazonaws.com/cloudpipe/cloudpipe-flow-runner:latest"
# Transitional dual-push, mirroring .github/workflows/build-prefect-flow-runner.yaml.
# prefect.yaml still pins the public :latest and deployments only re-register on a
# manual `prefect deploy --all`, so dropping this would leave the running
# flow-runner on a stale image. Remove at Step 6 of the ECR migration.
LEGACY_IMAGE="public.ecr.aws/l9e7l1h1/cloudpipe/cloudpipe-flow-runner:latest"

docker run --rm --privileged multiarch/qemu-user-static --reset -p yes

docker buildx build \
  --platform linux/arm64 \
  --provenance=false \
  --push \
  -f "$REPO_ROOT/images/prefect-flow-runner/Dockerfile" \
  -t "$IMAGE" \
  -t "$LEGACY_IMAGE" \
  "$REPO_ROOT"

cd "$REPO_ROOT/prefect"
PREFECT_API_URL=https://prefect.<YOUR_DOMAIN>/api \
  pixi run --manifest-path "$REPO_ROOT/pixi.toml" prefect deploy --all
