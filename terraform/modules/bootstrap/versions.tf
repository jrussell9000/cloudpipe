terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Intentionally local backend — this root creates the bucket the stack's
  # backend lives in, so it cannot use that backend itself. It is applied rarely
  # and manages a handful of buckets; if this state is lost, recovery is a
  # `terraform import` per bucket, not the 250+ resource recovery the state
  # bucket exists to prevent.
  #
  # Keep the state file. It is gitignored (*.tfstate), so it lives only where it
  # was applied from.
}

provider "aws" {
  region = var.region
}
