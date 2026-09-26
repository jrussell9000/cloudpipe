################################################################################
# Prefix-scoped writer roles, one per storage gateway
#
# `retire-globus-s3-access-keys` tasks 2.1–2.2, which that change's D8 moves ahead
# into this one: the AMI already bakes the loopback signing listener (10.1), and
# the staging test bed this change needs cannot exist without the role the
# listener assumes.
#
# WHY A ROLE AT ALL, when the S3 connector supports only static keys: it is the
# LISTENER that talks to AWS, not Globus. Globus signs with a dummy key that
# Envoy discards, Envoy re-signs with credentials from this role, and the key
# Globus holds therefore authenticates nothing. So the credential in the ingress
# path becomes a ~1 hour STS session instead of an access key that has never been
# rotated.
#
# ADDITIVE, AND NOTHING USES THESE YET (task 2.4: the plan must show only
# creations). Production keeps its IAM user until that change's section 8 cuts
# over. The roles exist first so confinement can be PROVEN read-only —
# `iam:SimulatePrincipalPolicy` per prefix, task 2.3 — before a byte moves
# through them.
#
# WHY ONE ROLE PER GATEWAY rather than one shared proxy role (D3): confinement
# stays an IAM property that AWS enforces, so the proxy is not in the security
# path and cannot enforce it wrongly. The rejected alternative — one role, with
# prefix rules inside proxy configuration — is the same objection that made
# pointing staging at production's IAM user unacceptable.
################################################################################

locals {
  # Ingress prefix for the production gateway. Declared here because nothing else
  # declares it: `globus-dest-base-path` is a REQUIRED Argo workflow parameter
  # with no default, supplied per submission, and its only standing value is the
  # Prefect queue manager's default (`prefect/flows/cloudpipe_queue_manager.py`,
  # `globus_dest_base_path: str = "/mmps_mproc"`). A role cannot be scoped to a
  # value a submitter chooses, so scoping it means declaring it.
  #
  # Keep this equal to that default. `tests/test_terraform_globus_s3_roles.py`
  # holds the two together, because nothing at plan time can: a divergence would
  # otherwise surface as AccessDenied on the first object of a transfer, after the
  # instance is up and the Globus session is established.
  globus_production_prefix = trim(var.globus_destination_prefix, "/")

  # One entry per storage gateway. The KEY is the Globus storage gateway display
  # name, which is also the systemd instance name (`cloudpipe-s3-listener@<key>`)
  # and the rendered listener's filename, so it is the one identifier that has to
  # agree across Globus, systemd and this module.
  #
  # Ports are WRITTEN DOWN, not derived. Two listeners on one host cannot share a
  # port, and `port = base + n` makes a collision an arithmetic surprise instead
  # of a visible line in this file.
  #
  # ONE PORT PER GATEWAY, no admin port. An earlier draft reserved 9444/9443 for
  # Envoy's admin interface, because the reference config binds it to
  # 127.0.0.1:9901 and two gateways cannot both do that. The listener render
  # settled it the other way: there is no admin interface, since it is
  # unauthenticated and serves /quitquitquit and /config_dump, and anything that
  # can reach the listener can reach admin. Reserving ports nothing binds would
  # only send a later reader looking for the process on 9444.
  #
  # `cut_over` says whether the Globus gateway is actually registered against its
  # listener. It is published in the config document (main.tf) as the gateway's
  # `s3_listener`, which is what `doctor` check 12 verifies — so it must describe
  # the live gateway, not the intended one. Staging was created against its
  # listener (`retire-globus-s3-access-keys` 5.2). Production still signs with its
  # IAM user's key and flips here in that change's 8.3, in the same PR as the
  # cutover itself.
  s3_gateways = merge(
    {
      (var.globus_collection_name) = {
        slug     = "production"
        prefix   = local.globus_production_prefix
        port     = 8444
        cut_over = false
      }
    },
    var.globus_staging_enabled ? {
      (local.staging_gateway_name) = {
        slug     = "staging"
        prefix   = local.staging_prefix
        port     = 8443
        cut_over = true
      }
    } : {},
  )

  # Derived from config only, never from the role resource: the config document
  # names these roles, and `render.gcs_config` must produce the same document from
  # the answers alone (tests/test_terraform_globus_config.py), with no state.
  s3_gateway_role_names = {
    for gateway_name, gateway in local.s3_gateways :
    gateway_name => "${var.name}-globus-${gateway.slug}-writer"
  }

  # What the config document says about each gateway's listener: the role it
  # signs as and the endpoint Globus was registered with — `https://127.0.0.1`,
  # the form staging was created with and GCS stored (`policies.s3_endpoint`).
  # Null until the gateway is cut over, so a gateway still on a static key
  # declares no listener rather than one it does not use.
  s3_listener_declarations = {
    for gateway_name, gateway in local.s3_gateways :
    gateway_name => gateway.cut_over ? {
      writer_role = local.s3_gateway_role_names[gateway_name]
      endpoint    = "https://127.0.0.1:${gateway.port}"
    } : null
  }
}

################################################################################
# Trust: the GCS instance role, and nothing else
#
# No external id and no session-name condition. The principal is a single role in
# this account, which is a stronger statement than either: an external id guards
# against a confused third party, and there is no third party here.
################################################################################

data "aws_iam_policy_document" "s3_gateway_assume" {
  statement {
    sid     = "OnlyTheGcsInstance"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.globus.arn]
    }
  }
}

resource "aws_iam_role" "s3_gateway" {
  for_each = local.s3_gateways

  name               = local.s3_gateway_role_names[each.key]
  description        = "Assumed by the loopback S3 signing listener for the ${each.key} Globus storage gateway. Confined to ${each.value.prefix}/ in ${var.globus_s3_bucket}."
  assume_role_policy = data.aws_iam_policy_document.s3_gateway_assume.json

  # One hour is the default and is deliberately not raised. The point of the
  # change is that the credential in the ingress path expires; a long session
  # walks that back. Envoy refreshes it for as long as the listener runs.
  max_session_duration = 3600

  tags = {
    Name    = "${var.name}-globus-${each.value.slug}-writer"
    Gateway = each.key
  }
}

################################################################################
# What each role may do, confined to its own prefix
#
# The action list deliberately MATCHES what the gateway's IAM user can do today,
# rather than the shorter list in task 2.1 — `s3:DeleteObject` and
# `s3:ListMultipartUploadParts` are kept. The cutover's job is to change how long
# the credential lives, not what it can reach: changing both at once makes any
# failure during it ambiguous, and "the transfer broke" would be indistinguishable
# from "the new role is missing an action". Narrowing is a later, separate change
# with its own evidence.
#
# What DOES change is reach: the production user has these actions across the
# WHOLE bucket, and this role has them under one prefix.
################################################################################

data "aws_iam_policy_document" "s3_gateway_writer" {
  for_each = local.s3_gateways

  statement {
    sid    = "ObjectsWithinPrefix"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = [
      "arn:${var.partition}:s3:::${var.globus_s3_bucket}/${each.value.prefix}/*",
    ]
  }

  # Listing is bucket-scoped in IAM, so the prefix restriction is a condition
  # rather than a resource. Note what this denies: a listing that arrives with an
  # EMPTY prefix — a client browsing the gateway root — rather than letting it
  # enumerate the bucket.
  #
  # That matters more than it looks. A collection rooted at the bucket root turns
  # `ls /` on the collection into exactly such a listing, and `doctor` check 9
  # ("Destination collection listing") lists `/` on every run. Staging is rooted
  # at its prefix so its `/` arrives as `<prefix>/` and matches; PRODUCTION is
  # rooted at the bucket root (`base_path = "/<bucket>"` in main.tf), so once
  # production uses this role, check 9 fails until either the collection is
  # re-rooted at this prefix or the check lists the prefix instead of `/`. Left
  # unresolved on purpose — the roles are unused today, and both fixes touch
  # things this commit should not.
  statement {
    sid    = "ListWithinPrefix"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:ListBucketMultipartUploads",
    ]
    resources = ["arn:${var.partition}:s3:::${var.globus_s3_bucket}"]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        "${each.value.prefix}/*",
        each.value.prefix,
      ]
    }
  }

  statement {
    sid       = "BucketLocation"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation"]
    resources = ["arn:${var.partition}:s3:::${var.globus_s3_bucket}"]
  }

  # The S3 connector documents s3:ListAllMyBuckets so it can populate a root
  # listing. AWS cannot resource-scope it, and it reveals only bucket names.
  statement {
    sid       = "ListAllMyBuckets"
    effect    = "Allow"
    actions   = ["s3:ListAllMyBuckets"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "s3_gateway_writer" {
  for_each = local.s3_gateways

  name   = "${var.name}-globus-${each.value.slug}-writer"
  role   = aws_iam_role.s3_gateway[each.key].name
  policy = data.aws_iam_policy_document.s3_gateway_writer[each.key].json
}

################################################################################
# The instance may assume exactly these roles
#
# Task 2.2. Named ARNs, not a wildcard on the account's roles: `sts:AssumeRole`
# on `*` would let the host reach every role that trusts it, which is the
# confinement this file exists to establish, spent.
#
# NOTE, for the listener render and for that change's section 9: the instance role
# ALSO still holds `s3:*Object` on the whole bucket (`aws_iam_policy.globus_s3`
# in main.tf). So this grant does not yet reduce what the host can reach — it
# establishes what the LISTENER signs as. The reduction lands when that direct
# grant goes, which is the last step of the cutover and not this one. Until then
# the listener's config is what keeps the two apart: Envoy's credential chain
# ends at instance metadata, so a listener that did not set
# `custom_credential_provider_chain` would fall back to the instance profile and
# sign with bucket-wide credentials (PROVEN against the pinned Envoy 1.39.1 —
# see the render in task 3.1).
################################################################################

data "aws_iam_policy_document" "globus_assume_gateway_roles" {
  statement {
    sid       = "AssumeGatewayWriterRoles"
    effect    = "Allow"
    actions   = ["sts:AssumeRole"]
    resources = [for role in aws_iam_role.s3_gateway : role.arn]
  }
}

resource "aws_iam_role_policy" "globus_assume_gateway_roles" {
  name   = "${var.name}-globus-assume-gateway-roles"
  role   = aws_iam_role.globus.name
  policy = data.aws_iam_policy_document.globus_assume_gateway_roles.json
}
