# CloudPipe GitOps (ArgoCD)

ArgoCD manages all Kubernetes resources except those created directly by Terraform during bootstrap. The single rule: **anything in the cluster that ArgoCD owns will be reverted to git within seconds if you change it manually**. Always commit and push; never `kubectl apply` to a resource ArgoCD controls.

---

## Structure

```
gitops/
  bootstrap/
    root-app.yaml          ← ApplicationSet that generates all apps
  apps/
    argo-workflows/        ← Umbrella chart (argo-workflows + pgbouncer subchart)
    aws-ebs-csi-driver/    ← Helm wrapper chart
    aws-load-balancer-controller/
    cert-manager/
    cluster-config/        ← Raw manifests (not a Helm chart)
    external-dns/
    external-secrets/
    grafana/               ← Helm wrapper chart + dashboard JSONs
    nvidia-device-plugin/  ← GPU device plugin + time-slicing config
    pipelines/
      workflow-templates.yaml  ← Separate Application for WorkflowTemplates
    prefect/               ← Umbrella chart (server + worker + oauth2-proxy)
    prometheus/            ← kube-prometheus-stack
    prometheus-operator-crds/
    reloader/
```

That is **14** directories, so the ApplicationSet generates 14 Applications.

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
- All `WorkflowTemplate` resources under `argo/workflows/cloudpipe_minproc/` and `fmri_first_level_proc/`
- The `cloudpipe-semaphores` ConfigMap
- The `globus-credentials` ExternalSecret

`argo/workflows/` also has an `exclude: 'cloudpipe_fullproc/**'` glob in the `directory` sync spec — that pipeline is planned but not implemented (no such directory exists yet, see [architecture.md](architecture.md#cloudpipe_fullproc-planned--design-only-not-implemented)); the exclusion is a placeholder so a future `cloudpipe_fullproc/` directory doesn't get auto-synced mid-development, and should be dropped once the pipeline is ready to deploy.

Sync wave: `3` (applied after cluster infrastructure).

The same `selfHeal: true` + `prune: true` policy applies. Deleting a WorkflowTemplate YAML from git will delete it from the cluster on the next sync.

---

## Applications reference

### argo-workflows

Umbrella chart with two dependencies: `argoproj/argo-workflows` v1.0.18 and `icoretech/pgbouncer` v4.1.9. PgBouncer was migrated from hand-written manifests in `templates/` to the subchart; `templates/pgbouncer.yaml` is now just a pointer comment.

Key overrides in `values.yaml`:

| Setting | Value | Why |
|---|---|---|
| `controller.configMap.create` | `false` | Terraform owns the controller ConfigMap (bucket name must stay in sync with `var.globus_s3_destination_bucket`) |
| `server.extraArgs` | `--auth-mode=sso --auth-mode=client` | SSO via Dex; CLI token auth as fallback |
| `controller.deploymentAnnotations` | `secret.reloader.stakater.com/reload: "argo-db"` | Reloader restarts the controller pod when the `argo-db` K8s Secret changes (e.g. after RDS password rotation) |
| `server.deploymentAnnotations` | same | Same restart trigger on the server |
| `useStaticCredentials` | `false` | Pod Identity used for S3 artifact access; no static AWS credentials |

Both `controller` and `server` run on the `argo` managed node group (taint `argoproj.io/backend=true`). Pipeline pods (runner SA) land on Karpenter-provisioned nodes.

`artifactRepository` is intentionally absent from `values.yaml` — it is defined in the controller ConfigMap managed by Terraform so the S3 bucket name stays in sync with the Terraform variable.

**Anything whose only chart consumer is the controller ConfigMap renders nowhere.** Because `controller.configMap.create` is `false`, `templates/controller/workflow-controller-config-map.yaml` is never rendered — so `controller.parallelism`, `controller.namespaceParallelism`, `controller.resourceRateLimit`, `server.sso` and `artifactRepository` are all *silently inert* if set in `values.yaml`. They live in `terraform/modules/argo-workflows/main.tf` instead. This bit twice: a documented pod-creation rate limit was never in effect until GitHub #206, and a `server.sso` block carried two stale-looking domain literals that no running pod ever read until GitHub #17. Add controller-config settings to the Terraform module, never here.

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
| `external-secrets-patch.yaml` | Patch for External Secrets Operator — ClusterRole/ClusterRoleBinding also created by Terraform (`terraform/modules/addons/external-secrets.tf`); ArgoCD manages the live state |
| `gpu-priority-classes.yaml` | Three `PriorityClass` objects ranking the GPU steps under spot scarcity (#370) — see below |
| `storage-class.yaml` | `ebs-sc` StorageClass (gp3, encrypted, default) — also created by Terraform; ArgoCD manages the live state |

**GPU priority classes.** Under spot scarcity pending GPU pods were served in
roughly arrival order, so `template-build` pods — each of which spawns another GPU
pod when it wins — competed equally with the `long-segmentation` pods that would
have retired work. The three classes rank completion above starts:

| PriorityClass | Value | Step |
|---|---|---|
| `cloudpipe-gpu-t1w-to-mni` | 300 | `t1w-to-mni` — ~30 s of GPU, unblocks a session's functional phase |
| `cloudpipe-gpu-long-segmentation` | 200 | `long-segmentation` — finishes a subject that already holds a template |
| `cloudpipe-gpu-template-build` | 100 | `template-build` — starts new GPU demand |

All three are `preemptionPolicy: Never`: they reorder the pending queue only and
never evict a running pod, which matters because each of these steps is 6–12 min
of non-resumable inference. Values sit above the unclassed default (0) and below
everything already on the cluster (`aws-guardduty-agent` 1000000,
`system-cluster-critical` 2000000000).

The `priorityClassName` references live on the three Argo templates. Because
`gitops/**` is outside `ci.yaml`'s `paths` filter, the pairing is guarded from the
other side, in `tests/argo/test_gpu_step_resources.py` — that test reads this
manifest, so a class renamed here without the template (or vice versa) fails CI.

**Rollout order matters.** `cluster-config` and `workflow-templates` are separate
ArgoCD Applications, so a single commit does not sync them atomically. A pod whose
template names a PriorityClass that does not exist yet is rejected by the Priority
admission plugin, so the classes must reach the cluster before (or with) the
template change. It self-heals once `cluster-config` syncs, but the window shows up
as GPU pods failing to create. Background: `docs/investigations/2026-09-10-gpu-spot-acquisition-review.md` §4.1.

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

The `domainFilters` entry is one of the two intentionally hardcoded base-domain literals — see [Hardcoded base domain](#hardcoded-base-domain-policy-and-exceptions).

### prometheus-operator-crds

Installs only the CRDs (ServiceMonitor, PodMonitor, PrometheusRule, etc.). Kept as a **separate** Application from `prometheus` so the CRDs are established before any chart that defines a ServiceMonitor syncs — including the stack itself. Ordering, not exclusivity: the full stack does run, in the `prometheus` app below.

### prometheus

Helm chart: `prometheus-community/kube-prometheus-stack` v77.14.0. Runs the Prometheus server, Alertmanager, and node-exporter. This is the datasource behind the two Prometheus-backed Grafana dashboards (infra-health and Karpenter); the other six read Athena.

### grafana

Helm chart: `grafana/grafana` v8.10.1. The dashboard JSONs live alongside the chart in `gitops/apps/grafana/dashboards/` and are provisioned as ConfigMaps, so a dashboard change ships through git like any other manifest — it is not edited in the UI. Grafana's AWS access (Athena query, Glue read, S3) comes from a Pod Identity association defined in Terraform, not from static credentials. See [observability.md](observability.md) for the dashboard inventory.

SSO URLs and the admin email are single-sourced from Terraform via the `grafana-oidc-config` ConfigMap and expanded by Grafana's own `$__env{...}` at startup, but the **ingress host cannot be** — see [Hardcoded base domain](#hardcoded-base-domain-policy-and-exceptions).

### nvidia-device-plugin

Helm chart: `nvidia/nvidia-device-plugin` v0.19.1. Advertises GPUs to the scheduler and configures **time-slicing at 3 replicas per physical GPU** (`failRequestsGreaterThanOne: false`), which is what lets 3 pods share one card.

> **Keep this in sync with Karpenter.** The `replicas` count here and the `gpu-timeslice-3x` NodeOverlay in `terraform/modules/karpenter/helm-values/` describe the same fact in two places. The slice count is bounded by the *smallest* card in the GPU pool — the g4dn's T4 at 15 GiB — not the largest, so raising it based on an A10G's 23 GiB will OOM on T4 nodes. A pod also needs its CPU request low enough that N slices fit on one node's vCPUs.

### aws-ebs-csi-driver / aws-load-balancer-controller

Standard AWS CSI and networking add-ons. Helm-managed by ArgoCD; IAM is managed by Pod Identity associations in Terraform.

(The `aws-efs-csi-driver` app was removed once `subregion-seg`, the last EFS PVC consumer, moved to S3 checkpointing — GitHub #77.)

> **Deleting an Application does not always delete everything it created.** Removing
> `aws-efs-csi-driver` left its `efs.csi.aws.com` CSIDriver object and its
> `aws-efs-csi-driver` namespace behind for three months, unowned by any Application
> ([#213](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/213), cleaned up by hand).
> The CSIDriver carried `helm.sh/resource-policy: keep`, which tells Helm — and therefore
> ArgoCD — to leave the resource in place when the release goes. That annotation exists so a
> driver *upgrade* cannot yank the CSIDriver out from under mounted volumes; the cost is that a
> genuine *removal* leaves residue nothing prunes. After removing an Application, check for
> leftovers (`kubectl get csidriver,crd,ns` and anything else cluster-scoped) rather than
> assuming the app-of-apps swept them.

---

## Ownership split: Terraform vs ArgoCD

This boundary matters when troubleshooting. A resource that Terraform creates will not be in ArgoCD's sync list; a resource that ArgoCD manages will not appear in `terraform state`.

| Resource | Owner |
|---|---|
| Helm release: ArgoCD itself | Terraform (`argocd.tf`) |
| Helm releases: all other apps | ArgoCD |
| ArgoCD ApplicationSet + root-app | Terraform only (`kubectl_manifest.argocd_root_app`). No Application watches `gitops/bootstrap/`, so a change to `root-app.yaml` needs `terraform apply` — pushing it to git does nothing. |
| EKS add-ons (vpc-cni, coredns, etc.) | Terraform |
| Karpenter NodePools / NodeClass | Terraform (via module) |
| IAM roles and Pod Identity associations | Terraform |
| RDS instances + ExternalSecrets for DB creds | Terraform |
| `argo-workflows-controller-configmap` | Terraform |
| WorkflowTemplates | ArgoCD (`workflow-templates` Application) |
| `cloudpipe-semaphores` ConfigMap | ArgoCD |
| `globus-credentials` ExternalSecret | ArgoCD |
| StorageClass (`ebs-sc`) | Both (Terraform creates; ArgoCD manages ongoing state) |
| `external-secrets-cert-controller-patch` ClusterRole/Binding | Both (Terraform creates; ArgoCD manages ongoing state) |

---

## Hardcoded base domain: policy and exceptions

Terraform single-sources the base domain from `var.domain` (`terraform/variables.tf`), and
`terraform/locals.tf` derives `argocd_url` / `argo_url` / `prefect_url` / `grafana_url` from it.
On the **gitops** side that is not fully achievable, and the remaining literals are a deliberate
decision (GitHub #17), not an oversight.

**Why it can't be fully parameterized.** A Helm `values.yaml` is *data*, not a template — Helm
renders `templates/`, never values. So a setting that lives in a **subchart's** value tree (for
example `grafana.ingress.hosts`) cannot reference a variable from inside the file. It can only be
overridden from outside the chart, or moved into the parent chart's `templates/`. A value is
therefore single-sourceable exactly when some *runtime* consumer expands it:

| Mechanism | Works because | Used by |
|---|---|---|
| `$__env{DOMAIN}` | Grafana's own binary interpolates it at startup | `grafana.ini` `root_url`, `auth.generic_oauth` URLs (GitHub #16) |
| `configMapKeyRef` env var | kubelet resolves it at pod start | `DOMAIN` / `ADMIN_EMAIL` from the Terraform-managed `grafana-oidc-config` ConfigMap |
| Terraform interpolation | rendered before the object is applied | everything in `terraform/`, incl. the live Argo `sso` key |

Kubernetes reads an Ingress `host` **literally** — there is no interpolator in that path at all.

**The two remaining literals, and why each stays:**

| Location | Value | Why it stays |
|---|---|---|
| [`gitops/apps/grafana/values.yaml`](https://github.com/jrussell9000/cloudpipe/blob/main/gitops/apps/grafana/values.yaml) | `ingress.hosts[0]` and the `external-dns.alpha.kubernetes.io/hostname` annotation | Subchart values consumed as literals by the Ingress object. Removing them means either disabling the subchart ingress and re-implementing it in the parent chart's `templates/`, or injecting `helm.parameters` from the ApplicationSet — both cost more risk than the duplication. |
| [`gitops/apps/external-dns/values.yaml`](https://github.com/jrussell9000/cloudpipe/blob/main/gitops/apps/external-dns/values.yaml) | `domainFilters[0]` | Rendered into a `--domain-filter=` container arg. Same constraint. |

`gitops/apps/prefect/values.yaml` still holds three literals (`prefectUiApiUrl`,
oauth2-proxy's `oidc-issuer-url` and `redirect-url`). These *are* env-var-backed upstream
(`PREFECT_UI_API_URL`, `OAUTH2_PROXY_OIDC_ISSUER_URL`, `OAUTH2_PROXY_REDIRECT_URL`) and so could
follow the `configMapKeyRef` row above — deferred because a wrong `redirect-url` locks the Prefect
UI out, so it needs a deliberate deploy plus an end-to-end login check rather than a drive-by edit.

**Two traps if you do attempt broader parameterization.** The obvious approach — add a uniform
`spec.source.helm.parameters` block to the `cluster-addons` ApplicationSet template — breaks in two
places:

1. `gitops/apps/pipelines/` has **no `Chart.yaml`**; it is a plain-manifest Application (an
   app-of-apps pointer to `workflow-templates`). Setting `spec.source.helm` makes ArgoCD treat it
   as a Helm chart, and with `prune: true` that misfires on the production WorkflowTemplates.
2. `gitops/apps/prometheus/values.yaml` already has a `grafana:` key (kube-prometheus-stack's
   subchart), so a blanket `grafana.ingress.*` parameter leaks into a second chart.

Note also that `root-app.yaml` is applied by Terraform with `file()`, not `templatefile()`
(`terraform/argocd.tf`). Switching it to `templatefile()` would let `var.domain` reach the
ApplicationSet without introducing a second source of truth — `{{ }}` (ArgoCD) and `${ }`
(Terraform) do not collide. That is the path to take if this is revisited.

**When changing the domain**, then, these files need a manual edit alongside `var.domain`:
`gitops/apps/grafana/values.yaml`, `gitops/apps/external-dns/values.yaml`,
`gitops/apps/prefect/values.yaml`.

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
