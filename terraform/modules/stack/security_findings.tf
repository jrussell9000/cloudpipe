# Security finding routing — NIST 800-171 IR-6 / SI-2 (POA&M P3-9b, to-do item C2)
#
# WHY THIS FILE EXISTS, AND WHY IT DOES NOT IMPORT WHAT ALREADY RUNS
#
# Detection in this account is strong and almost entirely inherited: GuardDuty
# (detector enabled with CloudTrail, DNS, flow-log, S3 data-event, EKS audit-log,
# EBS malware, RDS login and runtime-monitoring features), Inspector enhanced
# scanning, and Security Hub — subscribed since 2024-11-26 with eleven product
# subscriptions feeding it. **None of it is declared anywhere in this codebase.**
# It came from the Control Tower / organization baseline.
#
# A routing chain is also already live, and this is the part that matters:
#
#   EventBridge `security-hub-findings-new`
#     -> Lambda `SecurityHubFindingsProcessor`
#       -> SNS `security-hub-findings`
#         -> confirmed email subscription
#
# Two consecutive self-assessments recorded "GuardDuty detects; nothing delivers
# findings to a human." That was wrong at the resource layer — the chain exists.
# It is right at the *semantic* layer, which is where the defect lives: the rule's
# event pattern matches only
#
#   detail.findings.Compliance.SecurityControlId
#     in [S3.1, S3.2, S3.3, S3.6, S3.8, S3.12]  AND  Status == FAILED
#
# GuardDuty and Inspector findings carry no `Compliance.SecurityControlId` field
# at all, so they match no branch and are **silently dropped**. An inventory-level
# audit calls IR closed; a code-level audit calls it absent; neither is correct.
#
# This file therefore adds a SECOND, independent chain rather than importing and
# mutating the first. Reasons, in order of importance:
#
#   1. `security-hub-findings-new` targets a Lambda that is not in this codebase
#      and whose behaviour is unknown. Its narrow S3 filter is presumably
#      load-bearing for whoever built it. Widening someone else's rule to carry
#      our traffic couples two unrelated notification purposes.
#   2. The existing topic is encrypted with `alias/aws/sns` — the AWS *managed*
#      key. EventBridge cannot publish to such a topic (see the KMS note below),
#      so reusing it would have failed anyway, and failed silently.
#   3. Declaring the whole chain here is the actual compliance deliverable. The
#      standing defect in this account is not missing controls, it is controls
#      that no configuration claims — so nothing diffs, no test fails, and nobody
#      is notified if an organizational policy change removes them (finding N3).
#
# Deliberately NOT managed here: the GuardDuty detector, the Security Hub hub, the
# standards subscriptions, and the pre-existing rule/Lambda/topic. Adopting those
# is POA&M P3-1b and needs a `terraform import`, not a fresh declaration — a bare
# `resource` block for an existing detector would fail the apply, and getting it
# wrong risks disabling live detection. The `check` block at the bottom asserts
# the detector still exists without claiming ownership of it.
#
# STANDARDS SUBSCRIPTIONS ARE UNMANAGEABLE HERE, NOT MERELY UNADOPTED — AND THAT
# IS A HARDER FACT THAN THE PARAGRAPH ABOVE IMPLIES.
#
# The reason above is about adoption: an existing subscription needs an import.
# Creating a NEW one turns out to be impossible from this account regardless.
# `terraform/security_hub_standards.tf` declared the NIST SP 800-171 Rev. 2
# subscription for POA&M PG-5a, planned cleanly as 1 add, and failed at apply on
# 2026-09-24:
#
#   AccessDeniedException: This account is currently associated with a central
#   configuration policy, so only the administrator can access this operation.
#   (BatchEnableStandards, arn:aws:securityhub:<region>::standards/nist-800-171/v/2.0.0)
#
# This account is an organization MEMBER under Security Hub central configuration,
# so the set of enabled standards is a delegated-administrator decision. The denial
# is symmetric and confirmed in both directions: `BatchEnableStandards` is denied
# for writes, and `GetConfigurationPolicyAssociation` is denied for reads ("Must be
# a Security Hub delegated administrator with Central Configuration enabled"). The
# account therefore cannot even observe the policy that constrains it.
#
# That is also why NIST 800-53 Rev. 5 has been the single enabled standard since
# 2024-11-26 while 800-171 was available in this region the whole time: 800-53 was
# not enabled here either. It arrived through the same central policy. The file was
# removed rather than commented out or given a `count = 0` — an unappliable
# resource that plans as 1 add is worse than an absent one, because every future
# plan carries it and every future apply fails on it.
#
# Two facts from that file are preserved here because they are costly to re-derive
# and outlive the resource:
#
#   1. THE ARN FORM. The 800-171 standard is REGIONAL and uses `standards`:
#      `arn:aws:securityhub:<region>::standards/nist-800-171/v/2.0.0` — region
#      populated, account empty. The Terraform provider's own example subscribes to
#      CIS 1.2.0 with a GLOBAL arn and the path segment `ruleset`
#      (`arn:aws:securityhub:::ruleset/cis-aws-foundations-benchmark/v/1.2.0`);
#      copying that shape yields an ARN that does not resolve. CIS 1.2.0 is the
#      legacy outlier, not the template. Confirmed against `describe-standards`.
#      Note also that Security Hub offers only `nist-800-171/v/2.0.0` — there is no
#      Rev. 3 standard, so a programme migration to Rev. 3 has nothing to point at.
#   2. THE HUB'S SETTINGS, WHICH NOTHING HERE MANAGES. `describe-hub` reports
#      `ControlFindingGenerator: SECURITY_CONTROL`, so a control belonging to
#      several standards emits ONE consolidated finding; adding a standard mostly
#      enriches `RelatedRequirements` rather than doubling volume. If anyone flips
#      the hub to `STANDARD_CONTROL` that stops being true. `AutoEnableControls` is
#      `true`, so new controls are enabled without any diff in this repository.
#
# PG-5a IS NEVERTHELESS CLOSED, AND THE DENIAL WAS NEVER WHAT WAS BLOCKING IT.
#
# 800-171 Rev. 2 defines no controls of its own — it selects from 800-53, and its
# Appendix D publishes the crosswalk. The 800-53 Rev. 5 subscription that IS enabled
# here is therefore the parent framework, and AWS's evaluation of this account under
# it already is the "machine-generated, AWS-attested, not self-authored" evidence
# PG-5a asked for. The denial blocks a *relabelling*: no `NIST.800-171.r2/3.x.x`
# strings in `Compliance.RelatedRequirements`, and no per-requirement 800-171 score.
# That relabelling is POA&M PG-5a-i, requested from the organization 2026-09-24.
#
# So do not read the deleted file's absence as a compliance regression, and do not
# reintroduce the resource hoping a later apply succeeds — the boundary is
# organizational and will not move from here.
#
# See tools/nist-800-171-incident-response.md for what a human does when one of
# these emails arrives.

# ---------------------------------------------------------------------------
# KMS key
# ---------------------------------------------------------------------------

# A customer-managed key is NOT a hardening preference here — it is mandatory.
# From the SNS documentation: "To enable AWS services to publish events to
# encrypted Amazon SNS topics, you must use a customer managed key." The AWS
# managed key `alias/aws/sns` cannot be used, because its key policy cannot be
# edited to grant a service principal, and EventBridge publishes as a service
# principal rather than as an account IAM principal. (The pre-existing Lambda
# chain works on `alias/aws/sns` precisely because a Lambda execution role IS an
# account principal.)
#
# The alternative — an unencrypted topic — would work but trips Security Hub
# control SNS.1, i.e. it would generate a finding in the very system this chain
# is meant to report on.
resource "aws_kms_key" "security_findings" {
  description             = "Encrypts ${var.name} security finding notifications (SNS)"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  policy = data.aws_iam_policy_document.security_findings_kms.json

  tags = {
    Name = "${var.name}-security-findings"
  }
}

resource "aws_kms_alias" "security_findings" {
  name          = "alias/${var.name}-security-findings"
  target_key_id = aws_kms_key.security_findings.key_id
}

data "aws_iam_policy_document" "security_findings_kms" {
  # Without this the key is unmanageable: KMS rejects a key policy that does not
  # leave some principal able to administer it, and IAM policies alone cannot
  # grant access to a KMS key.
  statement {
    sid       = "AllowAccountAdministration"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  # SNS needs the key to encrypt each message at rest.
  statement {
    sid       = "AllowSNSToUseKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey*"]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }
  }

  # EventBridge needs it to publish INTO the encrypted topic.
  #
  # ⚠️ DO NOT add `aws:SourceArn` / `aws:SourceAccount` / `aws:SourceOrgID`
  # conditions to this statement. Adding them is the textbook confused-deputy
  # mitigation, it reads as strictly more secure, and it BREAKS DELIVERY: the SNS
  # documentation states that EventBridge-to-encrypted-topic publishing does not
  # support those condition keys. The failure is a silent non-delivery, which is
  # the exact defect this file exists to fix — so the "obvious" hardening would
  # quietly restore the bug. Scope is instead bounded by the topic policy below,
  # which restricts publishing to the two rules declared in this file.
  statement {
    sid       = "AllowEventBridgeToPublishToEncryptedTopic"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey*"]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------------------
# SNS topic and subscriptions
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "security_findings" {
  name              = "${var.name}-security-findings"
  display_name      = "cloudpipe security findings"
  kms_master_key_id = aws_kms_key.security_findings.id

  tags = {
    Name = "${var.name}-security-findings"
  }
}

resource "aws_sns_topic_policy" "security_findings" {
  arn    = aws_sns_topic.security_findings.arn
  policy = data.aws_iam_policy_document.security_findings_topic.json
}

data "aws_iam_policy_document" "security_findings_topic" {
  statement {
    sid       = "AllowAccountOwnerManagement"
    effect    = "Allow"
    actions   = ["SNS:GetTopicAttributes", "SNS:SetTopicAttributes", "SNS:Subscribe", "SNS:ListSubscriptionsByTopic", "SNS:Publish"]
    resources = [aws_sns_topic.security_findings.arn]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  # Unlike the KMS statement above, SourceArn IS supported here, so publishing is
  # scoped to exactly the two rules in this file rather than to EventBridge at
  # large. This is where the confused-deputy protection lives.
  statement {
    sid       = "AllowEventBridgeRulesToPublish"
    effect    = "Allow"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.security_findings.arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values = [
        aws_cloudwatch_event_rule.guardduty_findings.arn,
        aws_cloudwatch_event_rule.inspector_critical_findings.arn,
      ]
    }
  }
}

# Email subscriptions.
#
# Defaults to EMPTY on purpose. An `aws_sns_topic_subscription` with protocol
# `email` causes AWS to send a confirmation request the moment it is created, so a
# non-empty default would mail a shared mailbox on the next `terraform apply` by
# whoever runs it next, without that mailbox's owner having agreed. Set it in
# terraform.tfvars.
#
# Note that Terraform cannot confirm an email subscription — it stays
# `PendingConfirmation` until a human clicks the link, and Terraform reports the
# resource as created either way. So a green apply does NOT mean anyone is being
# notified. Verify with:
#
#   aws sns list-subscriptions-by-topic --topic-arn <arn> \
#     --query 'Subscriptions[].[Endpoint,SubscriptionArn]'
#
# A SubscriptionArn of the literal string "PendingConfirmation" means unconfirmed.
resource "aws_sns_topic_subscription" "security_findings_email" {
  for_each = toset(var.security_findings_emails)

  topic_arn = aws_sns_topic.security_findings.arn
  protocol  = "email"
  endpoint  = each.value
}

# ---------------------------------------------------------------------------
# EventBridge rules
# ---------------------------------------------------------------------------

# Two rules, not one, because EventBridge event patterns cannot express
# "(product A AND severity set X) OR (product B AND severity set Y)" — matching is
# conjunctive across fields. GuardDuty warrants HIGH and above; Inspector is
# CRITICAL-only because the HIGH tier is a standing CVE backlog rather than an
# event (there were 20+ active HIGH findings when this was written), and a rule
# that mails a backlog trains its reader to ignore it.
#
# Both patterns match on **ProductArn with a `suffix` operator, not ProductName**.
# ProductName is a display string: Inspector findings carry "Inspector", but
# GuardDuty's exact value could not be verified empirically because the account
# has zero active GuardDuty findings — and a pattern with a near-miss string
# matches nothing, silently, reporting ENABLED the whole time. ProductArn is
# structural (`arn:aws:securityhub:<region>::product/aws/<product>`) and was
# confirmed against live findings. The `suffix` match also makes the rule
# region-agnostic, which matters because Security Hub cross-region aggregation is
# on in this account (findings arrive from us-east-1 and us-west-2 as well).
#
# `Workflow.Status: NEW` and `RecordState: ACTIVE` keep this to genuinely new
# findings. Without them, every re-import of an already-triaged or suppressed
# finding re-notifies, and Inspector re-imports on every scan.

resource "aws_cloudwatch_event_rule" "guardduty_findings" {
  name        = "${var.name}-guardduty-high-critical"
  description = "GuardDuty HIGH/CRITICAL findings via Security Hub -> SNS (NIST 800-171 IR-6)"

  event_pattern = jsonencode({
    source        = ["aws.securityhub"]
    "detail-type" = ["Security Hub Findings - Imported"]
    detail = {
      findings = {
        ProductArn  = [{ suffix = "product/aws/guardduty" }]
        Severity    = { Label = ["HIGH", "CRITICAL"] }
        RecordState = ["ACTIVE"]
        Workflow    = { Status = ["NEW"] }
      }
    }
  })

  tags = {
    Name = "${var.name}-guardduty-high-critical"
  }
}

resource "aws_cloudwatch_event_rule" "inspector_critical_findings" {
  name        = "${var.name}-inspector-critical"
  description = "Inspector CRITICAL findings via Security Hub -> SNS (NIST 800-171 SI-2)"

  event_pattern = jsonencode({
    source        = ["aws.securityhub"]
    "detail-type" = ["Security Hub Findings - Imported"]
    detail = {
      findings = {
        ProductArn  = [{ suffix = "product/aws/inspector" }]
        Severity    = { Label = ["CRITICAL"] }
        RecordState = ["ACTIVE"]
        Workflow    = { Status = ["NEW"] }
      }
    }
  })

  tags = {
    Name = "${var.name}-inspector-critical"
  }
}

# Input transformers, so the email is readable rather than 8 KB of raw ASFF.
# An unreadable alert is an ignored alert, and an ignored alert is not routing.
resource "aws_cloudwatch_event_target" "guardduty_findings_sns" {
  rule      = aws_cloudwatch_event_rule.guardduty_findings.name
  target_id = "sns"
  arn       = aws_sns_topic.security_findings.arn

  input_transformer {
    input_paths = {
      severity    = "$.detail.findings[0].Severity.Label"
      title       = "$.detail.findings[0].Title"
      description = "$.detail.findings[0].Description"
      account     = "$.detail.findings[0].AwsAccountId"
      region      = "$.detail.findings[0].Region"
      time        = "$.detail.findings[0].UpdatedAt"
      id          = "$.detail.findings[0].Id"
    }

    input_template = <<-EOT
      "GuardDuty <severity>: <title>"
      ""
      "<description>"
      ""
      "Account: <account>  Region: <region>"
      "Observed: <time>"
      "Finding:  <id>"
      ""
      "Respond per tools/nist-800-171-incident-response.md."
      "Capture volatile evidence FIRST (section 3.2): Argo workflows are GC'd 24h"
      "after completion and Kubernetes events expire after 1 hour. Per-attempt pod"
      "exit codes and node assignments exist nowhere else."
    EOT
  }
}

resource "aws_cloudwatch_event_target" "inspector_critical_findings_sns" {
  rule      = aws_cloudwatch_event_rule.inspector_critical_findings.name
  target_id = "sns"
  arn       = aws_sns_topic.security_findings.arn

  input_transformer {
    input_paths = {
      title    = "$.detail.findings[0].Title"
      resource = "$.detail.findings[0].Resources[0].Id"
      time     = "$.detail.findings[0].UpdatedAt"
      id       = "$.detail.findings[0].Id"
    }

    input_template = <<-EOT
      "Inspector CRITICAL: <title>"
      ""
      "Resource: <resource>"
      "Observed: <time>"
      "Finding:  <id>"
      ""
      "This is a vulnerability finding, not an active incident. Triage per"
      "tools/nist-800-171-incident-response.md section 6.2 if the affected image"
      "digest is running, otherwise queue it in the POA&M."
    EOT
  }
}

# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

# Asserts the inherited GuardDuty detector still exists, without claiming
# ownership of it. This is the cheapest available answer to finding N3: the
# detector was created out-of-band, so today nothing in this repository would
# notice if an organizational policy change removed it.
#
# The two ways detection can stop have DIFFERENT teeth, and the asymmetry is
# deliberate rather than an oversight:
#
#   - Detector DELETED  -> this data source read fails -> hard error, and it
#     blocks every `terraform plan` in this root module, not just this file.
#     That is the intended severity (detection is gone), but it is also a real
#     operational hazard: unrelated infrastructure work stops until someone
#     restores the detector or comments this out. Do not "fix" that by deleting
#     the data source — restore the detector, or record a risk acceptance.
#   - Detector SUSPENDED -> reads fine, `status != "ENABLED"`, and the check
#     below emits a WARNING only. Terraform `check` blocks never fail a plan or
#     an apply. So the weaker, more likely failure mode is also the quieter one.
#     A `check` is documentation with a reminder attached, not an enforcement
#     point; the only enforcement here is the data source above it.
#
# Adopting the detector properly (`terraform import`, declare the feature set,
# enable EKS_RUNTIME_MONITORING) is POA&M P3-1b and is deliberately not done here.
data "aws_guardduty_detector" "inherited" {}

# WARNING ONLY — this does not and cannot block an apply. The topic is applied
# deliberately with zero subscribers (2026-08-17) so the rules and the KMS/topic
# policy are in place and diffable; the destination is a separate decision
# (POA&M P3-9c, "confirm a human triages the address"). Until it is made, findings
# are published to a topic nobody receives, which is strictly better than a
# missing chain but is NOT the control being claimed. Do not mark P3-9b complete
# on the strength of a green apply.
check "security_findings_have_a_subscriber" {
  assert {
    condition     = length(var.security_findings_emails) > 0
    error_message = "var.security_findings_emails is empty: GuardDuty/Inspector findings will be published to SNS and delivered to nobody. Set it in terraform.tfvars, then confirm the subscription email. A topic with no confirmed subscriber is not alert routing (POA&M P3-9c)."
  }
}

check "guardduty_detector_is_enabled" {
  assert {
    condition     = data.aws_guardduty_detector.inherited.status == "ENABLED"
    error_message = "The inherited GuardDuty detector is not ENABLED. Detection for this account has stopped; the routing in this file has nothing to route (finding N3, POA&M P3-1)."
  }
}

output "security_findings_topic_arn" {
  description = "SNS topic carrying GuardDuty HIGH/CRITICAL and Inspector CRITICAL findings. Publish a test message here to verify a human actually reads the destination (POA&M P3-9c)."
  value       = aws_sns_topic.security_findings.arn
}
