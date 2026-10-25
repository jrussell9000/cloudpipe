#!/bin/bash
# Stand up a CloudPipe deployment, in phases, from a Terraform root that calls
# the stack module. Nothing in this script is specific to one deployment: the
# root, its module call, the region and the cluster name are all resolved from
# the root the caller names — see targets.sh.
#
# Usage: install.sh [--root DIR] [--module NAME] [--region NAME] [--name NAME]
#                   [--phase N | --from-phase N] [--verbose]
#        install.sh --list-phases [--json]
#
# With no phase flag every phase runs, in order. `--phase N` runs exactly one and
# `--from-phase N` runs N through the last; both are safe because each phase
# re-reads what it needs and re-checks its own preconditions, rather than relying
# on an earlier phase in the same invocation. The one phase that cannot be made
# recoverable — closing the public EKS endpoint — re-proves its two gates every
# time it runs.
set -euo pipefail

# The Secrets Manager name the cloudflared ExternalSecret reads, as a literal in
# gitops/apps/cloudflared/templates/externalsecret.yaml. DELIBERATELY NOT derived
# from the deployment's name prefix: a derived name would be a secret no
# ExternalSecret reads, and cloudflared would wait for a token that never
# arrives. tests/test_installer_fixed_names.py holds the two equal.
TUNNEL_TOKEN_SECRET="cloudpipe/cloudflare-tunnel-token"

# Deployment resolution, target lists, exit codes and assert_targets_declared()
# are shared with cleanup.sh.
# shellcheck source=SCRIPTDIR/targets.sh
source "$(dirname "${BASH_SOURCE[0]}")/targets.sh"

# The CRDs ArgoCD installs that later phases depend on. Phase 5 waits for them;
# Phase 6 refuses to run without them, because the resources it applies are the
# ones gated on `crds_available`.
ESTABLISHED_CRDS=(
  clustersecretstores.external-secrets.io
  prometheusrules.monitoring.coreos.com
)

# ---------------------------------------------------------------------------
# The phase set, as data
# ---------------------------------------------------------------------------

# One record per phase: id|function|public endpoint open?|title.
#
# This is the ONLY place the phases are enumerated. --list-phases reads it, the
# dispatch below reads it, and the phase table in docs/infrastructure.md is held
# to it by a test — the 6-phase documented table was wrong for months while this
# script ran eight, which is the drift that test exists to catch.
#
# Titles are printed as JSON strings by hand, so they must stay free of double
# quotes and backslashes; tests/test_installer_phases.py asserts that.
#
# Titles are written for the deployer, who meets them in --list-phases, in the
# progress line each phase prints, and on the setup page's Run buttons. The
# Terraform detail (targets, flags, CRD names) belongs to the "What happens"
# column of the table in docs/infrastructure.md, not here.
PHASES=(
  "1|phase_1_network_and_cluster|true|Create the network and the Kubernetes cluster"
  "2|phase_2_cluster_addons|true|Install the cluster add-ons"
  "3|phase_3_kube_system_network_policies|true|Add the network rules for the cluster's system services"
  "4|phase_4_full_apply_and_tunnel_token|true|Create the remaining resources, including the Cloudflare tunnel, and store its token"
  "5|phase_5_wait_for_crds|true|Wait for ArgoCD to install the resource types later phases need"
  "6|phase_6_crd_dependent_resources|true|Save the install settings, then connect the cluster to its stored secrets"
  "7|phase_7_prove_the_tunnel|true|Check that the Cloudflare tunnel works, while the cluster can still be reached"
  "8|phase_8_close_the_public_endpoint|false|Close the cluster's public endpoint, leaving WARP as the only way in"
)

# One field of one phase, or non-zero for an id this installer does not have —
# which is how `--phase 9` is rejected.
phase_field() {
  local id=$1 field=$2 record
  for record in "${PHASES[@]}"; do
    if [[ ${record%%|*} == "$id" ]]; then
      IFS='|' read -r _ fn _ title <<<"$record"
      case "$field" in
        function) printf '%s' "$fn" ;;
        title) printf '%s' "$title" ;;
      esac
      return 0
    fi
  done
  return 1
}

phase_ids() {
  local record
  for record in "${PHASES[@]}"; do
    printf '%s\n' "${record%%|*}"
  done
}

# Printed without reading Terraform state, calling AWS or Cloudflare, or needing
# a root — so the setup wizard can render the phase list on a workstation that
# has nothing deployed yet, and so the docs test can run in CI. jq is not used
# for the same reason the rest of the script's prerequisites are not checked
# here: listing must work on a machine that has none of them.
list_phases() {
  local record id endpoint title first=true
  if [[ $LIST_JSON == true ]]; then
    printf '['
    for record in "${PHASES[@]}"; do
      IFS='|' read -r id _ endpoint title <<<"$record"
      [[ $first == true ]] || printf ','
      first=false
      printf '{"id":%s,"endpoint_public_access":%s,"title":"%s"}' "$id" "$endpoint" "$title"
    done
    printf ']\n'
    return 0
  fi
  printf '%-6s %-16s %s\n' Phase "Public endpoint" "What happens"
  for record in "${PHASES[@]}"; do
    IFS='|' read -r id _ endpoint title <<<"$record"
    printf '%-6s %-16s %s\n' "$id" "$([[ $endpoint == true ]] && echo open || echo closed)" "$title"
  done
}

# ---------------------------------------------------------------------------
# Shared machinery
# ---------------------------------------------------------------------------

# Takes a stack-relative address (`module.vpc`) — see targets.sh — and applies
# the root-level one Terraform needs (`module.stack.module.vpc`).
apply_target() {
  # The progress line is labelled with the stack-relative address: it is
  # shorter, and it is the part that tells one target from the next.
  local target label=$1
  target=$(stack_address "$1")
  shift
  echo "==> Applying $target..."
  if tf_step "$label" terraform apply -target="$target" "$@" -auto-approve; then
    echo "SUCCESS: $target"
  else
    echo "FAILED: $target"
    # 1, not 3: Terraform has already printed what went wrong, and an apply
    # failure is not this script's check failing.
    exit "$EXIT_ERROR"
  fi
}

# Poll until a command succeeds, or exit after a timeout. kubectl wait fails
# immediately on a resource that does not exist yet, so anything ArgoCD has not
# synced yet has to be polled for existence first.
poll_until() {
  local desc=$1 timeout=$2
  shift 2
  local elapsed=0 interval=10
  echo "    Waiting for $desc"
  until "$@" &>/dev/null; do
    if [[ $elapsed -ge $timeout ]]; then
      echo "TIMEOUT: $desc not reached after ${timeout}s"
      return 1
    fi
    sleep $interval
    elapsed=$((elapsed + interval))
  done
}

# Cloudflare API call. The Authorization header is read from a process
# substitution rather than passed with -H, so the API token never appears in
# the process argument list. CF_ACCOUNT_ID and CF_TUNNEL_ID come from
# require_cloudflare_ids, which every phase that needs them calls itself.
cf_api() {
  curl -fsS "https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}$1" \
    --header @<(printf 'Authorization: Bearer %s' "$CLOUDFLARE_API_TOKEN")
}

# The tunnel's account and id, read from the deployment rather than passed
# between phases: a standalone Phase 7 or 8 has no Phase 4 to have resolved them.
#
# Plain assignments, so a missing output fails here under `set -e`. Interpolated
# inline into the API URL instead, the failure was masked into a malformed URL —
# which inside wait_for_tunnel's poll surfaced minutes later as "tunnel not
# healthy" — and every poll iteration re-read remote state.
CF_ACCOUNT_ID=""
CF_TUNNEL_ID=""
require_cloudflare_ids() {
  [[ -n $CF_ACCOUNT_ID && -n $CF_TUNNEL_ID ]] && return 0
  CF_ACCOUNT_ID=$(stack_output cloudflare_account_id)
  CF_TUNNEL_ID=$(stack_output cloudflare_tunnel_id)
  if [[ -z $CF_ACCOUNT_ID || -z $CF_TUNNEL_ID ]]; then
    fail "$EXIT_CHECK_FAILED" \
      "ERROR: this deployment has no Cloudflare tunnel yet, so it cannot be proven." \
      "\`terraform output -raw cloudflare_tunnel_id\` gave nothing. Phase 4 creates the" \
      "tunnel: run \`--from-phase 4\` first."
  fi
}

fetch_tunnel_token() {
  # jq -j, not -r: -r appends a newline that is stored verbatim and makes
  # cloudflared fail registration with what looks like an authentication error.
  # -e plus `strings` fails loudly instead of storing the literal "null".
  cf_api "/cfd_tunnel/${CF_TUNNEL_ID}/token" | jq -je '.result | strings'
}

# A hash of the token Cloudflare currently issues, which is what the in-cluster
# Secret has to match. Re-derived on demand rather than carried over from the
# phase that stored the token: whoever runs Phase 7 alone needs it too, and
# fetching it again is one API call.
TUNNEL_TOKEN_SHA=""
require_tunnel_token_sha() {
  [[ -n $TUNNEL_TOKEN_SHA ]] && return 0
  TUNNEL_TOKEN_SHA=$(fetch_tunnel_token | sha256sum)
}

# Copy the tunnel's connector token into Secrets Manager, where External Secrets
# delivers it to cloudflared. Terraform deliberately never reads the token (ADR
# 014 Amendment 1 — it would land in state), so this is the only thing that
# keeps the stored copy current when the tunnel is recreated. Only hashes are
# compared; the token itself goes pipe-to-pipe and never reaches the terminal,
# a file, or argv.
sync_tunnel_token() {
  echo "==> Syncing the Cloudflare tunnel token into Secrets Manager..."
  require_tunnel_token_sha
  if aws secretsmanager describe-secret --secret-id "$TUNNEL_TOKEN_SECRET" &>/dev/null; then
    local stored
    stored=$(aws secretsmanager get-secret-value --secret-id "$TUNNEL_TOKEN_SECRET" \
      --query SecretString --output json | jq -j . | sha256sum)
    if [[ "$stored" == "$TUNNEL_TOKEN_SHA" ]]; then
      echo "    Stored token is current."
      return
    fi
    fetch_tunnel_token | aws secretsmanager put-secret-value \
      --secret-id "$TUNNEL_TOKEN_SECRET" --secret-string file:///dev/stdin >/dev/null
  else
    fetch_tunnel_token | aws secretsmanager create-secret \
      --name "$TUNNEL_TOKEN_SECRET" --secret-string file:///dev/stdin >/dev/null
  fi
  echo "    Stored token updated."
}

# The operator_emails addresses the Cognito pool has no user for, one per line,
# lower-cased. Nothing for a deployment with an external identity provider,
# whose pool output is null, or a root that does not name the variable
# operator_emails (the wizard's roots all do).
missing_operator_accounts() {
  local pool=$1 wanted have
  wanted=$(printf 'jsonencode(var.operator_emails)\n' | terraform console 2>/dev/null |
    jq -r 'fromjson? | .[]?' 2>/dev/null | tr '[:upper:]' '[:lower:]' | sort -u) || true
  [[ -n $wanted ]] || return 0
  # shellcheck disable=SC2016 # `email` is a JMESPath literal, not a command substitution
  have=$(aws cognito-idp list-users --user-pool-id "$pool" \
    --query 'Users[].Attributes[?Name==`email`].Value[]' --output text 2>/dev/null |
    tr '\t' '\n' | tr '[:upper:]' '[:lower:]' | sort -u) || true
  comm -23 <(printf '%s\n' "$wanted") <(printf '%s\n' "$have" | sed '/^$/d')
}

# Offer to create the operators' Cognito accounts, on a terminal, before the
# check below insists on one. Design D10 kept this a hand step; the acceptance
# install (2026-10-25) revisited it: the deployer has already named these people
# in operator_emails, and the hand-typed command is where `email_verified=true`
# gets left off, which the UIs then silently refuse. It asks, and does nothing
# without a yes. Without a terminal — the setup page's runner, a script — it
# never asks, and the check below prints the command as before.
#
# Never fails the phase itself: whether there is now a user is the next check's
# question, not this one's.
offer_operator_accounts() {
  local pool email answer missing=()
  pool=$(terraform output -json cognito_user_pool_id 2>/dev/null | jq -r '. // empty')
  [[ -n $pool ]] || return 0
  mapfile -t missing < <(missing_operator_accounts "$pool")
  [[ ${#missing[@]} -gt 0 && -n ${missing[0]} ]] || return 0
  [[ -t 0 && -t 1 ]] || return 0
  echo "The Cognito user pool $pool has no user for:"
  printf '    %s\n' "${missing[@]}"
  echo "Each account is created with a verified email, and Cognito emails it a temporary"
  echo "password. Its first sign-in sets a real password and enrolls an authenticator app."
  read -rp "Create these accounts now? [y/N] " answer || answer=""
  [[ $answer == [yY] || $answer == [yY][eE][sS] ]] || return 0
  for email in "${missing[@]}"; do
    if aws cognito-idp admin-create-user --user-pool-id "$pool" --username "$email" \
      --user-attributes Name=email,Value="$email" Name=email_verified,Value=true >/dev/null; then
      echo "    Created $email."
    else
      echo "    Could not create $email; the command above says why." >&2
    fi
  done
}

# With Cognito, nobody can sign in to Cloudflare Access — and so reach the API
# server over WARP — until a user exists in the pool. Closing the public endpoint
# before then leaves only the VPN. offer_operator_accounts asks first on a
# terminal; this is the check that holds either way.
#
# Prints nothing and succeeds for a deployment with an external identity
# provider, whose output is null.
cognito_has_users() {
  local pool count
  pool=$(terraform output -json cognito_user_pool_id | jq -r '. // empty')
  [[ -z "$pool" ]] && return 0
  count=$(aws cognito-idp list-users --user-pool-id "$pool" --limit 1 \
    --query 'length(Users)' --output text) || return 1
  if [[ "$count" -ge 1 ]]; then
    return 0
  fi
  echo "The Cognito user pool $pool has no users, so no operator can sign in over WARP." >&2
  echo "Create one per operator_emails entry, then sign in once to set a password and" >&2
  echo "enroll an authenticator app:" >&2
  echo "  aws cognito-idp admin-create-user --user-pool-id $pool \\" >&2
  echo "    --username <email> --user-attributes Name=email,Value=<email> Name=email_verified,Value=true" >&2
  echo "Then enroll WARP in this deployment's team and sign in once, while the endpoint is" >&2
  echo "still open: docs/deployer-first-hour.md, \"Step 9 — connect WARP and sign in for the" >&2
  echo "first time\". The first sign-in sets the password and enrolls the authenticator app." >&2
  return 1
}

# Whether THIS machine reaches the cluster's private API endpoint, which is the
# only way in once the public one closes. The pool check above proves an account
# exists, not that anyone can use it: enrolling WARP, the first sign-in, the
# device profile and the route to the VPC all have to work too, and the guide
# used to say the installer could not check them. It can, while the public
# endpoint is still open: ask an API server address inside the VPC, by the
# endpoint's own hostname so its certificate verifies, with this kubeconfig's
# credentials. An answer proves the whole path. (The mTLS Client VPN routes the
# same CIDR, so a deployer on the VPN passes too.)
#
# Safe against WARP on another team: a deployment that shares the VPC CIDR
# routes these addresses to a different VPC, where no API server presents a
# certificate for this cluster's hostname, so the check fails rather than
# passing by accident.
private_endpoint_reachable() {
  local endpoint host ip ips
  endpoint=$(aws eks describe-cluster --name "$CLUSTER_NAME" --query cluster.endpoint \
    --output text) || return 1
  host=${endpoint#https://}
  ips=$(aws ec2 describe-network-interfaces \
    --filters "Name=description,Values=Amazon EKS $CLUSTER_NAME" "Name=status,Values=in-use" \
    --query 'NetworkInterfaces[].PrivateIpAddress' --output text) || return 1
  ips=${ips//$'\t'/ }
  for ip in $ips; do
    if [[ $(kubectl --server "https://$ip" --tls-server-name "$host" --request-timeout=10s \
      get --raw /livez 2>/dev/null) == ok ]]; then
      echo "    The private endpoint answers at $ip."
      return 0
    fi
  done
  echo "This machine cannot reach the cluster's private API endpoint (${ips:-no addresses found})." >&2
  echo "After this phase that endpoint is the only way in. Connect WARP to this deployment's" >&2
  echo "team and sign in once: docs/deployer-first-hour.md, \"Step 9 — connect WARP and sign" >&2
  echo "in for the first time\". \`warp-cli status\` must say Connected, and to this team, not" >&2
  echo "another deployment's." >&2
  return 1
}

k8s_token_is_current() {
  [[ "$(kubectl -n cloudflared get secret cloudflared-token -o jsonpath='{.data.token}' \
    | base64 -d | sha256sum)" == "$TUNNEL_TOKEN_SHA" ]]
}

tunnel_is_healthy() {
  [[ "$(cf_api "/cfd_tunnel/${CF_TUNNEL_ID}" | jq -r '.result.status')" == "healthy" ]]
}

# The gate before the public endpoint closes: the in-cluster Secret must hold
# the CURRENT token (not merely exist — ESO refreshes hourly, so a stale value
# can sit there looking synced), cloudflared must be rolled out on it, and
# Cloudflare itself must report the tunnel healthy.
#
# The rollout restart is unconditional. Whether the running pod already holds the
# current token is not observable from the Secret — the token reaches cloudflared
# as an environment variable, which a Secret update does not refresh — so a phase
# running on its own cannot know, and guessing "no restart needed" leaves a pod
# on a stale token and a tunnel that never reports healthy. A rollout costs under
# a minute and is idempotent; that is the cheaper way to be wrong.
#
# Every step carries `|| return 1` because this is called as an `if` condition,
# and bash suspends `set -e` for the whole function body in that context — a
# failed step would otherwise fall through to closing the endpoint.
wait_for_tunnel() {
  poll_until "ExternalSecret cloudflared/cloudflared-token (ArgoCD sync)" 600 \
    kubectl -n cloudflared get externalsecret cloudflared-token || return 1
  kubectl -n cloudflared annotate externalsecret cloudflared-token \
    force-sync="$(date +%s)" --overwrite >/dev/null || return 1
  poll_until "the in-cluster tunnel token to match Secrets Manager" 300 k8s_token_is_current || return 1
  kubectl -n cloudflared rollout restart deployment/cloudflared || return 1
  kubectl -n cloudflared rollout status deployment/cloudflared --timeout=300s || return 1
  poll_until "Cloudflare to report the tunnel healthy" 300 tunnel_is_healthy
}

# ---------------------------------------------------------------------------
# Per-phase preconditions
# ---------------------------------------------------------------------------

# The cluster exists, and this shell's kubeconfig points at it.
#
# Both halves are re-established rather than assumed: `update-kubeconfig` is
# idempotent and a phase run on its own has no Phase 1 in the same process to
# have written the context. A phase that finds no cluster says which phase builds
# one instead of failing later inside a provider.
require_cluster() {
  require_deployment_region
  require_cluster_name
  if ! aws eks describe-cluster --name "$CLUSTER_NAME" >/dev/null 2>&1; then
    fail "$EXIT_CHECK_FAILED" \
      "ERROR: no EKS cluster named $CLUSTER_NAME exists in $DEPLOYMENT_REGION." \
      "Phase 1 creates the network and the cluster: run \`--from-phase 1\` first."
  fi
  aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$DEPLOYMENT_REGION"
}

# Helm's ECR Public credentials, which every phase that applies a helm_release
# needs. Written to ~/.docker/config.json and picked up by the Helm provider.
#
# The region here is a literal on purpose: ECR Public exists only in us-east-1,
# which is also why the example root pins its aws.us_east_1 provider alias rather
# than deriving it.
require_helm_ecr_login() {
  echo "==> Authenticating Helm to ECR Public..."
  aws ecr-public get-login-password --region us-east-1 \
    | helm registry login --username AWS --password-stdin public.ecr.aws
}

# The CRDs Phase 5 waits for. Checked, not waited for: a phase that needs them
# and does not find them is in the wrong order, and polling would hide that.
require_established_crds() {
  local crd missing=()
  for crd in "${ESTABLISHED_CRDS[@]}"; do
    kubectl get crd "$crd" -o jsonpath='{.status.conditions[?(@.type=="Established")].status}' \
      2>/dev/null | grep -q True || missing+=("$crd")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    fail "$EXIT_CHECK_FAILED" \
      "ERROR: these CRDs are not established: ${missing[*]}." \
      "ArgoCD installs them and Phase 5 waits for them: run \`--from-phase 5\` first."
  fi
}

# ---------------------------------------------------------------------------
# The phases
# ---------------------------------------------------------------------------
#
# A phase may read Terraform outputs, the cluster, AWS and Cloudflare. It may NOT
# read a shell variable another phase assigned — tests/test_installer_phases.py
# fails if one does, because that is what made `--phase N` unsafe before this
# script had it.

# The public endpoint is protected by IAM; Phase 8 disables it once the
# Cloudflare tunnel is proven healthy.
phase_1_network_and_cluster() {
  apply_target "module.vpc"
  apply_target "module.eks" -var="endpoint_public_access=true"

  # The cluster now exists and its state is written, so the deployment's own
  # region and cluster name can be read back from it. Every `aws` call in every
  # phase is steered at that region, never at the caller's profile region.
  require_cluster
  require_helm_ecr_login

  # Pre-create the namespace that Terraform resources depend on before ArgoCD
  # has had a chance to sync. --dry-run=client -o yaml | kubectl apply handles
  # the case where it already exists without erroring.
  echo "==> Pre-creating namespace..."
  kubectl create namespace argo-workflows --dry-run=client -o yaml | kubectl apply -f -
}

# Several stack-relative addresses as -targets of ONE apply:
#   apply_targets "<label>" <address>... -- <extra terraform args>...
#
# Every targeted apply pays about 30s before it creates anything — the plan,
# the refresh of everything its targets depend on, the state read and write.
# A no-op target pays it too: Phase 2's Globus module took 32s to change
# nothing. Applied one address at a time, Phase 2 paid it seven times and
# Phase 3 twelve, about eleven minutes of the acceptance install (2026-10-24).
# In one apply Terraform orders whatever has an edge and runs the rest in
# parallel, so this is only for addresses whose order Terraform can see.
apply_targets() {
  local label=$1 target targets=()
  shift
  while [[ $# -gt 0 && $1 != "--" ]]; do
    target=$1
    targets+=(-target="$(stack_address "$target")")
    shift
  done
  [[ ${1:-} == "--" ]] && shift
  echo "==> Applying ${#targets[@]} targets in one apply: $label..."
  if tf_step "$label" terraform apply "${targets[@]}" "$@" -auto-approve; then
    echo "SUCCESS: $label"
  else
    echo "FAILED: $label"
    exit "$EXIT_ERROR"
  fi
}

# EKS add-ons and Pod Identity associations, Karpenter, Argo Workflows' database
# and RBAC, the Globus ingress when enabled, and FinOps. NOT ArgoCD: its Helm
# release is a stack-level resource that no Phase 2 target depends on, so it is
# created by Phase 4's full apply (the phase title said otherwise until
# 2026-10-24).
#
# One apply rather than one per module. Their order is all in the graph: each
# reads only module.eks and module.vpc, both from Phase 1, except FinOps, which
# has `depends_on = [module.addons]`, and Globus, which reads
# module.argo_workflows. Nothing waits on a pod: the two Helm releases,
# Karpenter and Kubecost, set `wait = false`, and everything that needs a CRD is
# gated off by crds_available until Phase 6.
phase_2_cluster_addons() {
  require_cluster
  require_helm_ecr_login
  apply_targets "cluster add-ons" "${PHASE2_MODULES[@]}" -- -var="endpoint_public_access=true"
}

# NETWORK_POLICY_ENFORCING_MODE=strict blocks all traffic to pods with no policy,
# so these must exist before Phase 6 turns strict mode on — see targets.sh.
#
# One apply: nothing depends on their order, since enforcement is off until
# Phase 6, and on a re-run with it on, one apply shortens the gap between
# default_deny and the allow rules from minutes to seconds.
phase_3_kube_system_network_policies() {
  require_cluster
  apply_targets "kube-system NetworkPolicies" "${KUBE_SYSTEM_NETWORK_POLICIES[@]}" \
    -- -var="endpoint_public_access=true"
}

# Full apply with crds_available at its default of false, to catch anything the
# targeted phases did not cover. Public endpoint still open — the Cloudflare
# tunnel, its Access applications, the VPN and, with no external identity
# provider, the Cognito user pool and its clients are created here. Without a
# domain there is no ALB, certificate or external-dns to create: those resources
# and the targets above that name them plan nothing (design D2 of
# openspec/changes/optional-domain-and-cognito-auth).
#
# The token sync belongs to this phase, not the next: External Secrets must find
# the current value on its first sync.
# The namespaces ArgoCD creates that the full apply writes into. ArgoCD syncs on
# its own clock, and on a fresh install the full apply reached them first:
# `namespaces "prefect" not found`, `namespaces "grafana" not found`
# (second-account rerun, 2026-10-13). The reference cluster has had them since its
# first sync, which is why this wait did not exist. Ten minutes each, because a
# first sync pulls every chart; a namespace still missing after that is a sync
# failure, and the message says where to look.
#
# ArgoCD has to be INSTALLED before this can pass, and nothing before Phase 4
# installs it: its Helm release is a stack-level resource no Phase 2 target
# depends on. The wait used to run before any apply in this phase, so on a fresh
# install it waited ten minutes for a sync from an ArgoCD that did not exist
# (acceptance install, 2026-10-24). Phase 4 now applies ARGOCD_BOOTSTRAP first.
wait_for_argocd_namespaces() {
  local namespaces ns
  namespaces=$(stack_output argocd_created_namespaces)
  if [[ -z $namespaces ]]; then
    fail "$EXIT_INVALID" "ERROR: could not read the namespaces ArgoCD creates." \
      "Neither \`terraform output -raw argocd_created_namespaces\` nor the stack module's own" \
      "output gave a value."
  fi
  echo "==> Waiting for ArgoCD to create the namespaces this apply writes into..."
  for ns in $namespaces; do
    poll_until "namespace $ns" 600 kubectl get namespace "$ns" ||
      fail "$EXIT_CHECK_FAILED" "ERROR: ArgoCD has not created namespace $ns after 10 minutes." \
        "Check the ArgoCD applications: \`kubectl -n argocd get applications\`. A first sync" \
        "that cannot clone gitops_repo_url, or whose app is Degraded on a missing image, never" \
        "creates its namespace. Then continue with \`--from-phase 4\`."
  done
}

phase_4_full_apply_and_tunnel_token() {
  require_cluster
  require_helm_ecr_login
  # ArgoCD and its root app, so that the first sync — which creates the
  # namespaces the full apply writes into — can start. Their dependencies come
  # with them (the Cognito clients the Dex connector names, among others); none
  # is in an ArgoCD-created namespace.
  apply_targets "ArgoCD" "${ARGOCD_BOOTSTRAP[@]}" -- -var="endpoint_public_access=true"
  wait_for_argocd_namespaces
  echo "==> Applying remaining resources (crds_available=false)..."
  tf_step "full apply (crds_available=false)" \
    terraform apply -var="endpoint_public_access=true" -auto-approve

  require_cloudflare_ids
  sync_tunnel_token
}

phase_5_wait_for_crds() {
  require_cluster
  echo "==> Waiting for ArgoCD-managed CRDs to become established..."
  local crd
  for crd in "${ESTABLISHED_CRDS[@]}"; do
    poll_until "CRD $crd" 300 kubectl get crd "$crd"
    kubectl wait --for=condition=established "crd/$crd" --timeout=300s
  done
}

# Three flags that EVERY LATER APPLY MUST KEEP are persisted here rather than
# living only on this script's command lines, and then the CRD-dependent
# resources are applied with the public endpoint STILL open. This is the apply
# that creates the external-secrets-clusterstore ClusterSecretStore, and
# cloudflared cannot receive its token without it. It used to be the same apply
# that closed the public endpoint, which would lock the cluster before the tunnel
# could possibly be up.
phase_6_crd_dependent_resources() {
  require_cluster
  require_established_crds
  require_helm_ecr_login
  write_install_state

  # The -var flags are kept alongside the file: a -var wins over an auto.tfvars,
  # the two agree, and this apply then works even if the write failed.
  echo "==> Applying CRD-dependent resources (public endpoint still open)..."
  tf_step "CRD-dependent resources" terraform apply \
    -var="crds_available=true" \
    -var="vpc_cni_network_policy_enabled=true" \
    -var="endpoint_public_access=true" \
    -auto-approve
}

# An *.auto.tfvars is loaded by every `terraform` invocation in this directory,
# so a plain `terraform apply` tomorrow reads what this run decided (issue #635).
#
# Without the file, the defaults (all false) meant a plain apply silently
# (a) destroyed the ClusterSecretStore and every ExternalSecret behind
# `count = var.crds_available ? 1 : 0` — the Argo and Prefect RDS credentials,
# the pgbouncer userlist, the cloudflared token — and (b) set
# enableNetworkPolicy=false, making every NetworkPolicy in the cluster inert.
#
# Written in Phase 6, AFTER the Phase 4 apply that deliberately runs with
# crds_available at its default of false; writing it earlier breaks that step.
#
# Written through a temporary file and a rename so that re-running the phase is a
# no-op, and so an interrupted write cannot leave a half-written file that a
# later `terraform apply` would read as this deployment's recorded state.
#
# *.tfvars is gitignored (see .gitignore) — this is per-deployment state, not
# configuration to commit.
INSTALL_STATE_FILE="install-state.auto.tfvars"
write_install_state() {
  echo "==> Recording post-install state in $INSTALL_STATE_FILE..."
  local tmp
  tmp=$(mktemp "./${INSTALL_STATE_FILE}.XXXXXX")
  cat > "$tmp" <<'EOF'
# Written by install.sh Phase 6. Keep it: every later `terraform apply` needs
# these, and losing them destroys the ExternalSecrets that deliver this
# deployment's credentials and makes every NetworkPolicy in the cluster inert —
# neither of which announces itself. See docs/operations.md.
crds_available                 = true
vpc_cni_network_policy_enabled = true

# vpc_cni_strict_mode is deliberately NOT set here; it defaults to false. Strict
# mode denies any pod that no NetworkPolicy selects, and this repo ships policies
# for four namespaces while a running cluster has around twenty — cert-manager,
# external-secrets, the load balancer controller, prometheus, grafana, kubecost
# and the rest. Enabling it at install time would bring the cluster up with the
# control plane healthy and most add-ons mute. It is an opt-in hardening step for
# a deployment that has written policies for its own namespaces first; see
# docs/operations.md and issue #746.
EOF
  if cmp -s "$tmp" "$INSTALL_STATE_FILE" 2>/dev/null; then
    rm -f "$tmp"
    echo "    Already current."
    return 0
  fi
  mv "$tmp" "$INSTALL_STATE_FILE"
  echo "    Written."
}

# Do not close the public endpoint until the tunnel is proven. Run on its own,
# this is also how an operator re-proves a tunnel they have just repaired.
phase_7_prove_the_tunnel() {
  require_cluster
  require_cloudflare_ids
  require_tunnel_token_sha
  echo "==> Verifying the Cloudflare tunnel before locking down..."
  if ! wait_for_tunnel; then
    fail "$EXIT_CHECK_FAILED" \
      "FAILED: the Cloudflare tunnel is not healthy. The public EKS endpoint has been" \
      "LEFT OPEN (IAM-gated) so the cluster stays reachable. Fix the tunnel, then re-run" \
      "this phase."
  fi
  # The accounts are needed before Phase 8, not at it: enrolling WARP is a
  # sign-in, and Phase 8 checks that this machine got through. Offered here so
  # the run goes Phase 7, WARP, Phase 8 without stopping in between.
  offer_operator_accounts
  echo "==> Next, while the public endpoint is still open: connect WARP to this deployment's"
  echo "    team and sign in once (docs/deployer-first-hour.md, Steps 8 and 9). Phase 8"
  echo "    checks that this machine reaches the private endpoint before it closes the public one."
}

# Lock down. All three gates are re-checked here, every time this phase runs,
# whether or not Phase 7 ran in the same invocation: after this apply the cluster
# is reached only over WARP, so a tunnel that is not healthy, a user pool with
# nobody in it, or a machine that cannot reach the private endpoint means nobody
# can get back in.
phase_8_close_the_public_endpoint() {
  require_cluster
  require_cloudflare_ids
  require_tunnel_token_sha

  echo "==> Re-proving the Cloudflare tunnel before closing the public endpoint..."
  if ! wait_for_tunnel; then
    fail "$EXIT_CHECK_FAILED" \
      "FAILED: the Cloudflare tunnel is not healthy, so the public EKS endpoint has been" \
      "LEFT OPEN (IAM-gated) and nothing was applied. Fix the tunnel, then re-run this" \
      "phase."
  fi

  echo "==> Checking that someone can sign in before locking down..."
  offer_operator_accounts
  if ! cognito_has_users; then
    # 2, not 3: the check performed correctly and the answer is that a human has
    # to create a user. Only the exit code tells a caller those apart.
    fail "$EXIT_BLOCKED" \
      "FAILED: the public EKS endpoint has been LEFT OPEN (IAM-gated) and nothing was" \
      "applied. Create a user as printed above, then re-run this phase."
  fi

  echo "==> Checking that this machine reaches the private endpoint over WARP..."
  if ! private_endpoint_reachable; then
    # 2 again: nothing is broken that a script can see; a human has to enroll
    # WARP and sign in.
    fail "$EXIT_BLOCKED" \
      "FAILED: the public EKS endpoint has been LEFT OPEN (IAM-gated) and nothing was" \
      "applied. Connect WARP as printed above, then re-run this phase."
  fi

  echo "==> Final apply: disabling the public EKS endpoint..."
  tf_step "close the public endpoint" terraform apply \
    -var="crds_available=true" \
    -var="vpc_cni_network_policy_enabled=true" \
    -auto-approve

  # Refresh kubeconfig so kubectl uses the private endpoint from here on.
  aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$DEPLOYMENT_REGION"
}

# ---------------------------------------------------------------------------
# Arguments and dispatch
# ---------------------------------------------------------------------------

USAGE="Usage: install.sh [--root DIR] [--module NAME] [--region NAME] [--name NAME]
                  [--phase N | --from-phase N] [--verbose]
       install.sh --list-phases [--json]

$COMMON_USAGE
  --phase N      Run only phase N.
  --from-phase N Run phase N through the last phase.
  --list-phases  Print the phases and exit, without reading state or credentials.
  --json         With --list-phases, print them as JSON."

PHASE_ARG=""
FROM_PHASE_ARG=""
LIST_PHASES=false
LIST_JSON=false

# The --root/--module/--region/--name arguments as given, so the resume line a
# failed phase prints names the same deployment this run acted on.
RESUME_ARGS=()

parse_install_args() {
  parse_common_args "$@"

  local i=0 args=("${COMMON_ARGS_REST[@]+"${COMMON_ARGS_REST[@]}"}")
  while [[ $i -lt ${#args[@]} ]]; do
    case "${args[i]}" in
      --phase | --from-phase)
        [[ $((i + 1)) -lt ${#args[@]} ]] ||
          fail "$EXIT_INVALID" "ERROR: ${args[i]} needs a phase number." "$USAGE"
        case "${args[i]}" in
          --phase) PHASE_ARG=${args[i + 1]} ;;
          --from-phase) FROM_PHASE_ARG=${args[i + 1]} ;;
        esac
        i=$((i + 2))
        ;;
      --phase=* | --from-phase=*)
        case "${args[i]%%=*}" in
          --phase) PHASE_ARG=${args[i]#*=} ;;
          --from-phase) FROM_PHASE_ARG=${args[i]#*=} ;;
        esac
        i=$((i + 1))
        ;;
      --list-phases)
        LIST_PHASES=true
        i=$((i + 1))
        ;;
      --json)
        LIST_JSON=true
        i=$((i + 1))
        ;;
      -h | --help)
        printf '%s\n' "$USAGE"
        exit "$EXIT_OK"
        ;;
      *)
        fail "$EXIT_INVALID" "ERROR: unrecognised argument ${args[i]}." "$USAGE"
        ;;
    esac
  done

  if [[ -n $PHASE_ARG && -n $FROM_PHASE_ARG ]]; then
    fail "$EXIT_INVALID" "ERROR: --phase and --from-phase are alternatives; pass one." \
      "--phase runs that phase alone, --from-phase runs it and everything after it."
  fi
  if [[ $LIST_JSON == true && $LIST_PHASES != true ]]; then
    fail "$EXIT_INVALID" "ERROR: --json only applies to --list-phases." "$USAGE"
  fi

  local known
  known=$(phase_ids | tr '\n' ' ')
  local flag value
  for flag in --phase --from-phase; do
    value=$([[ $flag == --phase ]] && printf '%s' "$PHASE_ARG" || printf '%s' "$FROM_PHASE_ARG")
    [[ -z $value ]] && continue
    phase_field "$value" function >/dev/null ||
      fail "$EXIT_INVALID" "ERROR: $flag $value is not a phase of this installer." \
        "Phases are: ${known% }. Run --list-phases to see what each one does."
  done

  # Carried into the resume line a failed phase prints.
  RESUME_ARGS=()
  [[ -n $_ROOT_ARG ]] && RESUME_ARGS+=(--root "$_ROOT_ARG")
  [[ -n $_MODULE_ARG ]] && RESUME_ARGS+=(--module "$_MODULE_ARG")
  [[ -n $_REGION_ARG ]] && RESUME_ARGS+=(--region "$_REGION_ARG")
  [[ -n $_NAME_ARG ]] && RESUME_ARGS+=(--name "$_NAME_ARG")
  [[ $_VERBOSE_ARG == true ]] && RESUME_ARGS+=(--verbose)
  return 0
}

# Which phases this invocation runs: one, a tail, or all of them.
selected_phases() {
  local id
  if [[ -n $PHASE_ARG ]]; then
    printf '%s\n' "$PHASE_ARG"
    return 0
  fi
  for id in $(phase_ids); do
    [[ -n $FROM_PHASE_ARG && $id -lt $FROM_PHASE_ARG ]] && continue
    printf '%s\n' "$id"
  done
}

# Printed by the EXIT trap, so it covers a phase that failed anywhere — inside an
# apply, inside a gate, or inside a precondition — rather than only where the
# script remembered to say so. An operator then never has to choose between
# --phase and --from-phase under pressure.
CURRENT_PHASE=""
resume_hint() {
  local code=$?
  [[ $code -eq 0 || -z $CURRENT_PHASE ]] && return
  printf '\n' >&2
  printf 'Phase %s failed (exit %s). Once the cause is fixed, continue the install with:\n' \
    "$CURRENT_PHASE" "$code" >&2
  printf '  bash %s %s--from-phase %s\n' "$0" \
    "$([[ ${#RESUME_ARGS[@]} -gt 0 ]] && printf '%s ' "${RESUME_ARGS[*]}")" "$CURRENT_PHASE" >&2
}

main() {
  parse_install_args "$@"

  # Before anything that needs a root, state, credentials or a network: the
  # wizard and the docs test both list phases on machines that have none.
  if [[ $LIST_PHASES == true ]]; then
    list_phases
    exit "$EXIT_OK"
  fi

  local selected=()
  mapfile -t selected < <(selected_phases)

  # Resolved first, and it changes the working directory: every `terraform` call
  # acts on this root, and nothing is read relative to this script.
  resolve_root
  echo "==> Installing from $DEPLOYMENT_ROOT"
  init_step_logs

  # Checked before the first apply of a run that will reach Phase 4, which is
  # where it is first needed — failing there leaves a half-built cluster. A run
  # of the earlier phases alone does not need it, and Phases 7 and 8 need it for
  # their own API calls.
  #
  # No identity-provider secret is checked here. With Cognito (the default)
  # Terraform creates every client and secret itself; a deployment that supplies
  # `external_identity` reads its own secrets in its own configuration, which is
  # where a missing one is reported.
  local id
  for id in "${selected[@]}"; do
    if [[ $id -ge 4 ]]; then
      echo "==> Checking Cloudflare prerequisites..."
      require_cloudflare_api_token
      break
    fi
  done

  # Initialize Terraform. Ahead of the target validation, not after it: `init`
  # writes the module manifest that says which module call is the stack and where
  # Terraform resolved it to. It creates nothing in any cloud account.
  #
  # Never `-upgrade`. That re-resolves every provider to the newest version the
  # constraints allow and rewrites the root's lock file, so an install got
  # whatever was published that morning rather than the build the lock file and
  # CI tested. It got hashicorp/aws 6.69.0 on its release day, whose
  # `aws_cognito_managed_login_branding` read fails with "bufio.Scanner: token too
  # long" (hashicorp/terraform-provider-aws#50481; second-account rerun,
  # 2026-10-14). A lock that cannot satisfy the configuration fails here, naming
  # the provider; the fix is the example root's lock file, not -upgrade.
  #
  # Through tf_step like every apply, so its forty lines go to the run's log and
  # the terminal gets one, and a failure still prints Terraform's Error blocks:
  # streamed, it opened every invocation with a screen of provider versions
  # (acceptance install, 2026-10-25).
  tf_step "providers and modules" terraform init -input=false

  resolve_stack_module
  echo "==> Targeting module \"$STACK_MODULE_NAME\" ($STACK_DIR)"

  echo "==> Validating -target addresses against the configuration..."
  assert_targets_declared "${ALL_TARGETS[@]}"

  # [3/8] is the phase's place in the whole install, not in this invocation, so
  # `--phase 6` alone still reads as six of eight.
  local last_phase
  last_phase=$(phase_ids | tail -n 1)
  trap resume_hint EXIT
  for id in "${selected[@]}"; do
    CURRENT_PHASE=$id
    echo "==> [$id/$last_phase] Phase $id: $(phase_field "$id" title)"
    "$(phase_field "$id" function)"
  done
  CURRENT_PHASE=""

  if [[ -n $PHASE_ARG ]]; then
    echo "==> Phase $PHASE_ARG complete."
  else
    echo "==> Install complete."
  fi
}

# Run when executed, not when sourced: the tests source this file to exercise one
# function at a time against stubs.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
