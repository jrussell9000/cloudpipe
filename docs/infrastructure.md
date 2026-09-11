# CloudPipe Infrastructure

All infrastructure lives in `terraform/`. Run all Terraform commands from within that directory — never use `-chdir=terraform`.

---

## Terraform file map

| File / directory | Manages |
|---|---|
| `eks.tf` | EKS cluster, managed node groups, KMS keys, CloudWatch log group |
| `vpc.tf` | VPC, subnets, NAT gateway, S3 VPC endpoint, VPC flow logs |
| `vpn.tf` | AWS Client VPN endpoint, client certificates |
| `addons.tf` | Pod Identity associations for EBS CSI, External DNS, CloudWatch agent |
| `karpenter.tf` | Karpenter Helm release (via module) |
| `storage.tf` | StorageClass (`ebs-sc`) |
| `argowf.tf` | Argo Workflows SSO secrets, RBAC, Prefect→Argo cross-namespace bindings |
| `argocd.tf` | ArgoCD Helm release, Dex OIDC config (UW-Madison NetID), bootstrap ApplicationSet |
| `prefect.tf` | Prefect module call, oauth2-proxy secret |
| `globus.tf` | Globus module call |
| `finops.tf` | Kubecost + Athena CUR module |
| `metrics.tf` | Pipeline observability module (Glue database + hand-declared catalog tables, Athena workgroup, Grafana Pod Identity) |
| `metrics_bucket.tf` | The `cloudpipe-metrics` bucket (versioned) that holds all QC/cost records |
| `abcd_v7_metrics_retire.tf` | Bucket policy denying writes to the retired `<YOUR_S3_BUCKET>/metrics/*` prefix |
| `grafana.tf` | Grafana Helm release, ALB ingress, OIDC |
| `logging.tf` | S3 log bucket, CloudTrail |
| `dns.tf` | Route53 zone lookup, ACM certificates (us-east-1 + <YOUR_AWS_REGION>) |
| `ecr.tf` | ECR private repositories (primary registry for pipeline images) |
| `s3_lifecycle.tf` | S3 lifecycle rules for data bucket |
| `kube-system-network-policy.tf` | Default-deny/allow network policies in `kube-system` |
| `locals.tf` | Derived locals — service URLs, VPC DNS resolver |
| `variables.tf` | All input variables with defaults |
| `versions.tf` + `providers.tf` | Provider pins, AWS provider aliases, S3 backend config |
| `modules/` | Reusable sub-modules (see module reference below) |
| `bootstrap/` | Separate Terraform root that creates the `cloudpipe-terraform-state` S3 bucket itself — its own local backend, not part of the main stack's state. Rarely touched; see [ADR 015](decisions/015-s3-backend-after-state-loss.md). |

---

## State management

Terraform state lives in the `cloudpipe-terraform-state` S3 bucket (versioned, SSE-encrypted, public access blocked), configured via the `backend "s3"` block in `versions.tf` with native state locking (`use_lockfile = true`, requires Terraform >= 1.10). This bucket is itself managed by a separate, small Terraform config in `terraform/bootstrap/` — deliberately kept off the main stack's backend to avoid a chicken-and-egg dependency.

This replaced a local-only backend (no remote state at all) after a 2026-07 incident where the machine holding the only state file was lost, requiring a full `terraform import` recovery of the entire stack. See [ADR 015](decisions/015-s3-backend-after-state-loss.md) for the incident writeup, what was recovered, two pieces of pre-existing infrastructure drift it surfaced (Globus AMI pinning, Karpenter `expireAfter`), and a list of diffs that are now permanent/expected rather than bugs.

---

## VPC and networking

| Resource | Value |
|---|---|
| Primary CIDR | `10.0.0.0/16` — the only CIDR in use. `var.secondary_cidr_blocks` is declared, and the VPN has routes/auth rules that iterate over it, but `vpc.tf` never associates it, so it is empty in practice. |
| Private subnets | Three AZs (<YOUR_AWS_REGION>a/b/c), `/20` each — EKS nodes. Upper 3/4 of each is a `prefix` CIDR reservation for pod prefix delegation, see [pod IP address space](#pod-ip-address-space) |
| Public subnets | Three AZs, `/24` each — ALBs, Globus EC2 |
| NAT gateway | Single (cost optimisation) — private subnets route through it |
| S3 VPC Gateway endpoint | All route tables — S3 traffic stays on AWS backbone |
| VPC flow logs | → `cloudpipe-logging` S3 bucket under `vpc-flow-logs/`, 60s aggregation |

### Pod IP address space

Every pod gets a real VPC IP from the private subnet its node sits in, so the `/20`
per AZ (4,091 usable) is a hard ceiling on concurrency — and the binding constraint
is **fragmentation, not depletion**.

With `ENABLE_PREFIX_DELEGATION=true` the CNI allocates a **`/28` — 16 contiguous,
16-aligned addresses — at a time**. A node can therefore fail to place one more pod
while hundreds of addresses are free, because none of them form an aligned block of
16. Two things consume blocks:

1. **Per-node warm capacity.** `WARM_PREFIX_TARGET=1` alone makes each node hold one
   whole *spare* `/28` beyond current need. Measured during the 2026-08-10
   200-subject batch (GitHub #218): <YOUR_AWS_REGION>c had 113 nodes running just 268 pods
   but held 204 prefixes = **3,264 addresses reserved**, a 12× overprovision, with 99
   nodes holding 2 prefixes apiece and only **one** fully-free aligned `/28` left in
   the whole `/20`. Pods stalled in `Init:0/1` for up to 93 min on `failed to assign
   an IP address` (42,843 `FailedCreatePodSandBox` events). Adding
   `MINIMUM_IP_TARGET=10` / `WARM_IP_TARGET=2` — which **override**
   `WARM_PREFIX_TARGET` — cut this to 98 prefixes / 1,568 reserved and raised free
   IPs in 2c from 698 to 2,413, verified live on the running batch.
2. **Node primary ENI addresses**, which AWS assigns singly and scatters through the
   same `/20`. These sterilised 52 further `/28` slots (~700 addresses) that no
   amount of CNI tuning can recover, and the count grows with every node added.
   Fixed outside the CNI by **`prefix`-type subnet CIDR reservations** (GitHub
   #220, `aws_ec2_subnet_cidr_reservation.pod_prefixes` in `vpc.tf`), which stop
   AWS assigning single addresses out of the reserved range. Each private `/20` is
   split into a **lower `/22` left unreserved** for single addresses (node primary
   ENIs, secondary-ENI primaries, VPC endpoint / RDS / Client VPN ENIs, the CNI's
   single-IP fallback) and the **remaining 3/4 reserved** for delegation — 3,072
   addresses = **192 `/28` slots per AZ**, against the 113 nodes the 300-concurrent
   batch peaked at in one AZ.

   Verified live on 2026-08-11: a fresh Karpenter node in <YOUR_AWS_REGION>c took primary
   IP `10.0.32.34` (unreserved `10.0.32.0/22`) and exactly one delegated prefix,
   `10.0.45.0/28` (reserved `10.0.40.0/21`) — the two patterns no longer interleave.

   Two things to know about reservations before touching this:

   - They **decrement `AvailableIpAddressCount` immediately** (documented for
     `prefix`, unlike `explicit`), so that field now reports *free single
     addresses* — ~1,014, not ~4,070. Read `aws ec2
     get-subnet-cidr-reservations --subnet-id <id>` alongside it or pod headroom
     looks like it collapsed by 75%.
   - They are **not retroactive** and may legally span addresses already in use
     (`10.0.4.0/22` was created over an in-use endpoint ENI at `10.0.4.222`). So
     apply on a drained cluster, and note the long-lived non-node ENIs scattered
     across each `/20` keep ~3 slots per AZ blocked until they are recreated —
     ~1.6%, versus the 20% above.

   This is why a secondary VPC CIDR + CNI custom networking (the original proposal
   in #220) was **not** needed: reservations retrofit onto the existing private
   subnets, so pod IPs stay inside `10.0.0.0/16` and every `cidr_ipv4 =
   var.vpc_cidr` security-group rule and NetworkPolicy `ipBlock` in the repo keeps
   matching pod traffic. Custom networking remains the option if *total* pod
   address space — not fragmentation — ever becomes the binding constraint; the
   primary CIDR still has three unused `/18`s (`10.0.64.0/18`, `10.0.128.0/18`,
   `10.0.192.0/18`) for pod subnets, so even then no secondary CIDR is required.

AZ skew compounds it: Karpenter's `price-capacity-optimized` spot strategy has no
awareness of subnet IP headroom, and both `EC2NodeClass`es select all three private
subnets via `tags: {karpenter.sh/discovery: cloudpipe}`, so node placement follows
spot price and routinely piles ~70% of nodes into one AZ. Sizing headroom against an
even three-way split will under-provision.

### Client VPN

AWS Client VPN provides private access to the EKS API (which has no public endpoint in steady state) and internal services. Required before using `kubectl`, `argo`, or the web UIs.

- Client CIDR: `10.3.0.0/22`
- **Full tunnel** (`var.split_tunnel` defaults to `false`) — *all* client traffic, including public internet, goes through the VPN. This is deliberate and load-bearing: with split tunnel, traffic to the web UIs' **public** ALB IPs never enters the tunnel, so it is never NAT'd into the client CIDR that the ALB security groups trust, and Grafana/ArgoCD/Argo become unreachable. See the comment on `variable "split_tunnel"` in `terraform/variables.tf` and the SG rules in `vpn.tf`, `grafana.tf`, `argocd.tf`.
- Certificates: managed by Terraform (`certificate_validity_period_hours = 8760`, i.e. 1 year)

A replacement (Cloudflare Tunnel + Access, on a **new** UW-Madison OIDC client independent of the one behind ArgoCD/Argo Workflows SSO) is **partially implemented** — [ADR 014](decisions/014-cloudflare-tunnel-over-vpn.md) phase 1. `terraform/cloudflare.tf` holds the tunnel and its private-subnet routes, and `gitops/apps/cloudflared/` deploys the connector (both landed in `dfc72fc`), and the Access identity provider federates UW-Madison NetID — verified end to end, MFA included, keyed on the `eduperson_principal_name` claim. The Access **application and policy** are the remaining gap, so nothing is reachable through the tunnel yet. Phase 1 is deliberately additive: **the VPN is still the only proven remote-access path** and stays up until `kubectl get nodes` succeeds through the tunnel with the VPN disconnected.

---

## EKS cluster

| Attribute | Value |
|---|---|
| Name | `cloudpipe` |
| Region | `<YOUR_AWS_REGION>` |
| Kubernetes version | `1.35` |
| Public endpoint | Disabled in steady state (enabled only during `install.sh` bootstrap) |
| Private endpoint | Always enabled |
| etcd encryption | KMS key `eks_secrets` |
| IRSA | Enabled (used by legacy resources; new resources use Pod Identity) |
| Access entry | Terraform caller IAM role → `AmazonEKSClusterAdminPolicy` |

### EKS add-ons (managed by Terraform)

| Add-on | Notes |
|---|---|
| `vpc-cni` | Prefix delegation enabled (`ENABLE_PREFIX_DELEGATION=true`), with `MINIMUM_IP_TARGET=10` / `WARM_IP_TARGET=2` — these **override** `WARM_PREFIX_TARGET`, which is retained only as a fallback. See [pod IP address space](#pod-ip-address-space) for why. Strict network policy mode (`NETWORK_POLICY_ENFORCING_MODE=strict`) enabled after Phase 3 of `install.sh`. |
| `coredns` | Runs on `backend` node group; forwards to VPC DNS resolver |
| `kube-proxy` | Standard |
| `metrics-server` | Runs on `backend` node group |
| `eks-pod-identity-agent` | Installed `before_compute` so Pod Identity works from first node join |

`amazon-cloudwatch-observability` was **removed on 2026-09-08**. Container logs and Application Signals were already disabled, so ContainerInsights metrics were the addon's only remaining output — and nothing consumed them (0 CloudWatch alarms account-wide, no Grafana CloudWatch datasource, no reference to the namespace in this repo). Basic mode bills per unique metric, which is unbounded in pod count, so ephemeral Argo pods drove it to **$732 over the 2026-09-01..09-07 batch**. Cluster metrics come from Prometheus → Grafana. See the `addons` block in `terraform/eks.tf`.

### Managed node groups (always-on, fixed size)

These run system services and are not used for pipeline workloads.

| Node group | Instance | AMI | Size | Taint | Purpose |
|---|---|---|---|---|---|
| `backend` | `m7g.xlarge` | Bottlerocket ARM64 | 1 node, AZ-pinned to <YOUR_AWS_REGION>a | `CriticalAddonsOnly=true:NoSchedule` | CoreDNS, metrics-server, CloudWatch agent, Kubecost |
| `karpenter` | `m7g.xlarge` | Bottlerocket ARM64 | 1 node | — | Karpenter controller |
| `argo` | `m7g.xlarge` | Bottlerocket ARM64 | 1 node | `argoproj.io/backend=true:NoSchedule` | ArgoCD, Argo server, Prefect |

The `backend` group is pinned to <YOUR_AWS_REGION>a so it is always co-located with the EBS PVCs (Kubecost, Prefect) provisioned in that AZ.

### Karpenter node pools (on-demand provisioning for pipeline workloads)

There are **four** node pools. All are **spot-only** — no pool allows on-demand, and there is no on-demand fallback anywhere in the cluster.

| Node pool | Instance selection | Capacity type | Pool limits | Used by |
|---|---|---|---|---|
| `cpu-light-nodepool` | category `t` | spot | 160 CPU / 640 Gi | Globus control, inventory, S3 sync |
| `cpu-heavy-nodepool` | category `c`,`m`; sizes 2xlarge, 4xlarge; nitro | spot | 2560 CPU / 10240 Gi | BOLD→T1w registration, functional preprocessing |
| `first-level-nodepool` | families `m6gd`/`m7gd`/`r6gd`/`r7gd`/`c6gd`/`c7gd` (**Graviton/ARM64**, local NVMe); sizes xlarge–4xlarge | spot | 512 CPU / 4096 Gi | First-level (task-based) analysis |
| `gpu-nodepool` | families `g4dn`/`g5`/`g6`/`g6e`; sizes xlarge, 2xlarge; nitro; zones <YOUR_AWS_REGION>a/b/c | spot | 512 CPU / 2048 Gi | T1w→MNI registration (FireANTs), FastSurfer template-build + long-segmentation |

The `Pool limits` column is the Karpenter `spec.limits` ceiling on aggregate provisioned capacity — a safety stop, not a reservation and not a statement of what a pool typically runs.

`first-level-nodepool` is the only ARM64 pipeline pool; the `*gd` families are chosen for their local NVMe scratch. Images that run there must be built for ARM64 (see [images.md](images.md)).

**GPU time-slicing.** A `NodeOverlay` (`gpu-timeslice-3x`) advertises **3** schedulable GPU slices per physical GPU, so up to 3 pods share one card. The count is capped by the smallest card in the pool — the g4dn's T4 has 15 GiB — not by the largest. A separate `g6f-fractional-gpu` overlay covers the fractional-GPU g6f family.

All Karpenter nodes consolidate to zero when empty (`consolidateAfter: 10m`). Disruption budgets allow removing up to 20% of nodes at a time.

---

## Storage

There is no shared cluster filesystem. All inter-step data passes through S3 artifacts (see [ADR 004](decisions/004-s3-artifacts-for-inter-step-data.md)); the EFS filesystem, its `efs-sc` StorageClass, and the EFS CSI driver were removed once `subregion-seg` (the last consumer) moved to per-pod `emptyDir`s + S3 checkpointing (GitHub #77).

### EBS

Default StorageClass `ebs-sc` — gp3, encrypted, `WaitForFirstConsumer` binding. Used by Kubecost and Prefect for persistent volumes pinned to <YOUR_AWS_REGION>a.

---

## Databases (RDS PostgreSQL)

Two separate RDS instances share the same subnet group but have independent security groups.

| Instance | Identifier | Class | Database | User | Used by |
|---|---|---|---|---|---|
| Argo | `cloudpipe-argo` | `db.m7g.large` | `argoworkflows` | `argouser` | Argo Workflows persistence (workflow history, artifacts metadata) |
| Prefect | `cloudpipe-prefect` | `db.t4g.micro` | `prefect` | `prefectuser` | Prefect server metadata (flow runs, deployments, work pools) |

The classes differ deliberately. Prefect stores a little flow-run metadata and stays on the burstable `db.t4g.micro`. The Argo instance was upsized to `db.m7g.large` because it takes write traffic from every workflow pod in a batch.

Both instances:
- `storage_type = gp3`, encrypted
- Master password managed natively by RDS (`manage_master_user_password = true`) — password lives in RDS-owned Secrets Manager secret
- CloudWatch log exports: `postgresql`, `upgrade`
- Auto minor version upgrade enabled
- Not publicly accessible; reachable from within the VPC (pods) and, for operators, over the Client VPN
- `deletion_protection = false` (set to `true` before production promotion)

### PgBouncer (Argo RDS only)

PgBouncer was introduced while the Argo instance was still a `db.t4g.micro`, whose `max_connections ≈ 112`: bulk workflow operations (large deletes, parallel status updates from many workflow pods) burst past that ceiling and return `SQLSTATE 53300: too many clients`. PgBouncer caps real server connections at 20 and queues excess client requests.

The instance has since been upsized to `db.m7g.large`, which raises the ceiling considerably — but PgBouncer stays. Connection *count* scales with batch concurrency rather than instance size, so pooling is the durable fix; the upsize addressed throughput, not the connection ceiling.

Argo components never connect to RDS directly — all traffic goes through the `pgbouncer` Service (`pgbouncer:5432` in the `argo-workflows` namespace). See [argo-workflows.md — PgBouncer](argo-workflows.md#pgbouncer-connection-pooler) for configuration details.

### Secrets flow: RDS → pods

External Secrets Operator syncs the RDS-managed password into a Kubernetes Secret at runtime:

```
RDS native secret (Secrets Manager)
  → ClusterSecretStore (external-secrets-clusterstore, ESO Pod Identity)
    → ExternalSecret (argo-db / prefect-db in respective namespaces)
      → Kubernetes Secret consumed by Argo/Prefect pods
```

The `ClusterSecretStore` and `ExternalSecret` resources are gated behind `crds_available = true` — they cannot be applied until ArgoCD has installed the external-secrets CRDs (Phase 5 of `install.sh`).

---

## S3 buckets

| Bucket | Versioned | Purpose |
|---|---|---|
| `<YOUR_S3_BUCKET>` | **No** | Primary data — input BOLD, derivatives, first-level subject CSVs, config files (see architecture.md for key layout), plus archived pod logs under `logs/` |
| `cloudpipe-metrics` | **Yes** | All QC/cost metric records (`metrics/*`) and their compacted Parquet copies |
| `cloudpipe-finops` | — | CUR cost-and-usage reports, Athena query results (`grafana-query-results/`) |
| `cloudpipe-logging` | — | Aggregated log archive: VPC flow logs, CloudTrail, ALB access logs, S3 access logs |
| `cloudpipe-terraform-state` | **Yes** | Terraform remote state (managed by `terraform/bootstrap/`) |

Metrics live on their own **versioned** bucket, deliberately separated from the derivative data: `<YOUR_S3_BUCKET>` derivative prefixes are flushed before each test batch, which previously destroyed QC history along with them. Writes to the retired `<YOUR_S3_BUCKET>/metrics/*` prefix are now actively **denied** by bucket policy (`terraform/abcd_v7_metrics_retire.tf`) so a misconfigured writer fails loudly instead of silently orphaning records from Athena.

Note that `<YOUR_S3_BUCKET>` is **not** versioned — deletes there are unrecoverable without reprocessing. Several earlier data buckets still exist in the account; they are historical and not written by the current pipeline.

`cloudpipe-logging` lifecycle: → Glacier after 90 days → expire after 3 years (satisfies NIST 800-171 3.3.1 log retention).

A gateway VPC endpoint routes all S3 traffic from the VPC through the AWS backbone, avoiding NAT gateway data charges on large transfers.

---

## IAM and Pod Identity

All pod-level AWS permissions use EKS Pod Identity (not IRSA). Each service account has a dedicated IAM role and policy attached via Pod Identity Association.

| Service account | Namespace | Key permissions |
|---|---|---|
| `argo-workflows-controller` | `argo-workflows` | S3 read/write on `<YOUR_S3_BUCKET>` (artifact storage) |
| `argo-workflows-runner` | `argo-workflows` | S3 read/write on `<YOUR_S3_BUCKET>`, SSM read on `/cloudpipe/globus/*`, EC2 start/stop (via globus module), `workflowtaskresults` create/patch |
| `argo-workflows-server` | `argo-workflows` | S3 read on `<YOUR_S3_BUCKET>` (serve archived logs) |
| `prefect-worker` | `prefect` | S3 read/write on `<YOUR_S3_BUCKET>`, SSM read on Globus params; K8s RBAC to create/manage Jobs in `prefect` ns and list/create Workflows in `argo-workflows` ns |
| `ebs-csi-controller-sa` | `aws-ebs-csi-driver` | EBS CSI managed policy |
| `external-dns` | `external-dns` | Route53 record management on `<YOUR_DOMAIN>` zone |
| `external-secrets` | `external-secrets` | Secrets Manager `GetSecretValue` (for ClusterSecretStore) |
| `grafana` | `grafana` | Athena query on `cloudpipe_metrics_workgroup` + Glue read on the `cloudpipe_metrics` catalog/database/tables; S3 read on `cloudpipe-metrics/metrics/*`; S3 read+write on `cloudpipe-finops/grafana-query-results/*` |

A `cloudpipe-metrics-crawler` IAM role also exists in `modules/metrics/iam.tf`, but **no crawler uses it** — the scheduled crawlers were removed on 2026-07-30. It is kept unattached so a one-off crawler against a scratch database is possible during a schema investigation without re-deriving the trust policy. Its presence is not evidence that anything crawls on a schedule. (The one crawler that *does* still run is `cur_report_crawler` in `modules/finops/`, for AWS cost-and-usage reports — unrelated to pipeline metrics.)

Cross-namespace RBAC (defined in `argowf.tf`):
- `prefect-worker` → `argo-workflows` namespace: `argo-workflows-view` ClusterRole (for the concurrency gate's workflow list), custom `prefect-worker-argo-submit` Role (for `submit()`)

---

## Authentication and SSO

ArgoCD uses Dex as an OIDC broker. All three web UIs authenticate through it.

| UI | SSO path |
|---|---|
| ArgoCD | UW-Madison NetID OIDC (`login.<YOUR_INSTITUTION_DOMAIN>`) → Dex |
| Argo Workflows | Dex static client `argo-workflows` → oauth2-proxy |
| Prefect | Dex static client `prefect` → oauth2-proxy |

The admin NetID (set via `var.admin_netid`) is mapped to the `argo-admin` service account in `argo-workflows` namespace, which binds to the `argo-workflows-admin` ClusterRole.

Dex OIDC client credentials for UW-Madison are stored in Secrets Manager and read by Terraform during `argocd.tf` apply.

---

## DNS and TLS

Route53 hosted zone: `<YOUR_DOMAIN>`

Service hostnames are derived from `var.domain` in `locals.tf` — changing the domain variable propagates to all service URLs.

| Hostname | Service |
|---|---|
| `argo.<YOUR_DOMAIN>` | Argo Workflows UI |
| `argocd.<YOUR_DOMAIN>` | ArgoCD UI |
| `prefect.<YOUR_DOMAIN>` | Prefect UI |
| `kubecost.<YOUR_DOMAIN>` | Kubecost |
| `grafana.<YOUR_DOMAIN>` | Grafana (pipeline QC and cost dashboards) |

Two ACM certificates:
- `us-east-1` — required by services that use CloudFront
- `<YOUR_AWS_REGION>` — used by ALB listeners for all four services above

External DNS (running in `external-dns` namespace, managed by ArgoCD) automatically creates Route53 records for Kubernetes Ingress objects.

---

## Observability and logging

| Log stream | Destination | Retention |
|---|---|---|
| EKS control plane (api, audit, authenticator) | CloudWatch log group `/aws/eks/cloudpipe/cluster` | 365 days, KMS encrypted |
| Container Insights **metrics** | *removed 2026-09-08* — cluster metrics come from Prometheus → Grafana | — |
| Container **logs** (pod stdout/stderr) | S3 `<YOUR_S3_BUCKET>/logs/{workflow}/{pod}/main.log` — *not* CloudWatch | Bucket lifecycle |
| VPC flow logs | `cloudpipe-logging/vpc-flow-logs/` | 90d → Glacier → 3y expiry |
| CloudTrail (all regions, all mgmt events + S3 data events on `<YOUR_S3_BUCKET>`) | `cloudpipe-logging/cloudtrail/` | 90d → Glacier → 3y expiry |
| ALB access logs | `cloudpipe-logging/*/AWSLogs/` | 90d → Glacier → 3y expiry |

Pipeline pod logs are **not** forwarded to CloudWatch. Use the Argo UI or `argo logs` to access them while the workflow is running, or the Argo server's S3-backed log archive for completed workflows.

Until 2026-08-11 the addon's fluent-bit DaemonSet *did* also ship every pod's stdout to `/aws/containerinsights/cloudpipe/{application,argo-workflows}`, a second copy of the same bytes that nothing in this repo read. Measured over a 200-subject batch window it ingested ~234 GB/month, ≈$139/month all-in, so `containerLogs.enabled = false` turned it off. If searchable workflow logs are wanted, build them over the S3 archive (Athena or an OpenSearch ingest) — do not re-enable the addon's log path. Note this is unrelated to the control-plane `audit`/`authenticator` streams above, which are a NIST 800-171 control and stay.

Container Insights was removed entirely on 2026-09-08, for the same reason fluent-bit went: nothing read it. The addon had already been trimmed to metrics only, and basic mode's per-metric billing turned out to be the worst possible fit for this workload — it bills per *unique* metric, so every ephemeral Argo pod name minted new billable metrics at $0.30 each. Metric-months/day tracked pod churn 32x across the 2026-09-01..09-07 batch (20.9 idle → 679 peak → 9.9 once drained), costing **$675.78 in metrics plus $56.55 ingesting the performance log group that backed them**. Enhanced (per-observation) mode would have been *cheaper*, being bounded by scrape rate rather than pod count — the earlier note claiming basic was "far cheaper" had this backwards. If ContainerInsights is ever wanted back, use enhanced mode and give it a consumer first.

---

## Globus Connect Server (EC2)

The Globus Connect Server runs on a standalone EC2 instance in a public subnet, separate from EKS.

| Attribute | Value |
|---|---|
| AMI | Ubuntu 22.04 LTS x86_64 (Canonical) |
| Subnet | Public (<YOUR_AWS_REGION>a) |
| Public IP | Elastic IP (fixed — used for endpoint registration) |
| Storage | S3 storage gateway (`globus_use_s3_gateway = true`) — GridFTP writes directly to `<YOUR_S3_BUCKET>` |

Security group inbound rules (mandated by Globus Connect Server v5 architecture):

| Port | Source | Reason |
|---|---|---|
| 443 | `0.0.0.0/0` | HTTPS collections + GCS Manager API (must be world-open) |
| 50000–51000 | `0.0.0.0/0` | GridFTP data channels (peer-to-peer; must be world-open) |
| 22 | `var.globus_admin_prefix_list_id` | SSH admin access (managed prefix list) |

Access control is enforced by Globus OAuth2/OIDC, not network-layer filtering.

SSM parameters set by the `gcs-finalize-setup` script after endpoint creation:

| Parameter | Content |
|---|---|
| `/cloudpipe/globus/instance-id` | EC2 instance ID |
| `/cloudpipe/globus/collection-id` | Destination collection UUID (updated on instance replacement) |
| `/cloudpipe/globus/source-collection-id` | Source collection UUID (DAIRC MMPS endpoint) |
| `/cloudpipe/globus/source-base-path` | Root path on source collection |

The Globus instance is stopped when not actively transferring; `start-globus-instance-template` starts it at the beginning of each workflow and waits for status checks to pass.

---

## Terraform modules

| Module | Path | Manages |
|---|---|---|
| `addons` | `modules/addons` | Pod Identity associations for cert-manager, External Secrets, AWS LBC |
| `argo-workflows` | `modules/argo-workflows` | RDS instance, IAM/Pod Identity, RBAC, network policies, metrics; PgBouncer Deployment + Service in `gitops/apps/argo-workflows/templates/` |
| `finops` | `modules/finops` | Kubecost Helm release, Athena CUR table, IAM for cost data |
| `globus` | `modules/globus` | EC2 instance, EIP, security group, IAM role, SSM parameters |
| `karpenter` | `modules/karpenter` | Karpenter Helm release, NodePool/NodeClass manifests |
| `metrics` | `modules/metrics` | Glue catalog database + 9 raw and 9 compacted hand-declared catalog tables (**no crawlers**), Athena `cloudpipe_metrics_workgroup`, Grafana Pod Identity |
| `prefect` | `modules/prefect` | RDS instance, IAM/Pod Identity, RBAC, network policies |

---

## Bootstrap and install sequence

A fresh cluster install follows the phased sequence in `install.sh`. Do not run `terraform apply` directly on a new cluster — the phases are load-bearing:

| Phase | What happens |
|---|---|
| 1 | VPC + EKS with public endpoint enabled (bootstrapping requires reachable API) |
| 2 | EKS add-ons, Pod Identity, Karpenter, ArgoCD, service modules |
| 3 | kube-system NetworkPolicies applied before strict VPC CNI mode |
| 4 | Full apply (`crds_available=false`) — VPN created here |
| 5 | Wait for ArgoCD to sync and install CRDs (external-secrets, Prometheus) |
| 6 | Final apply: `crds_available=true`, `vpc_cni_strict_mode=true`, public endpoint disabled |

After Phase 6 the EKS API is private-only — connect via VPN for all subsequent `kubectl`/`terraform` operations.

Both scripts share their `-target` lists via `terraform/targets.sh`, which also validates every
address against the `.tf` sources before Terraform is invoked. `terraform apply -target=` on an
address declared nowhere is a hard error, not a no-op, so one stale entry used to abort the
bootstrap partway through — which is what a deleted-but-still-referenced
`module.aws_efs_csi_pod_identity` did for three months ([#211](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/211)),
unnoticed because the running cluster predates the removal and never re-runs the bootstrap. The
preflight reports *every* bad address at once instead of failing at the first; cleanup.sh derives
its teardown order by reversing the same list rather than keeping a second copy.

> **The check is a grep of the `.tf` files, not `terraform plan -target=`.** Plan would be
> authoritative, but it instantiates the kubernetes/helm providers, which at bootstrap time must
> reach a cluster that does not exist yet — the reason these applies are phased at all. So the
> preflight catches an undeclared address, not every way a target can be wrong.

### Key Terraform variables

| Variable | Default | Change when |
|---|---|---|
| `endpoint_public_access` | `false` | Set `true` only during `install.sh` bootstrap |
| `crds_available` | `false` | Set `true` after ArgoCD has installed CRDs (Phase 5) |
| `vpc_cni_strict_mode` | `false` | Set `true` after kube-system NetworkPolicies are in place (Phase 6) |
| `admin_netid` | — | NetID (without @<YOUR_INSTITUTION_DOMAIN>) granted ArgoCD + Argo admin access |
| `globus_client_id` | — | Globus service account app client ID (no default — must be provided) |
| `globus_s3_destination_bucket` | `<YOUR_S3_BUCKET>` | Change to target a different S3 bucket |
| `kubernetes_version` | `1.35` | Bump for EKS version upgrades |
| `domain` | `<YOUR_DOMAIN>` | Change for different deployment environments |
