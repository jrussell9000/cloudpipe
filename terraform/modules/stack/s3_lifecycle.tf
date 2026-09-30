# S3 lifecycle and Intelligent-Tiering configuration for the cloudpipe data bucket.
# The bucket itself is pre-existing and managed outside Terraform; these resources
# only layer lifecycle and INT archive-tier configuration on top of it.
#
# WARNING: aws_s3_bucket_lifecycle_configuration manages the COMPLETE set of lifecycle
# rules for the bucket. Any rules created outside Terraform (e.g., the GDA rule that
# caused the April 2026 early-deletion penalty) will be removed on next apply.
# Verify with `terraform plan` before applying.

# Transition all derivatives/ objects into Intelligent-Tiering immediately on creation.
# INT auto-moves objects between tiers based on access patterns:
#   Frequent Access   →  $0.023/GB  (days 0–29, same as Standard)
#   Infrequent Access →  $0.0125/GB (day 30+ with no access)
#   Archive Instant   →  $0.004/GB  (day 90+ with no access, enabled below)
#
# Unlike Glacier Deep Archive, there is NO minimum storage duration and NO early-deletion
# penalty — objects move back to Frequent Access instantly on next read.
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = var.globus_s3_destination_bucket

  rule {
    id     = "derivatives-intelligent-tiering"
    status = "Enabled"

    filter {
      prefix = "derivatives/"
    }

    transition {
      days          = 0
      storage_class = "INTELLIGENT_TIERING"
    }
  }

  # Anatomical inter-step scratch (scratch/{workflow.name}/anat/*): the FastSurfer
  # SUBJECTS_DIR handed between the template-build → parcellation → long-seg →
  # long-parc pods now that they no longer share an EFS volume. These are NOT
  # derivatives — nothing outside the owning workflow may read them, and the real
  # outputs land under derivatives/fastsurfer/.
  #
  # 7 days rather than 1: a workflow is capped at 12h (activeDeadlineSeconds) and
  # kept 24h after completion (ttlStrategy), so 7 leaves room to inspect the
  # intermediates of a failed run before they vanish. Storage is Standard and
  # transient, so the carrying cost of that margin is small.
  rule {
    id     = "scratch-expiration"
    status = "Enabled"

    filter {
      prefix = "scratch/"
    }

    expiration {
      days = 7
    }
  }

  # Argo uploads these multi-hundred-MB tarballs via multipart. A pod killed
  # mid-upload (spot reclaim) leaves orphaned parts that are billed but invisible
  # to ListObjects — the same shape of silent waste as the 104 GiB of orphaned
  # EFS data this migration is retiring. Applies bucket-wide, not just scratch/.
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Archive Instant Access (objects inactive 90+ days → $0.004/GB, millisecond retrieval)
# is enabled by default within Intelligent-Tiering — no explicit tiering configuration
# resource is needed. Only the optional ARCHIVE_ACCESS / DEEP_ARCHIVE_ACCESS tiers
# require an aws_s3_bucket_intelligent_tiering_configuration resource.
