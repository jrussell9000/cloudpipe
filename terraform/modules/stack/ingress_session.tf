# The three SSM parameters `ingress-mode=https` reads, kept outside any module a
# deployment can switch off.
#
# They used to live in `module.globus`, which is counted on `globus_enabled`. That
# made HTTPS ingress depend on the Globus host's module for no reason of its own:
# a deployment with the ingress off had no session clock and no source to read,
# and destroying the host would have destroyed the login timestamp with it
# (openspec/changes/retire-globus-host, design D5). Readers:
#
#   session-established-at  the ingress pod (images/globus/https_ingress.py), the
#                           queue manager's batch gate, `globus doctor`
#   source-collection-id    the queue manager (ingress_params), fmriprep_bench.py
#   source-base-path        the same
#
# The paths are unchanged, because every reader spells them out: Python, the Argo
# runner's IAM policy and the operator CLI all name `/cloudpipe/globus/...`. The
# prefix is the module's own default derivation, `/${name}/globus`. The stack has
# never passed `globus_ssm_prefix`, so the addresses move and the names do not.

locals {
  ingress_ssm_prefix = "/${local.name}/globus"

  # The value an unset parameter holds. It is the placeholder `module.globus`
  # always used, so every reader that already refuses it keeps refusing it: the
  # pod and the gate cannot parse it as a time, and a collection id that is not a
  # UUID fails the gate's listing.
  ingress_ssm_placeholder = "REPLACE_AFTER_GCS_SETUP"
}

resource "aws_ssm_parameter" "ingress_source_collection_id" {
  name  = "${local.ingress_ssm_prefix}/source-collection-id"
  type  = "String"
  value = var.globus_source_collection_id != "" ? var.globus_source_collection_id : local.ingress_ssm_placeholder

  tags = {
    Name = "${local.name}-globus-source-collection-id"
  }
}

resource "aws_ssm_parameter" "ingress_source_base_path" {
  name  = "${local.ingress_ssm_prefix}/source-base-path"
  type  = "String"
  value = var.globus_source_base_path != "" ? var.globus_source_base_path : local.ingress_ssm_placeholder

  tags = {
    Name = "${local.name}-globus-source-base-path"
  }
}

# When the Globus session behind the stored refresh token was established.
#
# Written by `globus login`, and never again by Terraform: `ignore_changes` is
# what keeps an apply from resetting the clock a batch depends on. Deliberately
# not derived from the secret's LastChangedDate, which also moves when the secret
# is edited for an unrelated reason.
resource "aws_ssm_parameter" "ingress_session_established_at" {
  name  = "${local.ingress_ssm_prefix}/session-established-at"
  type  = "String"
  value = local.ingress_ssm_placeholder

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${local.name}-globus-session-established-at"
  }
}

# From the counted module to here. Without these, the deployment with the ingress
# on plans a destroy and a create of all three, and the create writes the
# placeholder over the live login timestamp: `ignore_changes` protects a value
# only on an object Terraform already holds. A deployment with the ingress off
# has nothing at the old addresses, so for it these blocks are no-ops and the
# three parameters are simply created.
moved {
  from = module.globus[0].aws_ssm_parameter.globus_source_collection_id
  to   = aws_ssm_parameter.ingress_source_collection_id
}

moved {
  from = module.globus[0].aws_ssm_parameter.globus_source_base_path
  to   = aws_ssm_parameter.ingress_source_base_path
}

moved {
  from = module.globus[0].aws_ssm_parameter.globus_session_established_at
  to   = aws_ssm_parameter.ingress_session_established_at
}
