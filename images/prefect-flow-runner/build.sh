#!/usr/bin/env bash
# Run from anywhere — always builds from the repo root so COPY flows/ works.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="public.ecr.aws/l9e7l1h1/cloudpipe/cloudpipe-flow-runner:latest"

docker run --rm --privileged multiarch/qemu-user-static --reset -p yes

aws ecr-public get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin public.ecr.aws

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
