# Pre-baked Node AMIs

Karpenter provisions GPU nodes on-demand from fresh EC2 instances. Without pre-baking, each new node must pull the fastsurfer and fireants images from ECR before any pod can start. Fastsurfer alone is 5.8 GB compressed in ECR (fireants a further 5.3 GB) and roughly 1.5 minutes per fresh node; a 20% cold-node rate across 12,000 subjects wastes ~600 GPU-hours on image pulls alone. Fireants was added to the bake later (#123) after measurement showed its cold pull (~80s) is a much larger fraction of `t1w-to-mni`'s short GPU hold (36% pre-compute) than fastsurfer's pull is of the longer-running FastSurfer steps — see [issue #123](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/123).

The solution is a custom AMI that has both images already in containerd's image store. Karpenter selects the pre-baked AMI via `amiSelectorTerms` tags. When a new node starts, the images are already present and kubelet skips the pull.

---

## Architecture

```
ECR (private)
  ├── cloudpipe/fastsurfer:<sha-tag>
  └── cloudpipe/fireants:<sha-tag>
          │
          │  packer build (fastsurfer.pkr.hcl)
          ▼
  Custom AMI (AL2023 NVIDIA)
    - Base: amazon-eks-node-al2023-x86_64-nvidia-<eks-version>-*
    - fastsurfer + fireants baked into /var/lib/containerd (k8s.io namespace)
    - Tagged: fastsurfer-image-tag=<sha>, fireants-image-tag=<sha>, eks-version=<version>
          │
          │  amiSelectorTerms in gpu-nodeclass
          ▼
  Karpenter gpu-nodepool
    - Provisions g4dn/g5/g6 instances from the pre-baked AMI
    - nodeadm bootstraps the node to join the EKS cluster
    - both images survive the containerd restart during bootstrap
```

**Why AL2023 and not Bottlerocket?** Bottlerocket's immutable OS makes standard AMI customisation with Packer impractical — there is no supported way to inject content into the container image store before the first boot. AL2023 allows normal shell access during the Packer build.

**Files:**

| Path | Purpose |
|---|---|
| `packer/gpu-nodeclass/fastsurfer.pkr.hcl` | Packer template — builds the pre-baked AMI |
| `terraform/modules/karpenter/helm-values/gpu-nodeclass.yaml` | AL2023 EC2NodeClass, selects AMI by tag |
| `terraform/modules/karpenter/helm-values/gpu-nodepool.yaml` | GPU NodePool — references `gpu-nodeclass` |
| `terraform/karpenter.tf` | Sets `fastsurfer_ami_tag` and `fireants_ami_tag` passed into the module |

---

## Current state

| Item | Value |
|---|---|
| Fastsurfer tag | `sha-2478b13eb1e5e4cb901c37f03effcf43b9ee0dad` |
| FireANTs tag | `sha-e6e6cd8d9a99799dbff55bba40fe618fef7cd29f` |
| AMI ID | `ami-00f875f6c990e33a4` (built 2026-08-09; [#123](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/123) closed 2026-08-05 and live-verified) |
| EKS version | `1.35` |
| Base AMI | latest `amazon-eks-node-al2023-x86_64-nvidia-1.35-*` — resolved by `source_ami_filter` at build time, **not pinned**, so two builds of the same image tags can sit on different base AMIs |
| Region | `<YOUR_AWS_REGION>` |
| Root volume | 150 GiB gp3 (covers fastsurfer + fireants + runtime pulls of afni/synthmorph) |

The tags above are the authoritative pair: they are what `terraform/karpenter.tf` passes as
`fastsurfer_ami_tag`/`fireants_ami_tag`, and `amiSelectorTerms` matches the AMI on *both* tags.
Confirm against the live values rather than this table:

```bash
rg -n 'fastsurfer_ami_tag|fireants_ami_tag' terraform/karpenter.tf
aws ec2 describe-images --owners self \
  --filters "Name=tag:managed-by,Values=packer" \
  --query 'sort_by(Images,&CreationDate)[-1].{Id:ImageId,Name:Name,Created:CreationDate}'
```

---

## Rebuilding the AMI

Rebuild when:
- A new fastsurfer or fireants image is pushed to ECR (new SHA tag)
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
  -var fastsurfer_tag=<new-fastsurfer-sha-tag> \
  -var fireants_tag=<new-fireants-sha-tag> \
  -var ecr_registry=<acct>.dkr.ecr.<YOUR_AWS_REGION>.amazonaws.com \
  fastsurfer.pkr.hcl
```

Both `-var` flags are optional — each defaults to the tag currently pinned in the `.pkr.hcl` file, so you only need to pass the one that actually changed.

The build takes ~20-25 minutes: ~5 min to start and connect to the instance, ~2 min for containerd to start, ~2-4 min to pull fastsurfer and fireants, ~12-15 min to snapshot the volume and register the AMI. The snapshot phase is AWS backend work and is the hard ceiling — volume I/O tuning and instance type affect the pull phase but not the snapshot.

**What the build does:**
1. Launches a `g4dn.2xlarge` (8 vCPUs, 10 Gbps) from the latest `amazon-eks-node-al2023-x86_64-nvidia-<eks_version>-*` AMI
2. Connects via SSH tunnelled through SSM (no public IP, no key management)
3. Starts containerd (`systemctl start containerd`) — it is installed but not auto-started without the EKS nodeadm bootstrap
4. Pulls fastsurfer and fireants into the `k8s.io` containerd namespace with `ctr -n k8s.io images pull`
5. Stops the instance, snapshots the root volume, registers the AMI
6. Tags the AMI with `fastsurfer-image-tag`, `fireants-image-tag`, `eks-version`, and `managed-by=packer`

The new AMI ID is printed at the end of the build:
```
<YOUR_AWS_REGION>: ami-xxxxxxxxxxxxxxxxx
```

### 3. Deploy the new AMI

Update `fastsurfer_ami_tag` and/or `fireants_ami_tag` in `terraform/karpenter.tf` — **both must match tags actually baked into an existing AMI**, or `amiSelectorTerms` matches nothing and Karpenter cannot provision GPU nodes at all:

```hcl
module "karpenter" {
  ...
  fastsurfer_ami_tag = "<new-fastsurfer-sha-tag>"   # ← update this
  fireants_ami_tag   = "<new-fireants-sha-tag>"     # ← and/or this
}
```

Then apply, targeting only the GPU nodeclass to avoid touching unrelated resources:

```bash
cd terraform
terraform apply \
  -target=module.karpenter.kubectl_manifest.gpu-nodeclass \
  -target=module.karpenter.kubectl_manifest.gpu-nodepool
```

Use `-target` deliberately: a bare `terraform apply` here pulls in pre-existing, unrelated
IAM/addon drift. Also run it from an **up-to-date checkout** — `terraform apply` from a branch
behind `main` exits 0 having applied the *old* config, so verify the resource changed rather than
trusting the exit code.

Karpenter picks up the updated `amiSelectorTerms` immediately. New nodes will use the new AMI; existing nodes are not drained (Karpenter replaces them only when it needs to provision or consolidate).

---

## GitHub Actions automation

Implemented in `.github/workflows/build-gpu-nodeclass-ami.yaml`. Triggers automatically after `build-images.yaml` completes on `main` (via `workflow_run`). Also supports manual dispatch via the GitHub Actions UI.

**What the workflow does:**
1. Determines a candidate tag for each of fastsurfer and fireants independently (`sha-<head_sha>` for automatic runs, or the tag pinned in `karpenter.tf` for manual runs with no explicit tag)
2. Verifies each candidate image actually exists in ECR — an image not rebuilt in the triggering run falls back to its currently-baked tag, so the AMI always carries a real image for both
3. Skips the build only if *both* tags already match `karpenter.tf` — a rebuild of either image alone still triggers a new AMI carrying both. This comparison is tag-string-only, not AMI-existence — if `karpenter.tf` was hand-edited to pin a tag before any AMI was baked with it, dispatch manually with `force_rebuild: true` to bypass the skip.
4. Runs `packer build` on a `g4dn.2xlarge` using the `AWS_PACKER_ROLE_ARN` OIDC role
5. Sweeps up any builder instance, temporary key pair and temporary IAM profile/role that Packer did not delete (runs on cancel and failure too — see the gotcha below)
6. Commits the updated `fastsurfer_ami_tag` and `fireants_ami_tag` in `terraform/karpenter.tf` with `[skip ci]`

**The workflow does not roll the nodeclass.** It used to `kubectl apply` the
rendered `gpu-nodeclass` directly, but the EKS API endpoint is private-only
(`endpointPublicAccess=false`), so a GitHub-hosted runner cannot reach it — that
step timed out on every run that got to it. Widening `publicAccessCidrs` to
GitHub's Actions ranges is not possible either; EKS caps that list at 40 entries.

So a green run means **the AMI exists, not that anything is using it**. Finish the
roll from a machine with cluster access:

```bash
git pull
cd terraform && terraform apply
```

Until that runs, GPU nodes keep booting the previous AMI and every GPU pod
re-pulls whichever image changed at start. The `detect` job also prints a
**pre-bake drift** warning to the run summary, per image, whenever the tag
about to be baked disagrees with the tag the workflow templates pin, which is
the signal that a roll was built but never applied.

**Required secrets/variables:**
- `AWS_PACKER_ROLE_ARN` — output of `terraform output github_actions_packer_role_arn`
- `ECR_REGISTRY` — output of `terraform output ecr_registry` (already set for image builds)

**NVIDIA device plugin:**
AL2023 nodes (unlike Bottlerocket) do not bundle the NVIDIA device plugin in the OS bootstrap. The plugin runs as a DaemonSet managed by ArgoCD from `gitops/apps/nvidia-device-plugin/`. It targets `karpenter.sh/nodepool=gpu-nodepool` nodes via `nodeSelector` (overriding the chart's default NFD-based affinity, which requires `feature.node.kubernetes.io/pci-10de.present` — a label not present without Node Feature Discovery).

---

## Gotchas

**An interrupted build can strand its builder, and the builder is expensive at rest.** Packer stops the instance before snapshotting it, so a build killed in that window leaves a *stopped* `g4dn.2xlarge`. That costs nothing for compute but still bills for its 60 GiB root volume at 16000 IOPS / 1000 MB/s, about $105/month. A run cancelled on 2026-08-17 left one behind for three weeks ([#357](https://github.com/<YOUR_GITHUB_ORG>/<YOUR_GITHUB_REPO>/issues/357)). GitHub delivers a cancel's SIGINT only to the step's entry process, so the workflow `exec`s packer to receive it, and then runs an `always()` cleanup step keyed on the run's key-pair name, `packer_gpu-nodeclass_<run_id>-<attempt>`. Nothing sweeps up after a **local** build, so check by hand after interrupting one:

```bash
aws ec2 describe-instances --region <YOUR_AWS_REGION> \
  --filters 'Name=key-name,Values=packer_*' 'Name=instance-state-name,Values=pending,running,stopping,stopped' \
  --query 'Reservations[].Instances[].[InstanceId,State.Name,KeyName]' --output text
aws ec2 describe-key-pairs --region <YOUR_AWS_REGION> --query 'KeyPairs[?starts_with(KeyName,`packer`)].KeyName' --output text
aws iam list-roles --query 'Roles[?starts_with(RoleName,`packer-`)].RoleName' --output text
```

Terminate any instance first. Each temporary instance profile, its role, and the role's inline policy all share one `packer-<uuid>` name.

**containerd is not started by default** on the EKS AL2023 NVIDIA AMI without the nodeadm bootstrap. `sudo systemctl start containerd` is required in the provisioner before any `ctr` command will work.

**Pull into the `k8s.io` namespace** — not the default containerd namespace. `sudo ctr images pull` lands in the `default` namespace; kubelet only looks in `k8s.io`. Always use `sudo ctr -n k8s.io images pull`.

**AMI description must be ASCII only** — AWS rejects non-ASCII characters (including em dashes) in AMI descriptions with `InvalidParameterValue`. The description in `fastsurfer.pkr.hcl` uses plain hyphens.

**YAML tag values must be quoted** — `eks-version: 1.35` is parsed as a float by the YAML parser and rejected by the Karpenter CRD schema (`expected string`). Use `eks-version: "1.35"` (already done in `gpu-nodeclass.yaml`).

**Pre-baked images survive nodeadm bootstrap** — when a new node starts, Karpenter generates user-data that runs `nodeadm init`, which configures and restarts containerd. The images in `/var/lib/containerd` (written during the Packer build) survive this restart unchanged. No need to re-pull.

**Do not set `instanceStorePolicy: RAID0` on the GPU nodeclass** — nodeadm's RAID0 setup copies all of `/var/lib/containerd/` from the root EBS volume to the NVMe instance store on every boot. With the 5.8 GB fastsurfer image present, this copy takes 5+ minutes and blocks kubelet from starting, preventing node registration entirely; with fireants also baked, the copy is larger still. The GPU nodeclass deliberately omits `instanceStorePolicy` so containerd reads directly from the root EBS volume.

**Four GPU families are supported, and g6 is one of them.** The pool is
`["g4dn", "g5", "g6", "g6e"]` — T4, A10G, L4 and L40S — all single-GPU, `amd64`, spot-only, set
in `gpu-nodepool.yaml`'s `karpenter.k8s.aws/instance-family` requirement. The base
`amazon-eks-node-al2023-x86_64-nvidia-*` AMI archives open kernel modules via DKMS for all of
them. `nvidia-fabricmanager` does fail to start on these instances, but that is **benign**: it
serves NVSwitch multi-GPU systems and blocks neither node readiness nor GPU allocation — the
service reads `failed` on running g5 nodes that serve GPU workloads fine. `g5g`/`g6g` are
Graviton and excluded by the arch requirement; `g7` is omitted pending driver validation.

**`g6f` is the excluded family, and the reason is VRAM, not fabricmanager.** The
`nvidia-device-plugin` time-slicing config advertises **3** `nvidia.com/gpu` per physical GPU and
is **cluster-wide** — one config applied to every family in the pool. `g6f` exposes only a
~5.59 GiB 1/4-L4 slice, and the FastSurfer working set is ~3.7 GiB, so even *two* pods
(7.4 GiB) would OOM there. Keeping `g6f` in the pool would let a `g6f` node advertise slices it
cannot honour. Do **not** re-add it without first moving the device plugin to per-node configs.
Two separate NodeOverlays encode this: `gpu-timeslice-3x` injects `capacity: nvidia.com/gpu: 3`
while explicitly excluding `g6f`, and the older `g6f-fractional-gpu` overlay keeps `g6f` at
capacity 1 (needed because Karpenter's instance-type data reports zero GPUs for the fractional
family, so it would otherwise never provision one at all). Both counts must stay in sync with
`sharing.timeSlicing.resources[].replicas` in `gitops/apps/nvidia-device-plugin/values.yaml`.

**Why 3 slices and not 4** — two independent ceilings, and the binding one is not the card the
working set was measured on. *VRAM*: the cluster-wide count lands on `g4dn` too, whose T4 has
~15109 MiB usable; 4 × 3.7 GiB = 15156 MiB exceeds it and the fourth pod OOMs. At 3 the T4 sits
at 74% and the A10G at 48%. *CPU*: every GPU step requests `cpu: 1` (enforced by
`tests/argo/test_gpu_step_resources.py`), so N slices need N allocatable cores for GPU pods
alone — a 4-vCPU `xlarge` carries 3, not 4, and going to 4 would push the instance-size floor to
`2xlarge` and give back the cost win. Extra slices pay off at all only because the GPU steps
compute for ~17% of the wall time they hold a slice for; 3 slices puts that at ~51%.
