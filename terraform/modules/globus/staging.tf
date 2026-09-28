################################################################################
# Staging test bed for the Globus ingress
#
# A second High Assurance S3 storage gateway and collection live on the SAME GCS
# endpoint as production (they are created in Globus, not here — see the
# `simplify-globus-ingress` OpenSpec change). Everything they can touch is
# confined to one S3 prefix, so a change can be proven against real ABCD data
# and a real HA gateway without production being able to notice.
#
# This file owns only the AWS side of that: the two (empty) Secrets Manager
# containers the operator CLI writes into. Values are never set here — a secret
# string set by Terraform is a secret in Terraform state.
#
# Confinement used to rest on two independent things, the collection's root prefix
# and a named IAM user's prefix-scoped S3 policy. `retire-globus-s3-access-keys` 9.2
# removed the second, because there is no IAM user any longer: staging signs through
# its own loopback listener assuming `<name>-globus-staging-writer`, which
# `s3_gateway_roles.tf` confines to the staging prefix. So the second barrier still
# exists — it is an IAM ROLE now, declared beside production's, and it is the
# stronger form, because a role cannot accumulate another policy from outside this
# module the way a shared departmental user could.
################################################################################

locals {
  staging_gateway_name = "${var.globus_collection_name}-staging"
  staging_prefix       = trim(var.globus_staging_prefix, "/")

  # Secrets Manager names. Kept parallel to the production pair so the operator
  # CLI can derive both from one environment table:
  #   globus/refresh-token           <-> globus/refresh-token-staging
  #   globus/s3-gateway/<gateway>    <-> globus/s3-gateway/<gateway>-staging
  staging_token_secret_name      = "globus/refresh-token-staging"
  staging_credential_secret_name = "globus/s3-gateway/${local.staging_gateway_name}"
}


################################################################################
# Secrets Manager containers (values written by the operator CLI, not Terraform)
#
# recovery_window_in_days = 0 so the staging pair can be deleted and recreated
# without waiting out a recovery window — staging is disposable by design. The
# production equivalents keep the default window.
################################################################################

resource "aws_secretsmanager_secret" "globus_staging_s3_credential" {
  count                   = var.globus_staging_enabled ? 1 : 0
  name                    = local.staging_credential_secret_name
  description             = "AWS access key registered with the ${local.staging_gateway_name} Globus S3 storage gateway. Written by `globus rotate-s3-key --env staging`."
  recovery_window_in_days = 0

  tags = {
    Name = local.staging_credential_secret_name
  }
}

resource "aws_secretsmanager_secret" "globus_staging_refresh_token" {
  count                   = var.globus_staging_enabled ? 1 : 0
  name                    = local.staging_token_secret_name
  description             = "Globus refresh token for the staging collection. Written by `globus login --env staging`."
  recovery_window_in_days = 0

  tags = {
    Name = local.staging_token_secret_name
  }
}

################################################################################
# The GCS instance reads the staging S3 credential while reconciling the staging
# gateway. It never reads the refresh token — that one is for the transfer
# client (Argo pods and the probe), not for the server.
################################################################################

data "aws_iam_policy_document" "globus_staging_secret_read" {
  count = var.globus_staging_enabled ? 1 : 0

  statement {
    sid       = "ReadStagingGatewayCredential"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.globus_staging_s3_credential[0].arn]
  }
}

resource "aws_iam_role_policy" "globus_staging_secret_read" {
  count  = var.globus_staging_enabled ? 1 : 0
  name   = "${var.name}-globus-staging-secret-read"
  role   = aws_iam_role.globus.name
  policy = data.aws_iam_policy_document.globus_staging_secret_read[0].json
}
