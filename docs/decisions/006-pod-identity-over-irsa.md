# 006 — EKS Pod Identity over IRSA

**Status**: Accepted

## Context

Pipeline pods need AWS API access (S3, SSM, EC2) without embedding long-lived credentials. Two AWS-native mechanisms provide credential-less IAM access for EKS pods:

**IRSA (IAM Roles for Service Accounts)**: An OIDC provider is registered in IAM for the EKS cluster; pods annotate their service account with an IAM role ARN; the pod's token is exchanged for temporary credentials via `sts:AssumeRoleWithWebIdentity`. IRSA has been available since EKS 1.13. It requires managing an OIDC provider URL in every IAM trust policy, which creates a dependency on the specific cluster's OIDC endpoint.

**EKS Pod Identity**: Introduced in EKS 1.24, GA in late 2023. Uses a dedicated EKS agent on each node rather than an OIDC provider. IAM trust policies use `service:eks.amazonaws.com` as the principal instead of the cluster-specific OIDC URL. Pod Identity associations are managed through the EKS API and Terraform resource `aws_eks_pod_identity_association` rather than IAM annotations on service accounts.

## Decision

Use EKS Pod Identity for all pod-level AWS credential grants. IRSA is not configured on the cluster.

Trust policies reference `service:eks.amazonaws.com` as the principal with `condition: aws:SourceAccount = {account_id}`. Each service account that needs AWS access has a corresponding `aws_eks_pod_identity_association` in the relevant Terraform module.

## Consequences

- IAM trust policies are not coupled to the cluster's OIDC endpoint — replacing the cluster does not require updating every trust policy
- Pod Identity associations are managed in Terraform alongside the IAM roles, keeping IAM and K8s-level configuration in the same place
- The `eks-pod-identity-agent` DaemonSet must be running on all nodes; it is deployed as an EKS managed add-on in `terraform/addons.tf`
- The `useStaticCredentials: false` setting in the Argo Workflows Helm values is required — Argo must not inject static AWS credentials; the Pod Identity agent handles credential injection transparently via the instance metadata service
- External Secrets Operator, Argo controller, Argo server, Prefect worker, and all pipeline runner pods each have their own Pod Identity association — no shared credentials between components
- IRSA is not available as a fallback; if Pod Identity stops working (agent crash, misconfigured association), affected pods will receive no AWS credentials
