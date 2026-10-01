# Example root module

A minimal, complete Terraform root that deploys the CloudPipe stack. Start here
if you are standing up your own deployment.

The stack module creates everything: the VPC, the EKS cluster and its node pools,
RDS, the S3 buckets, ArgoCD and the GitOps bootstrap, Argo Workflows, Prefect,
Globus Connect Server, the metrics catalog and the Cloudflare Zero Trust tunnel.
A root's whole job is the four things a module is not allowed to contain:

| In this directory | Why it cannot be in the module |
|---|---|
| `terraform { backend }` | A module declares no backend. Commented out here so CI can validate with `-backend=false`. |
| `provider` blocks | A module configures no provider. The Kubernetes-family providers also need the cluster endpoint, and a provider may not be configured from a module's outputs. |
| `variable` blocks | What `terraform.tfvars` binds to. |
| `output` blocks | An output of a child module is not an output of the root, so each one is forwarded. |

`.terraform.lock.hcl` is committed, and is a copy of the reference deployment's.
Keep it: without a lock, every `terraform init` re-resolves providers against the
registry and re-fetches each one's checksums, which is a live dependency on that
provider's release hosting. Upgrade providers deliberately
(`terraform init -upgrade`), not by accident.

## Use it

```bash
cp -r terraform/modules/stack/example ~/my-cloudpipe
cd ~/my-cloudpipe
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars
terraform init && terraform validate
```

Then edit two things in `main.tf`:

1. **`source`.** It is `"../"` so that CI can validate this directory in place.
   From your own copy, point it at a tag rather than a branch — a module source
   that tracks a moving branch changes what your next apply does without you
   editing anything:

   ```hcl
   source = "git::https://github.com/<org>/cloudpipe.git//terraform/modules/stack?ref=<tag>"
   ```

2. **The `backend "s3"` block**, which is commented out. Do not leave state on
   local disk; see
   [ADR 015](../../../../docs-internal/decisions/015-s3-backend-after-state-loss.md),
   which exists because that went wrong once.

## Before the first apply

**A fresh install is phased. A single `terraform apply` on an empty account will
fail**, and not in an obvious way: the providers in `main.tf` look up an EKS
cluster that does not exist yet, so the failure is a data-source error rather
than anything about the resource you were creating. The VPC and the cluster are
applied with `-target` first, then the add-ons, then the rest. The sequence, and
why each phase is load-bearing, is in
[Bootstrap and install sequence](../../../../docs/infrastructure.md).

Prerequisites the stack expects to already exist:

- a Route53 hosted zone for `domain`;
- an S3 bucket for `globus_s3_destination_bucket`;
- an AWS managed prefix list holding the IP ranges allowed to reach the web UIs;
- a Cloudflare Zero Trust organization, and an API token exported as
  `CLOUDFLARE_API_TOKEN` with account-level *Zero Trust: Edit* as well as Tunnel
  and Access permissions. The token is an environment variable, never a Terraform
  variable — a variable would persist it in state;
- an OIDC client registered with your institution's identity provider, with its
  credentials placed in Secrets Manager by hand (Terraform reads that secret and
  must not own it);
- for Globus, a registered service client and a source collection. See
  [the Globus setup guide](../../../../docs/globus-setup.md).

## Inputs

`terraform.tfvars.example` lists every input with **no default** — twenty-one of
them. That is deliberate. A default for a value that identifies one deployment
is the worst failure mode available, because nothing errors: you inherit someone
else's domain, repository, or bucket name and discover it much later. Without a
default, Terraform prompts, or fails under `-input=false` naming the variable.

Every other input has a working default and is not repeated here. `vpc_cidr`,
`kubernetes_version`, `prefect_namespace` and about thirty others are sane
starting values rather than claims about who is deploying. To override one, add a
`variable` block to `main.tf` and one line to the `module "stack"` call;
[`../variables.tf`](../variables.tf) is the full list, with the descriptions and
the validation rules.

## Further reading

- [Infrastructure reference](../../../../docs/infrastructure.md) — what the stack
  creates, the file map, the bootstrap sequence.
- [Architecture](../../../../docs/architecture.md) — how the pieces fit together.
- [Operations](../../../../docs/operations.md) — running the pipeline once the
  cluster is up.
- [Architecture decision records](../../../../docs-internal/decisions/) — why things are
  the way they are, including the ones that were tried and rejected.
