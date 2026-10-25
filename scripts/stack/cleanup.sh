#!/bin/bash
# Tear a CloudPipe deployment down. Like install.sh, nothing here is specific to
# one deployment: the root, its module call, the region and the cluster name are
# resolved from the root the caller names — see targets.sh.
#
# One command, not phases: nothing drives a teardown phase by phase, and the
# failure paths below print the by-hand commands they could not complete.
#
# Usage: cleanup.sh [--root DIR] [--module NAME] [--region NAME] [--name NAME]
#                   [--verbose]
#
# --verbose streams Terraform's output instead of one progress line per destroy;
# it changes what is shown, never what is destroyed. See tf_step in targets.sh.
set -euo pipefail

# Takes a stack-relative address (`module.vpc`) — see targets.sh — and destroys
# the root-level one Terraform needs (`module.stack.module.vpc`).
destroy_target() {
  local target
  target=$(stack_address "$1")
  echo "==> Destroying $target..."
  if tf_step "$1" terraform destroy -target="$target" -auto-approve; then
    echo "SUCCESS: $target"
  else
    echo "FAILED: $target"
    exit "$EXIT_ERROR"
  fi
}

# Several stack-relative addresses as -targets of ONE destroy:
#   destroy_targets "<label>" <address>...
# For the same reason install.sh's apply_targets exists: each targeted destroy
# pays about 30s before it removes anything, a no-op target included.
destroy_targets() {
  local label=$1 target targets=()
  shift
  for target in "$@"; do
    targets+=(-target="$(stack_address "$target")")
  done
  echo "==> Destroying ${#targets[@]} targets in one destroy: $label..."
  if tf_step "$label" terraform destroy "${targets[@]}" -auto-approve; then
    echo "SUCCESS: $label"
  else
    echo "FAILED: $label"
    exit "$EXIT_ERROR"
  fi
}

# kubectl in this script acts on this deployment's cluster and on nothing else.
#
# It used to follow the shell's current kubectl context. With this deployment's
# cluster already gone no kubeconfig entry was written for it, so a re-run's
# kubectl calls went to whatever cluster the shell pointed at — PRODUCTION, on
# 2026-10-23: they scaled its ArgoCD controllers to 0 and deleted its five
# web-UI ingresses, and only the production ALB's deletion protection stopped
# the rest. Two guards, either of which alone would have prevented it:
#
# - a private kubeconfig for the whole run, holding only the entry
#   require_reachable_cluster writes for this cluster, so no other cluster is
#   visible to kubectl at all;
# - a kubectl function that refuses to run while this cluster does not exist,
#   so there is nothing for a call to reach even by accident.
#
# The Terraform Kubernetes-family providers never read a kubeconfig: they are
# configured from the cluster's own endpoint in the root.
CLEANUP_KUBECONFIG=$(mktemp "${TMPDIR:-/tmp}/cleanup-kubeconfig.XXXXXX")
export KUBECONFIG=$CLEANUP_KUBECONFIG
trap 'rm -f "$CLEANUP_KUBECONFIG"' EXIT

kubectl() {
  [[ $CLUSTER_EXISTS == true ]] || return 1
  command kubectl "$@"
}

# The cluster's API has to be reachable from this shell before anything is
# destroyed. After the install's last phase it is private, reachable only over
# WARP (or the VPN). Every kubectl call below ends in `|| true`, because a
# resource that is already gone is not an error — so an unreachable API reads as
# "nothing to delete": the deletion-protected web-UI ALB is left behind, and the
# VPC destroy then fails on its network interfaces, halfway through the teardown
# (found preparing the second-account acceptance run, 2026-10-22).
#
# No cluster is fine: there is nothing for kubectl to reach, and the teardown is
# cleaning up what a failed install left. Also writes the kubeconfig entry the
# Helm and Kubernetes providers use during the destroy.
CLUSTER_EXISTS=false
require_reachable_cluster() {
  if ! aws eks describe-cluster --name "$CLUSTER_NAME" >/dev/null 2>&1; then
    return 0
  fi
  CLUSTER_EXISTS=true
  aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$DEPLOYMENT_REGION" \
    --kubeconfig "$KUBECONFIG" >/dev/null
  if ! kubectl get --raw=/readyz --request-timeout=20s >/dev/null 2>&1; then
    fail "$EXIT_BLOCKED" \
      "ERROR: the cluster $CLUSTER_NAME exists, but its API cannot be reached from this shell." \
      "After the install's last phase the API is private: connect WARP to this deployment's" \
      "Zero Trust team (or the VPN), check that \`kubectl get nodes\` works, then run this" \
      "again. Nothing has been destroyed."
  fi
}

# Every Kubernetes object Terraform manages, by resource type: kubernetes_*,
# kubectl_* and helm_release. They are spread over the stack itself and modules
# the teardown never names (Prefect's, among others), so a list of modules
# cannot cover them. Data sources are not objects and are left out.
in_cluster_addresses() {
  { terraform state list 2>/dev/null || true; } |
    grep -E '(^|\.)(kubernetes_[a-z0-9_]+|kubectl_[a-z0-9_]+|helm_release)\.' |
    grep -vE '(^|\.)data\.' || true
}

# Destroy them while the cluster can still be reached, before the cluster goes.
# Left in state past that point, each one fails every later destroy that
# touches it, because its provider can no longer reach an API server: the VPC
# destroy stopped on prefect/prefect-db-credentials after the cluster was gone
# (second-account acceptance run, 2026-10-23).
destroy_in_cluster_resources() {
  local addresses=() targets=() state_address
  mapfile -t addresses < <(in_cluster_addresses)
  [[ ${#addresses[@]} -eq 0 ]] && return 0
  # Read back from `terraform state list`, so already root-level: they need no
  # stack_address conversion, unlike the stack-relative names in targets.sh.
  for state_address in "${addresses[@]}"; do
    targets+=(-target="$state_address")
  done
  echo "==> Destroying the ${#addresses[@]} Kubernetes objects still in state, while the cluster can be reached..."
  if tf_step "Kubernetes objects" terraform destroy "${targets[@]}" -auto-approve; then
    echo "SUCCESS: the Kubernetes objects"
  else
    echo "FAILED: the Kubernetes objects"
    exit "$EXIT_ERROR"
  fi
}

# A re-run after the cluster is already gone: its objects went with it, and
# nothing can delete them through an API that no longer exists. They leave state
# instead, named, so the destroys that follow do not try to reach it. After the
# confirmation, because it changes state.
forget_in_cluster_resources() {
  [[ $CLUSTER_EXISTS == true ]] && return 0
  local addresses=()
  mapfile -t addresses < <(in_cluster_addresses)
  [[ ${#addresses[@]} -eq 0 ]] && return 0
  echo "==> The cluster is gone, and its ${#addresses[@]} Kubernetes objects went with it."
  echo "    Removing them from state, so nothing below tries to reach it:"
  printf '      %s\n' "${addresses[@]}"
  terraform state rm "${addresses[@]}" >/dev/null
}

# The two RDS databases, Argo's and Prefect's. Each defaults to
# deletion_protection = true, which AWS enforces by refusing DeleteDBInstance,
# and the stack offers no variable to turn it off: a teardown used to stop at the
# Argo module with "Cannot delete protected DB Instance" (second-account
# acceptance run, 2026-10-22). Read from state, not rebuilt from a naming rule,
# so a deployment that names them differently is still handled.
rds_addresses() {
  printf '%s\n' \
    "$(stack_address module.argo_workflows).aws_db_instance.this" \
    "$(stack_address module.prefect).aws_db_instance.this"
}

# One attribute of a resource in state, or nothing if the resource is not there.
# `|| true` because `state show` exits non-zero for an address that has left
# state, which under pipefail would end the script — and that is exactly the
# re-run after a partial teardown.
state_attr() {
  { terraform state show -no-color "$1" 2>/dev/null || true; } |
    awk -v key="$2" '$1 == key { gsub(/"/, "", $3); print $3; exit }'
}

# No final snapshot is taken by default (db_skip_final_snapshot is true). One is
# taken when a deployment sets it false, and by any deployment whose state still
# records the old default until its next apply: named <cluster>-argo-final and
# <cluster>-prefect-final. A snapshot by that name left by an earlier teardown in
# this account makes DeleteDBInstance fail with DBSnapshotAlreadyExists, after
# everything before it has been destroyed. Checked before the confirmation, so
# it stops with nothing destroyed. Read from state, so it checks exactly the
# snapshots this teardown would take, and none when it takes none.
require_free_final_snapshot_names() {
  local address snapshot taken=()
  while read -r address; do
    snapshot=$(state_attr "$address" final_snapshot_identifier)
    [[ -z $snapshot || $snapshot == null ]] && continue
    if aws rds describe-db-snapshots --db-snapshot-identifier "$snapshot" >/dev/null 2>&1; then
      taken+=("$snapshot")
    fi
  done < <(rds_addresses)
  [[ ${#taken[@]} -eq 0 ]] && return 0
  fail "$EXIT_BLOCKED" \
    "ERROR: a database snapshot already has the name this teardown gives its final" \
    "snapshot: ${taken[*]}. It is left from an earlier teardown in this account, and" \
    "AWS would refuse to delete the database partway through. Keep it under another" \
    "name, or delete it, then run this again. Nothing has been destroyed." \
    "$(printf '  aws rds delete-db-snapshot --db-snapshot-identifier %s\n' "${taken[@]}")"
}

# Turn deletion protection off on each database still in state. After the
# confirmation, so it only happens in a teardown someone has agreed to. Notes any
# final snapshot the destroy will take, for the closing report.
FINAL_SNAPSHOTS=()
release_rds_deletion_protection() {
  local address instance snapshot
  while read -r address; do
    instance=$(state_attr "$address" identifier)
    [[ -z $instance ]] && continue
    snapshot=$(state_attr "$address" final_snapshot_identifier)
    [[ -n $snapshot && $snapshot != null ]] && FINAL_SNAPSHOTS+=("$snapshot")
    echo "==> Turning off deletion protection on database $instance..."
    aws rds modify-db-instance --db-instance-identifier "$instance" \
      --no-deletion-protection --apply-immediately >/dev/null
  done < <(rds_addresses)
}

# The deployment's audit trail (logging.tf): the CloudTrail and flow-log bucket,
# the key that encrypts it, and the ALB and S3 access-log bucket. A plain
# `terraform destroy` handled these the worst way possible. It deleted the
# bucket policy that protects the trail, the trail itself and the key (7-day
# window), then failed to delete the buckets because they are not empty
# (second-account teardown, 2026-10-23): the logs stayed, and became unreadable
# a week later.
#
# So the teardown asks, before anything is destroyed. Keep (the default): all of
# these leave state before the final destroy and stay as they are, protected
# and readable, and their lifecycle rules expire the logs over time. Delete: they
# leave state the same way, and are emptied and deleted after the final destroy,
# once CloudTrail has stopped writing to them.
audit_log_addresses() {
  local resource
  for resource in \
    aws_s3_bucket.logs \
    aws_s3_bucket_policy.logs \
    aws_s3_bucket_versioning.logs \
    aws_s3_bucket_lifecycle_configuration.logs \
    aws_s3_bucket_server_side_encryption_configuration.logs \
    aws_s3_bucket_public_access_block.logs \
    aws_s3_bucket_logging.logs \
    aws_kms_key.logs \
    aws_kms_alias.logs \
    aws_s3_bucket.access_logs \
    aws_s3_bucket_policy.access_logs \
    aws_s3_bucket_lifecycle_configuration.access_logs \
    aws_s3_bucket_server_side_encryption_configuration.access_logs \
    aws_s3_bucket_public_access_block.access_logs \
    aws_s3_bucket_ownership_controls.access_logs; do
    stack_address "$resource"
  done
}

# Read from state before the confirmation, since they leave it before the
# final destroy.
AUDIT_LOG_BUCKETS=()
AUDIT_LOG_KEY_ID=""
AUDIT_LOG_KEY_WINDOW=""
AUDIT_LOG_ALIAS=""
KEEP_AUDIT_LOGS=true
ask_about_audit_logs() {
  local resource bucket answer
  for resource in aws_s3_bucket.logs aws_s3_bucket.access_logs; do
    bucket=$(state_attr "$(stack_address "$resource")" bucket)
    if [[ -n $bucket ]]; then AUDIT_LOG_BUCKETS+=("$bucket"); fi
  done
  AUDIT_LOG_KEY_ID=$(state_attr "$(stack_address aws_kms_key.logs)" id)
  AUDIT_LOG_KEY_WINDOW=$(state_attr "$(stack_address aws_kms_key.logs)" deletion_window_in_days)
  AUDIT_LOG_ALIAS=$(state_attr "$(stack_address aws_kms_alias.logs)" name)
  [[ ${#AUDIT_LOG_BUCKETS[@]} -eq 0 ]] && return 0

  echo ""
  echo "==> This deployment's audit logs (CloudTrail, VPC flow logs, ALB and S3 access logs) are in:"
  printf '      s3://%s\n' "${AUDIT_LOG_BUCKETS[@]}"
  echo "    Keep them: the buckets, and the key that encrypts the CloudTrail record, stay"
  echo "    in this account, protected and readable; their lifecycle rules expire the"
  echo "    logs over time. Delete them: every object version is deleted, then the"
  echo "    buckets, and the key is scheduled for deletion. Deleting cannot be undone."
  read -rp "    Keep the audit logs? [Y/n] " answer
  case $answer in
    n | N | no | No | NO) KEEP_AUDIT_LOGS=false ;;
    *) KEEP_AUDIT_LOGS=true ;;
  esac
}

# Every audit-log resource leaves state, whichever was chosen: kept, the final
# destroy must not touch them; deleted, they are deleted below in the one order
# that works. The by-hand commands are printed first, because a re-run cannot
# read the names once they have left state.
release_audit_logs() {
  local address addresses=()
  while read -r address; do
    if terraform state list "$address" 2>/dev/null | grep -q .; then addresses+=("$address"); fi
  done < <(audit_log_addresses)
  [[ ${#addresses[@]} -eq 0 ]] && return 0
  if [[ $KEEP_AUDIT_LOGS == true ]]; then
    echo "==> Releasing the audit logs from state, to keep them..."
  else
    echo "==> Releasing the audit logs from state, to delete them after the final destroy..."
    echo "    If this script stops before the end, delete them by hand, for each bucket:"
    echo "      aws s3api delete-bucket-policy --bucket <bucket>"
    echo "      # then delete every object version, and:"
    echo "      aws s3api delete-bucket --bucket <bucket>"
    printf '    Buckets: %s\n' "${AUDIT_LOG_BUCKETS[*]}"
    if [[ -n $AUDIT_LOG_KEY_ID ]]; then
      echo "      aws kms schedule-key-deletion --region $DEPLOYMENT_REGION --key-id $AUDIT_LOG_KEY_ID --pending-window-in-days ${AUDIT_LOG_KEY_WINDOW:-7}"
    fi
  fi
  terraform state rm "${addresses[@]}"
}

# Deletes every object version and delete marker, 1000 at a time (the
# DeleteObjects limit). Listing again from the start each time, so nothing is
# skipped as pages disappear.
empty_bucket() {
  local bucket=$1 batch
  batch=$(mktemp "${TMPDIR:-/tmp}/cleanup-versions.XXXXXX")
  while :; do
    # shellcheck disable=SC2016 # `true` is a JMESPath literal, not a command substitution
    aws s3api list-object-versions --bucket "$bucket" --max-items 1000 --output json \
      --query '{Objects: [Versions, DeleteMarkers][][].{Key: Key, VersionId: VersionId}, Quiet: `true`}' \
      >"$batch"
    grep -q '"Key"' "$batch" || break
    aws s3api delete-objects --bucket "$bucket" --delete "file://$batch" >/dev/null
  done
  rm -f "$batch"
}

# After the final destroy, which removed the trail and every bucket's logging
# configuration, so nothing new is written. S3 server access logs are delivered
# up to hours late, though, so a bucket that is not empty when deleted is
# emptied again a few times before giving up.
delete_audit_logs() {
  [[ $KEEP_AUDIT_LOGS == true ]] && return 0
  local bucket attempt
  for bucket in "${AUDIT_LOG_BUCKETS[@]}"; do
    echo "==> Deleting audit-log bucket $bucket..."
    # The policy denies DeleteObjectVersion to everyone but the account root (AU-9).
    aws s3api delete-bucket-policy --bucket "$bucket" 2>/dev/null || true
    for attempt in 1 2 3 4 5; do
      empty_bucket "$bucket"
      aws s3api delete-bucket --bucket "$bucket" 2>/dev/null && break
      if [[ $attempt -eq 5 ]]; then
        echo "FAILED: s3://$bucket is still not empty; delete it by hand once deliveries stop:" >&2
        echo "  aws s3 rb s3://$bucket --force   # after deleting every object version" >&2
        exit "$EXIT_ERROR"
      fi
      sleep 30
    done
  done
  if [[ -n $AUDIT_LOG_ALIAS ]]; then
    aws kms delete-alias --region "$DEPLOYMENT_REGION" --alias-name "$AUDIT_LOG_ALIAS" 2>/dev/null || true
  fi
  if [[ -n $AUDIT_LOG_KEY_ID ]]; then
    echo "==> Scheduling deletion of the audit-log key $AUDIT_LOG_KEY_ID in ${AUDIT_LOG_KEY_WINDOW:-7} days..."
    aws kms schedule-key-deletion --region "$DEPLOYMENT_REGION" --key-id "$AUDIT_LOG_KEY_ID" \
      --pending-window-in-days "${AUDIT_LOG_KEY_WINDOW:-7}" >/dev/null
  fi
}

# The ECR registry's scanning configuration (ecr.tf) is account-wide, not this
# deployment's. Destroying it means turning enhanced scanning off, which an
# AWS Organizations member account is not allowed to do when the organization
# manages Inspector ("Member account ... is not authorized to disable itself",
# second-account teardown, 2026-10-23). It leaves state and stays as it is; with
# this deployment's repositories gone it has nothing to scan.
release_registry_scanning_configuration() {
  local address
  address=$(stack_address aws_ecr_registry_scanning_configuration.this)
  terraform state list "$address" 2>/dev/null | grep -q . || return 0
  echo "==> Releasing the account's ECR scanning configuration from state (left as it is)..."
  terraform state rm "$address"
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
init_step_logs
require_cloudflare_api_token

# `init` before the target validation, as in install.sh: it writes the module
# manifest that resolves which module call is the stack. Never `-upgrade`, for
# the reason install.sh gives: it trades the locked provider build for whatever
# was published last. Through tf_step, so its output goes to the run's log.
tf_step "providers and modules" terraform init -input=false
resolve_stack_module

echo "==> Validating -target addresses against the configuration..."
assert_targets_declared module.vpc module.eks "${PHASE2_MODULES[@]}"

# A teardown runs against a deployment that exists, so both of these come from
# its own state rather than from a flag.
require_deployment_region
require_cluster_name

# Read NOW, not at the end: these name the buckets this teardown must not delete,
# and the destroy below leaves this root with no outputs to read them from. Empty
# is tolerated — an older root may not re-export them — and the closing report
# says where to look instead.
SURVIVING_BUCKETS=()
for _output in terraform_state_bucket data_bucket metrics_bucket; do
  _bucket=$(stack_output "$_output")
  [[ -n "$_bucket" && "$_bucket" != "null" ]] && SURVIVING_BUCKETS+=("$_bucket")
done
unset _output _bucket

# Before the confirmation, so an unreachable cluster stops the run with nothing
# destroyed and nobody has typed "destroy" for a teardown that cannot finish.
echo "==> Checking that the cluster's API can be reached..."
require_reachable_cluster
echo "==> Checking that the databases' final snapshot names are free..."
require_free_final_snapshot_names
ask_about_audit_logs

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

release_rds_deletion_protection
forget_in_cluster_resources

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

# Same list install.sh applies — not a second hand-kept copy — and, like its
# apply, in one destroy: Terraform destroys a dependent before what it depends
# on, which is the order reading the list backwards used to give by hand.
destroy_targets "cluster add-ons" "${PHASE2_MODULES[@]}"

# Force-drain backend nodes before EKS destroys the node group.
# The backend node group hosts CoreDNS and other system addons with PodDisruptionBudgets.
# EKS respects PDBs during drain and will stall indefinitely if there is nowhere to
# reschedule (e.g. single-node group). --disable-eviction bypasses PDBs entirely,
# which is safe here since the whole cluster is being torn down.
destroy_in_cluster_resources

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

# The cluster's Secrets-encryption key (eks.tf) carries `prevent_destroy` for
# the same reason: scheduling its deletion makes every Secret in a live cluster
# unreadable once the window ends. With the cluster destroyed above there is
# nothing left for it to decrypt, so it leaves state too and its deletion is
# scheduled here, with the window it was created with. The second-account
# teardown's final destroy failed on it ("Instance cannot be destroyed",
# 2026-10-23). The cluster is checked again first: this must never run against
# a cluster that still exists.
EKS_KEY_ADDRESS=$(stack_address aws_kms_key.eks_secrets)
if terraform state list "$EKS_KEY_ADDRESS" 2>/dev/null | grep -q .; then
  if aws eks describe-cluster --region "$DEPLOYMENT_REGION" --name "$CLUSTER_NAME" >/dev/null 2>&1; then
    echo "FAILED: cluster $CLUSTER_NAME still exists; not scheduling deletion of its Secrets key." >&2
    exit "$EXIT_ERROR"
  fi
  EKS_KEY_ID=$(state_attr "$EKS_KEY_ADDRESS" id)
  EKS_KEY_WINDOW=$(state_attr "$EKS_KEY_ADDRESS" deletion_window_in_days)
  if [[ -z "$EKS_KEY_ID" || -z "$EKS_KEY_WINDOW" ]]; then
    echo "FAILED: could not read the cluster's Secrets key from state ($EKS_KEY_ADDRESS)." >&2
    exit "$EXIT_ERROR"
  fi
  echo "==> Releasing the cluster's Secrets key $EKS_KEY_ID from state..."
  echo "    If this script stops before the end, schedule its deletion by hand:"
  echo "      aws kms schedule-key-deletion --region $DEPLOYMENT_REGION --key-id $EKS_KEY_ID --pending-window-in-days $EKS_KEY_WINDOW"
  terraform state rm "$EKS_KEY_ADDRESS"
  echo "==> Scheduling deletion of key $EKS_KEY_ID in $EKS_KEY_WINDOW days..."
  aws kms schedule-key-deletion --region "$DEPLOYMENT_REGION" --key-id "$EKS_KEY_ID" \
    --pending-window-in-days "$EKS_KEY_WINDOW" >/dev/null
fi

release_audit_logs
release_registry_scanning_configuration

# Phase 3: Full destroy to catch any remaining resources (IAM, ECR, Route53
# records, ACM certificates, SSM parameters, EventBridge schedulers, etc.)
# crds_available=false prevents errors from missing CRD types post-cluster.
echo "==> Final full destroy for remaining resources..."
tf_step "remaining resources" terraform destroy -var="crds_available=false" -auto-approve

# After the destroy, because Cognito refuses to delete a pool that still has a
# domain. UpdateUserPool resets every setting it is not given to its default,
# which is harmless only because the next call deletes the pool.
if [[ -n "$COGNITO_POOL_ID" ]]; then
  echo "==> Deleting Cognito user pool $COGNITO_POOL_ID..."
  aws cognito-idp update-user-pool --user-pool-id "$COGNITO_POOL_ID" --deletion-protection INACTIVE
  aws cognito-idp delete-user-pool --user-pool-id "$COGNITO_POOL_ID"
fi

delete_audit_logs

# The buckets this teardown deliberately did not touch.
#
# They are created by the bootstrap root, whose state is separate from this one,
# so `terraform destroy` here cannot reach them — and each carries
# `prevent_destroy` with no `force_destroy`, so it could not delete them anyway.
# That is the design: a cluster is disposable, the data it processed is not, and a
# second install in this account adopts these rather than failing on them.
#
# Named at the END, after the destroy, because that is when someone is deciding
# whether they are finished. Printed rather than deleted, and there is no flag
# that deletes them: that flag's misuse is the one that cannot be undone.

# The databases' final snapshots, when there are any: none by default, since
# db_skip_final_snapshot is true. Reported like the buckets because they cost
# money, and a later teardown of a deployment with the same cluster name in this
# account stops until they are renamed or deleted.
report_final_snapshots() {
  [[ ${#FINAL_SNAPSHOTS[@]} -eq 0 ]] && return 0
  echo ""
  echo "==> These database snapshots were taken and kept (db_skip_final_snapshot is false"
  echo "    in this deployment's state):"
  printf '      %s\n' "${FINAL_SNAPSHOTS[@]}"
  echo "    They are the last copy of the Argo and Prefect databases. A later teardown"
  echo "    of a deployment named $CLUSTER_NAME in this account will stop until they are"
  echo "    gone. Only if you are finished with them:"
  printf '      aws rds delete-db-snapshot --db-snapshot-identifier %s\n' "${FINAL_SNAPSHOTS[@]}"
}

report_kept_audit_logs() {
  [[ $KEEP_AUDIT_LOGS == true && ${#AUDIT_LOG_BUCKETS[@]} -gt 0 ]] || return 0
  echo ""
  echo "==> The audit logs were kept, as asked:"
  printf '      s3://%s\n' "${AUDIT_LOG_BUCKETS[@]}"
  if [[ -n $AUDIT_LOG_KEY_ID ]]; then
    echo "    encrypted with KMS key $AUDIT_LOG_KEY_ID, which was kept too."
  fi
  echo "    Terraform no longer manages them. A re-install in this account fails on"
  echo "    these bucket names until they are deleted or imported."
}

report_surviving_buckets() {
  echo ""
  echo "==> These buckets were NOT deleted, on purpose:"
  if [[ ${#SURVIVING_BUCKETS[@]} -gt 0 ]]; then
    printf '      s3://%s\n' "${SURVIVING_BUCKETS[@]}"
  else
    echo "      (names unavailable — this root re-exports no bucket outputs;"
    echo "       read them from the bootstrap root's terraform.tfvars)"
  fi
  echo ""
  echo "    They hold this deployment's Terraform state, its imaging data, and its"
  echo "    record of what was processed. The bootstrap root owns them and keeps its"
  echo "    own state, so nothing here can delete them, and prevent_destroy means"
  echo "    Terraform will refuse to even when asked."
  echo ""
  echo "    Re-installing into this account REUSES them. Only if you are finished"
  echo "    with the data as well, and for each bucket above:"
  echo "      aws s3 rb s3://<bucket> --force"
  echo ""

  # Interactive only. `cleanup.sh` takes no flag that changes what it does, by
  # decision (`--verbose` changes only what it shows), and a teardown
  # driven from CI must not hang waiting for a terminal that is not there.
  if [[ -t 0 ]]; then
    read -r -p "    Press Enter once you have read the above. " _
  fi
}

report_final_snapshots
report_kept_audit_logs
report_surviving_buckets

echo "==> Cleanup complete."
