################################################################################
# Prometheus metrics — headless service + ServiceMonitor
# Ref: https://argo-workflows.readthedocs.io/en/latest/metrics/#prometheus-scraping
################################################################################

resource "kubernetes_service_v1" "controller_metrics" {
  metadata {
    name      = "argo-workflows-controller-metrics"
    namespace = var.namespace
    labels = {
      app = "workflow-controller"
    }
  }

  spec {
    selector = {
      app = "workflow-controller"
    }

    port {
      name        = "metrics"
      port        = 9090
      protocol    = "TCP"
      target_port = 9090
    }

    # Headless — Prometheus discovers pods directly rather than via kube-proxy
    cluster_ip = "None"
  }

  depends_on = [data.kubernetes_namespace_v1.this]
}

# count = 0 until ArgoCD installs the prometheus-operator CRDs; set crds_available=true after first ArgoCD sync
resource "kubectl_manifest" "service_monitor" {
  count     = var.crds_available ? 1 : 0
  yaml_body = <<-YAML
    apiVersion: monitoring.coreos.com/v1
    kind: ServiceMonitor
    metadata:
      name: argo-workflows
      namespace: ${var.namespace}
    spec:
      endpoints:
      - port: metrics
      selector:
        matchLabels:
          app: workflow-controller
      namespaceSelector:
        matchNames:
        - ${var.namespace}

  YAML

  # prometheus-operator-crds must be installed before ServiceMonitor CRD is available.
  # Enforce this at the root module level via depends_on on the module block.
  depends_on = [kubernetes_service_v1.controller_metrics]
}
