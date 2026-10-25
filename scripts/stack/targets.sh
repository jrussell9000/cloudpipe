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
# Whether --verbose was on the command line, rather than in the environment, so
# install.sh's resume line can repeat it.
_VERBOSE_ARG=false

# Arguments parse_common_args did not recognise, for the caller to handle.
COMMON_ARGS_REST=()

COMMON_USAGE="  --root DIR     The Terraform root to act on (default: the current directory).
  --module NAME  Which of the root's module calls is the stack. Derived when only
                 one of them resolves to it.
  --region NAME  The deployment's AWS region, when \`terraform output -raw region\`
                 cannot supply it yet.
  --name NAME    The EKS cluster's name, when \`terraform output -raw cluster_name\`
                 cannot supply it yet.
  --verbose      Stream Terraform's full output instead of one progress line per
                 apply or destroy (also CLOUDPIPE_INSTALL_VERBOSE=1). Either way
                 the full output is logged under <root>/.install-logs/."

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
      --verbose)
        CLOUDPIPE_INSTALL_VERBOSE=1
        _VERBOSE_ARG=true
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

# Phase 2 modules: -targets of one apply in install.sh and of one destroy in
# cleanup.sh. Terraform orders them by the graph, not by this list.
# shellcheck disable=SC2034
# `module.globus` stays in this list even though `globus_enabled` defaults to
# false. A `-target` that resolves to a module call with zero instances is a
# no-op: Terraform reports "No changes" and exits 0 (verified 2026-10-09 against
# a null-provider probe), and the declaration check below passes because the
# `module` block is declared either way. So one list covers both deployment
# shapes, and nobody has to keep a second one in step.
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

# One of the stack's outputs, by name, printed raw.
#
# `terraform output` reads the ROOT's outputs from state, and a `-target` apply
# never records an output that depends on nothing it targeted. On a fresh
# deployment every apply before Phase 4's is targeted, so `region` and
# `cluster_name` are simply absent, and Phase 1 stopped right after the EKS
# apply (second-account rerun, 2026-10-13). The reference deployment never hit
# it: its state has carried every output since its first full apply.
#
# The fallback evaluates the stack module's own output with `terraform console`,
# which reads configuration and state and changes neither. Its state lock is
# held only for the one expression piped in. It needs STACK_MODULE_NAME, so it
# applies once resolve_stack_module has run; before that, only the state is read.
stack_output() {
  local name=$1 value
  value=$(terraform output -raw "$name" 2>/dev/null || true)
  if [[ (-z $value || $value == "null") && -n ${STACK_MODULE_NAME:-} ]]; then
    value=$(printf 'module.%s.%s\n' "$STACK_MODULE_NAME" "$name" | terraform console 2>/dev/null || true)
    value=${value#\"}
    value=${value%\"}
  fi
  printf '%s' "$value"
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
    DEPLOYMENT_REGION=$(stack_output region)
  fi
  if [[ -z $DEPLOYMENT_REGION || $DEPLOYMENT_REGION == "null" ]]; then
    fail "$EXIT_INVALID" "ERROR: could not read the deployment's region." \
      "Neither \`terraform output -raw region\` nor the stack module's own \`region\` output" \
      "gave a value — the state may not exist yet. Pass --region <aws-region>." \
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
    CLUSTER_NAME=$(stack_output cluster_name)
  fi
  if [[ -z $CLUSTER_NAME || $CLUSTER_NAME == "null" ]]; then
    fail "$EXIT_INVALID" "ERROR: could not read the deployment's cluster name." \
      "Neither \`terraform output -raw cluster_name\` nor the stack module's own" \
      "\`cluster_name\` output gave a value — the state may not exist yet. Pass --name <cluster>."
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


# ---------------------------------------------------------------------------
# How Terraform's output is shown
# ---------------------------------------------------------------------------
#
# Every `terraform apply` and `terraform destroy` in both scripts runs through
# `tf_step`. A first install is about twenty of them, and streamed in full they
# are thousands of lines per phase: the scripts' own `==>` lines, which are the
# outline someone actually follows, scroll away, and the one `Error:` that
# matters is somewhere above. So by default each step is ONE line — a progress
# bar on a terminal, a start and a finish line otherwise — and the complete
# output goes to a log file per step under <root>/.install-logs/<run>/. A failed
# step prints Terraform's own error blocks and the log's path.
#
# `--verbose`, or CLOUDPIPE_INSTALL_VERBOSE=1, streams Terraform's output as
# before, and still writes the log. The stream used to go through
# `| tee /dev/tty`, which printed every line twice on a terminal (once to the
# tty, once to stdout) and, with no controlling terminal at all, made tee exit
# non-zero — so under pipefail every apply "failed". It now goes to stdout once.
#
# tests/test_installer_tf_step.py drives this against a fake `terraform`, and
# holds every apply and destroy in scripts/stack/ to going through it.

# Set by `--verbose` (parse_common_args) or exported by the caller.
CLOUDPIPE_INSTALL_VERBOSE=${CLOUDPIPE_INSTALL_VERBOSE:-}

# The per-run log directory, created by init_step_logs, and how many steps have
# written into it — which numbers the files in the order they ran.
STEP_LOG_DIR=""
STEP_COUNT=0

step_verbose() {
  [[ -n $CLOUDPIPE_INSTALL_VERBOSE && $CLOUDPIPE_INSTALL_VERBOSE != 0 ]]
}

# Create this run's log directory and say where it is, once.
#
# Under the deployment root because that is where an operator already is, and
# where the state the logs describe belongs. The logs hold plan output, which
# carries attribute values from state, so the directory is private to the user
# and carries its own `.gitignore` of `*`: a deployer's root lives in their own
# repository, whose ignore rules this repository's .gitignore cannot reach.
init_step_logs() {
  [[ -n $STEP_LOG_DIR ]] && return 0
  local base="${DEPLOYMENT_ROOT:-$PWD}/.install-logs" run dir i=1
  run=$(date -u +%Y%m%dT%H%M%SZ)
  (umask 077 && mkdir -p "$base") ||
    fail "$EXIT_ERROR" "ERROR: could not create the log directory $base."
  [[ -f $base/.gitignore ]] || printf '# Written by scripts/stack/targets.sh. Never commit these logs.\n*\n' \
    >"$base/.gitignore"
  # Two runs in the same second get a suffix rather than one log directory.
  dir="$base/$run"
  until (umask 077 && mkdir "$dir" 2>/dev/null); do
    [[ $i -le 20 ]] || fail "$EXIT_ERROR" "ERROR: could not create a log directory under $base."
    dir="$base/$run-$i"
    i=$((i + 1))
  done
  STEP_LOG_DIR=$dir
  echo "==> Full Terraform output for this run is logged under $STEP_LOG_DIR"
}

# Run one Terraform command as a step: tf_step "<label>" terraform apply ...
#
# Returns the command's own exit status, unchanged, so a caller's `if` and the
# scripts' `set -e` see exactly what they saw before. Callers rely on that: an
# apply_target failure prints FAILED, and an unguarded one ends the run and
# prints install.sh's `--from-phase N` line.
tf_step() {
  local label=$1
  shift
  init_step_logs
  STEP_COUNT=$((STEP_COUNT + 1))

  # The subcommand (apply, destroy) goes in the file name, so a directory
  # listing reads as what ran.
  local verb="" arg
  for arg in "${@:2}"; do
    if [[ $arg != -* ]]; then
      verb=$arg
      break
    fi
  done
  local log
  printf -v log '%s/%02d-%s.log' "$STEP_LOG_DIR" "$STEP_COUNT" "$(_step_slug "${verb:+$verb }$label")"

  local start=$SECONDS rc=0
  if step_verbose; then
    # The `if` keeps a failure from tripping `set -e` here, and PIPESTATUS[0] is
    # the command's status: a tee that could not write the log must not turn a
    # good apply into a failed one, nor a failed one into a good one.
    if "$@" 2>&1 | tee "$log"; then rc=0; else rc=${PIPESTATUS[0]}; fi
    return "$rc"
  fi

  local spinner_pid="" running="" cols
  : >"$log"
  if [[ -t 1 ]]; then
    cols=$(_term_cols)
    running=$(mktemp "${TMPDIR:-/tmp}/tf_step.XXXXXX")
    _step_progress "$label" "$log" "$running" "$start" "$cols" "$BASHPID" &
    spinner_pid=$!
  else
    printf '  → %s\n' "$label"
  fi

  # Terraform runs in the foreground, so Ctrl-C reaches it and it can release
  # the state lock; only the progress line runs in the background. TF_INPUT=0
  # because a prompt would be written to the log, where nobody can see it, and
  # the step would wait for an answer forever — this makes it an error, which
  # the summary below shows, instead.
  if TF_INPUT=0 "$@" >"$log" 2>&1; then rc=0; else rc=$?; fi

  if [[ -n $spinner_pid ]]; then
    rm -f "$running"
    wait "$spinner_pid" 2>/dev/null || true
    printf '\r\033[K'
  fi

  local done_n total nochange elapsed counts
  read -r done_n total nochange < <(_step_counts "$log")
  elapsed=$(_fmt_elapsed $((SECONDS - start)))
  if [[ $total -ge 0 ]]; then
    counts="$done_n/$total changed"
  else
    counts="$done_n changed"
  fi
  if [[ $rc -eq 0 ]]; then
    if [[ $nochange -eq 1 && $done_n -eq 0 ]] || [[ $total -eq 0 && $done_n -eq 0 ]]; then
      counts="no changes"
    fi
    printf '  ✓ %s  %s  %s\n' "$label" "$counts" "$elapsed"
    _step_warnings "$log"
  else
    if [[ $done_n -gt 0 || $total -gt 0 ]]; then
      printf '  ✗ %s  failed after %s  (%s)\n' "$label" "$elapsed" "$counts"
    else
      printf '  ✗ %s  failed after %s\n' "$label" "$elapsed"
    fi
    _step_errors "$log"
    printf '    Full log: %s\n' "$log"
  fi
  return "$rc"
}

# A file-name-safe form of a label: `apply module.vpc` -> `apply-module-vpc`.
_step_slug() {
  local s=${1,,}
  s=$(printf '%s' "$s" | tr -cs 'a-z0-9' '-')
  s=${s#-}
  s=${s:0:60}
  s=${s%-}
  printf '%s' "${s:-step}"
}

# 45s, 4m12s, 1h03m.
_fmt_elapsed() {
  local s=$1
  if [[ $s -lt 60 ]]; then
    printf '%ds' "$s"
  elif [[ $s -lt 3600 ]]; then
    printf '%dm%02ds' $((s / 60)) $((s % 60))
  else
    printf '%dh%02dm' $((s / 3600)) $((s % 3600 / 60))
  fi
}

_term_cols() {
  local c=${COLUMNS:-}
  [[ $c =~ ^[0-9]+$ ]] || c=$(tput cols 2>/dev/null || true)
  [[ $c =~ ^[0-9]+$ && $c -ge 40 ]] || c=80
  printf '%s' "$c"
}

# Three numbers from a step's log, as far as it has been written:
#   - resources finished: Terraform's `<addr>: Creation|Modifications|
#     Destruction complete after ...` lines. A replacement is two of them, and
#     two in the plan's total too (one to add, one to destroy);
#   - the plan's total, X+Y+Z of `Plan: X to add, Y to change, Z to destroy.`,
#     or -1 before that line has appeared (still refreshing and planning).
#     Imports and forgets are left out: they print no "complete after" line;
#   - 1 if Terraform said `No changes.`.
# Terraform colours its output even into a file, so escapes are stripped first.
_step_counts() {
  awk '
    function n(s, what) {
      if (match(s, "[0-9]+ to " what)) return substr(s, RSTART, RLENGTH) + 0
      return 0
    }
    BEGIN { esc = sprintf("%c", 27); ansi = esc "\\[[0-9;]*[A-Za-z]"; total = -1 }
    { gsub(ansi, "") }
    /: (Creation|Modifications|Destruction) complete after / { done++ }
    total < 0 && /^Plan: [0-9]+ to / { total = n($0, "add") + n($0, "change") + n($0, "destroy") }
    /^No changes\./ { nochange = 1 }
    END { printf "%d %d %d\n", done, total, nochange }
  ' "$1" 2>/dev/null || echo "0 -1 0"
}

# The object a step is waiting on, as far as the log has been written: the
# OLDEST resource whose `Creating...`, `Modifying...` or `Destroying...` line
# has no `complete after` line yet, because with parallel operations the one
# that started first and has not finished is the one a long wait is stuck on.
# One tab-separated line, or nothing when no operation is in flight:
#   <address>  <creating|modifying|destroying>  <seconds>  <others in flight>
# <seconds> is Terraform's own figure from the object's last `Still ...
# [.., 01m20s elapsed]` line, 0 before the first one (they come every 10s);
# _step_progress keeps it ticking between them.
_step_current() {
  awk '
    function secs(s,    t) {
      t = 0
      if (match(s, /[0-9]+h/)) t += substr(s, RSTART, RLENGTH - 1) * 3600
      if (match(s, /[0-9]+m/)) t += substr(s, RSTART, RLENGTH - 1) * 60
      if (match(s, /[0-9]+s/)) t += substr(s, RSTART, RLENGTH - 1)
      return t
    }
    BEGIN { esc = sprintf("%c", 27); ansi = esc "\\[[0-9;]*[A-Za-z]" }
    { gsub(ansi, "") }
    match($0, /: (Creating|Modifying|Destroying)\.\.\./) {
      addr = substr($0, 1, RSTART - 1)
      verb = tolower(substr($0, RSTART + 2, RLENGTH - 5))
      if (!(addr in seq)) { seq[addr] = ++n }
      what[addr] = verb; flight[addr] = 1; el[addr] = 0
      next
    }
    match($0, /: Still (creating|modifying|destroying)\.\.\. \[/) {
      addr = substr($0, 1, RSTART - 1)
      if ((addr in flight) && match($0, /[0-9hms]+ elapsed\]/)) el[addr] = secs(substr($0, RSTART, RLENGTH - 9))
      next
    }
    match($0, /: (Creation|Modifications|Destruction) complete after /) {
      delete flight[substr($0, 1, RSTART - 1)]
    }
    END {
      best = ""; count = 0
      for (a in flight) { count++; if (best == "" || seq[a] < seq[best]) best = a }
      if (best != "") printf "%s\t%s\t%d\t%d\n", best, what[best], el[best], count - 1
    }
  ' "$1" 2>/dev/null || true
}

# Terraform's `Error:` diagnostics from a step's log: each box from its
# `│ Error:` line through the closing `╵`, once each — the same diagnostic is
# often printed more than once — and at most three, then how many more there
# were. When there is no such block (a crash, or a failure before Terraform
# could say why), the last lines of the log instead, so a failure is never
# reported with nothing to go on.
_step_errors() {
  local log=$1 found
  found=$(awk -v max=3 '
    BEGIN { esc = sprintf("%c", 27); ansi = esc "\\[[0-9;]*[A-Za-z]" }
    function flush() {
      if (inblk && !(blk in seen)) { seen[blk] = 1; kept++; if (kept <= max) out[kept] = blk }
      blk = ""; inblk = 0; lines = 0
    }
    { gsub(ansi, "") }
    /^(│ )?Error: / { flush(); inblk = 1; blk = $0 "\n"; lines = 1; next }
    inblk && /^╵/ { blk = blk $0 "\n"; flush(); next }
    inblk && /^(│ )?Warning: / { flush(); next }
    inblk && lines < 40 { blk = blk $0 "\n"; lines++ }
    END {
      flush()
      for (i = 1; i <= kept && i <= max; i++) printf "%s", out[i]
      if (kept > max) printf "… %d more errors\n", kept - max
    }
  ' "$log" 2>/dev/null || true)
  if [[ -n $found ]]; then
    printf '%s\n' "$found" | sed 's/^/    /'
  else
    echo "    Terraform printed no Error: block. The last lines of its output:"
    tail -n 15 "$log" | sed 's/^/    /'
  fi
}

# One line per distinct Terraform `Warning:` title, e.g. "Check block assertion
# failed". A warning does not fail a step, which is exactly why it is easy to
# miss when the rest of the output is in a file.
#
# Except the two Terraform prints on EVERY `-target` apply, "Resource targeting
# is in effect" and "Applied changes may be incomplete": the phases target on
# purpose, so those say nothing about this step, and printed after each of
# twenty-odd steps they would bury the warning that does. Matched on the whole
# title, so a different warning is never hidden. They stay in the log, and
# --verbose shows everything.
_step_warnings() {
  awk '
    BEGIN {
      esc = sprintf("%c", 27); ansi = esc "\\[[0-9;]*[A-Za-z]"
      seen["Resource targeting is in effect"] = 1
      seen["Applied changes may be incomplete"] = 1
    }
    { gsub(ansi, "") }
    /^(│ )?Warning: / {
      t = $0; sub(/^(│ )?Warning: */, "", t); sub(/[ \t]+$/, "", t)
      if (!(t in seen)) { seen[t] = 1; printf "    ! Warning: %s\n", t }
    }
  ' "$1" 2>/dev/null || true
}

# The live line, redrawn in place until the step's marker file is removed (or
# the script that started it is gone). Before Terraform prints its plan there is
# nothing to measure against, so it says "planning…"; after, a bar of resources
# finished against the plan's total. The bar is clamped at full because a
# replacement can finish more "complete" lines than a careless total expects;
# the numbers beside it stay raw.
#
# Always run in the background, so `set -e` is dropped: a failed read here must
# cost a frame, not the progress line.
#
# After the bar, the object the step is waiting on (_step_current), so a wait
# that runs long names its resource while it is happening rather than in the
# log afterwards. Its time is Terraform's own, refreshed every 10s, and counted
# on here in between; it restarts when a different object becomes the oldest.
_step_progress() {
  set +e
  local label=$1 log=$2 running=$3 start=$4 cols=$5 parent=$6
  local frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏) i=0 done_n total nochange el line
  local addr verb tf_secs others object key="" base=0
  while [[ -e $running ]] && kill -0 "$parent" 2>/dev/null; do
    read -r done_n total nochange < <(_step_counts "$log")
    el=$(_fmt_elapsed $((SECONDS - start)))
    addr="" verb="" tf_secs=0 others=0 object=()
    IFS=$'\t' read -r addr verb tf_secs others < <(_step_current "$log")
    if [[ -n $addr ]]; then
      if [[ "$addr $verb" != "$key" ]] || ((SECONDS - base < tf_secs)); then
        key="$addr $verb"
        base=$((SECONDS - tf_secs))
      fi
      object=("$(_step_short_addr "$addr" "$label")" "$verb" "$(_fmt_elapsed $((SECONDS - base)))" "$others")
    else
      key=""
    fi
    line=$(_step_line "${frames[i % ${#frames[@]}]}" "$label" "$done_n" "$total" "$el" "$cols" "${object[@]}")
    printf '\r\033[K%s' "$line"
    i=$((i + 1))
    sleep 0.2
  done
}

# An in-flight object's address as the live line shows it: without the stack
# module's prefix, and without the step's own label when the address is inside
# it — `module.stack.module.eks.aws_eks_cluster.this[0]` under `module.eks` is
# `aws_eks_cluster.this[0]`.
_step_short_addr() {
  local addr=$1 label=$2
  [[ -n ${STACK_MODULE_NAME:-} ]] && addr=${addr#"module.$STACK_MODULE_NAME."}
  [[ -n $label && $addr == "$label."* ]] && addr=${addr#"$label."}
  printf '%s' "$addr"
}

# An address without its module path, for when the whole address does not fit:
# `module.eks_managed_node_group["argo"].aws_eks_node_group.this[0]` is
# `aws_eks_node_group.this[0] ["argo"]`. The innermost module key there is is
# kept, because it is what tells one instance of a module from the next; a
# keyless module inside a keyed one does not erase it.
_step_bare_addr() {
  local addr=$1 key="" re='^module\.[A-Za-z0-9_-]+(\[[^]]*\])?\.'
  while [[ $addr =~ $re ]]; do
    [[ -n ${BASH_REMATCH[1]} ]] && key=${BASH_REMATCH[1]}
    addr=${addr:${#BASH_REMATCH[0]}}
  done
  printf '%s%s' "$addr" "${key:+ $key}"
}

# One frame of the live line, fitted to `cols` columns: the label is shortened
# from the left with an ellipsis (the end of an address is the part that tells
# one target from the next), and the bar narrows only if that is not enough.
#
# Arguments seven to ten are the in-flight object, from _step_progress: its
# address, verb, time so far, and how many others are in flight. Its name is
# what a long wait needs read, so the rest of the line gives way to it, in this
# order, taking the first that fits: a 12-column bar and the whole address; no
# bar (the count beside it still says how far along the step is); the address
# without its module path; the step's label shortened to 16 columns; and only
# then the address cut from the left. The verb and the time are never cut. With
# fewer than 10 columns left for the address, it is left out.
_step_line() {
  local frame=$1 label=$2 done_n=$3 total=$4 el=$5 cols=$6 addr=${7:-}
  local bw=20 counts tail fixed lw filled bar_full bar_empty
  if [[ -n $addr && $total -ge 0 ]]; then
    _step_line_object "$@"
    return 0
  fi
  if [[ $total -lt 0 ]]; then
    tail="planning…"
    fixed=$((4 + 2 + 9 + 2 + ${#el}))
    lw=$((cols - 1 - fixed))
    printf '  %s %s  %s  %s' "$frame" "$(_fit_label "$label" "$lw")" "$tail" "$el"
    return 0
  fi
  counts="$done_n/$total"
  fixed=$((4 + 2 + 2 + 2 + ${#counts} + 2 + ${#el}))
  lw=$((cols - 1 - fixed - bw))
  if [[ $lw -lt 16 ]]; then
    bw=$((bw - (16 - lw)))
    [[ $bw -ge 5 ]] || bw=5
    lw=$((cols - 1 - fixed - bw))
  fi
  if [[ $total -le 0 || $done_n -ge $total ]]; then
    filled=$bw
  else
    filled=$((done_n * bw / total))
  fi
  printf -v bar_full '%*s' "$filled" ''
  printf -v bar_empty '%*s' $((bw - filled)) ''
  printf '  %s %s  [%s%s]  %s  %s' "$frame" "$(_fit_label "$label" "$lw")" \
    "${bar_full// /█}" "${bar_empty// /░}" "$counts" "$el"
}

# _step_line with an in-flight object; see there for the order things give way.
_step_line_object() {
  local frame=$1 label=$2 done_n=$3 total=$4 el=$5 cols=$6 addr=$7 verb=$8 took=$9
  local others=${10:-0} counts="$done_n/$total" tail width bw=12 filled bar="" name lab room
  tail=" $verb $took"
  [[ $others -gt 0 ]] && tail+=" +$others"
  width=$((cols - 1))
  # The line's widths: "  F L  [bar]  C  E  · <name><tail>" is 12 + bar, and
  # without the bar "  F L  C  E  · <name><tail>" is 8, each plus L, C and E,
  # then 4 for the separator.
  local with_bar=$((12 + bw + ${#counts} + ${#el} + 4 + ${#tail}))
  local no_bar=$((8 + ${#counts} + ${#el} + 4 + ${#tail}))
  local bare
  bare=$(_step_bare_addr "$addr")
  lab=$label
  if ((with_bar + ${#lab} + ${#addr} <= width)); then
    name=$addr
  elif ((no_bar + ${#lab} + ${#addr} <= width)); then
    name=$addr bw=0
  elif ((no_bar + ${#lab} + ${#bare} <= width)); then
    name=$bare bw=0
  else
    bw=0
    [[ ${#lab} -gt 16 ]] && lab=$(_fit_label "$lab" 16)
    room=$((width - no_bar - ${#lab}))
    if [[ $room -lt 10 ]]; then
      # No useful room for a name: the line without one.
      _step_line "$frame" "$label" "$done_n" "$total" "$el" "$cols"
      return 0
    fi
    name=$(_fit_label "$bare" "$room")
  fi
  if [[ $bw -gt 0 ]]; then
    if [[ $total -le 0 || $done_n -ge $total ]]; then filled=$bw; else filled=$((done_n * bw / total)); fi
    printf -v bar '%*s' "$filled" ''
    local rest
    printf -v rest '%*s' $((bw - filled)) ''
    bar="  [${bar// /█}${rest// /░}]"
  fi
  printf '  %s %s%s  %s  %s  · %s%s' "$frame" "$lab" "$bar" "$counts" "$el" "$name" "$tail"
}

_fit_label() {
  local label=$1 width=$2
  [[ $width -ge 4 ]] || width=4
  if [[ ${#label} -le $width ]]; then
    printf '%s' "$label"
  else
    printf '…%s' "${label: -$((width - 1))}"
  fi
}
