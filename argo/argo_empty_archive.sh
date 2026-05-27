#! /usr/bin/env bash
# This loops through all archived workflow UIDs and deletes them one by one
for uid in $(argo archive list -s argo.<YOUR_DOMAIN> --argo-http1 -n argo-workflows -o json | jq -r '.[].metadata.uid'); do
  argo archive delete $uid -s argo.<YOUR_DOMAIN> --argo-http1 -n argo-workflows
done

