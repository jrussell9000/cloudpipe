# Pre-baked Node AMIs

Karpenter provisions GPU nodes on-demand from fresh EC2 instances. Without pre-baking, each new node must pull the fastsurfer image from ECR Public before any pod can start. At 4.3 GB and roughly 1.5 minutes per fresh node, a 20% cold-node rate across 12,000 subjects wastes ~600 GPU-hours on image pulls alone.

The solution is a custom AMI that has the fastsurfer image already in containerd's image store. Karpenter selects the pre-baked AMI via `amiSelectorTerms` tags. When a new node starts, the image is already present and kubelet skips the pull.

---

## Architecture

```
ECR Public
  └── cloudpipe/fastsurfer:<sha-tag>
          │
          │  packer build (fastsurfer.pkr.hcl)
          ▼
  Custom AMI (AL2023 NVIDIA)
    - Base: amazon-eks-node-al2023-x86_64-nvidia-<eks-version>-*
    - fastsurfer baked into /var/lib/containerd (k8s.io namespace)
    - Tagged: fastsurfer-image-tag=<sha>, eks-version=<version>
          │
          │  amiSelectorTerms in gpu-nodeclass
          ▼
  Karpenter gpu-nodepool
    - Provisions g4dn/g5/g6 instances from the pre-baked AMI
    - nodeadm bootstraps the node to join the EKS cluster
    - fastsurfer image survives the containerd restart during bootstrap
```

**Why AL2023 and not Bottlerocket?** Bottlerocket's immutable OS makes standard AMI customisation with Packer impractical — there is no supported way to inject content into the container image store before the first boot. AL2023 allows normal shell access during the Packer build.

**Files:**

| Path | Purpose |
|---|---|
| `packer/gpu-nodeclass/fastsurfer.pkr.hcl` | Packer template — builds the pre-baked AMI |
| `terraform/modules/karpenter/helm-values/gpu-nodeclass.yaml` | AL2023 EC2NodeClass, selects AMI by tag |
| `terraform/modules/karpenter/helm-values/gpu-nodepool.yaml` | GPU NodePool — references `gpu-nodeclass` |
| `terraform/karpenter.tf` | Sets `fastsurfer_ami_tag` passed into the module |

---

## Current state

| Item | Value |
|---|---|
| Fastsurfer tag | `sha-0aae28dd7e437306ca44f6081d4954737b03a056` |
| AMI ID | `ami-06eac2eb71c795efe` |
| EKS version | `1.35` |
| Base AMI | `amazon-eks-node-al2023-x86_64-nvidia-1.35-v20260520` |
| Region | `<YOUR_AWS_REGION>` |
| Root volume | 150 GiB gp3 (covers fastsurfer + runtime pulls of afni/fireants/synthmorph) |

---

## Rebuilding the AMI

Rebuild when:
- A new fastsurfer image is pushed to ECR (new SHA tag)
- The EKS base AMI is refreshed (AWS releases security patches monthly)

### 1. Prerequisites

- AWS credentials active (SSO login)
- `packer` installed (`packer version`)
- `session-manager-plugin` installed (Packer uses SSH over SSM — no open ports needed)

### 2. Run the build

```bash
cd packer/gpu-nodeclass

packer init fastsurfer.pkr.hcl   # first time only — installs Amazon plugin

packer build \
  -var fastsurfer_tag=<new-sha-tag> \
  -var ecr_registry=public.ecr.aws/l9e7l1h1 \
  fastsurfer.pkr.hcl
```

The build takes ~20-25 minutes: ~5 min to start and connect to the instance, ~2 min for containerd to start, ~1-2 min to pull fastsurfer, ~12-15 min to snapshot the volume and register the AMI. The snapshot phase is AWS backend work and is the hard ceiling — volume I/O tuning and instance type affect the pull phase but not the snapshot.

**What the build does:**
1. Launches a `g4dn.2xlarge` (8 vCPUs, 10 Gbps) from the latest `amazon-eks-node-al2023-x86_64-nvidia-<eks_version>-*` AMI
2. Connects via SSH tunnelled through SSM (no public IP, no key management)
3. Starts containerd (`systemctl start containerd`) — it is installed but not auto-started without the EKS nodeadm bootstrap
4. Pulls fastsurfer into the `k8s.io` containerd namespace with `ctr -n k8s.io images pull`
5. Stops the instance, snapshots the root volume, registers the AMI
6. Tags the AMI with `fastsurfer-image-tag`, `eks-version`, and `managed-by=packer`

The new AMI ID is printed at the end of the build:
```
<YOUR_AWS_REGION>: ami-xxxxxxxxxxxxxxxxx
```

### 3. Deploy the new AMI

Update `fastsurfer_ami_tag` in `terraform/karpenter.tf`:

```hcl
module "karpenter" {
  ...
  fastsurfer_ami_tag = "<new-sha-tag>"   # ← update this
}
```

Then apply, targeting only the GPU nodeclass to avoid touching unrelated resources:

```bash
cd terraform
terraform apply \
  -target=module.karpenter.kubectl_manifest.gpu-nodeclass \
  -target=module.karpenter.kubectl_manifest.gpu-nodepool
```

Karpenter picks up the updated `amiSelectorTerms` immediately. New nodes will use the new AMI; existing nodes are not drained (Karpenter replaces them only when it needs to provision or consolidate).

---

## GitHub Actions automation

Implemented in `.github/workflows/build-gpu-nodeclass-ami.yaml`. Triggers automatically after `build-images.yaml` completes on `main` (via `workflow_run`). Also supports manual dispatch via the GitHub Actions UI.

**What the workflow does:**
1. Determines the candidate fastsurfer tag (`sha-<head_sha>` for automatic runs, or the tag pinned in `karpenter.tf` for manual runs with no explicit tag)
2. Verifies the fastsurfer image exists in ECR Public — skips the build if fastsurfer wasn't rebuilt in the triggering run
3. Compares the candidate tag to the tag currently baked into `karpenter.tf` — skips if already up to date
4. Runs `packer build` on a `g4dn.2xlarge` using the `AWS_PACKER_ROLE_ARN` OIDC role
5. Applies the updated `gpu-nodeclass` directly via `kubectl apply` (compatible with `server_side_apply = true` on the Terraform resource — no drift)
6. Commits the updated `fastsurfer_ami_tag` in `terraform/karpenter.tf` with `[skip ci]`

**Required secrets/variables:**
- `AWS_PACKER_ROLE_ARN` — output of `terraform output github_actions_packer_role_arn`
- `ECR_REGISTRY` — output of `terraform output ecr_registry` (already set for image builds)

**NVIDIA device plugin:**
AL2023 nodes (unlike Bottlerocket) do not bundle the NVIDIA device plugin in the OS bootstrap. The plugin runs as a DaemonSet managed by ArgoCD from `gitops/apps/nvidia-device-plugin/`. It targets `karpenter.sh/nodepool=gpu-nodepool` nodes via `nodeSelector` (overriding the chart's default NFD-based affinity, which requires `feature.node.kubernetes.io/pci-10de.present` — a label not present without Node Feature Discovery).

---

## Gotchas

**containerd is not started by default** on the EKS AL2023 NVIDIA AMI without the nodeadm bootstrap. `sudo systemctl start containerd` is required in the provisioner before any `ctr` command will work.

**Pull into the `k8s.io` namespace** — not the default containerd namespace. `sudo ctr images pull` lands in the `default` namespace; kubelet only looks in `k8s.io`. Always use `sudo ctr -n k8s.io images pull`.

**AMI description must be ASCII only** — AWS rejects non-ASCII characters (including em dashes) in AMI descriptions with `InvalidParameterValue`. The description in `fastsurfer.pkr.hcl` uses plain hyphens.

**YAML tag values must be quoted** — `eks-version: 1.35` is parsed as a float by the YAML parser and rejected by the Karpenter CRD schema (`expected string`). Use `eks-version: "1.35"` (already done in `gpu-nodeclass.yaml`).

**Pre-baked images survive nodeadm bootstrap** — when a new node starts, Karpenter generates user-data that runs `nodeadm init`, which configures and restarts containerd. The images in `/var/lib/containerd` (written during the Packer build) survive this restart unchanged. No need to re-pull.

**Do not set `instanceStorePolicy: RAID0` on the GPU nodeclass** — nodeadm's RAID0 setup copies all of `/var/lib/containerd/` from the root EBS volume to the NVMe instance store on every boot. With the 4.3 GB fastsurfer image present, this copy takes 5+ minutes and blocks kubelet from starting, preventing node registration entirely. The GPU nodeclass deliberately omits `instanceStorePolicy` so containerd reads directly from the root EBS volume.

**g4dn (T4) and g5 (A10G) are both supported** — the base `amazon-eks-node-al2023-x86_64-nvidia-*` AMI archives open kernel modules via DKMS for multiple GPU families. Both families are confirmed working with the pre-baked AMI. g6 (L4) remains excluded because nvidia-fabricmanager fails on single-GPU L4 instances (no NVLink fabric).
