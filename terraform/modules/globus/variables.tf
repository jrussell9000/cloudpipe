variable "name" {
  description = "Root name prefix used for resource naming"
  type        = string
}

variable "region" {
  description = "AWS region"
  type        = string
}

variable "partition" {
  description = "AWS partition (e.g. aws, aws-us-gov)"
  type        = string
}

variable "account_id" {
  description = "AWS account ID"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID to place the Globus instance in"
  type        = string
}

variable "public_subnet_id" {
  description = "Public subnet ID for the Globus EC2 instance"
  type        = string
}

variable "globus_ami_id" {
  description = <<-EOT
    AMI the Globus Connect Server instance boots. Built by
    `.github/workflows/build-globus-gcs-ami.yaml` from `packer/globus-gcs/`, which
    opens a pull request bumping the pin in `terraform/globus.tf`.

    No default, and no AMI-name filter or `most_recent = true`: the instance must
    boot the image someone chose, and an id that resolves differently over time is
    an unplanned replacement waiting for the next apply.

    **Changing this REPLACES the instance** (`ami` is ForceNew). That is the
    intended migration path, not an accident: the deployment key lives in SSM, so
    `cloudpipe-gcs-boot` re-registers the node on the replacement's first boot and
    the Elastic IP re-associates. The old node record stays behind in Globus until
    it is cleaned up — see task 10.5.
  EOT
  type        = string

  validation {
    # 8 hex for the pre-2018 form, 17 for the current one. Anything else is a
    # typo, and a typo here fails part-way through an apply rather than at plan.
    condition     = can(regex("^ami-([0-9a-f]{8}|[0-9a-f]{17})$", var.globus_ami_id))
    error_message = "globus_ami_id must be an AMI id, e.g. ami-00adec9774170bad2."
  }
}

variable "globus_ssm_prefix" {
  description = <<-EOT
    SSM Parameter Store prefix holding this deployment's Globus state (the
    endpoint id, the deployment key, the service-client credentials, the
    configuration document). Empty means `/<name>/globus`, which is what
    production has always used.

    Parameterized so a throwaway endpoint can be bootstrapped under its own
    prefix (`/cloudpipe-gcs-test`, task 10.6) against a role that cannot read
    production's. The instance reads the prefix from `/etc/cloudpipe/gcs.env`,
    which is the only deployment-specific thing user_data writes.

    Changing it on an existing deployment does not move the parameters — Terraform
    creates a new set under the new prefix and destroys the old one, taking the
    endpoint id and deployment key with it. Treat a change here as a new
    deployment.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.globus_ssm_prefix == "" || can(regex("^/[A-Za-z0-9_.\\-/]+[^/]$", var.globus_ssm_prefix))
    error_message = "globus_ssm_prefix must start with / and not end with one, e.g. /cloudpipe/globus."
  }
}

variable "globus_s3_bucket" {
  description = "S3 bucket name that the Globus Connect Server will access"
  type        = string
}

variable "globus_admin_prefix_list_id" {
  description = "ID of the AWS managed prefix list allowed SSH access to the Globus Connect Server"
  type        = string
}

variable "argo_runner_iam_role_name" {
  description = <<-EOT
    IAM role name of the Argo Workflows runner — receives EC2/SSM permissions to
    start the Globus instance.

    Empty means no runner policy at all, and that is the throwaway deployment's
    setting (task 10.6). This is the one input that names a role the module does
    not own: `runner_globus_ec2` is an inline policy attached to the *pipeline's*
    runner role. A second instantiation of this module would therefore grant the
    production runner `ec2:StartInstances` on a test instance — harmless in
    effect, but a production IAM change made as a side effect of standing up a
    throwaway, which is not a thing a throwaway should be able to do.

    Empty is not a sensible production value: nothing would be able to start the
    instance, and `globus doctor` would still pass, so the failure would surface
    as a stalled transfer. The root module passes it unconditionally.
  EOT
  type        = string
  default     = ""
}

################################################################################
# Globus Connect Server setup
################################################################################

# `globus_org_name`, `globus_contact_email` and `globus_client_id` were inert
# between the user_data rewrite (10.3) and `bootstrap.tf` (10.4). They are now
# baked into the bootstrap SSM document, which is the only thing that names the
# endpoint, so a change to any of them changes that document — and, because the
# endpoint is created once, takes effect only on a deployment that has not been
# bootstrapped yet. `globus_owner_email` remains inert; see its own comment.
variable "globus_org_name" {
  description = "Organization name displayed on the Globus endpoint. Baked into the `-globus-bootstrap` SSM document as `endpoint setup --organization`."
  type        = string
}

variable "globus_contact_email" {
  description = "Contact email displayed on the Globus endpoint. Baked into the `-globus-bootstrap` SSM document as `endpoint setup --contact-email`."
  type        = string
}

variable "globus_collection_name" {
  description = "Display name for the S3-backed Globus mapped collection."
  type        = string
  default     = "cloudpipe-s3"

  # This is not only a display name. It is also the storage gateway's name, the
  # systemd instance of `cloudpipe-s3-listener@`, and the filename under
  # /etc/cloudpipe/envoy — so a space or a slash in it would not be rejected, it
  # would produce a unit that cannot start and a path that is not where anything
  # looks. Restricted to what all three accept.
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._-]*$", var.globus_collection_name))
    error_message = "globus_collection_name becomes a systemd instance name and a filename, so it may contain only letters, digits, dot, underscore and hyphen, and must start with a letter or digit."
  }
}

variable "globus_gateway_name" {
  description = <<-EOT
    Display name of the PRODUCTION storage gateway, when it is not the
    collection's. Empty (the default) derives it from `globus_collection_name`,
    which is what a gateway this tooling created will match.

    Set it when the gateway predates the tooling and was named differently: the
    reconcile matches objects by display name, so a derived-but-wrong name
    declares a gateway that does not exist and leaves the real one unmanaged.
    Staging's gateway is created here, so it stays derived.
  EOT
  type        = string
  default     = ""
}

variable "globus_session_timeout_days" {
  description = <<-EOT
    How long a Globus High Assurance session lasts before a human must
    re-authenticate, in days. Rendered into the configuration document as
    `authentication_timeout_mins`.

    There is NO 30-day High Assurance ceiling — the ABCD source collection is HA
    and set to one year (see ADR 010's Correction). This is a deployment choice
    about how often someone is willing to log in, not a Globus limit.
  EOT
  type        = number
  default     = 30

  validation {
    condition     = var.globus_session_timeout_days > 0
    error_message = "globus_session_timeout_days must be positive."
  }
}

variable "globus_production_managed" {
  description = <<-EOT
    Whether the reconcile may APPLY changes to the production gateway and
    collection. `false` (the default) means it reports drift and changes nothing.

    Default false on purpose, for the deployment this repository runs: the first
    run of reconcile code against a live, hand-configured endpoint already
    serving transfers should not be able to modify it. Flip it to true
    deliberately, after staging has proved the plan. A fresh deployment gets
    `true` from `globus init` — the tool built its endpoint, so there is nothing
    pre-existing to protect (docs/globus.md, "Staging gateway").
  EOT
  type        = bool
  default     = false
}

variable "globus_reconcile_association_enabled" {
  description = <<-EOT
    Create an SSM association that re-runs the reconcile in `plan` mode whenever
    the declared configuration changes.

    Provisional. The Globus instance is normally STOPPED, and task 8.6 (T1.6)
    measures whether an association against a stopped instance ever resolves or
    just accumulates pending invocations. If it does not fire usefully, set this
    false and rely on `globus configure`, which starts the instance deliberately.
  EOT
  type        = bool
  default     = true
}

variable "globus_client_id" {
  description = <<-EOT
    Client ID of the Globus service client registered in the Globus Developers
    Portal. `globus bootstrap-endpoint` creates the endpoint under it, rather than
    under a personal identity, so ownership does not leave with the person who ran
    setup and no browser login is ever needed.

    Baked into the `-globus-bootstrap` SSM document as
    `endpoint setup --owner <this>@clients.auth.globus.org`. Note that this is the
    *client id*, not `globus_owner_email` — Globus takes a principal there, and a
    service client's principal is that synthetic address.
  EOT
  type        = string
}

variable "globus_source_collection_id" {
  description = "Globus source collection UUID (remote endpoint data is transferred from). Stored in SSM so flows read it at runtime."
  type        = string
}

variable "globus_source_base_path" {
  description = "Root path on the source collection containing subject data. Subject ID is appended automatically. Stored in SSM so flows read it at runtime."
  type        = string
}

variable "globus_owner_email" {
  description = <<-EOT
    Globus identity (email) of the person an institution should contact about this
    endpoint — typically an institutional Globus admin.

    NOT `endpoint setup --owner`: that takes a principal, and the principal is the
    service client (`globus_client_id`), which is what makes ownership outlive the
    person who ran setup. This is the human in the subscription request
    `globus bootstrap-endpoint` prints, and the advertised owner someone sets by
    hand once there is a subscription.

    Inert in this module — the request text is rendered by the CLI from the answers
    document, not by Terraform. Kept because deletion is not local to this module:
    it is part of the set `globus init` renders, and
    `tests/test_terraform_globus_config.py` holds that set exactly equal to the
    `var.globus_*` references in the root module block (task 12.2a).
  EOT
  type        = string
}

variable "globus_identity_domain" {
  description = "Identity domain permitted to authenticate to the storage gateway (e.g. your institution's domain, 'example.edu'). Passed as --domain during gateway creation."
  type        = string
}

variable "globus_staging_enabled" {
  description = <<-EOT
    Provision the AWS side of the Globus staging test bed: the two Secrets Manager
    containers the operator CLI writes into, and (if `globus_staging_iam_user_name`
    is set) an S3 policy confining that user to the staging prefix. The staging
    storage gateway and collection themselves are created in Globus, not by
    Terraform. Optional: `globus init` renders false for a fresh deployment. The
    root module's default is true because it describes the deployment this
    repository runs (docs/globus.md, "Staging gateway").
  EOT
  type        = bool
  default     = true
}

variable "globus_staging_iam_user_name" {
  description = <<-EOT
    Name of an **existing** IAM user whose access key the staging Globus S3 storage
    gateway registers. Terraform attaches an inline S3 policy confining it to the
    staging prefix; it never creates the user, because an organization SCP denies
    iam:CreateUser in this account for any name.

    Leave empty until a user exists. Staging's secrets are still created, and
    `globus doctor --env staging` reports the missing credential as a prerequisite
    rather than failing an apply.

    Do not point this at the production gateway's user. The policy is additive, so
    the production user would gain the staging prefix while keeping its bucket-wide
    access — the prefix confinement that makes staging safe to break would be gone,
    and the two environments would share one credential to rotate.
  EOT
  type        = string
  default     = ""
}

variable "globus_iam_user_name" {
  description = <<-EOT
    Name of the **existing** IAM user whose access key the PRODUCTION Globus S3
    storage gateway registers. Unlike the staging equivalent, this is a name only:
    no policy is attached and no key is touched. The production user predates this
    tooling, already holds a bucket-wide grant and both of its two key slots, and
    the standing instruction is to leave it alone.

    Declaring it is what lets `globus doctor` check 12 verify the credential at
    all. Empty is a legitimate state — an organization SCP denies iam:CreateUser,
    so "no user yet" is somewhere an operator lands and cannot fix themselves —
    and the check reports it rather than an apply failing.

    The Globus S3 connector authenticates with an IAM user's access key, not a
    role and not an instance profile, so this one user is the whole credential
    story for the gateway.
  EOT
  type        = string
  default     = ""
}

variable "globus_destination_prefix" {
  description = <<-EOT
    S3 key prefix in the pipeline bucket that production Globus transfers land
    under. Scopes the production gateway's writer role (`s3_gateway_roles.tf`) —
    that role can reach nothing else in the bucket.

    Must equal the destination the pipeline actually transfers to. Nothing else
    declares that value authoritatively: `globus-dest-base-path` is a required
    Argo workflow parameter with no default, and its only standing value is the
    Prefect queue manager's `globus_dest_base_path: str = "/mmps_mproc"`. Since
    the submitter chooses it per run and IAM cannot be scoped to a choice, the
    value has to be asserted here, and `tests/test_terraform_globus_s3_roles.py`
    asserts the two agree — a mismatch is not caught at plan time, it surfaces as
    AccessDenied on the first object of a transfer.

    Also published as the SSM parameter `<ssm prefix>/destination-listing-path`,
    which is the path `globus doctor`'s check 9 lists to prove the collection is
    writable. Both uses read `local.globus_production_prefix`, so the prefix
    permitted and the path checked cannot drift apart.

    There is no root variable behind this one — the root passes it explicitly. The
    root's `globus_dest_base_path`, which read like a destination setting and
    configured nothing, was deleted in 12.2a.
  EOT
  type        = string
  default     = "mmps_mproc"

  validation {
    # Load-bearing twice: an empty value would scope the writer role to the whole
    # bucket, AND would make the published listing path a bare "/" — the empty
    # s3:prefix the role's list condition denies, which is the check-9 failure this
    # parameter exists to remove.
    condition     = trim(var.globus_destination_prefix, "/") != ""
    error_message = "globus_destination_prefix must name a prefix. An empty value (or bare '/') would scope the production writer role to the whole bucket, which is the confinement this variable exists to create."
  }
}

variable "globus_staging_prefix" {
  description = <<-EOT
    S3 key prefix the staging collection is rooted at, relative to the pipeline
    bucket. The staging IAM user has no S3 access outside it. Keep it under
    `scratch/` so the bucket's 7-day scratch lifecycle rule reaps staging data
    without anyone remembering to.
  EOT
  type        = string
  default     = "scratch/globus-staging"

  validation {
    condition     = startswith(var.globus_staging_prefix, "scratch/")
    error_message = "globus_staging_prefix must live under scratch/ so lifecycle expiry applies to staging data."
  }
}

