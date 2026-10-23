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
#   CLOUDPIPE_ECR_REGISTRY                       the same ConfigMap — the one
#     value a Kubernetes manifest cannot read itself, since `image:` takes no
#     configMapKeyRef (scripts/jobs/anat-stats-aggregate.yaml renders it with
#     envsubst)
#   KUBECOST_BASE_URL, PREFECT_API_URL           the UI ingresses' hostnames, or
#     in port-forward mode (no ingress: the deployment has no domain) the
#     fixed localhost ports `cloudpipe ui kubecost` and `cloudpipe ui prefect`
#     forward to — so those tools need that session running
#   PREFECT_API_AUTH_STRING                      the prefect-api-auth Secret
#     (terraform/modules/prefect/auth.tf): the Prefect API requires basic auth
#     (#636), and every prefect CLI call and Prefect client reads this variable.
#     Never printed.
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
# Before reading anything, it checks that kubectl and the AWS credentials name the
# SAME cluster: the context's account must be the credentials' account, and the
# server kubectl connects to must be the endpoint `aws eks describe-cluster`
# reports for that context's ARN. A context's name is not where kubectl connects:
# on 2026-10-23 a kubeconfig whose context named production held another
# cluster's server URL. On any mismatch nothing is exported. On success it prints
# one line to stderr naming the cluster it bound to.
#
# Set KUBECONFIG (and AWS_PROFILE) BEFORE `pixi shell -e ops`: this runs once, at
# activation, so a KUBECONFIG exported inside the shell is not seen. To rebind an
# open shell, unset the variables above and `source scripts/cloudpipe-env.sh`.
#
# Sourced, not executed: no `set -e`, no `exit`.

_cloudpipe_target_ok() {
  local arn server region account name creds endpoint
  arn="$(kubectl config view --minify -o 'jsonpath={.contexts[0].context.cluster}' 2>/dev/null)"
  server="$(kubectl config view --minify -o 'jsonpath={.clusters[0].cluster.server}' 2>/dev/null)"
  if [[ ! "$arn" =~ ^arn:aws[a-z-]*:eks:([a-z0-9-]+):([0-9]{12}):cluster/([A-Za-z0-9._-]+)$ ]]; then
    echo "cloudpipe-env: kubectl's context is not an EKS cluster ARN (${arn:-none}); CLOUDPIPE_* not set" >&2
    return 1
  fi
  region="${BASH_REMATCH[1]}" account="${BASH_REMATCH[2]}" name="${BASH_REMATCH[3]}"
  if ! creds="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"; then
    echo "cloudpipe-env: no AWS credentials (SSO expired? run aws sso login); CLOUDPIPE_* not set" >&2
    return 1
  fi
  if [ "$creds" != "$account" ]; then
    echo "cloudpipe-env: kubectl targets account $account but AWS_PROFILE=${AWS_PROFILE:-default} is account $creds; CLOUDPIPE_* not set" >&2
    return 1
  fi
  if ! endpoint="$(aws eks describe-cluster --region "$region" --name "$name" \
    --query cluster.endpoint --output text 2>/dev/null)"; then
    echo "cloudpipe-env: cannot read $arn's endpoint to check kubectl's server; CLOUDPIPE_* not set" >&2
    return 1
  fi
  if [ "${server%/}" != "${endpoint%/}" ]; then
    echo "cloudpipe-env: kubectl's context is named $arn but connects to ${server:-nothing}, not its endpoint $endpoint; CLOUDPIPE_* not set." >&2
    echo "  aws eks update-kubeconfig --region $region --name $name --kubeconfig \"\${KUBECONFIG:-\$HOME/.kube/config}\"" >&2
    return 1
  fi
  echo "cloudpipe-env: bound to account $account, cluster $name ($server)" >&2
}

_cloudpipe_env() {
  local ns="${CLOUDPIPE_CONFIG_NAMESPACE:-argo-workflows}" cfg bucket metrics_bucket registry host

  _cloudpipe_target_ok || return 0

  if ! cfg="$(kubectl -n "$ns" get configmap cloudpipe-config \
    -o 'jsonpath={.data.bucket} {.data.metrics_bucket} {.data.ecr_registry}' 2>/dev/null)"; then
    echo "cloudpipe-env: cannot read cloudpipe-config (is WARP connected?); CLOUDPIPE_* not set" >&2
    return 0
  fi
  read -r bucket metrics_bucket registry <<<"$cfg"
  export CLOUDPIPE_BUCKET="${CLOUDPIPE_BUCKET:-$bucket}"
  export CLOUDPIPE_METRICS_BUCKET="${CLOUDPIPE_METRICS_BUCKET:-$metrics_bucket}"
  export CLOUDPIPE_ECR_REGISTRY="${CLOUDPIPE_ECR_REGISTRY:-$registry}"

  # The cluster answered above, so a missing ingress means port-forward mode
  # rather than an unreachable cluster. The ports are design D2's, fixed in
  # terraform/modules/stack/locals.tf (local.ui_base_urls).
  host="$(kubectl -n kubecost get ingress kubecost-alb-ingress \
    -o 'jsonpath={.spec.rules[0].host}' 2>/dev/null)"
  if [ -n "$host" ]; then
    export KUBECOST_BASE_URL="${KUBECOST_BASE_URL:-https://$host}"
  else
    export KUBECOST_BASE_URL="${KUBECOST_BASE_URL:-http://localhost:9090}"
  fi

  host="$(kubectl -n prefect get ingress prefect-ingress \
    -o 'jsonpath={.spec.rules[0].host}' 2>/dev/null)"
  if [ -n "$host" ]; then
    export PREFECT_API_URL="${PREFECT_API_URL:-https://$host/api}"
  else
    export PREFECT_API_URL="${PREFECT_API_URL:-http://localhost:4200/api}"
  fi

  # Base64-decoded into the variable directly, so the value never reaches the
  # terminal. Empty (and left unset) if the Secret does not exist yet — the
  # Prefect client then gets a 401 that names the problem.
  local auth
  if [ -z "${PREFECT_API_AUTH_STRING:-}" ] \
     && auth="$(kubectl -n prefect get secret prefect-api-auth \
       -o 'jsonpath={.data.auth-string}' 2>/dev/null | base64 --decode 2>/dev/null)" \
     && [ -n "$auth" ]; then
    export PREFECT_API_AUTH_STRING="$auth"
  fi
}

_cloudpipe_env
unset -f _cloudpipe_env _cloudpipe_target_ok
