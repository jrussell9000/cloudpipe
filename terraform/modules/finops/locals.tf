locals {
  finops_bucket_name = var.bucket_name
  athena_table_name  = replace("${var.root_name}-cur-report", "-", "_")
}
