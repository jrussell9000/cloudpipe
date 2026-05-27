locals {
  finops_bucket_name = "${var.root_name}-finops"
  athena_table_name  = replace("${var.root_name}-cur-report", "-", "_")
}
