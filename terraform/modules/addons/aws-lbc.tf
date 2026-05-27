# AWS LOAD BALANCER CONTROLLER
# Helm release managed by ArgoCD (gitops/apps/aws-load-balancer-controller/)
# Pod Identity Association keeps the IAM binding in Terraform; SA is created by Helm
module "aws_lbc_pod_identity" {
  source = "terraform-aws-modules/eks-pod-identity/aws"

  name                         = "aws-load-balancer-controller"
  attach_aws_lb_controller_policy = true

  associations = {
    cloudpipe = {
      cluster_name    = var.cluster_name
      namespace       = "aws-load-balancer-controller"
      service_account = "aws-load-balancer-controller"
    }
  }
}
