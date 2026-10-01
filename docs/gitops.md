# CloudPipe GitOps (ArgoCD)

ArgoCD manages all Kubernetes resources except those created directly by Terraform during bootstrap. The single rule: **anything in the cluster that ArgoCD owns will be reverted to git within seconds if you change it manually**. Always commit and push; never `kubectl apply` to a resource ArgoCD controls.

---

## Structure

```
gitops/
  bootstrap/
    root-app.yaml.tftpl              ← ApplicationSet that generates all apps (Terraform template)
    workflow-templates.yaml.tftpl    ← Application for argo/workflows (Terraform template)
  apps/
    argo-workflows/        ← Umbrella chart (argo-workflows + pgbouncer subchart)
    aws-ebs-csi-driver/    ← Helm wrapper chart
    aws-load-balancer-controller/
    cert-manager/
    cloudflared/           ← Cloudflare tunnel connectors
    cluster-config/        ← Our own manifests; a Helm chart with no dependency
    external-dns/
    external-secrets/
    grafana/               ← Helm wrapper chart + dashboard JSONs
    nvidia-device-plugin/  ← GPU device plugin + time-slicing config
    prefect/               ← Umbrella chart (server + worker + oauth2-proxy)
    prometheus/            ← kube-prometheus-stack
    prometheus-operator-crds/
    reloader/
```

That is **14** directories, so the ApplicationSet generates 14 Applications.

Each directory under `gitops/apps/` is a Helm wrapper chart: a thin `Chart.yaml` with an upstream dependency and a `values.yaml` that overrides defaults. ArgoCD renders the chart and applies the result. There is no longer an exception to that: `pipelines/`, which held a plain `Application` manifest rather than a chart, is gone — its object is rendered by Terraform from `gitops/bootstrap/` instead (see [workflow-templates Application](#workflow-templates-application)).

---

## Bootstrap Application (cluster-addons)

`gitops/bootstrap/root-app.yaml.tftpl` defines a single ArgoCD `ApplicationSet` named `cluster-addons`. It watches `gitops/apps/*` via a Git generator and creates one ArgoCD `Application` per directory, naming each after `path.basename`.

The file is a Terraform template, rendered by `templatefile()` in `terraform/modules/stack/argocd.tf`. Terraform supplies the repository URL and revision (`var.gitops_repo_url`, `var.gitops_revision`) and each app's Helm overrides. Two template languages share the file: `${ ... }` is Terraform's, expanded once at apply time, and `{{ ... }}` is ArgoCD's Go template, expanded per generated Application. The ApplicationSet sets `goTemplate: true`, so generator parameters are referenced as `{{.path.basename}}` and `{{.path.path}}` — under `goTemplate` the Git generator's `path` parameter is an object, not a string.

### Per-app Helm overrides

Values that differ between deployments of this stack — the region, the base domain, hostnames — come from Terraform rather than from `gitops/apps/<app>/values.yaml`, so that each one has a single input surface (ADR 020). The mechanism is the ApplicationSet's `templatePatch`, a Go template evaluated **per generated Application** and applied to it as a strategic-merge patch. Each branch is guarded on `.path.basename`, so an override reaches one app and leaves the others byte-identical:

```yaml
templatePatch: |
  {{- if eq .path.basename "external-dns" }}
  spec:
    source:
      helm:
        valuesObject:
          ...      # rendered from local.argocd_app_overrides
  {{- else }}
  spec: {}
  {{- end }}
```

The values live in `local.argocd_app_overrides` in `terraform/modules/stack/argocd.tf`, keyed by app directory name. Adding one means editing both files: the map entry, and a branch in the template that reads it. Five things to know:

- **The top-level keys of an entry are subchart names** and must match the dependency name in the app's `Chart.yaml`, because the patch sets that Application's Helm values, not the subchart's directly.
- **Unless the app declares no dependencies,** in which case the keys are the chart's own values and something in its `templates/` has to read them as `.Values.<key>`. `cluster-config` is the only app like this. Both shapes fail the same way when the key is wrong — Helm accepts the value and renders nothing with it — so `tests/test_argocd_app_overrides.py` checks each shape against its own source of truth.
- **Helm replaces lists; it does not merge them.** A list in an override has to be complete. `external-dns`'s `env` carries both `AWS_DEFAULT_REGION` and `AWS_REGION` for that reason.
- **Below that first key, an entry goes as deep as the chart does,** and only the leaf is owned by Terraform. `prefect`'s entry sets `prefect-server.server.uiConfig.prefectUiApiUrl`; `values.yaml` keeps `prefect-server.server.env`, the probes and the rollout strategy, and has to. Maps merge key by key, so the two coexist — which is why the duplication test compares leaves and not intermediate keys.
- **Every branch sets `spec`,** so this is one `if`/`else if` chain rather than one block per app — two branches firing would write the key twice in one patch document.
- **`spec.project` is not patchable.** ArgoCD restores it from the template after the patch is applied.
- **An assertion elsewhere in Terraform that reads `values.yaml` has to learn to read the override too.** `kubernetes_ingress_v1.argocd_ingress` refuses to plan unless Grafana's ALB annotations match `local.ui_alb_group_annotations` and do *not* include `wafv2-acl-arn` — Grafana is the one `cloudpipe-ui` IngressGroup member Terraform does not render, and a member disagreeing on a group-level annotation stops the controller reconciling the whole shared ALB. Once an annotation can come from the override, the file alone is no longer what the cluster gets, and the `wafv2-acl-arn` half of that check fails **open** on a key the override carries. `local.grafana_ingress_annotations` therefore merges the override over the file, override last, the way Helm does.

Grafana's entry also shows the two shapes a Kubernetes-flavoured value brings: a key can be an annotation name (`external-dns.alpha.kubernetes.io/hostname`, dots and a slash), and `defaultRegion` sits inside the `datasources` list — so the entry carries that whole list, Prometheus datasource included, because Helm replaces a list rather than merging into it.

Migrating an app is two changes in this order, never the reverse:

1. Add the override, leave `values.yaml` alone, and `terraform apply`. The override repeats what `values.yaml` already says, so nothing changes in the cluster — which is what makes it verifiable.
2. Then delete those keys from `values.yaml`. ArgoCD picks that up on its own; no apply is needed.

Merging step 2 first leaves a window where neither source supplies the value, and how loudly that fails is a property of the chart, not of the mechanism:

| App | What step 2 without step 1 does |
| --- | --- |
| `external-dns` | Silent. A Deployment with no `--domain-filter` arg at all — an external-dns that manages every hosted zone it can reach. |
| `aws-load-balancer-controller` | Loud. The chart refuses to render: `Chart cannot be installed without a valid clusterName!`. Region alone is silent, but the controller falls back to instance-metadata discovery, which is correct here. |
| `cluster-config` | Loud, by our choice. Its template reads the value through `required`, so the render fails with a message naming the override. We own this template, so the failure mode was ours to pick — a bare `.Values.region` would have rendered `region:` and stopped every `ExternalSecret` in the cluster from resolving, with nothing in the manifest saying why. |
| `prefect` | Silent, and the only one that fails **open**. The oauth2-proxy chart defaults `config.emailDomains` to `["*"]`, not to empty, so the generated `oauth2_proxy.cfg` admits every identity Dex will issue a token for instead of one domain. The same render also drops `--oidc-issuer-url` and `--redirect-url` and points the UI at `http://localhost:4200/api`. Every other app on this list either keeps working or refuses to render; this one renders something weaker than intended. |
| `grafana` | Silent, and open in the same way: `allowed_domains` disappears from `grafana.ini`, and Grafana's default is no domain restriction at all, so `allow_sign_up: true` admits every identity Dex will issue a token for. (Dex's only upstream connector is UW-Madison NetID, so the set that widens to is narrower than Prefect's, but the render is still weaker than intended and goes Healthy.) The rest of that render is loud: the ingress host and `grafana.ini`'s `domain` fall back to the chart's `chart-example.local`, which moves the ALB host rule and drops the external-dns annotation, and the `datasources.yaml` provisioning key vanishes with its volume mount, so every dashboard loses its datasource. |

Check either step locally before it lands. Where the chart is vendored under `gitops/apps/<app>/charts/`, `helm template` needs no network:

```bash
cd gitops/apps/external-dns
helm template external-dns . --namespace external-dns > /tmp/before.yaml
# write the rendered valuesObject to /tmp/override.yaml, then:
helm template external-dns . --namespace external-dns -f /tmp/override.yaml | diff -u /tmp/before.yaml -
```

Two things that recipe does not cover:

- **Most dependencies are not vendored** — only `argo-workflows`, `external-dns`, `prefect` and `prometheus` are, so the rest (`grafana` and `aws-load-balancer-controller` among them) need `helm dependency build` and network first. `aws-load-balancer-controller` also mints a fresh self-signed webhook cert on every render, so a raw diff of two renders always differs — blank `ca.crt`, `tls.crt`, `tls.key` and `caBundle` before comparing.
- **`-f` layers over the chart's own `values.yaml`; it does not replace it.** To model step 2, copy the chart and edit the copy's `values.yaml`. Passing a stripped file with `-f` proves nothing, because the keys are still in the base.
- **A no-diff render is not a working login.** `prefect`'s `redirect-url` has to match the callback `terraform/modules/stack/argocd.tf` registers for the `prefect` Dex static client, and `prefectUiApiUrl` is read by the browser, not by anything in the cluster. Both are plain strings in a Deployment: wrong values render, sync and go Healthy. After an apply that touches them, open the UI and complete a sign-in.

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

`gitops/bootstrap/workflow-templates.yaml.tftpl` is a standalone ArgoCD `Application` (not part of the ApplicationSet), rendered by `kubectl_manifest.argocd_workflow_templates` in `terraform/modules/stack/argocd.tf`. It watches `argo/workflows/` recursively and syncs everything it finds into the `argo-workflows` namespace.

**Terraform owns the object; ArgoCD owns what it syncs.** Editing this template and pushing does nothing until `terraform apply` runs — the same ownership boundary as `root-app.yaml.tftpl`. Editing a WorkflowTemplate under `argo/workflows/` needs no apply at all; this Application picks it up on its own.

It is rendered rather than read verbatim because `repoURL` and `targetRevision` are deployment values (ADR 020), and it reads the same `var.gitops_repo_url` / `var.gitops_revision` as the ApplicationSet, so the generated apps and the WorkflowTemplates cannot end up tracking different repositories. The override mechanism could not be used here: this was the one app with no `Chart.yaml`, and `spec.source.helm` on a plain-manifest Application makes ArgoCD treat these WorkflowTemplates as a Helm chart under `prune: true`.

The manifest also carries `argocd.argoproj.io/sync-options: Delete=false`. That is a leftover of the handover with teeth: the object used to be a resource of the generated `pipelines` Application, which carries ArgoCD's cascading-delete finalizer, and deleting `gitops/apps/pipelines/` deleted that Application. A resource-level Delete sync option always overrides the owning Application's policy, which is what let the live object survive to be adopted by Terraform. Keep it — it is inert while Terraform owns the object, and it is what a future re-adoption would need.

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

Umbrella chart with two dependencies: `argoproj/argo-workflows` v2.0.6 (app v4.1.3) and `icoretech/pgbouncer` v4.1.9. PgBouncer was migrated from hand-written manifests in `templates/` to the subchart; `templates/pgbouncer.yaml` is now just a pointer comment.

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
- `prefectUiApiUrl` must match the ALB hostname so browser API calls route correctly — it comes from `local.prefect_url` through the ApplicationSet, see [Per-app Helm overrides](#per-app-helm-overrides)

**prefect-worker**
- Type: `kubernetes` (Kubernetes work pool)
- Talks to server via in-cluster URL to avoid ALB round-trip
- Work pool: `cloudpipe-k8s-pool`

**oauth2-proxy** (SSO gate in front of prefect-server)
- Provider: OIDC via Dex (ArgoCD's Dex instance)
- Only one email domain allowed, from `var.institution_domain`. The issuer URL and `redirect-url` come from the same override; the redirect has to agree with the `prefect` Dex static client in `terraform/modules/stack/argocd.tf`, which is why both live in Terraform
- The chart's own default for the allowed domain is `["*"]`, so this value going missing widens access rather than breaking it
- `/api/` path bypasses SSO — required for Prefect CLI and programmatic access; access is VPN-gated at the ALB security group instead

### cluster-config

A plain Helm chart containing raw manifests (`templates/`). Not an upstream dependency — manifests are applied as-is.

It declares no dependencies, so it is the one app whose override keys are the chart's own values rather than a subchart's: `region` comes from `var.region` through the ApplicationSet — see [Per-app Helm overrides](#per-app-helm-overrides).

| Template | What it creates |
|---|---|
| `argo-server-rbac.yaml` | ClusterRoles and bindings for the Argo server and `argo-admin` SA (node reader, SSO RBAC, events reader cross-namespace) |
| `cluster-secret-store.yaml` | `ClusterSecretStore` named `aws-secrets-manager` pointing to Secrets Manager in `.Values.region` |
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
as GPU pods failing to create. Background: `docs-internal/investigations/2026-09-10-gpu-spot-acquisition-review.md` §4.1.

### reloader

Stakater Reloader watches for annotation-driven pod restarts. When a Secret or ConfigMap that matches a pod's annotation changes, Reloader rolls the Deployment.

Used for: Argo controller and server automatically restart when the `argo-db` Secret is updated (e.g. after RDS native password rotation).

Strategy: `annotations` (only restarts pods whose Deployment has the `secret.reloader.stakater.com/reload` annotation).

### external-secrets

Installs External Secrets Operator and its CRDs. Pod Identity association is managed in Terraform (`modules/addons/external-secrets.tf`). The SA name (`external-secrets`) must match the Pod Identity association.

### cert-manager

Installs CRDs and all three components (controller, webhook, cainjector) on the `backend` node group. Used by ArgoCD and AWS Load Balancer Controller for certificate management.

### external-dns

Watches Ingress and Service objects for hostnames in the deployment's domain (Terraform `domain` variable) and creates Route53 records. `policy: upsert-only` — will not delete records. `txtOwnerId: cloudpipe` prevents collisions if a second External DNS instance is deployed.

The domain it filters on and the AWS region are **not** in its `values.yaml`. They come from `var.domain` and `var.region` through the ApplicationSet — see [Per-app Helm overrides](#per-app-helm-overrides). A Deployment here with no `--domain-filter` arg at all is the signature of the override not arriving: external-dns then manages every hosted zone it can reach.

### prometheus-operator-crds

Installs only the CRDs (ServiceMonitor, PodMonitor, PrometheusRule, etc.). Kept as a **separate** Application from `prometheus` so the CRDs are established before any chart that defines a ServiceMonitor syncs — including the stack itself. Ordering, not exclusivity: the full stack does run, in the `prometheus` app below.

### prometheus

Helm chart: `prometheus-community/kube-prometheus-stack` v77.14.0. Runs the Prometheus server, Alertmanager, and node-exporter. This is the datasource behind the two Prometheus-backed Grafana dashboards (infra-health and Karpenter); the other six read Athena.

### grafana

Helm chart: `grafana/grafana` v8.10.1. The dashboard JSONs live alongside the chart in `gitops/apps/grafana/dashboards/` and are provisioned as ConfigMaps, so a dashboard change ships through git like any other manifest — it is not edited in the UI. Grafana's AWS access (Athena query, Glue read, S3) comes from a Pod Identity association defined in Terraform, not from static credentials. See [observability.md](observability.md) for the dashboard inventory.

SSO URLs and the admin email are single-sourced from Terraform via the `grafana-oidc-config` ConfigMap and expanded by Grafana's own `$__env{...}` at startup. Four more values reach the chart by the other route, an ApplicationSet override — the ingress hostname (`hosts` and the external-dns annotation), the SSO `allowed_domains`, and the whole `datasources` map, which is Terraform's for the sake of the Athena datasource's region alone: that region sits in a list, and Helm replaces a list rather than merging into it. See [Per-app Helm overrides](#per-app-helm-overrides).

The ALB annotations on that ingress **do** stay in `values.yaml`, and Terraform asserts they agree with `local.ui_alb_group_annotations` — Grafana is the one `cloudpipe-ui` IngressGroup member Terraform does not render. That assertion reads the override merged over the file, because either source can now carry an annotation.

### nvidia-device-plugin

Helm chart: `nvidia/nvidia-device-plugin` v0.19.1. Advertises GPUs to the scheduler and configures time-slicing (`failRequestsGreaterThanOne: false`) through two **profiles**, chosen per node by the `nvidia.com/device-plugin.config` label:

| Profile | Replicas per GPU | Nodes |
|---|---|---|
| `default` | 3 | unlabelled nodes, i.e. all of `gpu-nodepool` |
| `dense` | 4 | `gpu-dense-nodepool`, whose NodePool template sets the label |

The DaemonSet reaches both pools through a `karpenter.sh/nodepool In` affinity. **A GPU pool missing from that list gets nodes that come up Ready and advertise no GPU**, which is how the g7 nodes were stranded on 2026-09-04.

> **Keep this in sync with Karpenter.** Each profile's `replicas` and the NodeOverlay covering that pool's instance types (`gpu-timeslice-3x`, `gpu-dense-timeslice-4x` in `terraform/modules/karpenter/helm-values/`) describe the same fact in two places. Size a slice count to the *smallest* card in the pool and the *largest* per-pod VRAM, never to the card a batch happened to land on. The largest is t1w-to-mni at a measured **4903 MiB** per process: `nvidia-smi` per-process usage, not PyTorch's own counter, which reads only 3698 MiB because it misses the CUDA context and allocator cache. Re-measure with `scripts/jobs/gpu-t1w-vram-probe.yaml` after any fireANTs image or torch change. The CPU request must also be low enough that N slices fit on one node's vCPUs. `tests/argo/test_gpu_step_resources.py` checks all of this, but CI does not run for a change under `gitops/` alone, so run it locally.

### aws-ebs-csi-driver / aws-load-balancer-controller

Standard AWS CSI and networking add-ons. Helm-managed by ArgoCD; IAM is managed by Pod Identity associations in Terraform.

`aws-load-balancer-controller`'s `clusterName` and `region` are **not** in its `values.yaml`. They come from `module.eks.cluster_name` and `var.region` through the ApplicationSet — see [Per-app Helm overrides](#per-app-helm-overrides). `clusterName` comes from the module output rather than `var.name` so the value is the name of the cluster that actually exists. A missing `clusterName` fails the render outright, so this override going astray is visible as a Degraded Application rather than as silently wrong tags.

(The `aws-efs-csi-driver` app was removed once `subregion-seg`, the last EFS PVC consumer, moved to S3 checkpointing — GitHub #77.)

> **Deleting an Application does not always delete everything it created.** Removing
> `aws-efs-csi-driver` left its `efs.csi.aws.com` CSIDriver object and its
> `aws-efs-csi-driver` namespace behind for three months, unowned by any Application
> (#213, cleaned up by hand).
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
| ArgoCD ApplicationSet + root-app | Terraform only (`kubectl_manifest.argocd_root_app`). No Application watches `gitops/bootstrap/`, so a change to `root-app.yaml.tftpl` needs `terraform apply` — pushing it to git does nothing. |
| `workflow-templates` Application object | Terraform only (`kubectl_manifest.argocd_workflow_templates`), same boundary. The WorkflowTemplates it syncs are still ArgoCD's, and need no apply. |
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

Terraform single-sources the base domain from `var.domain` (`terraform/modules/stack/variables.tf`), and
`terraform/modules/stack/locals.tf` derives `argocd_url` / `argo_url` / `prefect_url` / `grafana_url` from it.
The **gitops** side used to keep literals of its own as a deliberate decision (GitHub #17), on the
argument that a subchart value has no interpolator to read a variable with.

**That policy is retired.** ADR 020 makes the
public repo the upstream, so no synced path may hold a deployment literal. The mechanism is the
`templatePatch` in `root-app.yaml.tftpl`, which sets `spec.source.helm.valuesObject` per app from
values Terraform renders. That answers the "why it can't be parameterized" argument below — an
override from outside the chart is exactly what a subchart value needs — and it avoids both traps
described here, because the patch is per app rather than uniform. The section is kept because the
runtime-interpolation table is still how the values that are *not* overridden reach the chart, and
because the two traps still rule out the uniform version of the mechanism.

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

**No domain literal is left under `gitops/apps/`.** Each was moved by the two-PR migration above,
in the order the apps were done:

| Location | Value | Now |
|---|---|---|
| `gitops/apps/external-dns/values.yaml` | `domainFilters[0]`, rendered into a `--domain-filter=` container arg | `var.domain` through the override (the first app migrated, and the worked example for the rest), alongside the region |
| `gitops/apps/prefect/values.yaml` | `prefectUiApiUrl`, oauth2-proxy's `oidc-issuer-url` and `redirect-url`, and the allowed email domain | `local.prefect_url` / `local.argocd_url` / `var.institution_domain` through the override (#565, #566). These are also env-var-backed upstream (`PREFECT_UI_API_URL`, `OAUTH2_PROXY_OIDC_ISSUER_URL`, `OAUTH2_PROXY_REDIRECT_URL`), so the `configMapKeyRef` row above would have worked too; the override was chosen for consistency with the other apps |
| `gitops/apps/grafana/values.yaml` | `ingress.hosts[0]` and the `external-dns.alpha.kubernetes.io/hostname` annotation — subchart values the Ingress object consumes literally — plus `allowed_domains` | `local.grafana_url` and `var.institution_domain` through the override. Moving the annotation is also what made the shared-ALB precondition in `terraform/modules/stack/argocd.tf` read the override merged over `values.yaml` rather than the file alone |

What remains under `gitops/` is not a domain and not a value: two region literals in
`cloudflared`'s AZ comments (task 3.8 of the readiness change). The `pipelines` Application's
`repoURL` was the last one that was — it could not move through the override, so the object moved
instead, to `gitops/bootstrap/workflow-templates.yaml.tftpl`, and the `pipelines` directory is gone
(task 3.7).

**Two traps if you do attempt broader parameterization.** The obvious approach — add a uniform
`spec.source.helm.parameters` block to the `cluster-addons` ApplicationSet template — breaks in two
places:

1. **An app directory with no `Chart.yaml` is not a chart**, and setting `spec.source.helm` on it
   makes ArgoCD treat whatever it syncs as one — with `prune: true` behind it. `gitops/apps/`
   holds no such directory today (`pipelines/` was the one, and its Application is rendered by
   Terraform now), but nothing stops the next one being added, so
   `tests/test_argocd_app_overrides.py` asserts that an app without a `Chart.yaml` gets neither an
   override entry nor a `templatePatch` branch.
2. `gitops/apps/prometheus/values.yaml` already has a `grafana:` key (kube-prometheus-stack's
   subchart), so a blanket `grafana.ingress.*` parameter leaks into a second chart.

Both traps rule out a *uniform* override, not any override. `templatePatch` is evaluated per
generated Application, so a patch guarded on `.path.basename` reaches one app and leaves the other
thirteen byte-identical — see [Per-app Helm overrides](#per-app-helm-overrides). That is why the
migration is one app at a time, in two pull requests each: the override first, applied and shown to
change nothing, then the literal's removal.

**When changing the domain**, no file under `gitops/apps/` needs a manual edit any more. Change
`var.domain` (and `var.institution_domain`, if the sign-in domain changes with it) and apply —
`terraform apply -target=kubectl_manifest.argocd_root_app` is enough to re-render the overrides,
and ArgoCD syncs each app from there.

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

Use the ArgoCD UI at `https://argocd.<domain>` for a visual diff between live state and git (requires VPN + UW-Madison NetID).
