#!/bin/bash

# Get all namespaced resource types that support listing
RESOURCE_TYPES=$(kubectl api-resources --namespaced --verbs=list -o name | tr '\n' ',')

# Get all resources of those types in all namespaces and filter by name
kubectl get "${RESOURCE_TYPES%,}" -A -o wide --show-kind | grep "$1"
