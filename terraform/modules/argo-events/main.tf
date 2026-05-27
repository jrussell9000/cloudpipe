# https://raw.githubusercontent.com/awslabs/data-on-eks/f94cebb6424ddcac0918730296b068098505aaaa/schedulers/terraform/argo-workflow/spark-team.tf

#---------------------------------------------------------------
# Argo Events
#---------------------------------------------------------------

# Create the Argo Events namespace
resource "kubernetes_namespace_v1" "argo_events" {
  metadata {
    name = var.argo_events_namespace
  }
}

# Argo Events Helm release is managed by ArgoCD (gitops/apps/argo-events/)

# Permissions necessary to read/send to/from SQS
data "aws_iam_policy_document" "sqs_argo_events" {
  statement {
    sid    = "AllowReadingAndSendingSQSfromArgoEvents"
    effect = "Allow"
    resources = [
      aws_sqs_queue.job_queue.arn,
      aws_sqs_queue.first_level_queue.arn,
    ]
    actions = [
      "sqs:ListQueues",
      "sqs:GetQueueUrl",
      "sqs:ListDeadLetterSourceQueues",
      "sqs:ListMessageMoveTasks",
      "sqs:ReceiveMessage",
      "sqs:SendMessage",
      "sqs:GetQueueAttributes",
      "sqs:ListQueueTags",
      "sqs:DeleteMessage"
    ]
  }
}

resource "aws_iam_policy" "sqs_argo_events" {
  description = "IAM policy for Argo Events"
  name_prefix = format("%s-%s-", var.name, "argo-events")
  path        = "/"
  policy      = data.aws_iam_policy_document.sqs_argo_events.json
}

# Pod Identity for Argo Events handler — gives SQS access to the handler SA
module "pod_identity_argo_events" {
  source = "terraform-aws-modules/eks-pod-identity/aws"

  name = var.argo_events_handler_sa
  additional_policy_arns = {
    SQSArgoEventsPolicy = aws_iam_policy.sqs_argo_events.arn
  }

  associations = {
    cloudpipe = {
      cluster_name    = var.cluster_name
      namespace       = var.argo_events_namespace
      service_account = var.argo_events_handler_sa
    }
  }

  depends_on = [kubernetes_namespace_v1.argo_events]
}

# Service account for the Argo Events handler — no IRSA annotation needed with Pod Identity
resource "kubernetes_service_account_v1" "argo_events_handler_sa" {
  metadata {
    name      = var.argo_events_handler_sa
    namespace = var.argo_events_namespace
  }
  automount_service_account_token = true
  depends_on                      = [kubernetes_namespace_v1.argo_events]
}

#---------------------------------------------------------------
# SQS
#---------------------------------------------------------------

# Creating a job queue for the workflow
resource "aws_sqs_queue" "job_queue" {
  name                        = "${var.name}-jobqueue.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
}

# Separate queue for fmri-first-level-proc jobs
resource "aws_sqs_queue" "first_level_queue" {
  name                        = "${var.name}-first-level-jobqueue.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
}

# Install eventbus pods
# count = 0 until ArgoCD installs the argo-events CRDs; set crds_available=true after first ArgoCD sync
resource "kubectl_manifest" "argo-events-eventbus" {
  count      = var.crds_available ? 1 : 0
  yaml_body  = file("${path.module}/yamls/eventbus.yaml")
  depends_on = [kubernetes_namespace_v1.argo_events]
}

# Install event source for AWS SQS
resource "kubectl_manifest" "argo-events-sqs-eventsource" {
  count = var.crds_available ? 1 : 0
  yaml_body = templatefile("${path.module}/yamls/eventsource-sqs.yaml", {
    event_sa   = kubernetes_service_account_v1.argo_events_handler_sa.metadata[0].name
    queue_name = aws_sqs_queue.job_queue.name
    region     = var.region
    endpoint   = aws_sqs_queue.job_queue.url
  })
  depends_on = [kubernetes_namespace_v1.argo_events]
}

resource "kubernetes_cluster_role_v1" "argo-events-handler" {
  metadata {
    name = "argo-events-handler-cluster-role"
  }
  rule {
    api_groups = ["argoproj.io"]
    resources  = ["workflows", "workflowtemplates", "cronworkflows", "clusterworkflowtemplates"]
    verbs      = ["get", "list", "create"]
  }
}

resource "kubernetes_role_binding_v1" "argo-events-handler-argo-events" {
  metadata {
    name      = "argo-events-handler-role-binding-argo-events"
    namespace = "argo-events"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.argo-events-handler.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.argo_events_handler_sa.metadata[0].name
    namespace = "argo-events"
    api_group = ""
  }
}

resource "kubernetes_role_binding_v1" "argo-events-handler-argo-workflows" {
  metadata {
    name      = "argo-events-handler-role-binding-argo-workflows"
    namespace = var.argo_workflows_namespace
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.argo-events-handler.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.argo_events_handler_sa.metadata[0].name
    namespace = "argo-events"
    api_group = ""
  }
}

# Create an Argo Event Sensor to trigger workflows
resource "kubectl_manifest" "argo-events-sensor" {
  count = var.crds_available ? 1 : 0
  yaml_body = templatefile("${path.module}/yamls/cloudpipe-trigger.yaml",
    {
      argoworkflows_ns            = var.argo_workflows_namespace
      workflow2trigger            = var.argo_workflows_workflow2trigger
      globus_source_collection_id = var.globus_source_collection_id
      globus_source_base_path     = var.globus_source_base_path
      globus_dest_collection_id   = var.globus_dest_collection_id
      globus_dest_base_path       = var.globus_dest_base_path
      globus_scan_types           = var.globus_scan_types
  })
  depends_on = [
    kubernetes_namespace_v1.argo_events,
    kubernetes_role_binding_v1.argo-events-handler-argo-events,
    kubernetes_role_binding_v1.argo-events-handler-argo-workflows,
  ]
}

# ── First-Level Processing SQS EventSource + Sensor ───────────────────────────

resource "kubectl_manifest" "argo-events-first-level-eventsource" {
  count = var.crds_available ? 1 : 0
  yaml_body = templatefile("${path.module}/yamls/first-level-eventsource-sqs.yaml", {
    event_sa   = kubernetes_service_account_v1.argo_events_handler_sa.metadata[0].name
    queue_name = aws_sqs_queue.first_level_queue.name
    region     = var.region
    endpoint   = aws_sqs_queue.first_level_queue.url
  })
  depends_on = [kubernetes_namespace_v1.argo_events]
}

resource "kubectl_manifest" "argo-events-first-level-sensor" {
  count = var.crds_available ? 1 : 0
  yaml_body = templatefile("${path.module}/yamls/first-level-trigger.yaml", {
    argoworkflows_ns = var.argo_workflows_namespace
  })
  depends_on = [
    kubernetes_namespace_v1.argo_events,
    kubernetes_role_binding_v1.argo-events-handler-argo-events,
    kubernetes_role_binding_v1.argo-events-handler-argo-workflows,
  ]
}
