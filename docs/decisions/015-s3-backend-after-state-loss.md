# 015 — S3 remote backend for Terraform state, adopted after a state-loss incident

**Status**: Accepted

## Context

This stack never had a remote Terraform backend. State lived only as a local `terraform.tfstate` file (gitignored, never checked in) on whatever operator's machine last ran `terraform/install.sh` — the only apply path, run manually and interactively. CI only ever ran `terraform init -backend=false` + `validate` (lint-only); it never applied.

The machine holding that state file was lost with no backup, discovered when recovering from a separate, unrelated problem (a misplaced AWS Client VPN client certificate). Losing the state file did not touch any real infrastructure — every AWS/Kubernetes resource in the stack (~470+ resources across `module.eks`, `module.vpc`, and every other module) was still running fine. Terraform simply had no record of which resource address mapped to which real object, so recovery meant reconstructing that mapping from scratch via `terraform import` blocks, using `aws` CLI and `kubectl` to discover real resource IDs and cross-checking each import against a clean `terraform plan` before ever applying.

One piece of infrastructure was a genuine, non-recoverable loss rather than a bookkeeping gap: the AWS Client VPN's CA. `vpn.tf`'s `tls_private_key`/`tls_self_signed_cert`/`tls_locally_signed_cert` resources are Terraform-computed values with no backing AWS object — ACM never exposes imported private-key material back out, by design. A new CA, server cert, and client cert had to be generated, and the VPN endpoint (new DNS name) was cut over to it.

## Decision

1. Created a dedicated `cloudpipe-terraform-state` S3 bucket (versioned, SSE-encrypted, public access blocked) via a small, separate bootstrap Terraform config in `terraform/bootstrap/` — its own local backend, deliberately kept outside the main stack's state. This sidesteps the chicken-and-egg problem of the backend bucket needing to exist before the backend that would manage it, and keeps this rarely-touched config's own blast radius trivial (one bucket, one-command re-import) if its state is ever lost again.
2. Wired `terraform/versions.tf` to use that bucket as an S3 backend with native state locking (`use_lockfile = true`), which required bumping `required_version` to `>= 1.10`.
3. Rebuilt state for the entire stack via `terraform import` blocks, resource by resource, verifying a clean (or explicitly understood) `terraform plan` after every batch before applying — never applying against a plan showing an unexpected destroy or replace.
4. Regenerated the VPN's CA/server/client certs and cut the Client VPN endpoint over; distributed a new `client.ovpn` (not checked into the repo — `*.ovpn` stays gitignored).

## Consequences

- Terraform state can no longer be lost by a single machine disappearing — it's versioned in S3, and even accidental corruption is recoverable from a prior version.
- Native S3 state locking now guards against concurrent applies corrupting state, which was previously unguarded entirely.
- `terraform/bootstrap/` is a new, separate Terraform root next to the main stack. It manages only the state bucket itself and should be touched rarely — don't confuse it for part of the main stack's resources when running `terraform init`/`plan`/`apply` from `terraform/`.
- The recovery import surfaced two pieces of real infrastructure drift that predated the state loss and would have caused disruptive changes on the next ordinary `apply`, incident or not:
  - The Globus GCS EC2 instance's AMI data source used `most_recent = true` with no version pin, which would have replaced the live instance the next time Canonical published a newer Ubuntu 22.04 AMI. Pinned to the AMI the instance actually runs (`modules/globus/main.tf`).
  - All 4 Karpenter NodePools had a live `expireAfter: 720h` (30-day forced node recycling) that had never been added to the tracked YAML templates. Added back to `modules/karpenter/helm-values/*.yaml` so applying wouldn't silently disable it.
- A handful of resources now show a permanent, expected diff on every `terraform plan` — documented here so they aren't mistaken for a lingering recovery gap:
  - The `ebs-sc` StorageClass is Terraform-created but ArgoCD-managed thereafter (`docs/gitops.md`'s documented pattern); Terraform's copy shows a permanent annotation diff that's deliberately left unapplied to avoid fighting ArgoCD's self-heal.
  - Several `aws_iam_policy_document`-derived IAM policies/roles (in `module.finops`, `module.addons`) show benign JSON-formatting diffs on every plan — AWS's IAM API doesn't return byte-identical JSON to what Terraform renders, even when the policy is semantically unchanged.
- The old VPN CA is gone permanently; the `client.ovpn` in use today is a new credential, not the one issued when the VPN was originally set up.
- Whether to keep maintaining this VPN long-term or replace it with a Cloudflare Tunnel is a separate, still-open decision — see [ADR 014](014-cloudflare-tunnel-over-vpn.md).
