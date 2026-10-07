# ------------------------------------------------------------------------------
# ArgoCD
# https://github.com/argoproj/argo-helm/tree/main/charts/argo-cd
# ------------------------------------------------------------------------------
resource "helm_release" "argocd" {
  name             = "argocd"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  namespace        = "argocd"
  create_namespace = true
  # It is generally recommended to pin to a specific version for stability
  version       = "9.5.17" # ArgoCD v3.4.3
  timeout       = 600      # 10 min — init jobs need time on first cluster bootstrap
  wait          = true
  wait_for_jobs = true # wait for argocd-redis-secret-init Job to complete

  lifecycle {
    # An external identity provider reaches Argo Workflows, Prefect and Grafana
    # through Dex, whose issuer is ArgoCD's URL. Without a domain that URL is
    # localhost, which inside those UIs' pods is the pod itself, so their SSO
    # cannot work (design D4, open question 8). Cognito mode has no such limit.
    precondition {
      condition     = local.use_cognito || local.publish_uis
      error_message = "external_identity needs a domain: without one, Dex's issuer is http://localhost:8080/api/dex, which the Argo Workflows, Prefect and Grafana pods cannot reach. Set domain, or leave external_identity null to use the Cognito user pool."
    }
  }

  values = [
    <<-EOT
    global:
      nodeSelector:
        eks.amazonaws.com/nodegroup: argo
      tolerations:
      - key: "argoproj.io/backend"
        value: "true"
        effect: "NoSchedule"

    server:
      extraArgs:
        - --insecure # TLS terminated at the AWS ALB
    configs:
      params:
        reposerver.enable.git.submodule: "false"
      cm:
        url: "${local.ui_base_urls["argocd"]}"
        dex.config: |
          connectors:
            - type: oidc
              id: ${local.dex_connector.id}
              name: ${local.dex_connector.name}
              config:
                issuer: ${local.dex_connector.issuer}
                clientID: "${local.dex_connector.client_id}"
                clientSecret: "${local.dex_connector.client_secret}"
                redirectURI: "${local.ui_base_urls["argocd"]}/api/dex/callback"
                scopes:
                  - openid
                  - profile
                  - email
                getUserInfo: ${local.dex_connector.get_user_info}
                userNameKey: email
                insecureSkipEmailVerified: ${local.dex_connector.insecure_skip_email_verified}
          staticClients:
            - id: argo-workflows
              name: Argo Workflows
              redirectURIs:
                - "${local.ui_base_urls["argo"]}/oauth2/callback"
              secret: $argo-workflows-dex-client:clientSecret
            - id: prefect
              name: Prefect
              redirectURIs:
                - "${local.ui_base_urls["prefect"]}/oauth2/callback"
              secret: $prefect-dex-client:clientSecret
            - id: grafana
              name: Grafana
              redirectURIs:
                - "${local.ui_base_urls["grafana"]}/login/generic_oauth"
              secret: $grafana-dex-client:clientSecret
      rbac:
        ${indent(4, local.argocd_rbac_values)}
    EOT
  ]
}

locals {
  # ArgoCD's RBAC settings, built as text because the policy has one line per
  # operator.
  #
  # Subjects are the operators' emails, in both identity modes. With a Dex
  # connector, ArgoCD 3 otherwise matches on the upstream user ID — an opaque
  # `sub` for Cognito, and a provider-specific username for any other — so
  # `email` is added to the claims ArgoCD matches policy subjects against.
  argocd_rbac_values = join("\n", concat(
    ["scopes: \"[groups, email]\"", "policy.default: role:readonly", "policy.csv: |"],
    [for email in local.admin_emails : "  g, ${email}, role:admin"],
  ))
}

# Dex's three static clients below, and their secrets, serve external mode. In
# Cognito mode those UIs are the pool's own clients (cognito.tf) and the static
# clients go unused: registered, but no UI is configured to use them. Removing
# them in that mode would put template directives in the heredoc above for no
# gain — a static client is usable only with its secret, which only these
# Secrets hold.

# ------------------------------------------------------------------------------
# Argo Workflows Dex static client secret
# Shared between Dex (argocd namespace) and the Argo Workflows server (argo-workflows
# namespace). Generated once; stored as a K8s secret in each namespace.
# ------------------------------------------------------------------------------
resource "random_password" "argo_workflows_dex_client" {
  length  = 32
  special = false
}

resource "kubernetes_secret_v1" "argo_workflows_dex_client" {
  metadata {
    name      = "argo-workflows-dex-client"
    namespace = "argocd"
    labels = {
      "app.kubernetes.io/part-of" = "argocd"
    }
  }
  data = {
    clientSecret = random_password.argo_workflows_dex_client.result
  }
  depends_on = [helm_release.argocd]
}

# ------------------------------------------------------------------------------
# Prefect Dex static client secret
# Shared between Dex (argocd namespace) and the oauth2-proxy sidecar (prefect
# namespace). Generated once; stored as a K8s secret in each namespace.
# ------------------------------------------------------------------------------
resource "random_password" "prefect_dex_client" {
  length  = 32
  special = false
}

resource "kubernetes_secret_v1" "prefect_dex_client" {
  metadata {
    name      = "prefect-dex-client"
    namespace = "argocd"
    labels = {
      "app.kubernetes.io/part-of" = "argocd"
    }
  }
  data = {
    clientSecret = random_password.prefect_dex_client.result
  }
  depends_on = [helm_release.argocd]
}

# ------------------------------------------------------------------------------
# Grafana Dex static client secret
# Shared between Dex (argocd namespace) and Grafana's native generic_oauth
# (grafana namespace). Generated once; stored as a K8s secret in each namespace.
# ------------------------------------------------------------------------------
resource "random_password" "grafana_dex_client" {
  length  = 32
  special = false
}

resource "kubernetes_secret_v1" "grafana_dex_client" {
  metadata {
    name      = "grafana-dex-client"
    namespace = "argocd"
    labels = {
      "app.kubernetes.io/part-of" = "argocd"
    }
  }
  data = {
    clientSecret = random_password.grafana_dex_client.result
  }
  depends_on = [helm_release.argocd]
}

# Dex's own OIDC client credentials are not read here. They come from
# local.dex_connector (cognito.tf): the Cognito app client this module creates,
# or whatever the caller supplies through var.external_identity (design D6).

# ------------------------------------------------------------------------------
# ArgoCD Repository Credentials
# PAT is stored in Secrets Manager at cloudpipe/github-pat as {"token":"..."}
# ArgoCD auto-discovers secrets with this label at startup.
# ------------------------------------------------------------------------------
data "aws_secretsmanager_secret_version" "github_pat" {
  secret_id = "cloudpipe/github-pat"
}

resource "kubernetes_secret_v1" "argocd_repo" {
  metadata {
    name      = "cloudpipe-repo"
    namespace = "argocd"
    labels = {
      # repo-creds = credential template that applies to all repos matching the URL prefix.
      # Using the GitHub user prefix covers both cloudPipe.git and any private submodules
      # (e.g. modules/ndaDownloader) that ArgoCD must recursively clone.
      "argocd.argoproj.io/secret-type" = "repo-creds"
    }
  }

  data = {
    type     = "git"
    url      = var.github_user_url
    username = "x-token"
    password = jsondecode(data.aws_secretsmanager_secret_version.github_pat.secret_string)["password"]
  }

  depends_on = [helm_release.argocd]
}

# ------------------------------------------------------------------------------
# ArgoCD ALB Ingress — a member of the shared internal `cloudpipe-ui` ALB
# (ui_alb.tf). The ALB-level annotations come from local.ui_alb_group_annotations.
#
# ALB access logs go to the SSE-S3 access-log bucket (logging.tf), not the
# SSE-KMS master log bucket, which ALB cannot write to. Unlike S3 server access
# logging this fails loudly: ELB writes a permission-check object when the
# attribute is set, and the controller surfaces the error on the Ingress.
# ------------------------------------------------------------------------------
locals {
  # Grafana's ingress annotations as ArgoCD will render them. Read here only to
  # assert they agree with the group (precondition below).
  #
  # Two sources, merged the way Helm merges them: the app's values.yaml, then the
  # ApplicationSet's valuesObject over the top (local.argocd_app_overrides below,
  # which owns the hostname annotation). Reading only the file would make the WAF
  # assertion fail OPEN — the annotation would be absent from values.yaml and
  # present on the rendered Ingress, which is the one combination that stops the
  # whole shared ALB reconciling.
  grafana_values_file_annotations = try(
    yamldecode(file("${path.module}/../../../gitops/apps/grafana/values.yaml")).grafana.ingress.annotations,
    {}
  )

  # Reading the override map here makes this Ingress depend on everything the map
  # reads — module.metrics, for the Athena datasource. That is ordering only, and
  # no cycle: nothing in those modules reads back from ArgoCD.
  grafana_override_annotations = try(
    local.argocd_app_overrides["grafana"].grafana.ingress.annotations,
    {}
  )

  grafana_ingress_annotations = merge(
    local.grafana_values_file_annotations,
    local.grafana_override_annotations,
  )

  grafana_group_annotation_mismatches = sort([
    for k, v in local.ui_alb_group_annotations : k
    if try(tostring(local.grafana_ingress_annotations[k]), null) != v
  ])

  # The WAF ARN is the one group-level setting Grafana must NOT carry — see the
  # comment on the annotation below. It cannot be covered by the mismatch list
  # above, which only checks keys that exist in ui_alb_group_annotations, so it
  # gets its own assertion.
  grafana_sets_waf_annotation = can(local.grafana_ingress_annotations["alb.ingress.kubernetes.io/wafv2-acl-arn"])
}

# Published mode only — see local.publish_uis (locals.tf). In port-forward mode
# there is no ALB to join and operators reach ArgoCD on localhost:8080.
resource "kubernetes_ingress_v1" "argocd_ingress" {
  count = local.publish_uis ? 1 : 0

  metadata {
    name      = "argocd-ingress"
    namespace = "argocd"
    annotations = merge(local.ui_alb_group_annotations, {
      # Create a Route53 alias record automatically via external-dns
      "external-dns.alpha.kubernetes.io/hostname" = "argocd.${data.aws_route53_zone.brc[0].name}"

      # WAFv2 association for the whole shared ALB (waf.tf) — Security Hub
      # ELB.16. This carries ON THIS INGRESS ALONE, unlike every other
      # group-level setting, which all five members repeat byte for byte.
      #
      # `wafv2-acl-arn` has Exclusive merge semantics, and upstream defines
      # Exclusive as "specified on a single Ingress within IngressGroup OR
      # specified with the same value across all" — so one member is not a
      # shortcut, it is a documented form. It is the right one here because the
      # value is a Terraform-generated ARN ending in a random UUID. Putting it in
      # local.ui_alb_group_annotations would oblige Grafana's GitOps-managed
      # ingress to hardcode that UUID, which means creating the ACL in one apply,
      # reading its ARN, and committing it in a second pass — and then living
      # with a literal that silently stops matching if the ACL is ever replaced.
      #
      # Failure modes of doing it here are the mild ones. If this Ingress is
      # deleted, the annotation disappears from the group and the controller
      # "keeps LoadBalancer WAFv2 settings unchanged" — protection stays on,
      # rather than being silently dropped. What would break the group is a
      # SECOND member specifying a different value, so the precondition below
      # fails the plan if Grafana's values.yaml ever grows this key.
      "alb.ingress.kubernetes.io/wafv2-acl-arn" = aws_wafv2_web_acl.ui_alb[0].arn

      "alb.ingress.kubernetes.io/target-type"     = "ip"
      "alb.ingress.kubernetes.io/certificate-arn" = aws_acm_certificate.primary_regional[0].arn

      # Backend Protocol is HTTP because we terminate tls at the load balancer
      # (therefore we passed --insecure to the argocd-server)
      "alb.ingress.kubernetes.io/backend-protocol"     = "HTTP"
      "alb.ingress.kubernetes.io/healthcheck-protocol" = "HTTP"
      "alb.ingress.kubernetes.io/healthcheck-path"     = "/"
    })
  }

  lifecycle {
    # Grafana is the one group member Terraform does not render. A mismatch on
    # any group-level annotation stops the controller reconciling the WHOLE
    # shared ALB, so refuse to plan rather than find out mid-cutover.
    precondition {
      condition     = length(local.grafana_group_annotation_mismatches) == 0
      error_message = "Grafana's rendered ingress annotations (gitops/apps/grafana/values.yaml merged under local.argocd_app_overrides[\"grafana\"]) disagree with local.ui_alb_group_annotations (ui_alb.tf) on: ${join(", ", local.grafana_group_annotation_mismatches)}. Every cloudpipe-ui IngressGroup member needs byte-identical values for these."
    }

    # wafv2-acl-arn is Exclusive and deliberately set on this Ingress only. A
    # second member setting it at all — even to the same ARN, which GitOps cannot
    # reliably track — is the conflict that stops the whole group reconciling.
    precondition {
      condition     = !local.grafana_sets_waf_annotation
      error_message = "Grafana's rendered ingress annotations set alb.ingress.kubernetes.io/wafv2-acl-arn. That annotation is Exclusive and belongs to kubernetes_ingress_v1.argocd_ingress alone (see argocd.tf); remove it from gitops/apps/grafana/values.yaml or from local.argocd_app_overrides[\"grafana\"], whichever carries it."
    }
  }

  spec {
    ingress_class_name = "alb"
    rule {
      host = "argocd.${data.aws_route53_zone.brc[0].name}"
      http {
        path {
          backend {
            service {
              name = "argocd-server"
              port {
                number = 80
              }
            }
          }
          path      = "/*"
          path_type = "ImplementationSpecific"
        }
      }
    }
  }

  depends_on = [helm_release.argocd]
}

# ------------------------------------------------------------------------------
# ArgoCD Root Application (GitOps Bootstrap)
# ------------------------------------------------------------------------------
locals {
  # Helm values the root ApplicationSet patches into each app, keyed by the app's
  # directory name under gitops/apps. Terraform owns the values that differ
  # between deployments of this stack; gitops/apps/<app>/values.yaml keeps
  # everything that does not (design D2 of
  # openspec/changes/archive/2026-10-01-public-upstream-readiness).
  #
  # Each entry is that app's Helm values, so where the app wraps an upstream
  # chart its top-level keys are subchart names and must match the dependency
  # name in the app's Chart.yaml — Helm silently ignores values it has no chart
  # for. cluster-config declares no dependencies, so its keys are the chart's own
  # values and have to be read by something in its templates/ directory.
  #
  # Helm replaces lists instead of merging them, so a list here has to be
  # complete — external-dns's `env` carries both variables, not only the one that
  # changed.
  #
  # An app reads its entry only if gitops/bootstrap/root-app.yaml.tftpl has a
  # branch for it. Adding a key here on its own does nothing.
  argocd_app_overrides = {
    # Read in published mode only. In port-forward mode the ApplicationSet
    # excludes gitops/apps/external-dns (root-app.yaml.tftpl), so this entry,
    # whose domainFilters would be [null], reaches no Application.
    "external-dns" = {
      "external-dns" = {
        domainFilters = [var.domain]
        env = [
          { name = "AWS_DEFAULT_REGION", value = var.region },
          { name = "AWS_REGION", value = var.region },
        ]
      }
    }

    # clusterName is the cluster the controller tags load balancers for, and the
    # chart refuses to render without it, so a deployment that changes the
    # cluster name has to change this. It comes from the EKS module rather than
    # var.name so the value is the name of the cluster that actually exists.
    "aws-load-balancer-controller" = {
      "aws-load-balancer-controller" = {
        clusterName = module.eks.cluster_name
        region      = var.region
      }
    }

    # cluster-config has no subchart: `region` is its own chart value, read by
    # templates/cluster-secret-store.yaml. Leaving it unset renders a
    # ClusterSecretStore with an empty region, which external-secrets rejects.
    "cluster-config" = {
      region = var.region
    }

    # Prefect's hostname reaches the browser twice, for two different consumers.
    # prefectUiApiUrl is the URL the loaded UI calls for its API, so it has to be
    # where the browser reaches Prefect — the ALB hostname, or localhost:4200 in
    # port-forward mode — rather than the in-cluster Service. redirect-url has
    # to match the callback registered for the `prefect` client — the Dex static
    # client above, or the Cognito client of that name (cognito.tf), which
    # registers the same URL — and the two disagreeing is an SSO failure with no
    # manifest diff to see it in. The issuer is the pool in Cognito mode and Dex
    # in external mode (local.ui_oidc_issuer); oauth2-proxy discovers the rest
    # from it, from inside its pod.
    #
    # emailDomains is the one value in this whole map whose upstream default is
    # permissive rather than empty: the oauth2-proxy chart ships ["*"], so a render
    # with this override missing admits every identity the IdP will issue a token
    # for. It is also a list, so it has to be complete.
    "prefect" = {
      "prefect-server" = {
        server = {
          uiConfig = {
            prefectUiApiUrl = "${local.ui_base_urls["prefect"]}/api"
          }
        }
      }
      "oauth2-proxy" = {
        config = {
          emailDomains = local.admin_email_domains
        }
        extraArgs = {
          "oidc-issuer-url" = local.ui_oidc_issuer
          "redirect-url"    = "${local.ui_base_urls["prefect"]}/oauth2/callback"
        }
      }
    }

    # Grafana's hostname reaches the ALB twice — the ingress host rule and the
    # external-dns annotation that creates the Route53 record for it. The group's
    # other annotations stay in values.yaml, and the preconditions on
    # kubernetes_ingress_v1.argocd_ingress above read this entry merged over that
    # file, because from here on either source can carry an ALB annotation.
    #
    # In port-forward mode there is no ALB to join, so the ingress is off, and
    # its hostname-bearing values are empty rather than a `null` that yamlencode
    # would render into a host rule. The annotation map stays a literal with an
    # empty value, not a conditional map: Helm merges it key by key either way,
    # and tests/test_argocd_app_overrides.py reads its keys from the text (task 3.2 of
    # openspec/changes/optional-domain-and-cognito-auth).
    #
    # root_url is the URL a browser reaches Grafana at, in either mode — https
    # under the domain, or http://localhost:3000 — which is why it is here and not
    # built from a bare domain in values.yaml, which could not switch scheme.
    # Grafana builds its OAuth redirect from it, so it has to agree with the
    # callback the `grafana` client registers.
    #
    # The whole `datasources` map is here for one value in it — defaultRegion.
    # Helm replaces lists instead of merging them, and the datasources are a list,
    # so an override of one field would drop the others. The Prometheus entry is
    # not deployment-specific and comes along only for that reason; the Athena
    # entry is, and reads the Glue database, workgroup and query-output bucket
    # from the resources that create them.
    "grafana" = {
      "grafana" = {
        ingress = {
          enabled = local.publish_uis
          annotations = {
            "external-dns.alpha.kubernetes.io/hostname" = local.publish_uis ? local.grafana_url : ""
          }
          hosts = local.publish_uis ? [local.grafana_url] : []
        }

        datasources = {
          "datasources.yaml" = {
            apiVersion = 1
            datasources = [
              {
                name      = "Prometheus"
                type      = "prometheus"
                uid       = "cloudpipe-prometheus"
                access    = "proxy"
                isDefault = false
                url       = "http://prometheus-kube-prometheus-prometheus.prometheus.svc:9090"
                jsonData  = { timeInterval = "30s" }
              },
              {
                name      = "Athena - CloudPipe Metrics"
                type      = "grafana-athena-datasource"
                uid       = "cloudpipe-athena"
                access    = "proxy"
                isDefault = true
                jsonData = {
                  authType       = "default"
                  defaultRegion  = var.region
                  catalog        = "AwsDataCatalog"
                  database       = module.metrics.glue_database_name
                  workgroup      = module.metrics.athena_workgroup_name
                  outputLocation = "s3://${var.name}-finops/grafana-query-results/"
                }
              },
            ]
          }
        }

        # Grafana's SSO, which differs by identity mode (local.ui_oidc_*,
        # cognito.tf): the Cognito pool's endpoints and its `grafana` client in
        # Cognito mode, Dex's routes and its `grafana` static client in external
        # mode. generic_oauth takes the three endpoints separately rather than
        # discovering them, and calls the token and userinfo ones from inside the
        # pod, which is why Cognito mode does not route Grafana through Dex.
        #
        # role_attribute_path grants Grafana's Admin role to the administrators
        # and Viewer to everyone else the SSO lets in — a JMESPath list literal,
        # because Cognito mode has several.
        "grafana.ini" = {
          server = {
            root_url = local.ui_base_urls["grafana"]
          }
          "auth.generic_oauth" = {
            client_id           = local.ui_oidc_client_ids["grafana"]
            auth_url            = local.ui_oidc_authorize_url
            token_url           = local.ui_oidc_token_url
            api_url             = local.ui_oidc_userinfo_url
            allowed_domains     = join(" ", local.admin_email_domains)
            role_attribute_path = "contains(`${jsonencode(local.admin_emails)}`, email) && 'Admin' || 'Viewer'"
          }
        }
      }
    }
  }
}

resource "kubectl_manifest" "argocd_root_app" {
  # Rendered, not read verbatim: the repository the ApplicationSet reads, the
  # revision it tracks, and each app's Helm overrides come from Terraform, so a
  # deployment value has one input surface (ADR 020, design D2 of
  # openspec/changes/archive/2026-10-01-public-upstream-readiness). Terraform's
  # `${ }` and ArgoCD's `{{ }}` do not collide, so one file can carry both.
  yaml_body = templatefile("${path.module}/../../../gitops/bootstrap/root-app.yaml.tftpl", {
    repo_url      = var.gitops_repo_url
    revision      = var.gitops_revision
    app_overrides = local.argocd_app_overrides
    publish_uis   = local.publish_uis
  })

  # Ensure ArgoCD is fully installed before trying to create the Application CRD
  depends_on = [helm_release.argocd]
}

resource "kubectl_manifest" "argocd_workflow_templates" {
  # The Application that syncs argo/workflows into the argo-workflows namespace.
  #
  # Rendered here rather than read from gitops/apps/, for the same reason as the
  # root ApplicationSet above: its repoURL and targetRevision are deployment
  # values, and a fork has to be able to set them without editing a manifest
  # (ADR 020, task 3.7 of
  # openspec/changes/archive/2026-10-01-public-upstream-readiness). It reads
  # the same two variables, so the generated apps and this one can never track
  # different repositories.
  #
  # This object was previously a resource of the generated `pipelines`
  # Application. It is adopted, not created: `Delete=false` on the manifest kept
  # it alive when that Application was deleted, and this apply takes ownership of
  # the live object. Server-side apply with force_conflicts is what lets it take
  # the fields ArgoCD's own server-side apply still owns.
  yaml_body = templatefile("${path.module}/../../../gitops/bootstrap/workflow-templates.yaml.tftpl", {
    repo_url = var.gitops_repo_url
    revision = var.gitops_revision
  })

  server_side_apply = true
  force_conflicts   = true

  depends_on = [helm_release.argocd]
}

################################################################################
# Kubernetes NetworkPolicy — argocd namespace
# NIST 800-171 H4: SC-7 Boundary Protection
#
# Applied after the ArgoCD Helm release so the namespace exists.
################################################################################

# Default deny all ingress and egress traffic.
resource "kubernetes_network_policy_v1" "argocd_default_deny" {
  metadata {
    name      = "default-deny-all"
    namespace = "argocd"
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }

  depends_on = [helm_release.argocd]
}

# Allow all intra-namespace ingress and egress.
# ArgoCD components communicate heavily with each other (server → redis, server →
# repo-server, application-controller → redis, server → dex, etc.) across several
# ports. Allowing unrestricted intra-namespace traffic avoids enumerating every
# ArgoCD-internal port while still blocking cross-namespace access.
resource "kubernetes_network_policy_v1" "argocd_intra_namespace" {
  metadata {
    name      = "allow-intra-namespace"
    namespace = "argocd"
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
    ingress {
      from {
        pod_selector {}
      }
    }
    egress {
      to {
        pod_selector {}
      }
    }
  }

  depends_on = [helm_release.argocd]
}

# Allow egress to CoreDNS in kube-system on UDP and TCP 53.
resource "kubernetes_network_policy_v1" "argocd_egress_dns" {
  metadata {
    name      = "allow-egress-dns"
    namespace = "argocd"
  }
  spec {
    pod_selector {}
    policy_types = ["Egress"]
    egress {
      ports {
        port     = "53"
        protocol = "UDP"
      }
      ports {
        port     = "53"
        protocol = "TCP"
      }
      to {
        ip_block {
          cidr = "0.0.0.0/0"
        }
      }
    }
  }

  depends_on = [helm_release.argocd]
}

# Allow HTTPS egress (443) for:
#   - Kubernetes API server (private VPC endpoint) — application-controller reconciliation
#   - GitHub (git clone/fetch from repo-server for GitOps syncs)
#   - AWS APIs accessed by argocd-notifications or other integrations
resource "kubernetes_network_policy_v1" "argocd_egress_https" {
  metadata {
    name      = "allow-egress-https"
    namespace = "argocd"
  }
  spec {
    pod_selector {}
    policy_types = ["Egress"]
    egress {
      ports {
        port     = "443"
        protocol = "TCP"
      }
      to {
        ip_block {
          cidr = "0.0.0.0/0"
        }
      }
    }
  }

  depends_on = [helm_release.argocd]
}

# Allow ingress to the ArgoCD server from the ALB.
# ALB is internet-facing with target-type=ip; health check and forwarded traffic
# arrive from ALB ENI IPs in the public subnets, which are within the VPC CIDR.
# Port 8080 is the HTTP port on the argocd-server pod (TLS terminated at ALB).
resource "kubernetes_network_policy_v1" "argocd_ingress_server" {
  metadata {
    name      = "allow-ingress-argocd-server"
    namespace = "argocd"
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "argocd-server"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "8080"
        protocol = "TCP"
      }
      from {
        ip_block {
          cidr = var.vpc_cidr
        }
      }
    }
  }

  depends_on = [helm_release.argocd]
}

# Allow kubelet liveness/readiness probes to argocd-repo-server (port 8084)
# and argocd-application-controller (port 8082). Probe traffic originates from
# the node IP (host network) and is blocked by default-deny-all without this rule.
resource "kubernetes_network_policy_v1" "argocd_ingress_repo_server_probe" {
  metadata {
    name      = "allow-ingress-repo-server-probe"
    namespace = "argocd"
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "argocd-repo-server"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "8084"
        protocol = "TCP"
      }
      from {
        ip_block {
          cidr = var.vpc_cidr
        }
      }
    }
  }

  depends_on = [helm_release.argocd]
}

resource "kubernetes_network_policy_v1" "argocd_ingress_app_controller_probe" {
  metadata {
    name      = "allow-ingress-app-controller-probe"
    namespace = "argocd"
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "argocd-application-controller"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "8082"
        protocol = "TCP"
      }
      from {
        ip_block {
          cidr = var.vpc_cidr
        }
      }
    }
  }

  depends_on = [helm_release.argocd]
}
