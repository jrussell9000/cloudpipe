#!/usr/bin/env bash
# Build and push the flow runner by hand, then re-register the deployments.
# CI (build-prefect-flow-runner.yaml) does the build on every push to main; this
# is the manual path.
#
#   bash images/prefect-flow-runner/build.sh
#
# The re-registration below needs PREFECT_API_URL and PREFECT_API_AUTH_STRING.
# They are read from the cluster here, as prefect/deploy.sh reads them, and
# checked BEFORE the build: failing after the push would leave a new image that
# no deployment was re-registered against.
#
# Run from anywhere — always builds from the repo root so COPY flows/ works.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
if [ -z "${PREFECT_API_URL:-}" ] || [ -z "${PREFECT_API_AUTH_STRING:-}" ]; then
  # -e and -u off while sourcing: see prefect/deploy.sh.
  set +eu
  # shellcheck source=SCRIPTDIR/../../scripts/cloudpipe-env.sh
  source "$REPO_ROOT/scripts/cloudpipe-env.sh"
  set -eu
fi
: "${PREFECT_API_URL:?could not read the Prefect API URL from the cluster — connect WARP, or export PREFECT_API_URL}"
: "${PREFECT_API_AUTH_STRING:?could not read the prefect-api-auth Secret from the cluster — connect WARP, or export PREFECT_API_AUTH_STRING}"
# Exported for the `pixi run` below, so deploy.sh does not read them again.
export PREFECT_API_URL PREFECT_API_AUTH_STRING

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
