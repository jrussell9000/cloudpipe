#! /usr/bin/env bash
#
# Delete every archived workflow record from the Argo Workflows archive.
#
# DESTRUCTIVE: this removes all archived workflow history in the namespace. Pod
# logs in S3 (logs/{workflow}/{pod}/main.log) and the metrics records in Athena
# are untouched — only Argo's own archive rows go.
#
# The `archive` subcommands are served by the Argo Server's API, not by the
# Kubernetes API, so unlike `argo list` they cannot run in KUBECONFIG mode and
# the server host has to be supplied. It comes from the environment rather than
# a flag so that no deployment's hostname is written into this repository:
#
#   ARGO_SERVER=argo.example.org:443 ARGO_HTTP1=true ./scripts/argo_empty_archive.sh
#
# ARGO_HTTP1=true is the env-var form of `--argo-http1`, and it is required when
# the server sits behind an ALB, which does not support HTTP/2 to the target.
# Visit /userinfo on the Argo Server web UI to check which identity the CLI will
# authenticate as.
#
# Lived at argo/argo_empty_archive.sh, alongside a one-line argo_lookup_command.txt
# whose contents are the ARGO_SERVER note above.

set -euo pipefail

: "${ARGO_SERVER:?set ARGO_SERVER to the Argo Server host, e.g. argo.example.org:443}"

NAMESPACE="${ARGO_NAMESPACE:-argo-workflows}"

# This loops through all archived workflow UIDs and deletes them one by one
for uid in $(argo archive list -n "$NAMESPACE" -o json | jq -r '.[].metadata.uid'); do
  argo archive delete "$uid" -n "$NAMESPACE"
done
