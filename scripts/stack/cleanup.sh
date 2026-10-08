#!/bin/bash
# Tear a CloudPipe deployment down. Like install.sh, nothing here is specific to
# one deployment: the root, its module call, the region and the cluster name are
# resolved from the root the caller names — see targets.sh.
#
# One command, not phases: nothing drives a teardown phase by phase, and the
# failure paths below print the by-hand commands they could not complete.
#
# Usage: cleanup.sh [--root DIR] [--module NAME] [--region NAME] [--name NAME]
set -euo pipefail

# Takes a stack-relative address (`module.vpc`) — see targets.sh — and destroys
# the root-level one Terraform needs (`module.stack.module.vpc`).
destroy_target() {
  local target
  target=$(stack_address "$1")
  echo "==> Destroying $target..."
  if terraform destroy -target="$target" -auto-approve 2>&1 | tee /dev/tty; then
    echo "SUCCESS: $target"
  else
    echo "FAILED: $target"
    exit "$EXIT_ERROR"
  fi
}

# Deployment resolution, target lists and assert_targets_declared() are shared
# with install.sh.
# shellcheck source=SCRIPTDIR/targets.sh
source "$(dirname "${BASH_SOURCE[0]}")/targets.sh"

parse_common_args "$@"
if [[ ${#COMMON_ARGS_REST[@]} -gt 0 ]]; then
  fail "$EXIT_INVALID" "ERROR: unrecognised argument ${COMMON_ARGS_REST[0]}." "$COMMON_USAGE"
fi

resolve_root
require_cloudflare_api_token

# `init` before the target validation, as in install.sh: it writes the module
# manifest that resolves which module call is the stack.
terraform init -upgrade
resolve_stack_module

echo "==> Validating -target addresses against the configuration..."
assert_targets_declared module.vpc module.eks "${PHASE2_MODULES[@]}"

# A teardown runs against a deployment that exists, so both of these come from
# its own state rather than from a flag.
require_deployment_region
require_cluster_name

echo "==> WARNING: This will permanently destroy this deployment's infrastructure."
echo "    Root:    $DEPLOYMENT_ROOT"
echo "    Module:  module.$STACK_MODULE_NAME"
echo "    Region:  $DEPLOYMENT_REGION"
echo "    Cluster: $CLUSTER_NAME"
echo ""
read -rp "Type 'destroy' to confirm: " CONFIRM
if [[ "$CONFIRM" != "destroy" ]]; then
  echo "Aborted."
  exit "$EXIT_BLOCKED"
fi

# Kubeconfig must be current for Helm/Kubernetes provider calls during destroy.
# Fails gracefully if the cluster is already gone.
aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$DEPLOYMENT_REGION" 2>/dev/null || true

# Phase 1: Tear down application-layer resources first (Helm releases, Kubernetes
# objects, CRD-dependent manifests) before removing the cluster itself.
# Destroyed in reverse install order to respect dependencies.
# Remove the shared web-UI ALB before Terraform destroys the addons module (and
# with it the AWS Load Balancer Controller). Otherwise the ALB is orphaned and
# `destroy -target=module.vpc` fails on its ENIs.
#
# All five UIs share ONE internal ALB (the IngressGroup named below, ui_alb.tf),
# which the controller deletes only once EVERY member ingress is gone. Grafana's
# ingress belongs to ArgoCD, whose self-heal would recreate it — and deleting
# the grafana Application is not enough, because the cluster-addons
# ApplicationSet regenerates it. So stop both ArgoCD controllers first.
#
# The group name is the literal `ui_alb.tf` annotates, DELIBERATELY NOT derived
# from the deployment's name prefix: a derived name would match no ALB, this
# whole block would find nothing to wait for, and the VPC destroy would then fail
# on the ENIs of an ALB nobody deleted. tests/test_installer_fixed_names.py holds
# the two equal.
UI_ALB_STACK="cloudpipe-ui"
UI_INGRESSES=(
  argocd/argocd-ingress
  argo-workflows/argoworkflows-ingress
  prefect/prefect-ingress
  kubecost/kubecost-alb-ingress
  grafana/grafana
)

# A deployment with no domain publishes no UI: there is no ALB, no certificate
# and no ingress, so there is nothing here to tear down. Decided by looking for
# the ingresses rather than by asking the configuration, because this also has to
# be right when the cluster is already gone — in which case the lookup finds
# nothing, which is the same answer.
#
# Stated rather than left to `|| true` and an empty tag lookup: a block that
# silently does nothing cannot be told apart from a block that silently failed.
ui_ingresses_exist() {
  local ref ns name
  for ref in "${UI_INGRESSES[@]}"; do
    ns=${ref%%/*} name=${ref#*/}
    if kubectl get ingress "$name" -n "$ns" &>/dev/null; then
      return 0
    fi
  done
  return 1
}

if ! ui_ingresses_exist; then
  echo "==> No web-UI ingresses found: skipping the shared ALB teardown."
  echo "    Expected for a deployment with no domain, which publishes no UI, and for a"
  echo "    cluster that is already gone."
  SKIP_UI_ALB_TEARDOWN=true
else
  SKIP_UI_ALB_TEARDOWN=false
fi

if [[ "$SKIP_UI_ALB_TEARDOWN" == false ]]; then

echo "==> Stopping ArgoCD controllers (so the Grafana ingress is not recreated)..."
kubectl -n argocd scale deployment/argocd-applicationset-controller --replicas=0 2>/dev/null || true
kubectl -n argocd scale statefulset/argocd-application-controller --replicas=0 2>/dev/null || true

echo "==> Deleting the web-UI ingresses..."
for ref in "${UI_INGRESSES[@]}"; do
  ns=${ref%%/*} name=${ref#*/}
  kubectl delete ingress "$name" -n "$ns" --wait=false 2>/dev/null || true
done

ui_alb_arns() {
  aws resourcegroupstaggingapi get-resources \
    --resource-type-filters elasticloadbalancing:loadbalancer \
    --tag-filters "Key=ingress.k8s.aws/stack,Values=$UI_ALB_STACK" \
    --query 'ResourceTagMappingList[].ResourceARN' --output text
}

# The ALB carries deletion_protection.enabled=true (Security Hub ELB.6, set in
# local.ui_alb_group_annotations). Without clearing it neither the controller nor
# the manual `delete-load-balancer` printed in the failure path below can delete
# the ALB: both return OperationNotPermitted.
#
# Called on EVERY pass of the wait loop rather than once, because the ingress
# deletions above are issued with --wait=false. While the group is still draining
# the controller keeps reconciling a non-empty group and re-applies its
# load-balancer-attributes, protection included, so a single clear can be undone
# moments later. Re-clearing each pass converges regardless of who wins the race.
#
# `|| true` covers exactly one benign failure: the ALB being deleted between the
# tag lookup and the modify call. It does not hide a stuck ALB — the wait loop is
# what decides that, and it still times out loudly.
clear_ui_alb_protection() {
  local arn
  for arn in $(ui_alb_arns); do
    aws elbv2 modify-load-balancer-attributes \
      --load-balancer-arn "$arn" \
      --attributes Key=deletion_protection.enabled,Value=false >/dev/null || true
  done
}

# A plain assignment, not `[[ -z "$(ui_alb_arns)" ]]`: inside a test a failed
# lookup (expired credentials, throttling) reads as "no ALB left" and the
# teardown carries on to the VPC. As an assignment, set -e stops the script.
echo "==> Waiting for the controller to delete the $UI_ALB_STACK ALB..."
clear_ui_alb_protection
remaining=$(ui_alb_arns)
for _ in $(seq 36); do
  [[ -z "$remaining" ]] && break
  sleep 5
  clear_ui_alb_protection
  remaining=$(ui_alb_arns)
done

if [[ -n "$remaining" ]]; then
  # The controller is gone or stuck: unblock the ingresses, then stop — the
  # ALB will not delete itself now, and the VPC destroy would fail on it later.
  echo "    Timed out — stripping finalizers so the ingresses can be deleted..."
  for ref in "${UI_INGRESSES[@]}"; do
    ns=${ref%%/*} name=${ref#*/}
    kubectl patch ingress "$name" -n "$ns" \
      --type=json -p='[{"op":"remove","path":"/metadata/finalizers"}]' 2>/dev/null || true
    kubectl delete ingress "$name" -n "$ns" --timeout=30s 2>/dev/null || true
  done
  echo "FAILED: the $UI_ALB_STACK ALB still exists. Delete it, then re-run this script:"
  for arn in $remaining; do
    echo "    aws elbv2 delete-load-balancer --load-balancer-arn $arn"
  done
  exit "$EXIT_CHECK_FAILED"
fi

fi # SKIP_UI_ALB_TEARDOWN

# Same list install.sh applies, read backwards — not a second hand-kept copy.
mapfile -t phase2_teardown < <(reversed "${PHASE2_MODULES[@]}")
for target in "${phase2_teardown[@]}"; do
  destroy_target "$target"
done

# Force-drain backend nodes before EKS destroys the node group.
# The backend node group hosts CoreDNS and other system addons with PodDisruptionBudgets.
# EKS respects PDBs during drain and will stall indefinitely if there is nowhere to
# reschedule (e.g. single-node group). --disable-eviction bypasses PDBs entirely,
# which is safe here since the whole cluster is being torn down.
echo "==> Draining backend node group..."
for node in $(kubectl get nodes -l eks.amazonaws.com/nodegroup=backend -o name 2>/dev/null); do
  kubectl drain "$node" \
    --ignore-daemonsets \
    --delete-emptydir-data \
    --disable-eviction \
    --force \
    --timeout=120s 2>/dev/null || true
done

# Phase 2: Destroy the EKS cluster and then the VPC.
echo "==> Destroying EKS cluster..."
destroy_target module.eks

echo "==> Destroying VPC..."
destroy_target module.vpc

# The operators' Cognito user pool (cognito.tf) refuses to be destroyed twice
# over: `prevent_destroy` fails any plan that deletes it, and Cognito's own
# deletion protection refuses DeleteUserPool. Both are there so that no ordinary
# apply can lock everyone out. A teardown is the one deliberate exception, so
# the pool leaves Terraform's state before the full destroy — which still
# removes its domain, app clients and branding — and is deleted afterwards.
# Absent in a deployment with an external identity provider; then this is a
# no-op.
COGNITO_POOL_ADDRESS="$(stack_address aws_cognito_user_pool.operators)[0]"
COGNITO_POOL_ID=""
if terraform state list "$COGNITO_POOL_ADDRESS" 2>/dev/null | grep -q .; then
  COGNITO_POOL_ID=$(terraform state show -no-color "$COGNITO_POOL_ADDRESS" \
    | awk '$1 == "id" { gsub(/"/, "", $3); print $3; exit }')
  if [[ -z "$COGNITO_POOL_ID" ]]; then
    echo "FAILED: could not read the Cognito user pool's ID from state ($COGNITO_POOL_ADDRESS)." >&2
    exit "$EXIT_ERROR"
  fi
  echo "==> Releasing Cognito user pool $COGNITO_POOL_ID from state..."
  # A re-run cannot find the ID again once it has left state, so say it now.
  echo "    If this script stops before the end, delete the pool by hand:"
  echo "      # only if the pool still has a domain:"
  echo "      aws cognito-idp delete-user-pool-domain --user-pool-id $COGNITO_POOL_ID \\"
  echo "        --domain \"\$(aws cognito-idp describe-user-pool --user-pool-id $COGNITO_POOL_ID --query UserPool.Domain --output text)\""
  echo "      aws cognito-idp update-user-pool --user-pool-id $COGNITO_POOL_ID --deletion-protection INACTIVE"
  echo "      aws cognito-idp delete-user-pool --user-pool-id $COGNITO_POOL_ID"
  terraform state rm "$COGNITO_POOL_ADDRESS"
fi

# Phase 3: Full destroy to catch any remaining resources (IAM, ECR, Route53
# records, ACM certificates, SSM parameters, EventBridge schedulers, etc.)
# crds_available=false prevents errors from missing CRD types post-cluster.
echo "==> Final full destroy for remaining resources..."
terraform destroy -var="crds_available=false" -auto-approve 2>&1 | tee /dev/tty

# After the destroy, because Cognito refuses to delete a pool that still has a
# domain. UpdateUserPool resets every setting it is not given to its default,
# which is harmless only because the next call deletes the pool.
if [[ -n "$COGNITO_POOL_ID" ]]; then
  echo "==> Deleting Cognito user pool $COGNITO_POOL_ID..."
  aws cognito-idp update-user-pool --user-pool-id "$COGNITO_POOL_ID" --deletion-protection INACTIVE
  aws cognito-idp delete-user-pool --user-pool-id "$COGNITO_POOL_ID"
fi

echo "==> Cleanup complete."
