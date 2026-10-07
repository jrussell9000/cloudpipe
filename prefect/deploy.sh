#!/usr/bin/env bash
# Register every deployment in prefect.yaml with the Prefect server, then read
# each one back from the server to prove what it stored.
#
#   pixi run -e ops prefect-deploy
#
# The ops environment exports PREFECT_API_URL and PREFECT_API_AUTH_STRING from
# the cluster (scripts/cloudpipe-env.sh). Outside it, export both by hand.
#
# Use this, never a bare `prefect deploy --all`. prefect.yaml names no bucket and
# no registry: it reads them as `{{ $CLOUDPIPE_* }}` placeholders, and this script
# fills them from the cloudpipe-config ConfigMap Terraform writes — the one the
# Argo WorkflowTemplates already read, so both halves of the pipeline agree by
# construction rather than by two hand-kept copies.
#
# Why the guard and the read-back are both needed: Prefect resolves a placeholder
# ONCE, at deploy time, and stores a plain literal. An unset one is not an error.
# It logs a WARNING and stores "", so a bare deploy succeeds and registers an image
# of `/cloudpipe/cloudpipe-flow-runner:latest` and an empty bucket, and nothing
# fails until the next scheduled run. A `{{ prefect.variables.* }}` placeholder
# behaves the same way — resolved at deploy time, "" when missing, and without
# even the warning — which is why the values are not kept as Prefect Variables:
# that would be a second copy of what Terraform owns, with a quieter failure.
# Both were measured against a local server, not read from the docs.
#
# Needs: kubectl able to read cloudpipe-config (WARP connected), PREFECT_API_URL
# and PREFECT_API_AUTH_STRING. CLOUDPIPE_CONFIG_NAMESPACE overrides where the
# ConfigMap lives.
set -euo pipefail

: "${PREFECT_API_URL:?set PREFECT_API_URL to the Prefect server API, e.g. https://prefect.example.org/api}"
# The API requires basic auth (#636). Checked here because a 401 from the middle
# of `prefect deploy --all` would leave some deployments registered and some not.
: "${PREFECT_API_AUTH_STRING:?set PREFECT_API_AUTH_STRING — run as 'pixi run -e ops prefect-deploy', or read it with: kubectl -n prefect get secret prefect-api-auth -o jsonpath='{.data.auth-string}' | base64 -d}"
NAMESPACE="${CLOUDPIPE_CONFIG_NAMESPACE:-argo-workflows}"

config() {
  local value
  value="$(kubectl -n "$NAMESPACE" get configmap cloudpipe-config -o "jsonpath={.data.$1}")"
  if [ -z "$value" ]; then
    echo "cloudpipe-config in namespace $NAMESPACE has no '$1' — refusing to deploy an empty value" >&2
    exit 1
  fi
  printf '%s' "$value"
}

CLOUDPIPE_BUCKET="$(config bucket)"
CLOUDPIPE_METRICS_BUCKET="$(config metrics_bucket)"
CLOUDPIPE_ECR_REGISTRY="$(config ecr_registry)"
export CLOUDPIPE_BUCKET CLOUDPIPE_METRICS_BUCKET CLOUDPIPE_ECR_REGISTRY

cd "$(dirname "$0")"
prefect --no-prompt deploy --all
python verify_deployments.py
