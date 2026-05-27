# GitHub Actions Runner Controller (ARC)

Self-hosted GitHub Actions runners running as pods inside EKS. Enables CI jobs
to reach private cluster resources — specifically the EKS API server and the
Prefect server — that are inaccessible from GitHub-hosted runners.

## Why this exists

The EKS API endpoint is private-only and the Prefect server sits behind a
VPN-gated ALB. GitHub-hosted runners are outside the VPC and cannot reach
either. A runner pod inside the cluster has in-cluster access to both, enabling
the `prefect deploy --all` step to be automated rather than run manually after
each push that changes `prefect/prefect.yaml`.

## Architecture

[Actions Runner Controller](https://github.com/actions/actions-runner-controller)
(ARC) is the official Kubernetes operator for self-hosted GitHub Actions runners.
It manages a pool of runner pods that register with GitHub and pick up workflow
jobs. Runners are ephemeral: a pod starts when a job is dispatched, runs the
job, then terminates.

```
GitHub Actions ──► ARC controller (EKS) ──► runner pod (EKS)
                                                  │
                                    ┌─────────────┼──────────────────┐
                                    │             │                  │
                              EKS API     Prefect server      ECR / S3
                           (in-cluster)  (in-cluster DNS)  (via pod identity)
```

## Implementation steps

### 1. IAM — pod identity for the runner

The runner pod needs AWS credentials to push to ECR and call `eks:DescribeCluster`.
Use EKS Pod Identity (already used for the Prefect worker).

Add to `terraform/ecr.tf`:

```hcl
resource "aws_iam_role" "github_runner" {
  name               = "${local.name}-github-runner"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_assume.json
}

data "aws_iam_policy_document" "github_runner" {
  statement {
    sid     = "ECRPrivateAuth"
    actions = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "ECRPrivatePush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = ["arn:aws:ecr:${local.region}:${local.account_id}:repository/cloudpipe-flow-runner"]
  }
}

resource "aws_iam_role_policy" "github_runner" {
  name   = "${local.name}-github-runner"
  role   = aws_iam_role.github_runner.name
  policy = data.aws_iam_policy_document.github_runner.json
}

resource "aws_eks_pod_identity_association" "github_runner" {
  cluster_name    = module.eks.cluster_name
  namespace       = "arc-runners"
  service_account = "cloudpipe-runner"
  role_arn        = aws_iam_role.github_runner.arn
}
```

`data.aws_iam_policy_document.pod_identity_assume` trusts
`pods.eks.amazonaws.com` — copy the pattern from `modules/prefect/iam.tf`.

### 2. Install ARC via Helm (gitops)

Add two ArgoCD applications to `gitops/appsets/`:

**ARC controller** (`arc-controller.yaml`):
```yaml
helm:
  repo: https://actions-github.io/actions-runner-controller
  chart: oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller
  version: 0.10.x          # pin to a specific minor
  releaseName: arc
  namespace: arc-systems
```

**Runner scale set** (`arc-runners.yaml`):
```yaml
helm:
  repo: oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set
  version: 0.10.x
  releaseName: cloudpipe-runner
  namespace: arc-runners
  values:
    githubConfigUrl: https://github.com/<org>/<repo>
    githubConfigSecret: arc-github-secret   # see step 3
    minRunners: 0
    maxRunners: 3
    containerMode:
      type: kubernetes                       # dind-less; jobs run as sibling pods
    template:
      spec:
        serviceAccountName: cloudpipe-runner
        nodeSelector:
          eks.amazonaws.com/nodegroup: backend
        tolerations:
          - key: CriticalAddonsOnly
            value: "true"
            effect: NoSchedule
        containers:
          - name: runner
            image: ghcr.io/actions/actions-runner:latest
```

### 3. GitHub PAT secret

ARC needs a GitHub PAT (or GitHub App) to register runners. Create a PAT with
`repo` scope (or use a GitHub App for org-level runners).

Store it as a Kubernetes secret in the `arc-runners` namespace:

```bash
kubectl create secret generic arc-github-secret \
  --namespace arc-runners \
  --from-literal=github_token=<PAT>
```

Mark this secret as `ignore_changes` in Terraform or create it out-of-band —
it contains a credential and should not be committed to git.

### 4. Label the workflow job

In `.github/workflows/build-prefect-flow-runner.yaml`, change the deploy job
runner from `ubuntu-latest` to the self-hosted runner label:

```yaml
deploy:
  needs: build
  runs-on: cloudpipe-runner    # matches releaseName of the scale set
  steps:
    - uses: actions/checkout@v6

    - name: Register deployments
      working-directory: prefect
      env:
        PREFECT_API_URL: http://prefect-server.prefect.svc.cluster.local:4200/api
      run: |
        pip install --quiet "prefect>=3.6.24"
        prefect deploy --all
```

No `PREFECT_API_KEY` is needed — the runner reaches the server directly via
in-cluster DNS, bypassing the ALB and oauth2-proxy entirely.

### 5. Remove the `prefect.yaml` warning step

Once the self-hosted runner is in place the warning added as a temporary
measure in the `build` job can be removed.

## Cost

Runner pods are ephemeral and billed only while running. A `prefect deploy`
job runs in under 60 seconds. At current Fargate/EC2 rates this is negligible.
The `minRunners: 0` setting means no pods idle between jobs.

## References

- [ARC documentation](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners-with-actions-runner-controller/quickstart-for-actions-runner-controller)
- [Runner scale set Helm values](https://github.com/actions/actions-runner-controller/blob/master/charts/gha-runner-scale-set/values.yaml)
