################################################################################
# The metrics bucket moved to the bootstrap root (design D7 of
# openspec/changes/publish-bootstrap-buckets).
#
# It records what was processed, so losing it with the cluster would be wrong —
# and a bucket named after the deployment that the deployment itself creates
# cannot survive a teardown and then be installed over: the second apply fails
# with BucketAlreadyOwnedByYou. So it is created outside the stack and adopted
# here by name, exactly as the data bucket always has been.
#
# WHY `removed` BLOCKS RATHER THAN `terraform state rm`:
#
# Deleting the resource declarations on their own would make the next apply plan
# a DESTROY of all five, because a resource in state and absent from the
# configuration is one Terraform believes it should delete. AWS would refuse to
# delete the bucket itself (2.56M objects, and no `force_destroy`), but it would
# happily delete the things protecting it: the policy carrying the transport
# Deny, the public-access block, and the versioning configuration whose
# noncurrent versions are the entire reason this bucket is separate from the
# data bucket.
#
# `removed` with `destroy = false` says the opposite — forget these, change
# nothing — so the apply that lands this change strips no protection and needs
# no operator state surgery. The plan reads as resources to forget, none to
# destroy.
#
# These blocks can go once that apply has run wherever this module is deployed.
# Until then, deleting them reintroduces exactly the destroy they prevent.
################################################################################

removed {
  from = aws_s3_bucket.metrics

  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_s3_bucket_versioning.metrics

  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_s3_bucket_public_access_block.metrics

  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_s3_bucket_server_side_encryption_configuration.metrics

  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_s3_bucket_policy.metrics

  lifecycle {
    destroy = false
  }
}
