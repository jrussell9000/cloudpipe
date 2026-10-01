#!/usr/bin/env bash
# Build and push the flow runner by hand, then re-register the deployments.
# CI (build-prefect-flow-runner.yaml) does the build on every push to main; this
# is the manual path.
#
#   PREFECT_API_URL=https://prefect.<your-domain>/api images/prefect-flow-runner/build.sh
#
# Run from anywhere — always builds from the repo root so COPY flows/ works.
set -euo pipefail

: "${PREFECT_API_URL:?set PREFECT_API_URL to the Prefect server API, e.g. https://prefect.example.org/api}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# The registry comes from the same ConfigMap prefect/deploy.sh reads, so the image
# pushed here is the image the deployments it registers will pull.
REGISTRY="$(kubectl -n "${CLOUDPIPE_CONFIG_NAMESPACE:-argo-workflows}" \
  get configmap cloudpipe-config -o 'jsonpath={.data.ecr_registry}')"
: "${REGISTRY:?cloudpipe-config has no ecr_registry}"
IMAGE="$REGISTRY/cloudpipe/cloudpipe-flow-runner:latest"

docker run --rm --privileged multiarch/qemu-user-static --reset -p yes

docker buildx build \
  --platform linux/arm64 \
  --provenance=false \
  --push \
  -f "$REPO_ROOT/images/prefect-flow-runner/Dockerfile" \
  -t "$IMAGE" \
  "$REPO_ROOT"

# Never a bare `prefect deploy --all` — see prefect/deploy.sh for why.
pixi run --manifest-path "$REPO_ROOT/pixi.toml" prefect-deploy
