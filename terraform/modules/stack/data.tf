################################################################################
# Common data
#
# These lived in the root's providers.tf until the stack was extracted. The
# root keeps its own copies of only what a provider block configures itself
# with; everything the stack's resources read is declared here, so the module
# is self-contained and an example root does not have to supply them.
#
# Duplicating a data source is free: it is a read, not a managed object, so the
# same lookup in two modules costs one extra API call and no state.
################################################################################

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# Resolves the underlying IAM role ARN regardless of credential type (SSO, assumed-role, user).
# aws_caller_identity.current.arn returns a session ARN for SSO logins, which EKS access entries
# cannot match. aws_iam_session_context.current.issuer_arn always returns the IAM role ARN.
data "aws_iam_session_context" "current" {
  arn = data.aws_caller_identity.current.arn
}

data "aws_availability_zones" "available" {
  # Exclude Local Zones (e.g. <region>-bos-1a) — not supported by all instance types.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}
