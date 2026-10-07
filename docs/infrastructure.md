# CloudPipe Infrastructure

All infrastructure lives in `terraform/`. Run all Terraform commands from within that directory — never use `-chdir=terraform`.

## Root and stack module

The root is **only** what a module may not contain: the S3 backend, the provider configurations, the variable declarations `terraform.tfvars` binds to, and the `moved` blocks recording the extraction. Everything this project manages is in `terraform/modules/stack/`, called once from `terraform/main.tf`.

| Root file | Why it cannot move into the module |
|---|---|
| `versions.tf` | A module declares no `backend`. |
| `providers.tf` | A module configures no provider. What is left here is only what a `provider` block needs before any module runs — the cluster endpoint and the ECR Public token. Every other data source moved with the resources that read it. |
| `variables.tf` | The declarations `terraform.tfvars` binds to. The module declares the same 51 names; `main.tf` passes each one through, which `tests/test_stack_module_extraction.py` checks in both directions. |
| `main.tf` | The `module "stack"` call. Aliased providers (`aws.us_east_1`, `aws.billing`) are passed explicitly — Terraform hands a module its default provider configurations automatically but never an aliased one. |
| `outputs.tf` | An output of a child module is not an output of the root, so each one is re-exported under the same name. |
| `moved_to_stack.tf` | `moved` blocks may only be written in the *calling* module. **Keep them.** They are a permanent record: deleting them makes any state that has not been migrated — another deployment's, a restored backup — plan a destroy and recreate. |
| `abcd_v7_metrics_retire.tf` | A history file, kept in the root by design; it reads one variable and nothing else. |

Why one module rather than several: Terraform can move addresses into a child module only through `moved` blocks in the caller, so a partial extraction leaves cross-referencing resources split across two modules for no safety gain. See design D4 of `openspec/changes/archive/2026-10-01-public-upstream-readiness`.

### The example root

`terraform/modules/stack/example/` is a second, minimal root that calls the same module: `main.tf`, `terraform.tfvars.example`, a README and a copy of the root's `.terraform.lock.hcl`. It is what an outside deployment starts from, and it has to live *under* `terraform/modules` because that — not `terraform/` — is what the sync publishes. It is not deployed by anything; nothing in it is production configuration.

CI runs `terraform init -backend=false -lockfile=readonly && terraform validate` in it, next to the same two steps for the real root. That is the guard against the module's interface moving without the example following it: an input that loses its default, a renamed output, a new provider alias. `tests/test_stack_example_root.py` covers what validate cannot see — a required input the example never declares is a *prompt*, not an error, until `-input=false`.

Its lock file is a byte-for-byte copy of the root's, asserted by that test. After a provider upgrade, copy the root's lock over the example's in the same commit, or CI's `-lockfile=readonly` init fails there.

### Variables that must be set

**Eighteen** variables have **no default**, because a default is one deployment's value handed to everyone who does not set it — and nothing errors. A fork inheriting `domain` publishes services under a domain it does not own; one inheriting `github_oidc_allowed_subs` trusts this repository's workflows to assume its roles. Without a default, Terraform prompts, or fails under `-input=false` naming the variable.

The five Globus identity variables — `globus_client_id`, `globus_org_name`, `globus_contact_email`, `globus_owner_email`, `globus_identity_domain` — are rendered by `globus init`. The other thirteen are below, because their defaults would be a claim about who is deploying. Both `terraform.tfvars.example` files are checked against the full eighteen, derived from `variables.tf` rather than listed, so a nineteenth is covered without anyone editing a test.

| Group | Variables |
|---|---|
| Identity | `domain` (may be `null`, for port-forward mode — see [deployer-first-hour.md](deployer-first-hour.md#reaching-the-web-uis-without-a-domain)), `operator_emails` |
| AWS | `region`, `cloudflare_account_id`, `globus_admin_prefix_list_id` |
| Source control | `github_user_url`, `github_repo`, `gitops_repo_url`, `github_oidc_allowed_subs` |
| Data | `globus_s3_destination_bucket`, `globus_source_collection_id` |
| Cloudflare Zero Trust | `cloudflare_team_domain`, `cloudflare_team_name` |

`operator_emails` is who runs the deployment: the administrators of every web UI, and — with the Amazon Cognito user pool the stack creates by default — who may enroll a WARP device and reach the cluster.

All thirteen carry a `validation` block except `cloudflare_team_name`, which Cloudflare generates, so there is no shape to check. `tests/test_terraform_required_variables.py` keeps the facts aligned: no default, a validation block, and a line in the example file for each one the root does not compute itself.

Two placeholders in the example root's `terraform.tfvars.example` are deliberately **not** well-formed. `globus_source_collection_id` and `globus_client_id` are UUIDs, that file is published, and the publish gate fails on any 8-4-4-4-12 hex string — a valid-looking placeholder is indistinguishable from a real collection identifier both to the gate and to a reader. Terraform's `validation` rejects them until they are replaced, which is the intended failure.

Everything else keeps its default on purpose. `prefect_namespace` and `vpc_cidr` are sane starting values, not claims about who is deploying.

---

## Terraform file map

Paths below are relative to `terraform/modules/stack/` unless stated otherwise.

| File / directory | Manages |
|---|---|
| `eks.tf` | EKS cluster, managed node groups, KMS keys, CloudWatch log group |
| `vpc.tf` | VPC, subnets, NAT gateway, S3 VPC endpoint, VPC flow logs |
| `vpn.tf` | AWS Client VPN endpoint, client certificates |
| `addons.tf` | Pod Identity associations for EBS CSI, External DNS, CloudWatch agent |
| `karpenter.tf` | Karpenter Helm release (via module) |
| `storage.tf` | StorageClass (`ebs-sc`) |
| `argowf.tf` | Argo Workflows SSO secrets, RBAC, Prefect→Argo cross-namespace bindings |
| `argocd.tf` | ArgoCD Helm release, Dex OIDC config, bootstrap ApplicationSet |
| `prefect.tf` | Prefect module call, oauth2-proxy secret |
| `globus.tf` | Globus module call |
| `finops.tf` | Kubecost + Athena CUR module |
| `metrics.tf` | Pipeline observability module (Glue database + hand-declared catalog tables, Athena workgroup, Grafana Pod Identity) |
| `metrics_bucket.tf` | The `cloudpipe-metrics` bucket (versioned) that holds all QC/cost records |
| `grafana.tf` | Grafana's Terraform-owned Secrets (Dex SSO client, local admin, image-renderer token) + OIDC ConfigMap. The release itself, its Ingress and the image renderer are GitOps (`gitops/apps/grafana/`) |
| `logging.tf` | S3 log bucket, CloudTrail |
| `dns.tf` | Route53 zone lookup, ACM certificates (us-east-1 + the deployment region) |
| `ecr.tf` | ECR private repositories (primary registry for pipeline images) |
| `s3_lifecycle.tf` | S3 lifecycle rules for data bucket |
| `kube-system-network-policy.tf` | Default-deny/allow network policies in `kube-system` |
| `locals.tf` | Derived locals — service URLs, VPC DNS resolver |
| `variables.tf` | All input variables with defaults (the root declares the same names and passes them in) |
| `data.tf` | The account/partition/AZ lookups the stack's resources read; it was the root's "common data" block |
| `example/` | A minimal root that calls this module, for an outside deployment to copy. Validated by CI, deployed by nothing — see [The example root](#the-example-root) |
| `../<name>/` | Reusable sub-modules, siblings of `stack/` (see module reference below) |
| `terraform/versions.tf` + `terraform/providers.tf` | Provider pins, AWS provider aliases, S3 backend config — in the root, see above |
| `terraform/abcd_v7_metrics_retire.tf` | Bucket policy denying writes to the data bucket's retired `metrics/*` prefix — in the root |
| `bootstrap/` | Separate Terraform root that creates the `cloudpipe-terraform-state` S3 bucket itself — its own local backend, not part of the main stack's state. Rarely touched; see ADR 015. |

---

## State management

Terraform state lives in the `cloudpipe-terraform-state` S3 bucket (versioned, SSE-encrypted, public access blocked), configured via the `backend "s3"` block in the root's `versions.tf` with native state locking (`use_lockfile = true`, requires Terraform >= 1.10). This bucket is itself managed by a separate, small Terraform config in `terraform/bootstrap/` — deliberately kept off the main stack's backend to avoid a chicken-and-egg dependency.

This replaced a local-only backend (no remote state at all) after a 2026-07 incident where the machine holding the only state file was lost, requiring a full `terraform import` recovery of the entire stack. See ADR 015 for the incident writeup, what was recovered, two pieces of pre-existing infrastructure drift it surfaced (Globus AMI pinning, Karpenter `expireAfter`), and a list of diffs that are now permanent/expected rather than bugs.

---

## VPC and networking

| Resource | Value |
|---|---|
| Primary CIDR | `10.0.0.0/16` — the only CIDR in use. `var.secondary_cidr_blocks` is declared, and the VPN has routes/auth rules that iterate over it, but `vpc.tf` never associates it, so it is empty in practice. |
| Private subnets | Three AZs (`a`/`b`/`c` in the deployment region), `/20` each — EKS nodes. Upper 3/4 of each is a `prefix` CIDR reservation for pod prefix delegation, see [pod IP address space](#pod-ip-address-space) |
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
   200-subject batch (GitHub #218): the `c` zone had 113 nodes running just 268 pods
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

   Verified live on 2026-08-11: a fresh Karpenter node in the `c` zone took primary
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

### Remote access (Cloudflare WARP)

The EKS API (no public endpoint in steady state) and all five web UIs are **private**: nothing is reachable from the internet. Operators connect the Cloudflare WARP client, enrolled with an SSO login (MFA required), before using `kubectl`, `argo`, the web UIs, `pixi run prefect-deploy`, or the Kubecost scripts. See ADR 014 and plans 010/012.

- **Path:** WARP → Cloudflare Gateway → tunnel `cloudpipe-eks` → `cloudflared` (two replicas, `gitops/apps/cloudflared/`) → the three private `/20`s, which the tunnel routes.
- **Who:** one Access application, `private_services` (`terraform/modules/stack/cloudflare.tf`), covers TCP 443 and 80 on those subnets, gated by the `cluster_admins` policy. Adding a person there grants both `kubectl` reachability and the UIs; each UI still logs in separately through Dex.
- **Which traffic:** the device profile's split tunnel is in **Include** mode for `var.vpc_cidr` only; everything else stays on the local network.
- **Session:** the WARP identity lasts 24h. When it lapses, `kubectl` shows a *TLS handshake timeout* → `warp-cli debug access-reauth`.
- **Web UIs:** served by one internal ALB — see [DNS and TLS](#dns-and-tls).

### Client VPN (fallback, pending decommission)

AWS Client VPN predates WARP and still works as a fallback: it source-NATs clients into the VPC CIDR, which the EKS API and the shared UI ALB's security groups trust. It is scheduled for removal after a soak with the VPN disconnected (#359, plan 012 §6).

- Client CIDR: `10.3.0.0/22`
- **Full tunnel** (`var.split_tunnel` defaults to `false`). This was load-bearing while the web UIs had **public** ALBs: with split tunnel, traffic to their public IPs never entered the VPN, so it was never NAT'd into an address the ALB security groups trusted. With the UIs on an internal ALB, split tunnel would also work, but the setting is left alone until the VPN is removed.
- Certificates: managed by Terraform (`certificate_validity_period_hours = 8760`, i.e. 1 year)

---

## EKS cluster

| Attribute | Value |
|---|---|
| Name | `cloudpipe` |
| Region | `var.region` |
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

`amazon-cloudwatch-observability` was **removed on 2026-09-08**. Container logs and Application Signals were already disabled, so ContainerInsights metrics were the addon's only remaining output — and nothing consumed them (0 CloudWatch alarms account-wide, no Grafana CloudWatch datasource, no reference to the namespace in this repo). Basic mode bills per unique metric, which is unbounded in pod count, so ephemeral Argo pods drove it to **$732 over the 2026-09-01..09-07 batch**. Cluster metrics come from Prometheus → Grafana. See the `addons` block in `terraform/modules/stack/eks.tf`.

### Managed node groups (always-on, fixed size)

These run system services and are not used for pipeline workloads.

| Node group | Instance | AMI | Size | Taint | Purpose |
|---|---|---|---|---|---|
| `backend` | `m7g.xlarge` | Bottlerocket ARM64 | 1 node, pinned to the region's `a` zone | `CriticalAddonsOnly=true:NoSchedule` | CoreDNS, metrics-server, CloudWatch agent, Kubecost |
| `karpenter` | `m7g.xlarge` | Bottlerocket ARM64 | 1 node | — | Karpenter controller |
| `argo` | `m7g.xlarge` | Bottlerocket ARM64 | 1 node | `argoproj.io/backend=true:NoSchedule` | ArgoCD, Argo server, Prefect |

The `backend` group is pinned to the `a` zone so it is always co-located with the EBS PVCs (Kubecost, Prefect) provisioned in that AZ.

### Karpenter node pools (on-demand provisioning for pipeline workloads)

There are **five** node pools. All are **spot-only** — no pool allows on-demand, and there is no on-demand fallback anywhere in the cluster.

| Node pool | Instance selection | Capacity type | Pool limits | Used by |
|---|---|---|---|---|
| `cpu-light-nodepool` | category `t` | spot | 160 CPU / 640 Gi | Globus control, inventory, S3 sync |
| `cpu-heavy-nodepool` | category `c`,`m`; sizes 2xlarge, 4xlarge; nitro | spot | 2560 CPU / 10240 Gi | BOLD→T1w registration, functional preprocessing |
| `first-level-nodepool` | families `m6gd`/`m7gd`/`r6gd`/`r7gd`/`c6gd`/`c7gd` (**Graviton/ARM64**, local NVMe); sizes xlarge–4xlarge | spot | 512 CPU / 4096 Gi | First-level (task-based) analysis |
| `gpu-nodepool` | families `g4dn`/`g5`/`g6`/`g6e`; sizes xlarge, 2xlarge, **except** `g5`/`g6`/`g6e.2xlarge`; nitro; zones `a`/`b`/`c`; weight 10 (preferred) | spot | 512 CPU / 2048 Gi | T1w→MNI registration (FireANTs), FastSurfer template-build + long-segmentation |
| `gpu-dense-nodepool` | types `g5.2xlarge`/`g6.2xlarge`/`g6e.2xlarge`; nitro; zones `a`/`b`/`c`; no weight (fallback) | spot | 512 CPU / 2048 Gi | The same three GPU steps, when every `gpu-nodepool` offering is out of capacity |

The `Pool limits` column is the Karpenter `spec.limits` ceiling on aggregate provisioned capacity — a safety stop, not a reservation and not a statement of what a pool typically runs.

`first-level-nodepool` is the only ARM64 pipeline pool; the `*gd` families are chosen for their local NVMe scratch. Images that run there must be built for ARM64 (see [images.md](images.md)).

**GPU time-slicing.** Slice counts are set per GPU pool. `gpu-nodepool` nodes carry **3** schedulable GPU slices per physical GPU: its smallest card, the g4dn's T4, has 15 GiB, and an xlarge has only ~3920m of CPU for `cpu: 1` pods. `gpu-dense-nodepool` nodes carry **4**. Those are 2xlarge nodes with a 22–45 GiB card, and 4 is the most that fits the largest per-pod VRAM: t1w-to-mni, measured at 4903 MiB per process. The pool shipped at 5, sized from an estimate of ~4.4 GiB, and was cut to 4 once the measurement showed four t1w-to-mni pods on one card would overflow. The count reaches Karpenter through a `NodeOverlay` per pool's types (`gpu-timeslice-3x`, and `gpu-dense-timeslice-4x` at weight 10). It reaches each node through the device-plugin profile selected by the `nvidia.com/device-plugin.config` label, which only the dense pool sets. Because overlays match instance types in every pool, the two pools must never admit the same type; `tests/argo/test_gpu_step_resources.py` enforces that and the other invariants. The GPU templates accept either pool through a `karpenter.sh/nodepool In` node affinity. A separate `g6f-fractional-gpu` overlay covers the fractional-GPU g6f family, which no pool currently admits.

All Karpenter nodes consolidate to zero when empty (`consolidateAfter: 10m`). Disruption budgets allow removing up to 20% of nodes at a time.

---

## Storage

There is no shared cluster filesystem. All inter-step data passes through S3 artifacts (see ADR 004); the EFS filesystem, its `efs-sc` StorageClass, and the EFS CSI driver were removed once `subregion-seg` (the last consumer) moved to per-pod `emptyDir`s + S3 checkpointing (GitHub #77).

### EBS

Default StorageClass `ebs-sc` — gp3, encrypted, `WaitForFirstConsumer` binding. Used by Kubecost and Prefect for persistent volumes pinned to the `a` zone.

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

The `ClusterSecretStore` and `ExternalSecret` resources are gated behind `crds_available = true` — they cannot be applied until ArgoCD has installed the external-secrets CRDs (Phase 5 of `install.sh`). The flag is persisted in `terraform/install-state.auto.tfvars`; an apply that does not read it plans to **destroy** all of them (#635).

---

## S3 buckets

| Bucket | Versioned | Purpose |
|---|---|---|
| Data bucket (`var.globus_s3_destination_bucket`) | **No** | Primary data — input BOLD, derivatives, first-level subject CSVs, config files (see architecture.md for key layout), plus archived pod logs under `logs/` |
| `cloudpipe-metrics` | **Yes** | All QC/cost metric records (`metrics/*`) and their compacted Parquet copies |
| `cloudpipe-finops` | — | CUR cost-and-usage reports, Athena query results (`grafana-query-results/`) |
| `cloudpipe-logging` | — | Aggregated log archive: VPC flow logs, CloudTrail, ALB access logs, S3 access logs |
| `cloudpipe-terraform-state` | **Yes** | Terraform remote state (managed by `terraform/bootstrap/`) |

Metrics live on their own **versioned** bucket, deliberately separated from the derivative data: the data bucket's derivative prefixes are flushed before each test batch, which previously destroyed QC history along with them. Writes to its retired `metrics/*` prefix are now actively **denied** by bucket policy (`terraform/abcd_v7_metrics_retire.tf`) so a misconfigured writer fails loudly instead of silently orphaning records from Athena.

Note that the data bucket is **not** versioned — deletes there are unrecoverable without reprocessing. Several earlier data buckets still exist in the account; they are historical and not written by the current pipeline.

`cloudpipe-logging` lifecycle: → Glacier after 90 days → expire after 3 years (satisfies NIST 800-171 3.3.1 log retention).

A gateway VPC endpoint routes all S3 traffic from the VPC through the AWS backbone, avoiding NAT gateway data charges on large transfers.

---

## IAM and Pod Identity

All pod-level AWS permissions use EKS Pod Identity (not IRSA). Each service account has a dedicated IAM role and policy attached via Pod Identity Association.

| Service account | Namespace | Key permissions |
|---|---|---|
| `argo-workflows-controller` | `argo-workflows` | S3 read/write on the data bucket (artifact storage) |
| `argo-workflows-runner` | `argo-workflows` | S3 read/write on the data bucket, SSM read on `/cloudpipe/globus/*`, EC2 start/stop (via globus module), `workflowtaskresults` create/patch |
| `argo-workflows-server` | `argo-workflows` | S3 read on the data bucket (serve archived logs) |
| `prefect-worker` | `prefect` | S3 read/write on the data bucket, SSM read on Globus params; K8s RBAC to create/manage Jobs in `prefect` ns and list/create Workflows in `argo-workflows` ns |
| `ebs-csi-controller-sa` | `aws-ebs-csi-driver` | EBS CSI managed policy |
| `external-dns` | `external-dns` | Route53 record management on the `var.domain` zone |
| `external-secrets` | `external-secrets` | Secrets Manager `GetSecretValue` (for ClusterSecretStore) |
| `grafana` | `grafana` | Athena query on `cloudpipe_metrics_workgroup` + Glue read on the `cloudpipe_metrics` catalog/database/tables; S3 read on `cloudpipe-metrics/metrics/*`; S3 read+write on `cloudpipe-finops/grafana-query-results/*` |

A `cloudpipe-metrics-crawler` IAM role also exists in `modules/metrics/iam.tf`, but **no crawler uses it** — the scheduled crawlers were removed on 2026-07-30. It is kept unattached so a one-off crawler against a scratch database is possible during a schema investigation without re-deriving the trust policy. Its presence is not evidence that anything crawls on a schedule. (The one crawler that *does* still run is `cur_report_crawler` in `modules/finops/`, for AWS cost-and-usage reports — unrelated to pipeline metrics.)

Cross-namespace RBAC (defined in `argowf.tf`):
- `prefect-worker` → `argo-workflows` namespace: `argo-workflows-view` ClusterRole (for the concurrency gate's workflow list), custom `prefect-worker-argo-submit` Role (for `submit()`)

---

## Authentication and SSO

By default every sign-in goes through the Amazon Cognito user pool the stack creates (`cognito.tf`), with an authenticator app required as a second factor. A deployment that integrates an identity provider directly supplies it through the module's `external_identity` input instead, and the UIs then sign in through ArgoCD's Dex.

| UI | Cognito mode (default) | `external_identity` set |
|---|---|---|
| ArgoCD | Dex, with the pool as its upstream | Dex, with the external provider as its upstream |
| Argo Workflows | the pool's `argo-workflows` client | Dex static client `argo-workflows` |
| Prefect (oauth2-proxy) | the pool's `prefect` client | Dex static client `prefect` |
| Grafana | the pool's `grafana` client | Dex static client `grafana` |

In both modes the addresses in `var.operator_emails` hold the administrator role: ArgoCD's `role:admin`; the `argo-admin` service account in the `argo-workflows` namespace, which binds to the `argo-workflows-admin` ClusterRole; and Grafana's Admin role. The sign-in proxies admit only those addresses' email domains.

---

## DNS and TLS

Route53 hosted zone: `var.domain`, written `<domain>` below.

Service hostnames are derived from `var.domain` in `locals.tf` — changing the domain variable propagates to all service URLs.

| Hostname | Service |
|---|---|
| `argo.<domain>` | Argo Workflows UI |
| `argocd.<domain>` | ArgoCD UI |
| `prefect.<domain>` | Prefect UI |
| `kubecost.<domain>` | Kubecost |
| `grafana.<domain>` | Grafana (pipeline QC and cost dashboards) |

Two ACM certificates:
- `us-east-1` — required by services that use CloudFront
- the deployment region — the `*.<domain>` wildcard, used by the web-UI ALB's HTTPS listener for all five hostnames above

External DNS (running in `external-dns` namespace, managed by ArgoCD) automatically creates Route53 records for Kubernetes Ingress objects.

### Web-UI load balancer

All five hostnames are aliases for **one `internal` ALB**, shared through the AWS Load Balancer Controller IngressGroup `cloudpipe-ui` (#360, plan 012). Each UI keeps its own Ingress (host rule, backend, health check); the ALB-level settings are shared.

- **Group-level annotations must be byte-identical on every member**, or the controller stops reconciling the *whole* ALB. Four members are Terraform (`argocd.tf` and the `argo-workflows`, `prefect`, `finops` modules), which all merge `local.ui_alb_group_annotations` from `terraform/modules/stack/ui_alb.tf`. Grafana's Ingress is GitOps-managed (`gitops/apps/grafana/values.yaml`) and repeats the same values; a precondition on the ArgoCD Ingress **fails `terraform plan`** if they differ, so change both together. That precondition reads `values.yaml` merged under Grafana's entry in `local.argocd_app_overrides`, because the hostname annotation comes from Terraform now and either source can carry an annotation — reading the file alone would let the `wafv2-acl-arn` check pass on an Ingress that does set the key.
- **Security group** `cloudpipe-ui-alb-sg` (`ui_alb.tf`) admits 443/80 from `var.vpc_cidr` only. Members reference it by its Name tag, because Grafana's values cannot take a Terraform ID.
- **DNS:** the records live in the public zone and resolve to the ALB's private IPs, both from the internet and from a WARP client — no split-horizon zone. In-cluster callers (Argo Workflows, Prefect and Grafana reaching Dex at `argocd.<domain>`) resolve the same way and reach the ALB directly inside the VPC.
- **Access logs:** one prefix, `alb-ui/`, in `cloudpipe-logging-access`. Filter per UI on each log line's `domain_name` field.
- **Deletion protection** is on, via `deletion_protection.enabled=true` in the shared `load-balancer-attributes` (Security Hub control **ELB.6**). The controller can no longer delete the ALB, which changes teardown — see below.
- **WAF:** the regional WAFv2 web ACL `cloudpipe-ui-alb` (`terraform/modules/stack/waf.tf`, Security Hub control **ELB.16**) is associated by the `wafv2-acl-arn` annotation on the **ArgoCD Ingress alone** — it is the one group-level setting the other four members do not repeat, because the ARN is Terraform-generated and Grafana's values cannot hardcode it. **Both managed rule groups block.** Known Bad Inputs was verified live 2026-09-23, a Log4j JNDI canary returns 403; the Core rule set was promoted out of Count on 2026-09-24 and verified the same way, an XSS canary on `/waf-canary-core` returning 403 where the same path without the payload still returns 404. Four Core rules stay at Count **permanently** — `SizeRestrictions_BODY`, `SizeRestrictions_QUERYSTRING`, `CrossSiteScripting_BODY` and `GenericRFI_BODY` — because normal admin-UI use trips them: Grafana panel queries POST whole dashboard JSON, which runs past WAF's 8 KB body-inspection cap. Check for them in the API response as `RuleActionOverrides`, not just for the group-level override, since an ACL that reads as promoted while those four have been flattened would 403 every dashboard load. Only BLOCK and COUNT records are logged, to `aws-waf-logs-cloudpipe-ui`, with the `authorization` and `cookie` headers redacted. Because plain allows are not logged, **an empty log group does not mean a clean review** — it is equally consistent with broken delivery, so the promotion procedure in `terraform/modules/stack/waf.tf` starts by sending canaries to establish a positive control, and carries the reviewed Logs Insights query.
- **Teardown:** the ALB is deleted only once every member Ingress is gone. `terraform/cleanup.sh` stops the ArgoCD controllers first so the Grafana Ingress is not recreated, deletes the Ingresses, then clears deletion protection on each pass of its wait loop before checking the `ingress.k8s.aws/stack=cloudpipe-ui` tag. Clearing protection has to happen *after* the Ingresses are deleted: while the group is non-empty the controller re-applies the attribute on every reconcile. Deleting the ALB by hand needs the same `modify-load-balancer-attributes` call first.

---

## Observability and logging

| Log stream | Destination | Retention |
|---|---|---|
| EKS control plane (api, audit, authenticator) | CloudWatch log group `/aws/eks/cloudpipe/cluster` | 365 days, KMS encrypted |
| Container Insights **metrics** | *removed 2026-09-08* — cluster metrics come from Prometheus → Grafana | — |
| Container **logs** (pod stdout/stderr) | S3 `<bucket>/logs/{workflow}/{pod}/main.log` — *not* CloudWatch | Bucket lifecycle |
| VPC flow logs | `cloudpipe-logging/vpc-flow-logs/` | 90d → Glacier → 3y expiry |
| CloudTrail (all regions, all mgmt events + S3 data events on the data bucket) | `cloudpipe-logging/cloudtrail/` | 90d → Glacier → 3y expiry |
| ALB access logs | `cloudpipe-logging/*/AWSLogs/` | 90d → Glacier → 3y expiry |
| Web-UI WAF (BLOCK/COUNT records only; `authorization` and `cookie` redacted) | CloudWatch log group `aws-waf-logs-cloudpipe-ui` | 365 days, KMS encrypted |

Pipeline pod logs are **not** forwarded to CloudWatch. Use the Argo UI or `argo logs` to access them while the workflow is running, or the Argo server's S3-backed log archive for completed workflows.

Until 2026-08-11 the addon's fluent-bit DaemonSet *did* also ship every pod's stdout to `/aws/containerinsights/cloudpipe/{application,argo-workflows}`, a second copy of the same bytes that nothing in this repo read. Measured over a 200-subject batch window it ingested ~234 GB/month, ≈$139/month all-in, so `containerLogs.enabled = false` turned it off. If searchable workflow logs are wanted, build them over the S3 archive (Athena or an OpenSearch ingest) — do not re-enable the addon's log path. Note this is unrelated to the control-plane `audit`/`authenticator` streams above, which are a NIST 800-171 control and stay.

Container Insights was removed entirely on 2026-09-08, for the same reason fluent-bit went: nothing read it. The addon had already been trimmed to metrics only, and basic mode's per-metric billing turned out to be the worst possible fit for this workload — it bills per *unique* metric, so every ephemeral Argo pod name minted new billable metrics at $0.30 each. Metric-months/day tracked pod churn 32x across the 2026-09-01..09-07 batch (20.9 idle → 679 peak → 9.9 once drained), costing **$675.78 in metrics plus $56.55 ingesting the performance log group that backed them**. Enhanced (per-observation) mode would have been *cheaper*, being bounded by scrape rate rather than pod count — the earlier note claiming basic was "far cheaper" had this backwards. If ContainerInsights is ever wanted back, use enhanced mode and give it a consumer first.

---

## Globus Connect Server (EC2)

The Globus Connect Server runs on a standalone EC2 instance in a public subnet, separate from EKS.

| Attribute | Value |
|---|---|
| AMI | Ubuntu 22.04 LTS x86_64 (Canonical) |
| Subnet | Public (`a` zone) |
| Public IP | Elastic IP (fixed — used for endpoint registration) |
| Storage | S3 storage gateway — GridFTP writes directly to the data bucket, with no staging volume |

Security group inbound rules (mandated by Globus Connect Server v5 architecture):

| Port | Source | Reason |
|---|---|---|
| 443 | `0.0.0.0/0` | HTTPS collections + GCS Manager API (must be world-open) |
| 50000–51000 | `0.0.0.0/0` | GridFTP data channels (peer-to-peer; must be world-open) |
| 22 | `var.globus_admin_prefix_list_id` | SSH admin access (managed prefix list) |

Access control is enforced by Globus OAuth2/OIDC, not network-layer filtering.

SSM parameters the workflows read:

| Parameter | Content |
|---|---|
| `/cloudpipe/globus/instance-id` | EC2 instance ID (Terraform) |
| `/cloudpipe/globus/collection-id` | Destination collection UUID (recorded by `globus configure`; unchanged by instance replacement) |
| `/cloudpipe/globus/source-collection-id` | Source collection UUID (NBDC Data Hub collection) |
| `/cloudpipe/globus/source-base-path` | Root path on source collection |

The full list, with what sets each one, is in
[globus.md → SSM parameters](globus.md#ssm-parameters).

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

A fresh cluster install follows the phased sequence in `install.sh`. That script drives the reference deployment's own Terraform root and is not yet part of the published tree, so if you are deploying your own copy, work the table below by hand — every phase, in order. Do not run `terraform apply` directly on a new cluster either way; the phases are load-bearing.

Every `-target` address below is written as it reads **inside** the stack module. From a root that calls the stack as `module "stack"`, `module.vpc` is `-target=module.stack.module.vpc`; if you named your module call something else, use that name.

**Before Phase 1**, confirm `CLOUDFLARE_API_TOKEN` is exported (`pixi run cloudpipe preflight` checks it). It is first needed by the Phase 4 full apply, and failing there leaves a half-built cluster. With the default Cognito identity, no identity-provider secret is created by hand: Terraform creates the pool's clients and passes their secrets on.

**Before Phase 8**, create at least one Cognito user — the commands, and what the operator then does at their first sign-in, are in [deployer-first-hour.md → create the operators' sign-in accounts](deployer-first-hour.md#step-8--create-the-operators-sign-in-accounts). Phase 8 closes the public EKS endpoint, after which the cluster is reached only over WARP, and nobody can sign in to WARP until the pool has a user. `install.sh` checks this and stops before Phase 8 if the pool is empty.

| Phase | What happens | Public endpoint |
|---|---|---|
| 1 | `-target` the VPC, then EKS, with `endpoint_public_access=true` (bootstrapping needs a reachable API). Then `aws eks update-kubeconfig`, log Helm in to ECR Public, and pre-create the `argo-workflows` namespace — Phase 2's resources need it before ArgoCD has synced anything | open |
| 2 | `-target` each add-on module in turn: the EBS CSI and external-dns Pod Identity modules, Karpenter, add-ons, Argo Workflows, Globus, FinOps | open |
| 3 | `-target` the kube-system NetworkPolicies, **before** strict VPC CNI mode — strict mode blocks every pod with no policy, CoreDNS included, and the cluster deadlocks | open |
| 4 | Full apply, `crds_available=false`. Creates the Cloudflare tunnel, its Access applications, and the VPN. **Then sync the tunnel token** — see below | open |
| 5 | Wait for ArgoCD to sync and install the CRDs (`clustersecretstores.external-secrets.io`, `prometheusrules.monitoring.coreos.com`), and for each to report `Established` | open |
| 6 | Write `install-state.auto.tfvars` (see below), then apply with `crds_available=true`, `vpc_cni_network_policy_enabled=true` and `vpc_cni_strict_mode=true`, **endpoint still open**. This creates the External Secrets `ClusterSecretStore`, without which cloudflared cannot receive its token | open |
| 7 | **Prove the tunnel before closing anything** — see below. If it is not healthy, stop here: the cluster stays reachable through the IAM-gated public endpoint | open |
| 8 | Apply with the same three flags and no `endpoint_public_access`, which disables the public endpoint. Then refresh the kubeconfig | **closed** |

After Phase 8 the EKS API is private-only. Reach it through the Cloudflare tunnel (WARP), or the VPN as a fallback, for every later `kubectl` and `terraform` operation.

> **Phase 6 persists its three flags to `terraform/install-state.auto.tfvars`,** and they must stay `true` for the life of the cluster. Terraform loads any `*.auto.tfvars` in the working directory, so a later plain `terraform apply` keeps them without `-var` flags. They cannot simply default to `true`, because Phases 1–4 run against a cluster that has neither the CRDs nor the kube-system policies. The file is gitignored (it is per-deployment state), so recreate it after a fresh clone — losing it destroys the ExternalSecrets and makes every NetworkPolicy inert, neither of which announces itself (#635).

> **Phases 6, 7 and 8 used to be one apply,** which closed the public endpoint in the same step that created the store cloudflared needs — locking the cluster before the tunnel could possibly be up. They are separate on purpose. Do not merge them back.

#### Phase 4: syncing the tunnel token

Terraform deliberately never reads the tunnel's connector token, because it would land in state. So after Phase 4 it has to be copied into Secrets Manager, where External Secrets delivers it to cloudflared. Run this from the Terraform root:

```bash
CF_ACCOUNT_ID=$(terraform output -raw cloudflare_account_id)
CF_TUNNEL_ID=$(terraform output -raw cloudflare_tunnel_id)
curl -fsS "https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}/cfd_tunnel/${CF_TUNNEL_ID}/token" \
    --header @<(printf 'Authorization: Bearer %s' "$CLOUDFLARE_API_TOKEN") \
  | jq -je '.result | strings' \
  | aws secretsmanager create-secret --name cloudpipe/cloudflare-tunnel-token \
      --secret-string file:///dev/stdin >/dev/null
```

Use `put-secret-value --secret-id` instead of `create-secret --name` if the secret already exists. Three details are load-bearing:

- **`jq -j`, not `jq -r`.** `-r` appends a newline that is stored verbatim, and cloudflared then fails to register with what looks like an authentication error.
- **The header comes from a process substitution**, so the API token never appears in a process argument list.
- **The connector token goes pipe to pipe**, never to the terminal, a file, or an argument.

#### Phase 7: proving the tunnel

All four, in order — each is a way the tunnel can look ready while not being so:

1. The `cloudflared/cloudflared-token` ExternalSecret exists (ArgoCD has synced it). Annotate it with a fresh `force-sync` value: External Secrets refreshes hourly, so a stale value can sit there looking synced.
2. The in-cluster Secret holds the **current** token — compare a hash of it with a hash of the Secrets Manager value, not merely that it exists.
3. If the token changed, `kubectl -n cloudflared rollout restart deployment/cloudflared`: the token reaches cloudflared as an environment variable, which a Secret update does not refresh in a running pod. Then wait for `rollout status`.
4. Cloudflare reports the tunnel `healthy`: `GET /client/v4/accounts/<account>/cfd_tunnel/<tunnel-id>` returns `"status": "healthy"`.

Only then run Phase 8.

#### Target validation

Both scripts share their `-target` lists via `terraform/targets.sh`, which also validates every
address against the `.tf` sources before Terraform is invoked. `terraform apply -target=` on an
address declared nowhere is a hard error, not a no-op, so one stale entry used to abort the
bootstrap partway through — which is what a deleted-but-still-referenced
`module.aws_efs_csi_pod_identity` did for three months (#211),
unnoticed because the running cluster predates the removal and never re-runs the bootstrap. The
same thing then happened to every address at once when the resources moved into the stack module,
so the lists are now written relative to the stack and `tests/test_terraform_targets.py` runs the
validation in CI rather than only at install. The
preflight reports *every* bad address at once instead of failing at the first; cleanup.sh derives
its teardown order by reversing the same list rather than keeping a second copy.

> **The check is a grep of the `.tf` files, not `terraform plan -target=`.** Plan would be
> authoritative, but it instantiates the kubernetes/helm providers, which at bootstrap time must
> reach a cluster that does not exist yet — the reason these applies are phased at all. So the
> preflight catches an undeclared address, not every way a target can be wrong.

### Key Terraform variables

| Variable | Default | Change when |
|---|---|---|
| `endpoint_public_access` | `false` | Set `true` for Phases 1–7 of the bootstrap; dropped in Phase 8, once the tunnel is proven |
| `crds_available` | `false` | Set `true` from Phase 6, after ArgoCD has installed the CRDs (Phase 5). Persisted in `install-state.auto.tfvars`; `false` destroys every `ExternalSecret` |
| `vpc_cni_network_policy_enabled` | `false` | Set `true` from Phase 6. Turns the VPC CNI network-policy **agent** on; `false` makes every NetworkPolicy in the cluster inert. Persisted in `install-state.auto.tfvars` |
| `vpc_cni_strict_mode` | `false` | Set `true` from Phase 6, after the kube-system NetworkPolicies are in place (Phase 3). Chooses strict over standard enforcement; only has effect while the agent above is on |
| `operator_emails` | — | The operators: administrators of every web UI and, with Cognito, who may reach the cluster |
| `globus_client_id` | — | Globus service account app client ID (no default — must be provided) |
| `globus_s3_destination_bucket` | — | The data bucket (no default — must be provided) |
| `kubernetes_version` | `1.35` | Bump for EKS version upgrades |
| `domain` | — | Base domain for every service hostname, or `null` for port-forward mode (no default — must be provided) |
