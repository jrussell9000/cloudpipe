# NAT Gateway Ingress Cost — ECR Public Image Pulls on Batch Scale-Up

**Date:** 2026-07-20
**Scope:** Attribute the large, recurring VPC NAT-gateway ingress cost (~1 TB on
batch days) and scope the fix.
**Related:** [2026-07-17-gpu-nodepool-spot-scarcity-g6-enablement.md](2026-07-17-gpu-nodepool-spot-scarcity-g6-enablement.md)
(same Karpenter scale-up behavior that drives the spikes),
`terraform/vpc.tf` (VPC flow logs + S3 gateway endpoint), `terraform/ecr.tf`.

## 1. Summary

NAT-gateway ingress is flat at ~0.8 GB/day when idle but spikes to **300 GB–1.8 TB
on batch-processing days**. The driver is **container image pulls from ECR Public
(`public.ecr.aws`)**: it is internet-facing (CloudFront), so every pull traverses
the NAT gateway. On a batch, Karpenter provisions many fresh spot nodes, each
pulling the multi-GB FastSurfer/freesurfer/AFNI images.

It is **not** the Globus ingest (that lands in S3; only 3% of flow-log ingress is
non-AWS) and **not** S3 reads (those correctly use the free S3 gateway endpoint).

At ~11 TB of NAT ingress over the last 60 days and $0.045/GB data processing, this
is **≈$250/month on ingress alone** (more with egress), almost entirely
eliminable by moving workflow images to **private ECR in <YOUR_AWS_REGION>**, whose layer
blobs are served from S3 via the gateway endpoint already in place.

## 2. The data

NAT gateway `nat-04a0b4fbed5edb146`, ENI `eni-0d5cb69dba9ab5699`, private IP
`10.0.48.117`. CloudWatch `AWS/NATGateway` `BytesInFromDestination`
(internet → pods), daily, <YOUR_AWS_REGION>:

| Day | GB | Day | GB |
|---|---|---|---|
| idle baseline | ~0.82 | 2026-06-29 | 997 |
| 2026-05-25 | **1,810** | 2026-06-30 | 1,470 |
| 2026-05-23 | 320 | 2026-07-01 | 1,170 |
| 2026-05-26 | 356 | 2026-07-04 | 1,248 |
| 2026-06-25 | 290 | 2026-07-05 | 1,483 |
| 2026-06-23 | 175 | 2026-07-16 | 721 |
| … | … | 2026-07-17 | 298 |

Every spike lands on a known processing day (the May cluster is the v6 dataset run
— see the v6 cost records dated 05-27/28/29; 07-16/17 are the recent test batch).
Non-batch days sit at the ~0.82 GB idle floor. 60-day ingress total ≈ **11 TB**.

## 3. Attribution method — and a correction

**Do not trust the naive flow-log heuristic.** VPC flow logs (Parquet, delivering
since 2026-07-19 to `s3://cloudpipe-logging/vpc-flow-logs/`) let you filter
`flow_direction='ingress' AND public pkt_srcaddr AND private pkt_dstaddr`. On the
idle window that returned ~10 GB "NAT ingress," of which 83% classified as S3
<YOUR_AWS_REGION>. **That is wrong as a NAT metric:** S3 traffic uses the S3 **gateway
endpoint** (`vpce-0c01f8f680ebcc8e5`, prefix list `pl-7ba54012`, associated with
all three private route tables) and never touches the NAT. The heuristic counts
it anyway because S3's IPs are public either way.

The billed truth is **CloudWatch `AWS/NATGateway`**, which showed only ~0.92 GB of
actual NAT ingress that same day — ~10× less. Always cross-check flow-log
attribution against the NAT CloudWatch metrics.

The flow logs remain the right tool for attributing a *live* spike; they just
postdate the historical spikes here (delivery started 2026-07-19). The **next
batch will be captured**, which will confirm the ECR-Public attribution directly.

## 4. Root cause

- Workflow images are pulled from **ECR Public** (`public.ecr.aws/<alias>/cloudpipe/*`);
  the `ecr-registry` workflow parameter comes from the `cloudpipe-config` ConfigMap,
  set from the Terraform `ecr_registry` output (`terraform/ecr.tf`).
- ECR Public is internet-facing (CloudFront-fronted) → pulls go through the NAT.
- The **only** VPC endpoint is the S3 gateway. There is no ECR endpoint (and none
  exists for ECR *Public* regardless).
- Karpenter scales fresh spot nodes to zero between batches, so each batch pulls
  every image fresh on every new node (`imagePullPolicy: IfNotPresent` only dedups
  within a node's lifetime). Large images (FastSurfer/freesurfer/AFNI, multi-GB) ×
  many nodes = the 300 GB–1.8 TB spikes.

FastSurfer is already partly mitigated by AMI pre-baking
(`build-gpu-nodeclass-ami.yaml`); the CPU images are not baked and pull fresh.

## 5. Cost

~11 TB ingress / 60 days × $0.045/GB ≈ **$495 / 60 days ≈ $250/month on ingress
alone**. Egress data processing adds more. The NAT hourly charge (~$32/mo) is
fixed and unaffected. The addressable waste is the per-GB processing on image
pulls.

## 6. Fix — migrate workflow images to private ECR (<YOUR_AWS_REGION>)

Private ECR serves layer blobs from S3, which the **existing S3 gateway endpoint
already carries for free**; only the small ECR API calls need reaching. Net NAT
image-pull traffic drops to ~zero.

Concrete changes:

1. **`terraform/ecr.tf`** — add `aws_ecr_repository` (private, <YOUR_AWS_REGION>) for the
   13 images currently in `local.ecr_images` (a private build-cache repo already
   exists as a pattern). Change the `ecr_registry` output to the private registry
   (`<acct>.dkr.ecr.<YOUR_AWS_REGION>.amazonaws.com/<prefix>`). Extend the GHA push role's
   `ECRPrivate*` statements from the cache repo to the new image repos.
2. **`.github/workflows/build-images.yaml`** (and `build-fmri-first-level-proc.yaml`,
   `build-prefect-flow-runner.yaml`) — swap `login-ecr` `registry-type: public`
   for private ECR login; push to the private registry.
3. **GHA variable `ECR_REGISTRY` / `cloudpipe-config` ConfigMap** — set to the
   private prefix. Workflow templates use `{{workflow.parameters.ecr-registry}}`
   and follow automatically.
4. **`terraform/vpc.tf`** — add interface endpoints
   `com.amazonaws.<YOUR_AWS_REGION>.ecr.api` and `…ecr.dkr` (private DNS enabled, in the
   private subnets, SG allowing 443 from the VPC CIDR). This eliminates the
   residual ECR-API NAT traffic; the layer bytes already go via the S3 gateway.
   Cost: ~$7/mo per endpoint per AZ (~$43/mo across 3 AZs) — far below the savings,
   and reducible to fewer AZs if desired.
5. **`build-gpu-nodeclass-ami.yaml`** — point the fastsurfer reference at the
   private registry.

Not required:
- **Node IAM** — the Karpenter/EKS node role already has
  `AmazonEC2ContainerRegistryReadOnly` (`terraform/eks.tf:243,302`); private ECR
  pulls work without change.
- **`alpine:3.21`** — the only hardcoded third-party public image (5 refs in the
  fastsurfer templates), ~3 MB, negligible NAT. Mirror to private ECR later for
  completeness; not worth blocking on.

Complementary levers: extend AMI pre-baking to the CPU images; Karpenter
consolidation to reduce node churn and re-pulls.

## 7. Verification / next steps

- The now-live flow logs will capture the next batch's NAT spike; classify
  `pkt_srcaddr` against the AWS IP ranges to confirm ECR Public / CloudFront is the
  dominant source (expected before the migration; ~zero after).
- After migration, watch `AWS/NATGateway BytesInFromDestination` on the next batch
  day — it should stay near the idle floor instead of spiking.

## 8. Reference

- NAT gateway: `nat-04a0b4fbed5edb146`, ENI `eni-0d5cb69dba9ab5699`, priv
  `10.0.48.117`, pub `18.189.149.176`.
- S3 gateway endpoint: `vpce-0c01f8f680ebcc8e5`; S3 prefix list `pl-7ba54012`.
- Flow logs: `s3://cloudpipe-logging/vpc-flow-logs/AWSLogs/aws-account-id=<YOUR_AWS_ACCOUNT_ID>/`
  (Parquet, hive-partitioned, custom format with `pkt-srcaddr`/`pkt-dstaddr`).
- Cost basis: NAT data processing $0.045/GB (<YOUR_AWS_REGION>).
