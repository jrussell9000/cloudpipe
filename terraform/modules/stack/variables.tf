#################
# AWS Variables #
#################

variable "domain" {
  description = "Base domain name for all CloudPipe services. Must match an existing Route53 hosted zone in your account. null runs port-forward mode, with no published UIs."
  type        = string

  # No default: a fork that inherits ours publishes services under a domain it
  # does not own, and the ACM validation then fails against someone else's zone.
  # null is a deliberate choice, written as `domain = null`.
  #
  # null selects port-forward mode (`local.publish_uis`, design D1 of
  # openspec/changes/optional-domain-and-cognito-auth): no ALB, certificate,
  # WAF, ingress, DNS record or external-dns, and each UI reached with
  # `kubectl port-forward` on a fixed localhost port (design D2). Operators
  # sign in through the Cognito pool, so no domain also needs no institutional
  # SSO.
  #
  # The wizard's schema carries the same rule and the same wording;
  # tests/test_setup_wizard_schema.py holds the two together.
  validation {
    condition     = var.domain == null || can(regex("^[a-z0-9.-]+\\.[a-z]{2,}$", var.domain))
    error_message = "domain must be null (port-forward mode) or a bare DNS name with a TLD, e.g. example.org — no scheme and no trailing dot."
  }
}

variable "region" {

  type = string

  validation {
    condition     = can(regex("^[a-z]{2}(-gov)?-[a-z]+-[0-9]$", var.region))
    error_message = "region must be an AWS region id: two letters, a dash, a word, a dash, a digit."
  }
}


#################
# VPC Variables #
#################

variable "vpc_cidr" {
  description = "VPC CIDR for Batch on EKS"
  type        = string
  default     = "10.0.0.0/16"
}


# RFC6598 range 100.64.0.0/10
# Note you can only /16 range to VPC. You can add multiples of /16 if required
variable "secondary_cidr_blocks" {
  description = "Secondary CIDR blocks to be attached to VPC"
  default     = ["10.1.0.0/16", "10.2.0.0/16"]
  type        = list(string)
}

#################
# EKS Variables #
#################

variable "name" {
  default = "cloudpipe"
  type    = string
}

# The metrics bucket is created by the bootstrap root, not by this module, because
# it records what was processed and must outlive the cluster (design D7 of
# openspec/changes/publish-bootstrap-buckets). This module configures it by name:
# its access logging, the Glue table that reads it, and the workloads that write
# to it.
#
# Null means "the name this module used to create", `<name>-metrics` — see
# local.metrics_bucket. A deployer who named the bucket something else in the
# bootstrap root sets the same string here. No validation block: the bootstrap
# root validates the name where a deployer supplies it, and a validation on a
# nullable variable would have to special-case null in a way the wizard's schema
# mirror then has to translate.
variable "metrics_bucket" {
  description = "Name of the metrics bucket, created by the bootstrap root. Null uses <name>-metrics."
  type        = string
  default     = null
}

variable "kubernetes_version" {
  description = "Version of EKS to install on the control plane (Major and Minor version only, do not include the patch)"
  type        = string
  default     = "1.35"
}

variable "endpoint_public_access" {
  description = "Enable public access to the EKS API server. Set to true during initial install for bootstrapping, then flipped to false at the end of install.sh once the VPN is in place."
  type        = bool
  default     = false
}


####################
# Prefect Variables #
####################

variable "prefect_namespace" {
  type    = string
  default = "prefect"
}

variable "prefect_db_name" {
  type        = string
  description = "PostgreSQL database name for Prefect metadata"
  default     = "prefect"
}

variable "prefect_db_username" {
  type        = string
  description = "Database admin account username"
  default     = "prefectuser"
}

variable "prefect_work_pool" {
  type        = string
  description = "Name of the Prefect Kubernetes work pool"
  default     = "cloudpipe-k8s-pool"
}

############################
# Argo Workflows Variables #
############################

variable "argo_workflows_namespace" {
  type    = string
  default = "argo-workflows"
}


variable "argo_workflows_db_name" {
  type        = string
  description = "Database name"
  default     = "argoworkflows"
}

variable "argo_workflows_db_username" {
  type        = string
  description = "Database admin account username"
  default     = "argouser"
}

variable "argo_workflows_db_table_name" {
  type        = string
  description = "The name of the database table to create for Argo Workflows"
  default     = "argo_workflows"
}

variable "crds_available" {
  description = "Set to true once ArgoCD has synced and installed all CRDs (external-secrets, prometheus-operator). Gate kubectl_manifest resources that depend on those CRDs."
  type        = bool
  default     = false
}

variable "vpc_cni_network_policy_enabled" {
  description = "Enable the VPC CNI network-policy agent (enableNetworkPolicy). FALSE MAKES EVERY NetworkPolicy IN THE CLUSTER INERT, including each default-deny-all — see issue #635. install.sh sets it true in Phase 6 and persists it in install-state.auto.tfvars. Separate from vpc_cni_strict_mode, which only chooses standard vs strict once the agent is on."
  type        = bool
  default     = false
}

variable "vpc_cni_strict_mode" {
  description = "Enable NETWORK_POLICY_ENFORCING_MODE=strict for VPC CNI. Opt-in, and install.sh deliberately never sets it: strict mode denies any pod that no NetworkPolicy selects, and this repo ships policies for four namespaces while a running cluster has around twenty (#746). Set it true only once your own namespaces have policies, and only while vpc_cni_network_policy_enabled is true — it has no effect otherwise."
  type        = bool
  default     = false
}


#######################
# GitOps Variables    #
#######################

variable "github_user_url" {
  type = string

  validation {
    condition     = startswith(var.github_user_url, "https://")
    error_message = "github_user_url must be an https URL."
  }
}

variable "github_repo" {
  description = "GitHub repository in owner/repo format, used to scope the GHA OIDC trust policy for ECR push access."
  type        = string

  validation {
    condition     = can(regex("^[^/]+/[^/]+$", var.github_repo))
    error_message = "github_repo must be in owner/repo format, with no scheme and no .git suffix."
  }
}

variable "gitops_repo_url" {
  description = <<-EOT
    Git repository the ArgoCD `cluster-addons` ApplicationSet reads `gitops/apps/`
    from, and that every generated Application tracks. Deliberately separate from
    `github_repo`, which scopes the GitHub Actions OIDC trust policy: after the
    flip in ADR 020 these name different repositories, because ArgoCD follows the
    public upstream while CI still runs in this repository.
  EOT
  type        = string

  validation {
    condition     = startswith(var.gitops_repo_url, "https://")
    error_message = "gitops_repo_url must be an https clone URL — ArgoCD reads it without a credential here."
  }
}

variable "gitops_revision" {
  description = <<-EOT
    Git revision the ApplicationSet generator scans and every generated
    Application tracks. One value feeds both fields: a generator reading one
    revision while the Applications sync another drifts with no error.
  EOT
  type        = string
  default     = "main"
}

variable "github_oidc_allowed_subs" {
  description = <<-EOT
    Exact `token.actions.githubusercontent.com:sub` claim values allowed to assume
    the GitHub Actions roles (ECR push and Packer AMI builds). Defaults to
    main-branch runs only, which covers every workflow that assumes these roles
    today. Three (build-images, build-fmri-first-level-proc,
    build-prefect-flow-runner) trigger on `push: branches:[main]` plus
    `workflow_dispatch`. The fourth, build-gpu-nodeclass-ami, triggers on
    `workflow_run` filtered to main plus `workflow_dispatch`; a workflow_run job
    executes the default branch's copy of the workflow, so its ref claim is also
    refs/heads/main. A `workflow_dispatch` from main mints the same ref claim as
    a push.

    Note the subject is ref-shaped only because no job sets a GitHub
    `environment:`. A job with one mints `repo:<repo>:environment:<name>`
    instead, which this list would NOT match — adding an environment to a build
    job means adding its subject here in the same change, or the job loses its
    credentials.

    Add an entry here — temporarily — to let a non-main branch assume the roles,
    e.g. to test a change to build-gpu-nodeclass-ami.yaml before merging it:
    "repo:<owner>/<repo>:ref:refs/heads/my-branch".
    StringLike is used, so `*` is a wildcard in these values; keep them exact
    unless a wildcard is genuinely wanted.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.github_oidc_allowed_subs) > 0
    error_message = "github_oidc_allowed_subs must not be empty: an empty list trusts nothing and every build loses its credentials."
  }
}


#######################
# Globus Variables    #
#######################

variable "globus_s3_destination_bucket" {
  description = "S3 bucket name that the Globus Connect Server will access"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9.-]{3,63}$", var.globus_s3_destination_bucket))
    error_message = "globus_s3_destination_bucket must be a valid S3 bucket name."
  }
}

variable "globus_admin_prefix_list_id" {
  description = "ID (not ARN) of the AWS managed prefix list allowed SSH access to the Globus Connect Server (e.g. pl-xxxxxxxx)"
  type        = string

  validation {
    condition     = startswith(var.globus_admin_prefix_list_id, "pl-")
    error_message = "globus_admin_prefix_list_id must be a prefix list ID (pl-...), not an ARN."
  }
}

# No defaults on the four institution-identifying variables below, on purpose
# (#526). Each default was this deployment's own value, and a default cannot
# fail: an apply that never rendered the answers document would stand up an
# endpoint advertising a stranger's organization and mailbox
# (`globus_org_name` and `globus_contact_email` are baked into the bootstrap
# SSM document) behind a gateway that admits only that stranger's institution
# (`globus_identity_domain` is the gateway's `--domain`). `globus init` renders
# all four.
variable "globus_org_name" {
  description = "Organization name displayed on the Globus endpoint. Baked into the bootstrap SSM document as `endpoint setup --organization`. Rendered by `globus init` from the answers document."
  type        = string
}

variable "globus_contact_email" {
  description = "Contact email displayed on the Globus endpoint. Baked into the bootstrap SSM document as `endpoint setup --contact-email`. Rendered by `globus init` from the answers document's `contact_email`."
  type        = string
}

variable "globus_owner_email" {
  description = "Email of the person an institution contacts about this endpoint — typically an institutional Globus admin. The human in the subscription request `globus bootstrap-endpoint` prints; NOT `endpoint setup --owner`, which is the service client. Rendered by `globus init` from the answers document's `owner_email`."
  type        = string
}

variable "globus_identity_domain" {
  description = "Identity domain permitted to authenticate to the Globus storage gateway (e.g. your institution's domain). Passed as the gateway's `--domain`. Rendered by `globus init` from the answers document's `identity_domain`."
  type        = string
}

variable "globus_gateway_name" {
  description = "Display name of the production Globus storage gateway. This deployment's gateway predates the operator CLI and is named `cloudpipe-s3-gateway`, beside a `cloudpipe-s3` collection; the reconcile matches by display name, so this has to say so. Empty derives it from globus_collection_name, which is what a fresh deployment gets from `globus init`."
  type        = string
  default     = "cloudpipe-s3-gateway"
}

variable "globus_collection_name" {
  description = "Display name for the S3-backed Globus mapped collection."
  type        = string
  default     = "cloudpipe-s3"
}

variable "globus_client_id" {
  description = "Client ID of the Globus service client registered in the Globus Developers Portal. `globus bootstrap-endpoint` creates the endpoint under it rather than under a personal identity."
  type        = string
}

variable "globus_source_collection_id" {
  description = "Globus source collection UUID (NBDC Data Hub collection)."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f-]{36}$", var.globus_source_collection_id))
    error_message = "globus_source_collection_id must be a Globus collection UUID."
  }
}

variable "globus_source_base_path" {
  description = "Root path on the Globus source collection containing subject data. Subject ID is appended automatically."
  type        = string
  default     = "/abcd/derivatives/mmps_mproc"
}

# `globus_dest_base_path` and `globus_scan_types` were declared here and read by
# nothing — no Terraform code referenced either. Both are really parameters of
# `prefect/flows/cloudpipe_queue_manager.py`, which Terraform has no part in
# setting, so an operator who followed the setup guide's "Set
# `globus_dest_base_path = \"/mmps_mproc\"`" changed nothing and was told nothing.
# `terraform.tfvars.example` even suggested `/incoming`, a value that disagrees
# with what the pipeline actually writes to.
#
# Deleted rather than wired up to the new `globus_destination_prefix` on
# module.globus. Wiring would have made the disagreement worse, not better: that
# variable scopes the writer role's IAM prefix, so a root variable set to
# `/incoming` would confine the role to a prefix nothing writes to and every
# transfer would fail with AccessDenied. The prefix genuinely lives in two
# systems, and the honest arrangement is one declaration in each with a test
# holding them equal — see tests/test_terraform_globus_s3_roles.py.

variable "globus_staging_enabled" {
  description = "Provision the AWS side of the Globus staging gateway: its two Secrets Manager containers. It used to also attach an S3 policy to a named IAM user; `retire-globus-s3-access-keys` 9.2 removed that, since staging signs through its own listener as a prefix-scoped role. The staging gateway and collection themselves are created in Globus, not by Terraform. This default is THIS deployment's value — it keeps staging as the proving ground for a production endpoint that predates the operator CLI, and it applies Terraform on these defaults, so flipping it would drop staging here. A fresh deployment gets `false` from `globus init`, which always renders this variable (docs/globus.md, Staging gateway)."
  type        = bool
  default     = true
}



variable "globus_session_timeout_days" {
  description = "How often a human must re-authenticate to Globus, in days. Rendered into the GCS configuration document as `authentication_timeout_mins`. There is no 30-day High Assurance ceiling (ADR 010's Correction) — this is a choice, not a limit."
  type        = number
  default     = 30
}

variable "globus_production_managed" {
  description = "Whether the on-instance reconcile may APPLY changes to the production gateway and collection. False means it reports drift and changes nothing. Held false from 2026-04 until 2026-09-23 while this hand-built endpoint was brought under the reconcile; flipped to true once `configure --plan-only` read the live endpoint correctly and proposed exactly one action (the session timeout). The module's own default stays false, so a deployment that has not made this decision is still safe by default. Revert by setting this to false — no other change is needed, and the reconcile goes back to reporting."
  type        = bool
  default     = true
}



####################
# VPN Variables    #
####################


variable "certificate_validity_period_hours" {
  description = "Validity period for client certificates in hours (default is 1 year)"
  type        = number
  default     = 8760
}

# Not a credential — an account identifier, and this file is not part of the
# public mirror (only terraform/modules/** is). The Cloudflare API TOKEN is a
# different thing entirely and never appears in Terraform: it is supplied via
# the CLOUDFLARE_API_TOKEN environment variable. See cloudflare.tf.
variable "cloudflare_account_id" {
  description = "Cloudflare account ID owning the Zero Trust organization"
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.cloudflare_account_id))
    error_message = "cloudflare_account_id must be the 32-character hex account id from the Cloudflare dashboard."
  }
}

variable "grafana_namespace" {
  description = "Kubernetes namespace where Grafana is deployed (must match the gitops/apps/grafana ArgoCD Application namespace)."
  type        = string
  default     = "grafana"
}

variable "client_cidr_block" {
  description = "CIDR block from which client IP addresses will be assigned when connected to VPN"
  type        = string
  default     = "10.3.0.0/22"
}

variable "split_tunnel" {
  description = "Whether to enable split tunnel mode. This allows client to access public internet and private resources."
  type        = bool
  # Full-tunnel: split-tunnel only routes the VPN's authorized routes (the
  # private VPC CIDRs) through the tunnel, so traffic to the web apps' public
  # ALB IPs never enters the tunnel and never gets NAT'd into the client CIDR
  # the ALB security groups trust (see terraform/modules/stack/vpn.tf, terraform/modules/stack/grafana.tf,
  # terraform/modules/stack/argocd.tf). Full-tunnel is required for those SG rules to work.
  default = false
}

###################
# Batch Variables  #
###################



#####################
# Logging Variables #
#####################

variable "log_bucket" {
  description = "Master bucket for holding (or archiving) diverse log streams"
  type        = string
  default     = "cloudpipe-logging"
}

# Access logs cannot share the master bucket. ALB access logs are written by a
# regional AWS account principal (not a service principal) and AWS supports only
# SSE-S3 on that destination; the master bucket is SSE-KMS with a CMK, which is
# why `access_logs.s3.enabled=false` is set on every ingress today. S3 server
# access logging hit the same wall silently — `cloudpipe-finops` has been
# configured to log to `cloudpipe-logging/finops/` since April and has delivered
# exactly zero objects, because the CMK policy never granted
# logging.s3.amazonaws.com.
#
# So the two high-volume, low-value access-log classes get their own AES256
# bucket. That also removes per-object KMS request charges, which matter here
# precisely because these logs are many tiny objects.
variable "access_log_bucket" {
  description = "SSE-S3 bucket for ALB access logs and S3 server access logs. Separate from log_bucket because neither producer can write to an SSE-KMS destination."
  type        = string
  default     = "cloudpipe-logging-access"
}

# Retention is tiered by how much each log class is actually worth, because the
# cost driver is volume and the compliance value is concentrated in CloudTrail.
#
# NIST 800-171 3.3.1 sets no fixed duration — it requires retention "to the
# extent needed" for the organization to investigate. The 90-day figure people
# quote comes from the NIST 800-53 AU-11 moderate baseline, and it applies to
# the audit record that establishes accountability. For cloudpipe that record is
# CloudTrail, which carries the DUA-relevant object-level access to the data bucket and
# is small enough that a full year is affordable.
#
# Access logs and flow logs are supplementary detail (referrer, user agent,
# per-request latency, packet-level src/dst) on top of that record, so they get
# 30 days rather than 7. Seven is the tempting number and it is the wrong one:
# an intrusion is routinely discovered weeks after it happens, and at these
# volumes the difference between 7 and 30 days is well under a dollar a month.
variable "audit_log_retention_days" {
  description = "Retention for CloudTrail — the accountability record backing the ABCD DUA audit obligation."
  type        = number
  default     = 365
}

variable "flow_log_retention_days" {
  description = "Retention for VPC flow logs. High object count, supplementary to CloudTrail."
  type        = number
  default     = 30
}

variable "access_log_retention_days" {
  description = "Retention for ALB access logs and S3 server access logs in access_log_bucket."
  type        = number
  default     = 30
}

variable "athena_result_retention_days" {
  description = "Days before Athena query results in the finops bucket expire. Does not affect CUR data or the Kubecost store."
  type        = number
  default     = 7
}

# The state bucket is named in the backend block (versions.tf), which Terraform
# evaluates before variables and will not interpolate, so it cannot be read from
# there. It is declared here only so CloudTrail can select object-level events on
# it — state holds provider secrets in plain text. Null means "derive it from
# var.name", which is how this deployment is named; an outside deployment whose
# state bucket is named differently, or lives in another account, sets it
# explicitly. A selector on a bucket that does not exist matches nothing and is
# harmless.
variable "terraform_state_bucket" {
  description = "Bucket holding this deployment's Terraform state, for CloudTrail data events. Defaults to <name>-terraform-state."
  type        = string
  default     = null
}

# Versioning on the audit bucket is an AU-9 tamper-evidence control, not a
# backup (see the H8 risk acceptance, which deliberately does NOT cover this
# bucket). A deleted log leaves a noncurrent version behind for long enough to
# notice; it is not meant to be a durable second copy, so it expires fast.
variable "noncurrent_log_version_retention_days" {
  description = "How long noncurrent versions of audit logs survive. AU-9 tamper evidence, not backup."
  type        = number
  default     = 7
}

# `container_log_retention_days` was removed with the CloudWatch container-log
# pipeline (fluent-bit is off at the addon — see eks.tf). Pod logs live in S3
# under a bucket lifecycle policy, not a CloudWatch retention setting. Do not
# reintroduce this variable without also reintroducing a writer.

# Destinations for GuardDuty HIGH/CRITICAL and Inspector CRITICAL findings — see
# security_findings.tf. Empty by default and deliberately so: an SNS email
# subscription mails a confirmation request the instant it is created, so a
# non-empty default would mail a shared mailbox on whoever's next `apply`,
# without that mailbox's owner having agreed to receive it.
#
# The account already has a `security-hub-findings` topic delivering six S3 Config
# controls to the institution's cloud-services support address. That is the
# obvious destination to reuse, but confirm a human triages it before pointing
# more traffic there — a confirmed SNS subscription proves an address accepts
# mail, not that anyone reads it (POA&M P3-9c).
#
# Terraform cannot confirm an email subscription. A green apply leaves it
# `PendingConfirmation` until a human clicks the link, so a successful apply does
# NOT mean anyone is being notified.
variable "security_findings_emails" {
  description = "Email addresses subscribed to the security-findings SNS topic. Each requires manual confirmation after apply."
  type        = list(string)
  default     = []
}


##############################
# Identity / Cloudflare
##############################

variable "external_identity" {
  description = <<-EOT
    An identity provider this module does not create, for a deployment that
    already integrates one directly.

    Supply an already-configured Cloudflare Access identity provider and policy,
    and the values Dex needs to use the same provider. The module allows that
    provider on its two Access applications and renders that Dex connector; it
    builds the connector's `redirectURI` itself, from its own service URLs,
    because it is the only place that knows them in both access modes.

    Nothing here names an institution or a protocol profile on purpose. A direct
    integration is one provider's shape — endpoint layouts, attribute names,
    assurance profiles — so it belongs in the deployment's own configuration
    rather than in this module (design D6 of
    openspec/changes/optional-domain-and-cognito-auth).

    Leave it null to use the Cognito user pool this module creates (cognito.tf),
    with institutional SSO arriving through Cognito federation instead. That is
    the default for new deployments, and the wizard never offers this input.
  EOT

  type = object({
    access_identity_provider_id = string
    access_policy_id            = string
    dex_connector = object({
      id            = string
      name          = string
      issuer        = string
      client_id     = string
      client_secret = string
    })
  })

  # The Dex client secret is in here. It reaches state anyway, through the ArgoCD
  # Helm release's values, which is a recorded tradeoff; marking it keeps it out
  # of plan output.
  sensitive = true

  # Defaulted, because the wizard does not offer this input and a variable with
  # no default that nothing asks for is a prompt mid-apply — or an error under
  # `-input=false`, which is how any scripted apply runs.
  default = null
}

variable "operator_emails" {
  description = <<-EOT
    Email addresses of the operators. In every mode they hold the administrator
    role in each web UI, and their email domains are the ones the SSO proxies
    admit. In Cognito mode (no `external_identity`) they are also who may enroll
    a WARP device and reach the cluster, and each needs a user in the pool with
    the same address — the module does not create users (output
    `cognito_user_pool_id` names the pool to create them in).
  EOT
  type        = list(string)

  validation {
    condition     = length(var.operator_emails) > 0
    error_message = "operator_emails must hold at least one address: with none, no one can administer the deployment."
  }

  validation {
    condition     = alltrue([for email in var.operator_emails : can(regex("^[^@\\s]+@[a-z0-9.-]+\\.[a-z]{2,}$", email))])
    error_message = "operator_emails must be email addresses with a lower-case domain, e.g. operator@example.org."
  }
}

variable "cognito_federation" {
  description = <<-EOT
    One institutional identity provider, federated into the Cognito user pool
    (design D5 of openspec/changes/optional-domain-and-cognito-auth).

    This is how an institution's single sign-on reaches a deployment. Cloudflare
    Access and Dex do not change: they keep talking to the pool, and the pool
    talks to the institution. The institution registers ONE redirect URI —
    Cognito's, printed by the `cognito_federation_redirect_uri` output — instead
    of one per relying party.

    `type` is "saml" or "oidc".

      - SAML needs `metadata_url`, the IdP metadata document's URL.
      - OIDC needs `oidc_issuer` and `client_secret_id`, the name of a Secrets
        Manager secret holding {"clientID": ..., "clientSecret": ...}. That
        secret is the institution's credential: it is created by hand and read
        by a data source, never owned by Terraform, which is the same
        convention `external_identity` follows.

    `mfa_evidence` is how this provider's multifactor authentication is proven,
    and exactly one form is required. Cognito "delegates all authentication
    processes to the IdP and doesn't offer them additional authentication
    factors", so a federated user never meets the pool's own authenticator
    requirement — something has to stand in its place for 3.5.3:

      - `claim` + `claim_value`: a claim the IdP asserts per login, which the
        Cloudflare Access policy then REQUIRES. `acr` is the usual claim name,
        and several research federations define a standard value for an MFA
        login — the provider's own team knows which they assert. This is the
        strong form: a login that arrives without it is denied.
      - `attestation`: a reference to the institution's written policy, for a
        provider that cannot assert a per-login claim. SAML carries its
        authentication context in the assertion rather than as an attribute, so
        SAML federation usually lands here. Nothing is enforced at login; the
        deployer's SSP cites this string.

    `attribute_mapping` maps pool attributes to the IdP's claim names. The
    default maps email and email_verified from claims of the same name. Both
    matter: the UIs sign in on email, and Cognito marks a federated user's
    address verified only if something maps `email_verified` — an unverified
    address is refused by the Dex connector.
  EOT

  type = object({
    type             = string
    name             = optional(string, "institution")
    metadata_url     = optional(string)
    oidc_issuer      = optional(string)
    client_secret_id = optional(string)
    authorize_scopes = optional(string, "openid email profile")
    attribute_mapping = optional(map(string), {
      email          = "email"
      email_verified = "email_verified"
    })
    mfa_evidence = object({
      claim       = optional(string)
      claim_value = optional(string)
      attestation = optional(string)
    })
  })

  default = null

  validation {
    condition     = var.cognito_federation == null || contains(["saml", "oidc"], var.cognito_federation.type)
    error_message = "cognito_federation.type must be \"saml\" or \"oidc\"."
  }

  # 1 to 32 characters, and Cognito rejects a leading or trailing underscore or
  # separator. It reaches the authorize URL as `identity_provider`, so it is
  # also the one value the institution may see.
  validation {
    condition     = var.cognito_federation == null || can(regex("^[^_[:space:]][[:print:]]{0,30}[^_[:space:]]$", var.cognito_federation.name))
    error_message = "cognito_federation.name must be 2 to 32 printable characters and may not begin or end with an underscore or a space."
  }

  validation {
    condition     = var.cognito_federation == null || var.cognito_federation.type != "saml" || try(startswith(var.cognito_federation.metadata_url, "https://"), false)
    error_message = "cognito_federation.metadata_url is required for a SAML provider and must be an https URL."
  }

  validation {
    condition     = var.cognito_federation == null || var.cognito_federation.type != "oidc" || try(startswith(var.cognito_federation.oidc_issuer, "https://"), false)
    error_message = "cognito_federation.oidc_issuer is required for an OIDC provider and must be an https URL."
  }

  validation {
    condition     = var.cognito_federation == null || var.cognito_federation.type != "oidc" || !endswith(coalesce(var.cognito_federation.oidc_issuer, "x"), "/")
    error_message = "cognito_federation.oidc_issuer must not end in a slash — Cognito appends the discovery path to it."
  }

  validation {
    condition     = var.cognito_federation == null || var.cognito_federation.type != "oidc" || try(length(var.cognito_federation.client_secret_id) > 0, false)
    error_message = "cognito_federation.client_secret_id is required for an OIDC provider: the name of a hand-created Secrets Manager secret holding {\"clientID\": ..., \"clientSecret\": ...}."
  }

  # The MFA-evidence rule, which is the one validation here that is about
  # compliance rather than shape. A federation with no evidence is rejected
  # before anything is applied, because Cognito adds no factor of its own.
  validation {
    condition = var.cognito_federation == null || (
      (try(length(var.cognito_federation.mfa_evidence.claim) > 0, false) && try(length(var.cognito_federation.mfa_evidence.claim_value) > 0, false))
      != try(length(var.cognito_federation.mfa_evidence.attestation) > 0, false)
    )
    error_message = "cognito_federation.mfa_evidence must declare exactly one form: either `claim` AND `claim_value` (a per-login claim the Access policy will require), or `attestation` (a reference to the institution's written MFA policy). Cognito delegates authentication to the provider and adds no second factor of its own, so one of the two is what satisfies NIST 800-171 3.5.3."
  }

  validation {
    condition     = var.cognito_federation == null || try(length(var.cognito_federation.attribute_mapping["email"]) > 0, false)
    error_message = "cognito_federation.attribute_mapping must map `email`: every web UI signs in on the email address, and an unmapped one leaves the federated user with no identity the deployment can match."
  }
}

variable "cloudflare_team_domain" {
  description = <<-EOT
    The Zero Trust team domain, `<team>.cloudflareaccess.com`. It is the
    `auth_domain` of the organization and the host WARP and the browser are sent
    to, so it has to match what Cloudflare assigned this account.
  EOT
  type        = string

  validation {
    condition     = endswith(var.cloudflare_team_domain, ".cloudflareaccess.com")
    error_message = "cloudflare_team_domain must end in .cloudflareaccess.com."
  }
}

variable "cloudflare_team_name" {
  description = <<-EOT
    The organization's `name` as Cloudflare generated it — a separate value from
    the team domain, and not derivable from it. Reading it back from the dashboard
    is the only way to get it right; a wrong value is accepted and renames the
    organization.
  EOT
  type        = string
}
