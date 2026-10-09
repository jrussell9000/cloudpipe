#!/bin/bash
# Shared machinery for install.sh and cleanup.sh: which deployment they act on,
# the `-target` address lists, and the preflight that every address is declared.
#
# Both scripts drive Terraform through a sequence of `-target` applies/destroys
# (install in order, cleanup in reverse). Keeping the lists here means a module
# deleted from the .tf files cannot go on being referenced by one script and not
# the other — which is exactly how `module.aws_efs_csi_pod_identity` outlived the
# EFS removal and broke a fresh bootstrap (GitHub #211).
#
# NOTHING HERE IS SPECIFIC TO ONE DEPLOYMENT. The root, the module call's name,
# the region and the cluster name are all resolved from the root the caller names
# — see `resolve_root`, `resolve_stack_module` and `require_deployment_region`.
# These scripts are published, and a deployer's root is their own copy of
# terraform/modules/stack/example/, outside this repository, with its own module
# name and `source`.
#
# Source this from install.sh/cleanup.sh. Terraform is invoked from the resolved
# root, which `resolve_root` makes the working directory.

# Exit codes. The taxonomy src/cloudpipe_setup/exits.py publishes, so that a
# caller — a person, or the setup wizard once it drives these phases — can tell a
# human blocker from a failed check from a typo. tests/test_installer_exits.py
# holds these equal to that module's.
#
# Read by install.sh and cleanup.sh, which shellcheck cannot see from here.
# shellcheck disable=SC2034
EXIT_OK=0
# shellcheck disable=SC2034
EXIT_ERROR=1       # unexpected: Terraform or a tool failed and has said why
EXIT_BLOCKED=2     # blocked on something only a human can do
EXIT_CHECK_FAILED=3 # a check or verification failed
EXIT_INVALID=4     # invalid input or configuration

# Resolved deployment identity. Written by the resolvers below, read by both
# scripts. Empty until the matching resolver has run.
# shellcheck disable=SC2034
DEPLOYMENT_ROOT=""
STACK_MODULE_NAME=""
STACK_DIR=""
DEPLOYMENT_REGION=""
CLUSTER_NAME=""

_ROOT_ARG=""
_MODULE_ARG=""
_REGION_ARG=""
_NAME_ARG=""

# Arguments parse_common_args did not recognise, for the caller to handle.
COMMON_ARGS_REST=()

COMMON_USAGE="  --root DIR     The Terraform root to act on (default: the current directory).
  --module NAME  Which of the root's module calls is the stack. Derived when only
                 one of them resolves to it.
  --region NAME  The deployment's AWS region, when \`terraform output -raw region\`
                 cannot supply it yet.
  --name NAME    The EKS cluster's name, when \`terraform output -raw cluster_name\`
                 cannot supply it yet."

# Fail with one of the codes above. The message goes to stderr, because stdout
# carries the Terraform output a caller may be piping.
fail() {
  local code=$1
  shift
  printf '%s\n' "$@" >&2
  exit "$code"
}

parse_common_args() {
  COMMON_ARGS_REST=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --root | --module | --region | --name)
        [[ $# -ge 2 && -n ${2:-} ]] || fail "$EXIT_INVALID" "ERROR: $1 needs a value." "$COMMON_USAGE"
        case "$1" in
          --root) _ROOT_ARG=$2 ;;
          --module) _MODULE_ARG=$2 ;;
          --region) _REGION_ARG=$2 ;;
          --name) _NAME_ARG=$2 ;;
        esac
        shift 2
        ;;
      --root=* | --module=* | --region=* | --name=*)
        local value=${1#*=}
        [[ -n $value ]] || fail "$EXIT_INVALID" "ERROR: ${1%%=*} needs a value." "$COMMON_USAGE"
        case "${1%%=*}" in
          --root) _ROOT_ARG=$value ;;
          --module) _MODULE_ARG=$value ;;
          --region) _REGION_ARG=$value ;;
          --name) _NAME_ARG=$value ;;
        esac
        shift
        ;;
      *)
        COMMON_ARGS_REST+=("$1")
        shift
        ;;
    esac
  done
}

# Three of the stack's no-default inputs, used as the fingerprint of a root the
# scripts may act on. Three rather than all of them, so a deployer who trimmed
# their root to the inputs they override is not locked out — the same three, for
# the same reason, as src/cloudpipe_setup/roots.py.
ROOT_FINGERPRINT=(domain region cloudflare_account_id)

# Resolve the Terraform root and make it the working directory.
#
# Verified, not assumed: `terraform apply -target=...` in a directory that is not
# the deployment's root is not an error, it is an apply against something else.
# And every later `terraform output` would read that other deployment's state.
resolve_root() {
  local candidate=${_ROOT_ARG:-$PWD} missing=()
  if [[ ! -d $candidate ]]; then
    fail "$EXIT_INVALID" "ERROR: $candidate is not a directory." \
      "Pass --root pointing at your copy of terraform/modules/stack/example/."
  fi
  DEPLOYMENT_ROOT=$(cd "$candidate" && pwd)

  if ! compgen -G "$DEPLOYMENT_ROOT/*.tf" >/dev/null; then
    fail "$EXIT_INVALID" "ERROR: $DEPLOYMENT_ROOT holds no Terraform configuration (*.tf)." \
      "Pass --root pointing at your copy of terraform/modules/stack/example/."
  fi

  if ! grep -qE '^[[:space:]]*module[[:space:]]+"[^"]+"' "$DEPLOYMENT_ROOT"/*.tf; then
    fail "$EXIT_INVALID" \
      "ERROR: $DEPLOYMENT_ROOT declares no module call, so nothing there deploys the stack." \
      "Pass --root pointing at your copy of terraform/modules/stack/example/."
  fi

  local name
  for name in "${ROOT_FINGERPRINT[@]}"; do
    grep -qE "^[[:space:]]*variable[[:space:]]+\"${name}\"" "$DEPLOYMENT_ROOT"/*.tf || missing+=("$name")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    fail "$EXIT_INVALID" \
      "ERROR: $DEPLOYMENT_ROOT does not look like a CloudPipe stack root: it declares no" \
      "${missing[*]} variable. Looked for variable declarations for ${ROOT_FINGERPRINT[*]}." \
      "Pass --root pointing at your copy of terraform/modules/stack/example/."
  fi

  # Every `terraform` call in both scripts acts on this directory, so a failed cd
  # must not fall through to running them wherever the caller happened to be.
  cd "$DEPLOYMENT_ROOT" || fail "$EXIT_ERROR" "ERROR: could not enter $DEPLOYMENT_ROOT."
}

# Which module call in the root is the stack, and where Terraform resolved it to.
#
# MUST run after `terraform init`, which writes the module manifest this reads.
# The manifest is what makes a `source` that is a registry address or a git URL
# work: it records the directory Terraform actually downloaded the module to.
# Reading a path relative to this script instead — which is what
# STACK_DIR="$TF_DIR/modules/stack" did — only ever worked for a root inside this
# repository, which no deployer's root is.
#
# The stack is identified by what it declares, not by its name: a deployer may
# call the module anything, and the reference root calls more than one module. A
# candidate whose directory declares every address these scripts target is the
# stack; that is the same check `assert_targets_declared` reports on, run quietly
# over each candidate.
resolve_stack_module() {
  local manifest="$DEPLOYMENT_ROOT/.terraform/modules/modules.json"
  if [[ ! -f $manifest ]]; then
    fail "$EXIT_INVALID" "ERROR: $manifest does not exist, so the stack module cannot be" \
      "resolved. Run \`terraform init\` in $DEPLOYMENT_ROOT first."
  fi

  local keys=() key dir candidates=()
  mapfile -t keys < <(jq -r '.Modules[] | select(.Key != "") | select(.Key | contains(".") | not) | .Key' "$manifest")

  for key in "${keys[@]}"; do
    [[ -n $_MODULE_ARG && $key != "$_MODULE_ARG" ]] && continue
    dir=$(_module_dir "$manifest" "$key") || continue
    if _targets_declared_in "$dir" "${ALL_TARGETS[@]}"; then
      candidates+=("$key")
      STACK_MODULE_NAME=$key
      STACK_DIR=$dir
    fi
  done

  if [[ ${#candidates[@]} -eq 0 ]]; then
    if [[ -n $_MODULE_ARG ]]; then
      fail "$EXIT_INVALID" "ERROR: module \"$_MODULE_ARG\" in $DEPLOYMENT_ROOT does not resolve" \
        "to the CloudPipe stack: its directory declares none of the addresses these scripts" \
        "target. Module calls found: ${keys[*]:-none}."
    fi
    fail "$EXIT_INVALID" "ERROR: no module call in $DEPLOYMENT_ROOT resolves to the CloudPipe" \
      "stack. Module calls found: ${keys[*]:-none}. Check the module's \`source\`, re-run" \
      "\`terraform init\`, or name the call with --module."
  fi

  if [[ ${#candidates[@]} -gt 1 ]]; then
    fail "$EXIT_INVALID" "ERROR: $DEPLOYMENT_ROOT calls the stack more than once" \
      "(${candidates[*]}). Name the one to install with --module."
  fi
}

# The directory Terraform resolved a module key to, absolute. The manifest
# records a local `source` as a path relative to the root and a downloaded one as
# a path under .terraform/, so both resolve against the root; an absolute path is
# passed through.
_module_dir() {
  local manifest=$1 key=$2 dir
  dir=$(jq -r --arg key "$key" '.Modules[] | select(.Key == $key) | .Dir' "$manifest")
  [[ -n $dir && $dir != "null" ]] || return 1
  [[ $dir == /* ]] || dir="$DEPLOYMENT_ROOT/$dir"
  [[ -d $dir ]] || return 1
  (cd "$dir" && pwd)
}

# Takes a stack-relative address (`module.vpc`) and prints the root-level one
# Terraform needs (`module.stack.module.vpc`), under whatever name this root
# gives the module call.
stack_address() {
  echo "module.${STACK_MODULE_NAME}.$1"
}

# The two target lists are consumed only by the scripts that source this file,
# which is invisible when shellcheck checks this file on its own.

# Phase 2 modules, in apply order. cleanup.sh destroys these in reverse.
# shellcheck disable=SC2034
PHASE2_MODULES=(
  module.aws_ebs_csi_pod_identity
  module.external_dns_pod_identity
  module.karpenter
  module.addons
  module.argo_workflows
  module.globus
  module.finops
)

# Phase 3 NetworkPolicies. NETWORK_POLICY_ENFORCING_MODE=strict blocks all traffic
# to pods with no policy, so CoreDNS and Karpenter must have policies in place
# before strict mode is enabled — otherwise the cluster deadlocks (no DNS → no
# policy apply → no DNS).
# shellcheck disable=SC2034
KUBE_SYSTEM_NETWORK_POLICIES=(
  kubernetes_network_policy_v1.kube_system_default_deny
  kubernetes_network_policy_v1.kube_system_intra_namespace
  kubernetes_network_policy_v1.kube_system_egress_https
  kubernetes_network_policy_v1.kube_system_egress_vpc_dns
  kubernetes_network_policy_v1.kube_system_coredns_ingress_dns
  kubernetes_network_policy_v1.kube_system_coredns_ingress_metrics
  kubernetes_network_policy_v1.kube_system_coredns_ingress_probe
  kubernetes_network_policy_v1.kube_system_metrics_server_egress_kubelet
  kubernetes_network_policy_v1.kube_system_metrics_server_ingress_probe
  kubernetes_network_policy_v1.kube_system_karpenter_ingress_probe
  kubernetes_network_policy_v1.kube_system_karpenter_ingress_metrics
  kubernetes_network_policy_v1.kube_system_karpenter_egress_pod_identity
)

# Every address either script targets. This is what identifies the stack module
# (resolve_stack_module) as well as what is validated before an apply, so the two
# cannot disagree about which configuration is being driven.
# shellcheck disable=SC2034
ALL_TARGETS=(
  module.vpc
  module.eks
  "${PHASE2_MODULES[@]}"
  "${KUBE_SYSTEM_NETWORK_POLICIES[@]}"
)

# Whether every given stack-relative address is declared in DIR. Quiet; returns
# non-zero on the first address that is not, for use as a condition.
_targets_declared_in() {
  local dir=$1 target
  shift
  compgen -G "$dir/*.tf" >/dev/null || return 1
  for target in "$@"; do
    case "$target" in
      module.*)
        grep -qE "^[[:space:]]*module[[:space:]]+\"${target#module.}\"" "$dir"/*.tf || return 1 ;;
      *.*)
        grep -qE "^[[:space:]]*resource[[:space:]]+\"${target%%.*}\"[[:space:]]+\"${target#*.}\"" \
          "$dir"/*.tf || return 1 ;;
      *)
        return 1 ;;
    esac
  done
}

# Fail fast if any of the given stack-relative -target addresses is not declared.
# `terraform apply|destroy -target=<undeclared address>` is a hard error, not a
# no-op, and the callers run under `set -e` — so a single stale address aborts
# partway through with no indication whether the remaining targets are sound.
# Validating the whole list up front reports every bad address at once.
#
# This greps the .tf sources rather than running `terraform plan -target=...`,
# because plan needs the kubernetes/helm providers to reach a cluster that does
# not exist yet at bootstrap time — the reason the applies are phased at all. The
# sources it greps are now the ones Terraform resolved for this root's module
# call (resolve_stack_module), rather than a path inside this repository.
assert_targets_declared() {
  local target bad=()
  if [[ -z $STACK_MODULE_NAME || -z $STACK_DIR ]]; then
    fail "$EXIT_INVALID" "ERROR: the stack module has not been resolved." \
      "resolve_root and resolve_stack_module must run before this check."
  fi
  for target in "$@"; do
    _targets_declared_in "$STACK_DIR" "$target" || bad+=("$target")
  done
  if [[ ${#bad[@]} -gt 0 ]]; then
    printf 'ERROR: these -target addresses are declared in no .tf file under %s:\n' "$STACK_DIR" >&2
    printf '  %s\n' "${bad[@]}" >&2
    fail "$EXIT_CHECK_FAILED" \
      "Remove them from the script's target list, or declare the missing configuration."
  fi
}

# The deployment's region, and every `aws` call steered at it.
#
# Read from the stack's own output rather than parsed out of the deployer's
# tfvars: `name` has a default the deployer may never write down, so a parser
# would have to re-encode Terraform's defaults, and `terraform console` — the
# other way to ask — holds a state lock for as long as it is open.
#
# Exported, and never defaulted to the caller's profile. The AWS provider takes
# its region from the root's own variable, so Terraform does not need this; every
# `aws` call in these scripts does, and without it they silently act in whatever
# region the caller's profile names. That is the bug `cloudpipe preflight`
# shipped with (#620): regional reads that used the profile's region instead of
# the deployment's.
#
# Both names are set because AWS_REGION is consulted before AWS_DEFAULT_REGION by
# current AWS CLI versions, so setting only the latter can lose to an AWS_REGION
# the caller already exported.
require_deployment_region() {
  [[ -n $DEPLOYMENT_REGION ]] && return 0
  if [[ -n $_REGION_ARG ]]; then
    DEPLOYMENT_REGION=$_REGION_ARG
  else
    DEPLOYMENT_REGION=$(terraform output -raw region 2>/dev/null || true)
  fi
  if [[ -z $DEPLOYMENT_REGION || $DEPLOYMENT_REGION == "null" ]]; then
    fail "$EXIT_INVALID" "ERROR: could not read the deployment's region." \
      "\`terraform output -raw region\` gave nothing — the state may not exist yet, or this" \
      "root may not re-export the stack's \`region\` output. Pass --region <aws-region>." \
      "The caller's AWS profile is deliberately not used as a fallback: it describes the" \
      "workstation, not the deployment."
  fi
  export AWS_DEFAULT_REGION="$DEPLOYMENT_REGION"
  export AWS_REGION="$DEPLOYMENT_REGION"
}

# The EKS cluster's name, which is the stack's `name` input and defaults to
# something the deployer may never have written down — hence the output rather
# than a tfvars read.
require_cluster_name() {
  [[ -n $CLUSTER_NAME ]] && return 0
  if [[ -n $_NAME_ARG ]]; then
    CLUSTER_NAME=$_NAME_ARG
  else
    CLUSTER_NAME=$(terraform output -raw cluster_name 2>/dev/null || true)
  fi
  if [[ -z $CLUSTER_NAME || $CLUSTER_NAME == "null" ]]; then
    fail "$EXIT_INVALID" "ERROR: could not read the deployment's cluster name." \
      "\`terraform output -raw cluster_name\` gave nothing — the state may not exist yet, or" \
      "this root may not re-export the stack's \`cluster_name\` output. Pass --name <cluster>."
  fi
}

# Fail fast without CLOUDFLARE_API_TOKEN. The Cloudflare provider takes its
# credentials ONLY from this variable (a Terraform variable would persist the
# token in state, which is why the example root's provider block is empty), and
# without it every Cloudflare resource fails with error 9106 — in both scripts
# that happens in the untargeted full apply/destroy, long after the targeted
# phases have already changed the cluster.
#
# How the token is kept between sessions is the deployer's business; what it has
# to be is account-scoped with Zero Trust: Edit, plus Tunnel and Access
# permissions.
require_cloudflare_api_token() {
  if [[ -z ${CLOUDFLARE_API_TOKEN:-} ]]; then
    cat >&2 <<'EOF'
ERROR: CLOUDFLARE_API_TOKEN is not set.

Export an account-scoped Cloudflare API token with Zero Trust: Edit, plus Tunnel
and Access permissions, in the shell you run this from:

  export CLOUDFLARE_API_TOKEN=...   # never echoed, never written to a file

It is read from the environment and never taken as a Terraform variable, which
would persist it in state. `pixi run cloudpipe preflight` checks that the token
is present and carries the Zero Trust scope before an install begins.
EOF
    exit "$EXIT_BLOCKED"
  fi
}

# Print the given arguments in reverse order, one per line. Used by cleanup.sh to
# tear down in the opposite order to install, without a second hand-kept list.
reversed() {
  local i
  for ((i = $#; i > 0; i--)); do
    printf '%s\n' "${!i}"
  done
}
