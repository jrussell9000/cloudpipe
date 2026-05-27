# CloudPipe Infrastructure

All infrastructure lives in `terraform/`. Run all Terraform commands from within that directory — never use `-chdir=terraform`.

---

## Terraform file map

| File / directory | Manages |
|---|---|
| `eks.tf` | EKS cluster, managed node groups, KMS keys, CloudWatch log group |
| `vpc.tf` | VPC, subnets, NAT gateway, S3 VPC endpoint, VPC flow logs |
| `vpn.tf` | AWS Client VPN endpoint, client certificates |
| `addons.tf` | Pod Identity associations for EBS/EFS CSI, External DNS, CloudWatch agent |
| `karpenter.tf` | Karpenter Helm release (via module) |
| `storage.tf` | EFS file system + mount targets, StorageClasses (ebs-sc, efs-sc) |
| `argowf.tf` | Argo Workflows SSO secrets, RBAC, Prefect→Argo cross-namespace bindings |
| `argocd.tf` | ArgoCD Helm release, Dex OIDC config (UW-Madison NetID), bootstrap ApplicationSet |
| `prefect.tf` | Prefect module call, oauth2-proxy secret |
| `globus.tf` | Globus module call |
| `finops.tf` | Kubecost + Athena CUR module |
| `metrics.tf` | Pipeline observability module (Glue database + crawlers, Athena workgroup, Grafana Pod Identity) |
| `queue.tf` | (Placeholder — SQS/Argo Events removed; Prefect submits directly to Argo API) |
| `batch.tf` | AWS Batch (legacy, not used by current pipelines) |
| `logging.tf` | S3 log bucket, CloudTrail, Container Insights log groups |
| `dns.tf` | Route53 zone lookup, ACM certificates (us-east-1 + <YOUR_AWS_REGION>) |
| `ecr.tf` | ECR Public registry reference |
| `s3_lifecycle.tf` | S3 lifecycle rules for data bucket |
| `locals.tf` | Derived locals — service URLs, VPC DNS resolver |
| `variables.tf` | All input variables with defaults |
| `versions.tf` + `providers.tf` | Provider pins, AWS provider aliases |
| `modules/` | Reusable sub-modules (see module reference below) |

---

## VPC and networking

| Resource | Value |
|---|---|
| Primary CIDR | `10.0.0.0/16` |
| Private subnets | Three AZs (<YOUR_AWS_REGION>a/b/c), `/20` each — EKS nodes |
| Public subnets | Three AZs, `/24` each — ALBs, Globus EC2 |
| Intra subnets | EKS control plane ENIs |
| NAT gateway | Single (cost optimisation) — private subnets route through it |
| S3 VPC Gateway endpoint | All route tables — S3 traffic stays on AWS backbone |
| VPC flow logs | → `cloudpipe-logging` S3 bucket under `vpc-flow-logs/`, 60s aggregation |

### Client VPN

AWS Client VPN provides private access to the EKS API (which has no public endpoint in steady state) and internal services. Required before using `kubectl`, `argo`, or the web UIs.

- Client CIDR: `10.3.0.0/22` (split tunnel — public internet still uses local route)
- Certificates: managed by Terraform (`certificate_validity_period_hours = 8760`, i.e. 1 year)

### EC2 Instance Connect Endpoint (EICE)

Used for operator access to the RDS instances (PostgreSQL on port 5432) without a bastion host. The EICE security group is referenced in the RDS security groups to allow inbound connections.

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
| `vpc-cni` | Prefix delegation enabled (`ENABLE_PREFIX_DELEGATION=true`). Strict network policy mode (`NETWORK_POLICY_ENFORCING_MODE=strict`) enabled after Phase 3 of `install.sh`. |
| `coredns` | Runs on `backend` node group; forwards to VPC DNS resolver |
| `kube-proxy` | Standard |
| `metrics-server` | Runs on `backend` node group |
| `eks-pod-identity-agent` | Installed `before_compute` so Pod Identity works from first node join |
| `amazon-cloudwatch-observability` | Basic Container Insights mode (per-metric billing, not per-observation). Application Signals disabled. Runs on `backend` node group. |

### Managed node groups (always-on, fixed size)

These run system services and are not used for pipeline workloads.

| Node group | Instance | AMI | Size | Taint | Purpose |
|---|---|---|---|---|---|
| `backend` | `m7g.xlarge` | Bottlerocket ARM64 | 1 node, AZ-pinned to <YOUR_AWS_REGION>a | `CriticalAddonsOnly=true:NoSchedule` | CoreDNS, metrics-server, CloudWatch agent, Kubecost |
| `karpenter` | `m7g.xlarge` | Bottlerocket ARM64 | 1 node | — | Karpenter controller |
| `argo` | `m7g.xlarge` | Bottlerocket ARM64 | 1 node | `argoproj.io/backend=true:NoSchedule` | ArgoCD, Argo server, Prefect |

The `backend` group is pinned to <YOUR_AWS_REGION>a so it is always co-located with the EBS PVCs (Kubecost, Prefect) provisioned in that AZ.

### Karpenter node pools (on-demand provisioning for pipeline workloads)

| Node pool | Instance family | Capacity type | Max resources | Used by |
|---|---|---|---|---|
| `cpu-light-nodepool` | t-series (AMD64) | spot + on-demand | 64 CPU / 128 Gi | Globus control, inventory, S3 sync |
| `cpu-heavy-nodepool` | c/m/r 2xl–4xl (AMD64) | spot | 256 CPU / 1024 Gi | BOLD→T1w registration, functional preprocessing |
| `gpu-nodepool` | g4dn/g6 xlarge, NVIDIA GPU (AMD64) | spot + on-demand | 128 CPU / 512 Gi | T1w→MNI registration (FireANTs) |

All Karpenter nodes consolidate to zero when empty (`consolidateAfter: 10m`). Disruption budgets allow removing up to 20% of nodes at a time. Startup taint `efs.csi.aws.com/agent-not-ready` prevents pods from scheduling before the EFS CSI driver is ready.

---

## Storage

### EFS

Single encrypted EFS file system with mount targets in all three private subnets (one per AZ). Used for per-workflow shared PVCs.

| Attribute | Value |
|---|---|
| Performance mode | `generalPurpose` |
| Throughput mode | `elastic` |
| Encryption | AES-256 (AWS-managed key) |
| StorageClass | `efs-sc` (`ReadWriteMany`, dynamic provisioning) |
| PVC lifecycle | 50 Gi per workflow, deleted on workflow completion (`volumeClaimGC: OnWorkflowCompletion`) |

### EBS

Default StorageClass `ebs-sc` — gp3, encrypted, `WaitForFirstConsumer` binding. Used by Kubecost and Prefect for persistent volumes pinned to <YOUR_AWS_REGION>a.

---

## Databases (RDS PostgreSQL)

Two separate RDS instances share the same subnet group but have independent security groups.

| Instance | Identifier | Database | User | Used by |
|---|---|---|---|---|
| Argo | `cloudpipe-argo` | `argoworkflows` | `argouser` | Argo Workflows persistence (workflow history, artifacts metadata) |
| Prefect | (in `modules/prefect`) | `prefect` | `prefectuser` | Prefect server metadata (flow runs, deployments, work pools) |

Both instances:
- `storage_type = gp3`, encrypted
- Master password managed natively by RDS (`manage_master_user_password = true`) — password lives in RDS-owned Secrets Manager secret
- CloudWatch log exports: `postgresql`, `upgrade`
- Auto minor version upgrade enabled
- Not publicly accessible; reachable from within VPC (pods) and via EICE (operators)
- `deletion_protection = false` (set to `true` before production promotion)

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

| Bucket | Purpose |
|---|---|
| `abcd-v7` | Primary data — input BOLD, derivatives, config files (see architecture.md for key layout) |
| `<YOUR_INPUT_S3_BUCKET>` | Legacy — first-level subject CSVs; Prefect worker and Argo runner have read access |
| `cloudpipe-logging` | Aggregated log archive: VPC flow logs, CloudTrail, ALB access logs, S3 access logs |

`cloudpipe-logging` lifecycle: → Glacier after 90 days → expire after 3 years (satisfies NIST 800-171 3.3.1 log retention).

A gateway VPC endpoint routes all S3 traffic from the VPC through the AWS backbone, avoiding NAT gateway data charges on large transfers.

---

## IAM and Pod Identity

All pod-level AWS permissions use EKS Pod Identity (not IRSA). Each service account has a dedicated IAM role and policy attached via Pod Identity Association.

| Service account | Namespace | Key permissions |
|---|---|---|
| `argo-workflows-controller` | `argo-workflows` | S3 read/write on `abcd-v7` (artifact storage) |
| `argo-workflows-runner` | `argo-workflows` | S3 read/write on `abcd-v7` + `<YOUR_INPUT_S3_BUCKET>`, SSM read on `/cloudpipe/globus/*`, EC2 start/stop (via globus module), `workflowtaskresults` create/patch |
| `argo-workflows-server` | `argo-workflows` | S3 read on `abcd-v7` (serve archived logs) |
| `prefect-worker` | `prefect` | S3 read/write on `abcd-v7`, S3 read on `<YOUR_INPUT_S3_BUCKET>`, SSM read on Globus params; K8s RBAC to create/manage Jobs in `prefect` ns and list/create Workflows in `argo-workflows` ns |
| `ebs-csi-controller-sa` | `aws-ebs-csi-driver` | EBS CSI managed policy |
| `efs-csi-controller-sa` | `aws-efs-csi-driver` | EFS CSI managed policy |
| `external-dns` | `external-dns` | Route53 record management on `<YOUR_DOMAIN>` zone |
| `cloudwatch-agent` | `amazon-cloudwatch` | CloudWatch agent policy |
| `external-secrets` | `external-secrets` | Secrets Manager `GetSecretValue` (for ClusterSecretStore) |
| `grafana` | `grafana` | Athena query + Glue read on `cloudpipe_metrics`; S3 read on `abcd-v7/metrics/*`; S3 write on `cloudpipe-finops/grafana-query-results/*` |

Cross-namespace RBAC (defined in `argowf.tf`):
- `prefect-worker` → `argo-workflows` namespace: `argo-workflows-view` ClusterRole (for `count_running()`), custom `prefect-worker-argo-submit` Role (for `submit()`)

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
| Container Insights (basic mode) | CloudWatch | Default (15 months) |
| VPC flow logs | `cloudpipe-logging/vpc-flow-logs/` | 90d → Glacier → 3y expiry |
| CloudTrail (all regions, all mgmt events + S3 data events on `abcd-v7`) | `cloudpipe-logging/cloudtrail/` | 90d → Glacier → 3y expiry |
| ALB access logs | `cloudpipe-logging/*/AWSLogs/` | 90d → Glacier → 3y expiry |

Pipeline pod logs are **not** forwarded to CloudWatch. Use the Argo UI or `argo logs` to access them while the workflow is running, or the Argo server's S3-backed log archive for completed workflows.

Container Insights runs in basic mode (`kubernetes: {}` config only — per-metric billing). Enhanced mode and Application Signals are explicitly disabled to avoid ~$80/month in unnecessary APM charges for batch workloads.

---

## Globus Connect Server (EC2)

The Globus Connect Server runs on a standalone EC2 instance in a public subnet, separate from EKS.

| Attribute | Value |
|---|---|
| AMI | Ubuntu 22.04 LTS x86_64 (Canonical) |
| Subnet | Public (<YOUR_AWS_REGION>a) |
| Public IP | Elastic IP (fixed — used for endpoint registration) |
| Storage | S3 storage gateway (`globus_use_s3_gateway = true`) — GridFTP writes directly to `abcd-v7` |

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
| `argo-workflows` | `modules/argo-workflows` | RDS instance, IAM/Pod Identity, RBAC, network policies, metrics |
| `finops` | `modules/finops` | Kubecost Helm release, Athena CUR table, IAM for cost data |
| `globus` | `modules/globus` | EC2 instance, EIP, security group, IAM role, SSM parameters |
| `karpenter` | `modules/karpenter` | Karpenter Helm release, NodePool/NodeClass manifests |
| `metrics` | `modules/metrics` | Glue catalog database + 5 crawlers, Athena `cloudpipe_metrics_workgroup`, Grafana Pod Identity |
| `prefect` | `modules/prefect` | RDS instance, IAM/Pod Identity, RBAC, network policies |
| `argo-events` | `modules/argo-events` | Argo Events resources (retained for future use; no active EventSources) |

---

## Bootstrap and install sequence

A fresh cluster install follows the phased sequence in `install.sh`. Do not run `terraform apply` directly on a new cluster — the phases are load-bearing:

| Phase | What happens |
|---|---|
| 1 | VPC + EKS with public endpoint enabled (bootstrapping requires reachable API) |
| 2 | EKS add-ons, Pod Identity, Karpenter, ArgoCD, service modules |
| 3 | kube-system NetworkPolicies applied before strict VPC CNI mode |
| 4 | Full apply (`crds_available=false`) — VPN created here |
| 5 | Wait for ArgoCD to sync and install CRDs (external-secrets, Argo Events, Prometheus) |
| 6 | Final apply: `crds_available=true`, `vpc_cni_strict_mode=true`, public endpoint disabled |

After Phase 6 the EKS API is private-only — connect via VPN for all subsequent `kubectl`/`terraform` operations.

### Key Terraform variables

| Variable | Default | Change when |
|---|---|---|
| `endpoint_public_access` | `false` | Set `true` only during `install.sh` bootstrap |
| `crds_available` | `false` | Set `true` after ArgoCD has installed CRDs (Phase 5) |
| `vpc_cni_strict_mode` | `false` | Set `true` after kube-system NetworkPolicies are in place (Phase 6) |
| `admin_netid` | — | NetID (without @<YOUR_INSTITUTION_DOMAIN>) granted ArgoCD + Argo admin access |
| `globus_client_id` | — | Globus service account app client ID (no default — must be provided) |
| `globus_s3_destination_bucket` | `abcd-v7` | Change to target a different S3 bucket |
| `kubernetes_version` | `1.35` | Bump for EKS version upgrades |
| `domain` | `<YOUR_DOMAIN>` | Change for different deployment environments |
