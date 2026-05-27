# CloudPipe GitOps (ArgoCD)

ArgoCD manages all Kubernetes resources except those created directly by Terraform during bootstrap. The single rule: **anything in the cluster that ArgoCD owns will be reverted to git within seconds if you change it manually**. Always commit and push; never `kubectl apply` to a resource ArgoCD controls.

---

## Structure

```
gitops/
  bootstrap/
    root-app.yaml          ← ApplicationSet that generates all apps
  apps/
    argo-events/           ← Helm wrapper chart
    argo-workflows/        ← Helm wrapper chart
    aws-ebs-csi-driver/    ← Helm wrapper chart
    aws-efs-csi-driver/    ← Helm wrapper chart
    aws-load-balancer-controller/
    cert-manager/
    cluster-config/        ← Raw manifests (not a Helm chart)
    external-dns/
    external-secrets/
    pipelines/
      workflow-templates.yaml  ← Separate Application for WorkflowTemplates
    prefect/               ← Umbrella chart (server + worker + oauth2-proxy)
    prometheus-operator-crds/
    reloader/
```

Each directory under `gitops/apps/` is a Helm wrapper chart: a thin `Chart.yaml` with an upstream dependency and a `values.yaml` that overrides defaults. ArgoCD renders the chart and applies the result.

---

## Bootstrap Application (cluster-addons)

`gitops/bootstrap/root-app.yaml` defines a single ArgoCD `ApplicationSet` named `cluster-addons`. It watches `gitops/apps/*` via a Git generator and creates one ArgoCD `Application` per directory, naming each after `path.basename`.

```yaml
syncPolicy:
  automated:
    prune: true      # delete K8s resources removed from git
    selfHeal: true   # revert any manual kubectl change
  syncOptions:
    - CreateNamespace=true
    - ServerSideApply=true
```

`selfHeal: true` is the critical setting. Argo checks the live cluster state continuously; drift from git is corrected automatically. There is no "manual override" window.

---

## workflow-templates Application

`gitops/apps/pipelines/workflow-templates.yaml` is a standalone ArgoCD `Application` (not part of the ApplicationSet). It watches `argo/workflows/` recursively and syncs everything it finds into the `argo-workflows` namespace.

This Application owns:
- All `WorkflowTemplate` resources under `argo/workflows/cloudpipe_minproc/`, `cloudpipe_fullproc/`, and `fmri_first_level_proc/`
- The `cloudpipe-semaphores` ConfigMap
- The `globus-credentials` ExternalSecret

Sync wave: `3` (applied after cluster infrastructure).

The same `selfHeal: true` + `prune: true` policy applies. Deleting a WorkflowTemplate YAML from git will delete it from the cluster on the next sync.

---

## Applications reference

### argo-workflows

Helm chart: `argoproj/argo-workflows` v1.0.7

Key overrides in `values.yaml`:

| Setting | Value | Why |
|---|---|---|
| `controller.parallelism` | `1000` | Allow up to 1000 concurrently running workflows across all templates |
| `controller.resourceRateLimit` | `limit: 20, burst: 35` | Throttle K8s API pod creates to avoid API server overload during mass submission |
| `controller.configMap.create` | `false` | Terraform owns the controller ConfigMap (bucket name must stay in sync with `var.globus_s3_destination_bucket`) |
| `server.extraArgs` | `--auth-mode=sso --auth-mode=client` | SSO via Dex; CLI token auth as fallback |
| `controller.deploymentAnnotations` | `secret.reloader.stakater.com/reload: "argo-db"` | Reloader restarts the controller pod when the `argo-db` K8s Secret changes (e.g. after RDS password rotation) |
| `server.deploymentAnnotations` | same | Same restart trigger on the server |
| `useStaticCredentials` | `false` | Pod Identity used for S3 artifact access; no static AWS credentials |

Both `controller` and `server` run on the `argo` managed node group (taint `argoproj.io/backend=true`). Pipeline pods (runner SA) land on Karpenter-provisioned nodes.

`artifactRepository` is intentionally absent from `values.yaml` — it is defined in the controller ConfigMap managed by Terraform so the S3 bucket name stays in sync with the Terraform variable.

### prefect

Umbrella chart combining three sub-charts pinned to the same release (`2026.4.10163454`):

**prefect-server**
- External RDS (embedded PostgreSQL disabled)
- Credentials from `prefect-db-credentials` ExternalSecret (not created by Helm)
- Telemetry disabled (`PREFECT_SERVER_ANALYTICS_ENABLED=false`) — ABCD data, metadata stays on-premises
- `prefectUiApiUrl` must match the ALB hostname so browser API calls route correctly

**prefect-worker**
- Type: `kubernetes` (Kubernetes work pool)
- Talks to server via in-cluster URL to avoid ALB round-trip
- Work pool: `cloudpipe-k8s-pool`

**oauth2-proxy** (SSO gate in front of prefect-server)
- Provider: OIDC via Dex (ArgoCD's Dex instance)
- Only `@<YOUR_INSTITUTION_DOMAIN>` email domain allowed
- `/api/` path bypasses SSO — required for Prefect CLI and programmatic access; access is VPN-gated at the ALB security group instead

### cluster-config

A plain Helm chart containing raw manifests (`templates/`). Not an upstream dependency — manifests are applied as-is.

| Template | What it creates |
|---|---|
| `argo-server-rbac.yaml` | ClusterRoles and bindings for the Argo server and `argo-admin` SA (node reader, SSO RBAC, events reader cross-namespace) |
| `cluster-secret-store.yaml` | `ClusterSecretStore` named `aws-secrets-manager` pointing to Secrets Manager in <YOUR_AWS_REGION> |
| `external-secrets-patch.yaml` | Patch for External Secrets Operator |
| `fluent-bit-config.yaml` | Fluent Bit ConfigMap — routes argo-workflows container logs to `/aws/containerinsights/cloudpipe/argo-workflows` CloudWatch log group; all other pods go to the generic application log group |
| `storage-class.yaml` | `ebs-sc` StorageClass (gp3, encrypted, default) — also created by Terraform; ArgoCD manages the live state |

### reloader

Stakater Reloader watches for annotation-driven pod restarts. When a Secret or ConfigMap that matches a pod's annotation changes, Reloader rolls the Deployment.

Used for: Argo controller and server automatically restart when the `argo-db` Secret is updated (e.g. after RDS native password rotation).

Strategy: `annotations` (only restarts pods whose Deployment has the `secret.reloader.stakater.com/reload` annotation).

### external-secrets

Installs External Secrets Operator and its CRDs. Pod Identity association is managed in Terraform (`modules/addons/external-secrets.tf`). The SA name (`external-secrets`) must match the Pod Identity association.

### cert-manager

Installs CRDs and all three components (controller, webhook, cainjector) on the `backend` node group. Used by ArgoCD and AWS Load Balancer Controller for certificate management.

### external-dns

Watches Ingress and Service objects for hostnames in `<YOUR_DOMAIN>` and creates Route53 records. `policy: upsert-only` — will not delete records. `txtOwnerId: cloudpipe` prevents collisions if a second External DNS instance is deployed.

### prometheus-operator-crds

Installs only the CRDs (ServiceMonitor, PodMonitor, PrometheusRule, etc.) without running a Prometheus stack. Required so other charts can define ServiceMonitors without error.

### aws-ebs-csi-driver / aws-efs-csi-driver / aws-load-balancer-controller

Standard AWS CSI and networking add-ons. Helm-managed by ArgoCD; IAM is managed by Pod Identity associations in Terraform.

EFS CSI note: `deleteAccessPointRootDir: true` — deletes the EFS access point path when a PVC is deleted (prevents orphaned data on EFS).

---

## Ownership split: Terraform vs ArgoCD

This boundary matters when troubleshooting. A resource that Terraform creates will not be in ArgoCD's sync list; a resource that ArgoCD manages will not appear in `terraform state`.

| Resource | Owner |
|---|---|
| Helm release: ArgoCD itself | Terraform (`argocd.tf`) |
| Helm releases: all other apps | ArgoCD |
| ArgoCD ApplicationSet + root-app | Terraform (bootstrapped), then ArgoCD self-manages |
| EKS add-ons (vpc-cni, coredns, etc.) | Terraform |
| Karpenter NodePools / NodeClass | Terraform (via module) |
| IAM roles and Pod Identity associations | Terraform |
| RDS instances + ExternalSecrets for DB creds | Terraform |
| `argo-workflows-controller-configmap` | Terraform |
| WorkflowTemplates | ArgoCD (`workflow-templates` Application) |
| `cloudpipe-semaphores` ConfigMap | ArgoCD |
| `globus-credentials` ExternalSecret | ArgoCD |
| StorageClasses (`ebs-sc`, `efs-sc`) | Both (Terraform creates; ArgoCD manages ongoing state) |

---

## Adding a new add-on

1. Create `gitops/apps/<name>/Chart.yaml` with the upstream chart as a dependency.
2. Create `gitops/apps/<name>/values.yaml` with overrides.
3. If the chart's service account needs AWS access, add a Pod Identity association in Terraform and apply it.
4. Commit and push. ArgoCD creates the Application automatically within ~30 seconds (Git generator polls on push).

---

## Common ArgoCD operations

```bash
# Check sync status of all apps
kubectl get applications -n argocd

# Force a sync (bypasses the poll interval)
kubectl annotate application <name> -n argocd \
  argocd.argoproj.io/refresh=hard --overwrite

# View sync history
kubectl get application <name> -n argocd -o json \
  | jq '.status.history[-5:]'

# Suspend auto-sync on an app (e.g. while debugging)
kubectl patch application <name> -n argocd \
  --type=merge -p '{"spec":{"syncPolicy":{"automated":null}}}'
# Re-enable:
kubectl patch application <name> -n argocd \
  --type=merge -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
```

Use the ArgoCD UI at `https://argocd.<YOUR_DOMAIN>` for a visual diff between live state and git (requires VPN + UW-Madison NetID).
