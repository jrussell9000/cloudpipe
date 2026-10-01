# Sourced by pixi when it activates the `ops` environment:
#
#   pixi run -e ops python src/validate_test_batch.py --subjects …
#   pixi shell -e ops
#
# Exports this deployment's values from the cluster — the same sources the pods
# read — so workstation tools carry no literal and there is no hand-kept .env
# file to go stale:
#
#   CLOUDPIPE_BUCKET, CLOUDPIPE_METRICS_BUCKET   the cloudpipe-config ConfigMap
#   KUBECOST_BASE_URL, PREFECT_API_URL           the UI ingresses' hostnames
#
# The region is not here: tools take it from AWS_REGION or the AWS profile
# (src/metrics/deployment_env.py), which a workstation already has.
#
# Needs kubectl to reach the cluster (WARP connected). Without it nothing is
# exported, and each tool then fails naming the variable it needed rather than
# guessing — that error is the signal to rely on; the warning below goes to
# stderr during activation, which `pixi run` does not always show. A value
# already in the environment is kept,
# so `CLOUDPIPE_BUCKET=… pixi run -e ops …` still overrides.
#
# Sourced, not executed: no `set -e`, no `exit`.

_cloudpipe_env() {
  local ns="${CLOUDPIPE_CONFIG_NAMESPACE:-argo-workflows}" cfg bucket metrics_bucket host

  if ! cfg="$(kubectl -n "$ns" get configmap cloudpipe-config \
    -o 'jsonpath={.data.bucket} {.data.metrics_bucket}' 2>/dev/null)"; then
    echo "cloudpipe-env: cannot read cloudpipe-config (is WARP connected?); CLOUDPIPE_* not set" >&2
    return 0
  fi
  read -r bucket metrics_bucket <<<"$cfg"
  export CLOUDPIPE_BUCKET="${CLOUDPIPE_BUCKET:-$bucket}"
  export CLOUDPIPE_METRICS_BUCKET="${CLOUDPIPE_METRICS_BUCKET:-$metrics_bucket}"

  host="$(kubectl -n kubecost get ingress kubecost-alb-ingress \
    -o 'jsonpath={.spec.rules[0].host}' 2>/dev/null)"
  if [ -n "$host" ]; then
    export KUBECOST_BASE_URL="${KUBECOST_BASE_URL:-https://$host}"
  fi

  host="$(kubectl -n prefect get ingress prefect-ingress \
    -o 'jsonpath={.spec.rules[0].host}' 2>/dev/null)"
  if [ -n "$host" ]; then
    export PREFECT_API_URL="${PREFECT_API_URL:-https://$host/api}"
  fi
}

_cloudpipe_env
unset -f _cloudpipe_env
