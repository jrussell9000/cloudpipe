######################
# Globus Connect Server
######################

data "aws_subnet" "public" {
  id = var.public_subnet_id
}

locals {
  # Where this deployment's Globus state lives in Parameter Store. Defaulted
  # rather than required so production's paths are unchanged: a Terraform
  # variable default cannot interpolate another variable, so the derivation has
  # to happen here.
  ssm_prefix = var.globus_ssm_prefix != "" ? var.globus_ssm_prefix : "/${var.name}/globus"
}

# The base image used to come from a `data "aws_ami"` filtered to one hard-coded
# Ubuntu 22.04 image id — a lookup whose answer was already known, which failed
# closed only because the filter matched exactly one image. It is now an input,
# because the instance no longer boots a stock Ubuntu image: it boots the AMI
# built from packer/globus-gcs/, with Globus Connect Server, Envoy and the
# reconcile package already installed and nothing deployment-specific baked in.

# Security Group
resource "aws_security_group" "globus" {
  name_prefix = "${var.name}-globus-"
  description = "Globus Connect Server"
  vpc_id      = var.vpc_id
}

# Ports for SSH (443) and GridFTP (50000-51000) must be open to 0.0.0.0/0 per Globus Connect Server v5
# architecture. HTTPS collections are accessed directly by clients (not relayed through Globus
# infrastructure), and GridFTP data channels are peer-to-peer between endpoints worldwide.
# Restricting either to specific CIDRs breaks interoperability with other institutions.
# Access control is enforced by Globus OAuth2/OIDC authentication, not network-layer filtering.
# See: https://docs.globus.org/globus-connect-server/v5/#open-tcp-ports_section
resource "aws_vpc_security_group_ingress_rule" "globus_https_v4" {
  security_group_id = aws_security_group.globus.id
  description       = "HTTPS inbound - required open to all (Globus HTTPS collections + GCS Manager API)"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "globus_https_v6" {
  security_group_id = aws_security_group.globus.id
  description       = "HTTPS inbound IPv6 - required open to all (Globus HTTPS collections + GCS Manager API)"
  cidr_ipv6         = "::/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "globus_gridftp" {
  security_group_id = aws_security_group.globus.id
  description       = "GridFTP data channel - required open to all (peer-to-peer between Globus endpoints)"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 50000
  to_port           = 51000
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "globus_ssh" {
  security_group_id = aws_security_group.globus.id
  description       = "SSH from managed prefix list"
  prefix_list_id    = var.globus_admin_prefix_list_id
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "globus_http" {
  security_group_id = aws_security_group.globus.id
  description       = "HTTP - Ubuntu package repository"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "globus_https" {
  security_group_id = aws_security_group.globus.id
  description       = "HTTPS - Globus REST API and S3"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "globus_gridftp" {
  security_group_id = aws_security_group.globus.id
  description       = "GridFTP data channel outbound"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 50000
  to_port           = 51000
  ip_protocol       = "tcp"
}

# IAM
data "aws_iam_policy_document" "globus_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "globus" {
  name_prefix        = "${var.name}-globus-"
  assume_role_policy = data.aws_iam_policy_document.globus_assume_role.json
}

data "aws_iam_policy_document" "globus_s3" {
  statement {
    sid    = "GlobusS3Access"
    effect = "Allow"
    actions = [
      "s3:GetBucketLocation",
      "s3:ListBucket",
      "s3:ListBucketMultipartUploads",
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = [
      "arn:${var.partition}:s3:::${var.globus_s3_bucket}",
      "arn:${var.partition}:s3:::${var.globus_s3_bucket}/*",
    ]
  }
}

resource "aws_iam_policy" "globus_s3" {
  name_prefix = "${var.name}-globus-s3-"
  policy      = data.aws_iam_policy_document.globus_s3.json
}

resource "aws_iam_role_policy_attachment" "globus_s3" {
  role       = aws_iam_role.globus.name
  policy_arn = aws_iam_policy.globus_s3.arn
}

resource "aws_iam_role_policy_attachment" "globus_ssm" {
  role       = aws_iam_role.globus.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "globus" {
  name_prefix = "${var.name}-globus-"
  role        = aws_iam_role.globus.name
}

# EC2 Instance
resource "aws_instance" "globus" {
  ami                    = var.globus_ami_id
  instance_type          = "c5n.xlarge"
  subnet_id              = var.public_subnet_id
  vpc_security_group_ids = [aws_security_group.globus.id]
  iam_instance_profile   = aws_iam_instance_profile.globus.name

  # Two values, and that is the whole of it. Everything this host does at boot
  # is in the AMI; the only thing it cannot know is which deployment it belongs
  # to, so that is the only thing passed (D11). The endpoint setup script, the
  # apt installs and the re-registration script that used to be written here are
  # all gone — they were unreadable, untestable heredocs that re-derived host
  # setup on every launch, and two of their installs no longer work on 24.04.
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    ssm_prefix = local.ssm_prefix
    region     = var.region
  })

  # An in-place user_data update, not a replacement — but EC2 can only accept one
  # while the instance is STOPPED, which is this instance's normal state. Apply a
  # change to the template while it is running and the apply fails with
  # IncorrectInstanceState, having changed nothing.
  user_data_replace_on_change = false

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 50
    encrypted             = true
    delete_on_termination = true
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  tags = {
    Name = "${var.name}-globus"
  }
}

# Elastic IP
resource "aws_eip" "globus" {
  domain = "vpc"

  tags = {
    Name = "${var.name}-globus"
  }
}

resource "aws_eip_association" "globus" {
  instance_id   = aws_instance.globus.id
  allocation_id = aws_eip.globus.id
}

################################################################################
# SSM Parameters — instance ID and collection ID for workflow use
################################################################################

# Stored in SSM so the Argo start-instance step can discover the instance ID
# at runtime without baking it into the workflow template.
resource "aws_ssm_parameter" "globus_instance_id" {
  name  = "${local.ssm_prefix}/instance-id"
  type  = "String"
  value = aws_instance.globus.id

  tags = {
    Name = "${var.name}-globus-instance-id"
  }
}

# Which gateway the production session gate measures against.
#
# Published rather than derived. The queue manager's gate reads the declared
# `authentication_timeout_mins` out of the configuration document, and that
# document has declared TWO gateways since staging landed — so without a name
# the lookup fell back to a default (#506). It returned the right number only
# because the default and production's declaration are both 30 days, and would
# have diverged the moment anyone lowered the production timeout.
#
# The obvious shortcut — "the gateway that is not staging" — is the mistake
# task 11.4a already made from the other direction, deriving a display name that
# described no live gateway. The name is an input here, so it is published as
# one.
resource "aws_ssm_parameter" "globus_gateway_name" {
  name  = "${local.ssm_prefix}/gateway-name"
  type  = "String"
  value = local.globus_gateway_display_name

  tags = {
    Name = "${var.name}-globus-gateway-name"
  }
}

resource "aws_ssm_parameter" "globus_source_collection_id" {
  name  = "${local.ssm_prefix}/source-collection-id"
  type  = "String"
  value = var.globus_source_collection_id

  tags = {
    Name = "${var.name}-globus-source-collection-id"
  }
}

resource "aws_ssm_parameter" "globus_source_base_path" {
  name  = "${local.ssm_prefix}/source-base-path"
  type  = "String"
  value = var.globus_source_base_path

  tags = {
    Name = "${var.name}-globus-source-base-path"
  }
}

# When the Globus session behind the stored refresh token was established.
#
# Written by `globus login`, read by `globus doctor` and by the queue manager's
# batch gate. Deliberately NOT derived from the secret's LastChangedDate: that
# also moves when the secret is edited for an unrelated reason, which would
# silently reset the clock a 300-subject batch depends on.
#
# Terraform creates it empty and never writes it again. An empty value reads as
# "no login recorded", which the gate treats as unusable — the safe direction.
resource "aws_ssm_parameter" "globus_session_established_at" {
  name  = "${local.ssm_prefix}/session-established-at"
  type  = "String"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-session-established-at"
  }
}

################################################################################
# The GCS configuration document — the declared state the on-instance reconcile
# compares the endpoint against (`/<name>/globus/config`).
#
# Shape and rules: src/globus_admin/schemas/gcs-config.schema.json, validated at
# runtime by globus_admin.configdoc. `globus doctor` fails its configuration
# check if what lands here does not satisfy that validator, so a malformed render
# is caught by the checklist rather than by a half-finished reconcile.
#
# NO SECRET VALUE APPEARS HERE. `credential_secret` names a Secrets Manager
# secret; the key inside it is written by `globus rotate-s3-key` and read on the
# instance. Terraform never sees it, so it never enters Terraform state.
################################################################################

locals {
  # Globus takes minutes; operators think in days. One conversion, here, rather
  # than a magic 43200 repeated across tfvars and documentation.
  globus_session_timeout_mins = var.globus_session_timeout_days * 24 * 60

  # The gateway's display name is the collection's unless one is given. A
  # gateway created before this tooling need not follow the derivation — this
  # deployment's is `cloudpipe-s3-gateway` beside a `cloudpipe-s3` collection —
  # and the reconcile matches objects by display name, so a derived-but-wrong
  # name declares a gateway that does not exist. `render.py` derives the same
  # way, and `tests/test_terraform_globus_config.py` holds the two together.
  globus_gateway_display_name = (
    trimspace(var.globus_gateway_name) != "" ? trimspace(var.globus_gateway_name) : var.globus_collection_name
  )

  globus_production_gateway = {
    display_name                = local.globus_gateway_display_name
    type                        = "s3"
    bucket                      = var.globus_s3_bucket
    domains                     = [var.globus_identity_domain]
    high_assurance              = true
    authentication_timeout_mins = local.globus_session_timeout_mins
    credential_secret           = "globus/s3-gateway/${var.globus_collection_name}"
    # Null until `retire-globus-s3-access-keys` 8.3 cuts production over; `doctor`
    # check 12 reports that as "not cut over yet" rather than failing. Keyed by
    # the collection name because that is how `local.s3_gateways` keys production.
    s3_listener = local.s3_listener_declarations[var.globus_collection_name]
    # Production starts unmanaged: the reconcile reports its drift and applies
    # nothing. It is flipped to true deliberately, once staging has proved the
    # plan, so the first run of new code cannot touch the live gateway.
    managed = var.globus_production_managed
  }

  globus_staging_gateway = {
    display_name                = "${var.globus_collection_name}-staging"
    type                        = "s3"
    bucket                      = var.globus_s3_bucket
    domains                     = [var.globus_identity_domain]
    high_assurance              = true
    authentication_timeout_mins = local.globus_session_timeout_mins
    credential_secret           = "globus/s3-gateway/${var.globus_collection_name}-staging"
    # `lookup`, not an index: this block is evaluated even when staging is
    # disabled, and `local.s3_gateways` then has no staging entry.
    s3_listener = lookup(local.s3_listener_declarations, local.staging_gateway_name, null)
    managed     = true
  }

  globus_config = {
    storage_gateways = concat(
      [local.globus_production_gateway],
      var.globus_staging_enabled ? [local.globus_staging_gateway] : [],
    )
    # The two collections differ in visibility, and the difference is declared
    # rather than left to create-time flags in code. `simplify-globus-ingress`
    # 8.8 rebuilt the staging collection from config and it came back PUBLIC and
    # guest-enabled, because the reconcile's flags were constants copied from
    # production's setup appendix while staging was created `--private
    # --force-encryption --no-allow-guest-collections` (`retire` 5.2a). Stating
    # both here is what stops a recreate from silently widening a collection
    # rooted at the ABCD bucket.
    collections = concat(
      [{
        display_name = var.globus_collection_name
        gateway      = local.globus_gateway_display_name
        base_path    = "/${var.globus_s3_bucket}"
        managed      = var.globus_production_managed
        # Production is the collection the pipeline and the operator browse to.
        # Read off the live collection in the 1.1 snapshot, not assumed. The
        # first version of this block declared `force_encryption = false` from
        # the setup appendix, which does not pass the flag — but production has
        # it ON. Inert while nothing compared the field; the moment 11.10 adds
        # the comparison, a `configure --yes` would have turned forced
        # encryption OFF on the production collection, because production is
        # `managed: true`.
        public                  = true
        allow_guest_collections = true
        force_encryption        = true
      }],
      var.globus_staging_enabled ? [{
        display_name = "${var.globus_collection_name}-staging"
        gateway      = "${var.globus_collection_name}-staging"
        base_path    = "/${var.globus_s3_bucket}/${trim(var.globus_staging_prefix, "/")}"
        managed      = true
        # Staging is a test bed inside the same bucket. Nothing browses to it and
        # nothing shares from it, so it is closed on every axis it can be.
        public                  = false
        allow_guest_collections = false
        force_encryption        = true
      }] : [],
    )
    roles = []
  }
}

resource "aws_ssm_parameter" "globus_config" {
  name = "${local.ssm_prefix}/config"
  type = "String"
  # `jsonencode` sorts object keys, so an unchanged declaration renders
  # byte-identical and produces no diff. That is what lets the SSM association
  # trigger on a real change to the document rather than on every apply.
  value = jsonencode(local.globus_config)

  tags = {
    Name = "${var.name}-globus-config"
  }
}

# WHERE `doctor` check 9 should list, per environment. Published rather than
# derived, because the two collections are rooted differently and deriving it is
# what went wrong: check 9 listed a hard-coded `/`, which is the collection root,
# and for production that is the BUCKET root — an empty S3 prefix. Harmless while
# the gateway held a bucket-wide key; an AccessDenied the moment the writer role is
# confined to a prefix, reported as a failed listing with no hint that the path was
# the problem.
#
# The asymmetry is real and is the reason this is one value per environment rather
# than one rule:
#
#   production collection base_path = /<bucket>                  -> list /<prefix>
#   staging    collection base_path = /<bucket>/<staging prefix> -> list /
#
# So staging's check was passing for a reason that has nothing to do with being
# correct, and a single shared rule would have broken it. Terraform knows both the
# rooting and the prefix; `doctor` should not re-infer either.
resource "aws_ssm_parameter" "globus_destination_listing_path" {
  name = "${local.ssm_prefix}/destination-listing-path"
  type = "String"
  # Collection-relative, so the leading slash is the collection root and the rest
  # is the key prefix the production writer role permits.
  value = "/${local.globus_production_prefix}"

  tags = {
    Name = "${var.name}-globus-destination-listing-path"
  }
}

resource "aws_ssm_parameter" "globus_staging_destination_listing_path" {
  count = var.globus_staging_enabled ? 1 : 0

  name = "${local.ssm_prefix}/staging/destination-listing-path"
  type = "String"
  # The staging collection is already rooted inside its own prefix, so its root IS
  # the confined prefix and there is nothing to append.
  value = "/"

  tags = {
    Name = "${var.name}-globus-staging-destination-listing-path"
  }
}

resource "aws_ssm_parameter" "globus_staging_session_established_at" {
  count = var.globus_staging_enabled ? 1 : 0

  name  = "${local.ssm_prefix}/staging/session-established-at"
  type  = "String"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-staging-session-established-at"
  }
}

# The staging twin of `globus_collection_id` below, and required for the same
# reason: `globus configure --env staging` writes the live collection's id here,
# and `globus doctor --env staging` lists it among the parameters that must hold
# a real value before a transfer can run.
#
# It was missing until the staging collection actually existed, and the shape of
# the omission is worth keeping in mind: `configure` deliberately does NOT create
# a parameter it finds absent — it reports `NO_PARAMETER` and says "run
# `terraform apply -target=module.globus` to create it". That is the right
# boundary (Terraform owns containers, the CLI owns values), but with no resource
# declared it closes a loop: the CLI sends you to an apply that creates nothing,
# and the collection id stays unrecorded.
resource "aws_ssm_parameter" "globus_staging_collection_id" {
  count = var.globus_staging_enabled ? 1 : 0

  name  = "${local.ssm_prefix}/staging/collection-id"
  type  = "String"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-staging-collection-id"
  }
}

# Placeholder — overwritten by `pixi run globus bootstrap-endpoint` after the
# one-time interactive Globus login. Workflow submissions read it at runtime.
resource "aws_ssm_parameter" "globus_collection_id" {
  name  = "${local.ssm_prefix}/collection-id"
  type  = "String"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    # Terraform owns the container, not the value: without this, every apply
    # would revert what bootstrap-endpoint wrote to the placeholder.
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-collection-id"
  }
}

# Placeholder — overwritten by `pixi run globus bootstrap-endpoint` on first
# setup. This is what makes an AMI bump survivable: on every later boot,
# cloudpipe-gcs-boot fetches this key and re-registers the node non-interactively
# (no browser, and the endpoint, collection and their UUIDs are unchanged).
resource "aws_ssm_parameter" "globus_deployment_key" {
  name  = "${local.ssm_prefix}/deployment-key"
  type  = "SecureString"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-deployment-key"
  }
}

# Placeholders for the Globus Auth service credentials that let the reconcile run
# GCS management commands (storage-gateway, collection, user-credentials) without
# a human's browser session. Terraform creates the containers; both values are
# written by `pixi run globus register-service-client`, which registers the client
# through the Globus Auth API and stores the secret it is shown exactly once.
#
# That leaves one step no API performs, because it needs an endpoint to exist:
#   globus-connect-server endpoint role create administrator \
#     <client-uuid>@clients.auth.globus.org
#
# No `aws ssm put-parameter` line here on purpose: it was the one instruction in
# this module that named a parameter path in prose, and prose does not move when
# `globus_ssm_prefix` does. The command reads both names from the same derivation
# the module uses, so neither can drift.
resource "aws_ssm_parameter" "globus_gcs_client_id" {
  name  = "${local.ssm_prefix}/gcs-client-id"
  type  = "SecureString"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-gcs-client-id"
  }
}

resource "aws_ssm_parameter" "globus_gcs_client_secret" {
  name  = "${local.ssm_prefix}/gcs-client-secret"
  type  = "SecureString"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-gcs-client-secret"
  }
}

# Placeholder — overwritten by `pixi run globus bootstrap-endpoint` after the
# endpoint exists. Needed alongside gcs-client-id/secret so the reconcile can
# authenticate GCS management commands without an interactive login session.
resource "aws_ssm_parameter" "globus_endpoint_id" {
  name  = "${local.ssm_prefix}/endpoint-id"
  type  = "String"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-endpoint-id"
  }
}

# What node records the endpoint held the last time this host registered itself,
# written by `cloudpipe-gcs-boot` (report_nodes) and read by `pixi run globus
# doctor`. `globus-connect-server node list` is answered by the GCS Manager
# running ON the node, and the node is stopped between batches — so this parameter
# is the only way a workstation can say anything about node records at all.
#
# It exists to catch what an AMI bump leaves behind: `aws_instance.ami` is
# ForceNew, so a new pin replaces the host, and the replaced host's node record
# stays on the endpoint naming an address that is gone.
#
# String, not SecureString: node UUIDs and the public addresses of a host that
# advertises them in DNS. `ignore_changes` because the host owns the value and
# Terraform only owns the container — the same shape as the parameters above. Note
# the standard tier's 4 KB ceiling, which reconcile.node_report truncates to.
resource "aws_ssm_parameter" "globus_node_report" {
  name  = "${local.ssm_prefix}/node-report"
  type  = "String"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-node-report"
  }
}

# The reconcile's last plan: whether the endpoint matched its declared
# configuration, as of the last run. Same reason as the node report above — the
# comparison needs `globus-connect-server` listings, which only the GCS Manager on
# the node serves, and the node is stopped between batches. Before this the plan
# existed only as SSM command output, addressed by command id and aged out, which
# is why `globus doctor` check 7 could report nothing.
#
# String and `ignore_changes` for the same reasons: the value is a diagnostic the
# host owns, Terraform owns only the container. Trimmed by reconcile.plan_report to
# the standard tier's 4 KB, counts first so truncation never changes the severity.
resource "aws_ssm_parameter" "globus_reconcile_plan" {
  name  = "${local.ssm_prefix}/reconcile-plan"
  type  = "String"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-reconcile-plan"
  }
}

# Whether each gateway's S3 signing listener was serving, as of the last run of the
# listener install document (s3_listeners.tf), written by
# `reconcile.listener_report` and read by `globus doctor` check 14.
#
# The reason is sharper than for the two parameters above. Those describe things
# only the GCS Manager can answer; this one describes a socket that binds
# `127.0.0.1` and nothing else, because that is what makes an unauthenticated
# signing proxy safe to run at all (`retire-globus-s3-access-keys` D4). A
# workstation has no address to probe, and there is deliberately no plan to give it
# one.
#
# What it buys: a listener that is not running answers transfers with a plain 403,
# identical to the one an unassumable role or a wrong prefix produces. Without this
# parameter all three read as a broken credential.
#
# String and `ignore_changes` for the same reasons as the two above: the value is a
# diagnostic the host owns and Terraform owns only the container. Trimmed by
# reconcile.listener_report to the standard tier's 4 KB.
resource "aws_ssm_parameter" "globus_listener_report" {
  name  = "${local.ssm_prefix}/listener-report"
  type  = "String"
  value = "REPLACE_AFTER_GCS_SETUP"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name}-globus-listener-report"
  }
}

################################################################################
# Globus instance SSM permissions — the host records what setup produced (the
# collection UUID, the deployment key) and reads back what it needs at boot
################################################################################

data "aws_iam_policy_document" "globus_ssm_write" {
  statement {
    sid    = "WriteGlobusParams"
    effect = "Allow"
    actions = [
      "ssm:PutParameter",
    ]
    resources = [
      aws_ssm_parameter.globus_collection_id.arn,
      aws_ssm_parameter.globus_deployment_key.arn,
      aws_ssm_parameter.globus_endpoint_id.arn,
      aws_ssm_parameter.globus_node_report.arn,
      aws_ssm_parameter.globus_reconcile_plan.arn,
      aws_ssm_parameter.globus_listener_report.arn,
    ]
  }

  # This deployment's prefix, not a list of names. The list it replaced named
  # four parameters and omitted `config` — which the reconcile reads on every
  # run, and which worked only because AmazonSSMManagedInstanceCore (below)
  # grants ssm:GetParameter on "*". Confining the reads is what exposed that, so
  # the grant has to be the honest one: everything under this prefix, which is
  # all of this deployment's Globus state and nothing else.
  statement {
    sid    = "ReadGlobusInstanceParams"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]
    resources = [
      "arn:${var.partition}:ssm:${var.region}:${var.account_id}:parameter${local.ssm_prefix}",
      "arn:${var.partition}:ssm:${var.region}:${var.account_id}:parameter${local.ssm_prefix}/*",
    ]
  }
}

resource "aws_iam_role_policy" "globus_ssm_write" {
  name   = "${var.name}-globus-ssm-write"
  role   = aws_iam_role.globus.name
  policy = data.aws_iam_policy_document.globus_ssm_write.json
}

################################################################################
# Confining the instance's parameter reads to its own prefix.
#
# `AmazonSSMManagedInstanceCore`, attached above because the SSM agent and
# Session Manager need it, grants `ssm:GetParameter` and `ssm:GetParameters` on
# `Resource: "*"` (checked against version v2 of the AWS-managed policy, not
# assumed). So until this existed, the GCS host could read every parameter in the
# account — every other team's included — and the named-ARN grant above was
# decoration as far as reads were concerned.
#
# A Deny rather than a hand-written replacement for the managed policy: AWS
# revises that policy as the agent gains actions, and a copy would rot silently
# into a broken agent. A Deny is evaluated after every Allow no matter where the
# Allow came from, so it holds whatever AWS adds next.
#
# `/aws/service/*` is excepted because those parameters are AWS's own public
# catalogue (AMI ids, agent manifests) — readable by anyone with an account, so
# denying them buys nothing and would break any later use of them.
#
# This is what task 10.6/T6 verifies from the other side: a throwaway instance on
# prefix `/cloudpipe-gcs-test` must be REFUSED when it reads production's
# deployment key. If that read succeeds, this statement is not working. The test
# prefix is named only as illustration — because this is a Deny with
# `not_resources` on the deployment's OWN prefix, it denies every other path, so
# the evidence does not depend on what the throwaway is called.
################################################################################

data "aws_iam_policy_document" "globus_ssm_confine" {
  statement {
    sid    = "DenyParameterReadsOutsideThisDeployment"
    effect = "Deny"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
      "ssm:GetParameterHistory",
    ]
    not_resources = [
      "arn:${var.partition}:ssm:${var.region}:${var.account_id}:parameter${local.ssm_prefix}",
      "arn:${var.partition}:ssm:${var.region}:${var.account_id}:parameter${local.ssm_prefix}/*",
      "arn:${var.partition}:ssm:*:*:parameter/aws/service/*",
    ]
  }
}

resource "aws_iam_role_policy" "globus_ssm_confine" {
  name   = "${var.name}-globus-ssm-confine"
  role   = aws_iam_role.globus.name
  policy = data.aws_iam_policy_document.globus_ssm_confine.json
}

################################################################################
# Argo runner EC2 permissions — start Globus instance + read SSM parameters.
# SSM send-command permissions are only needed for the POSIX staging approach
# (globus-s3-sync-template runs aws s3 sync on the instance via SSM).
#
# Skipped when no runner role is named, which is the throwaway deployment of task
# 10.6 — see `argo_runner_iam_role_name`. This is the only resource here attached
# to a role the module does not create, so it is the only one a second
# instantiation could use to change production.
################################################################################

resource "aws_iam_role_policy" "runner_globus_ec2" {
  count = var.argo_runner_iam_role_name == "" ? 0 : 1

  name = "${var.name}-runner-globus-ec2"
  role = var.argo_runner_iam_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "StartGlobusInstance"
        Effect   = "Allow"
        Action   = ["ec2:StartInstances"]
        Resource = "arn:${var.partition}:ec2:${var.region}:${var.account_id}:instance/${aws_instance.globus.id}"
      },
      {
        # Describe actions are not resource-scoped by AWS
        Sid      = "DescribeGlobusInstance"
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstanceStatus", "ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        Sid    = "ReadGlobusSSMParameters"
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        Resource = [
          aws_ssm_parameter.globus_instance_id.arn,
          aws_ssm_parameter.globus_collection_id.arn,
        ]
      },
    ]
  })
}

################################################################################
# EventBridge Scheduler — nightly stop safety net
################################################################################

data "aws_iam_policy_document" "globus_scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "globus_scheduler" {
  name_prefix        = "${var.name}-globus-scheduler-"
  assume_role_policy = data.aws_iam_policy_document.globus_scheduler_assume.json
}

resource "aws_iam_role_policy" "globus_scheduler_ec2" {
  name = "${var.name}-globus-scheduler-ec2"
  role = aws_iam_role.globus_scheduler.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "StopGlobusInstance"
      Effect   = "Allow"
      Action   = ["ec2:StopInstances"]
      Resource = "arn:${var.partition}:ec2:${var.region}:${var.account_id}:instance/${aws_instance.globus.id}"
    }]
  })
}

# Stops the instance at midnight UTC every day.
# Acts as a safety net if a workflow completes without explicitly stopping the instance.
resource "aws_scheduler_schedule" "globus_stop" {
  name       = "${var.name}-globus-stop"
  group_name = "default"

  flexible_time_window { mode = "OFF" }
  schedule_expression          = "cron(0 0 * * ? *)"
  schedule_expression_timezone = "UTC"

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ec2:stopInstances"
    role_arn = aws_iam_role.globus_scheduler.arn
    input    = jsonencode({ InstanceIds = [aws_instance.globus.id] })
  }
}
