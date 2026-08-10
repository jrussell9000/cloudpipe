# 006 — EKS Pod Identity over IRSA

**Status**: Accepted

## Context

Pipeline pods need AWS API access (S3, SSM, EC2) without embedding long-lived credentials. Two AWS-native mechanisms provide credential-less IAM access for EKS pods:

**IRSA (IAM Roles for Service Accounts)**: An OIDC provider is registered in IAM for the EKS cluster; pods annotate their service account with an IAM role ARN; the pod's token is exchanged for temporary credentials via `sts:AssumeRoleWithWebIdentity`. IRSA has been available since EKS 1.13. It requires managing an OIDC provider URL in every IAM trust policy, which creates a dependency on the specific cluster's OIDC endpoint.

**EKS Pod Identity**: Introduced in EKS 1.24, GA in late 2023. Uses a dedicated EKS agent on each node rather than an OIDC provider. IAM trust policies use `service:eks.amazonaws.com` as the principal instead of the cluster-specific OIDC URL. Pod Identity associations are managed through the EKS API and Terraform resource `aws_eks_pod_identity_association` rather than IAM annotations on service accounts.

## Decision

Use EKS Pod Identity for all pod-level AWS credential grants. **No pod uses IRSA.**

Trust policies reference `service:eks.amazonaws.com` as the principal with `condition: aws:SourceAccount = {account_id}`.

Two details of the mechanism differ from what a reader might expect:

- **Associations are not bare `aws_eks_pod_identity_association` resources.** No such resource appears anywhere in `terraform/`. Every grant goes through the community `terraform-aws-modules/eks-pod-identity/aws` module, which bundles the IAM role, its policy, and an `associations = { … }` map of `{cluster_name, namespace, service_account}` in one block — see `terraform/modules/addons/cert-manager.tf` for the canonical shape. There are ~13 such modules (argo controller/server/runner, external-secrets, cert-manager, aws-lbc, external-dns, grafana, prefect worker, kubecost agent + its AMP/Prometheus roles, EBS CSI, CloudWatch observability).
- **`enable_irsa = true` is still set** on the EKS module (`terraform/eks.tf:48`), so the cluster's IAM OIDC provider *does* exist. Nothing consumes it: the only `sts:AssumeRoleWithWebIdentity` in the stack is `terraform/ecr.tf`, and that is the unrelated **GitHub Actions** OIDC provider (`token.actions.githubusercontent.com`) used by CI to push images, not the cluster's. So "IRSA is not configured" is best read as "no workload uses IRSA" — the provider is a dormant leftover, not a live second path.

## Consequences

- IAM trust policies are not coupled to the cluster's OIDC endpoint — replacing the cluster does not require updating every trust policy
- Pod Identity associations are managed in Terraform alongside the IAM roles, keeping IAM and K8s-level configuration in the same place
- The `eks-pod-identity-agent` DaemonSet must be running on all nodes; it is deployed as an EKS managed add-on in `terraform/addons.tf`
- The `useStaticCredentials: false` setting in the Argo Workflows Helm values is required — Argo must not inject static AWS credentials; the Pod Identity agent handles credential injection transparently via the instance metadata service
- External Secrets Operator, Argo controller, Argo server, Prefect worker, and all pipeline runner pods each have their own Pod Identity association — no shared credentials between components
- The `eks-pod-identity-agent` add-on carries `before_compute = true` (`terraform/eks.tf`), so it is installed ahead of any node group — a pod scheduled on a brand-new node has a working credential path from the start rather than racing the add-on install
- IRSA is not wired up as a fallback; if Pod Identity stops working (agent crash, misconfigured association), affected pods will receive no AWS credentials. The dormant OIDC provider means recovery would be *possible* without recreating cluster infrastructure, but every trust policy would still have to be rewritten
