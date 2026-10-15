################################################################################
# Where the workflows' images come from
################################################################################
# Every WorkflowTemplate composes `{{ecr-registry}}/cloudpipe/<name>@<digest>`,
# and this file decides what `ecr-registry` is (openspec/changes/
# publish-images-for-deployers, decision 2).
#
#   private       This deployment's own private ECR. Right for a deployment that
#                 builds its images, which is the reference one. Anyone else's
#                 private ECR starts empty, and every pull would fail.
#
#   public-cache  (default) A pull-through cache whose upstream is ECR Public.
#                 The publisher copies each pinned image there by digest, so the
#                 digests resolve unchanged. The first pull of a digest imports
#                 it into this account's private ECR under the cache prefix; every
#                 later pull is served from there over the S3 gateway endpoint,
#                 not across the NAT gateway.
#
# The templates need no change either way: the cache path is
#   <acct>.dkr.ecr.<region>.amazonaws.com/<prefix>/<publisher alias>
# and `/cloudpipe/<name>@<digest>` appended to it names
# `public.ecr.aws/<publisher alias>/cloudpipe/<name>@<digest>` upstream.

locals {
  image_cache_enabled = var.image_registry == "public-cache"

  ecr_registry = (
    local.image_cache_enabled
    ? "${local.ecr_private_registry}/${var.image_cache_prefix}/${var.image_publisher_alias}"
    : local.ecr_private_registry
  )
}

resource "aws_ecr_pull_through_cache_rule" "public" {
  count = local.image_cache_enabled ? 1 : 0

  ecr_repository_prefix = var.image_cache_prefix
  # ECR Public needs no credential, so there is no credential_arn.
  upstream_registry_url = "public.ecr.aws"

  lifecycle {
    # ECR scan filters match by prefix, and the private repositories are
    # `<name>/<image>`, so a cache prefix sharing that start would put a
    # repository under two scan rules (ecr.tf). A precondition rather than a
    # variable validation: the setup wizard reads a validation that names a
    # second variable as "conditionally required".
    precondition {
      condition     = !startswith(var.image_cache_prefix, var.name) && !startswith(var.name, var.image_cache_prefix)
      error_message = "image_cache_prefix must not share a prefix with var.name; ECR scan filters match by prefix."
    }
  }
}

# Settings for the repositories ECR creates on a first pull. Without a template
# they get ECR's defaults and no lifecycle policy, so every digest ever pinned
# would be kept forever.
#
# The lifecycle rule differs from the private registry's on purpose. There,
# untagged images expire after 7 days and the pinned ones survive because CI also
# pushes a sha- tag. A cached image pulled by digest has NO tag, so that rule
# would expire every image a week after its first pull, and each pod would pull
# it through the cache again. Keeping the newest few per repository keeps every
# digest the templates pin today and the ones they pinned just before, which are
# what an in-flight workflow may still run.
resource "aws_ecr_repository_creation_template" "image_cache" {
  count = local.image_cache_enabled ? 1 : 0

  prefix      = var.image_cache_prefix
  description = "Repositories created by the pull-through cache of the published pipeline images"
  applied_for = ["PULL_THROUGH_CACHE"]

  image_tag_mutability = "MUTABLE"

  encryption_configuration {
    encryption_type = "AES256"
  }

  lifecycle_policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the ${var.image_cache_keep} most recently cached images per repository"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = var.image_cache_keep
      }
      action = { type = "expire" }
    }]
  })

  # No `resource_tags`. ECR creates cached repositories as its
  # AWSServiceRoleForECRTemplate service-linked role, which may create a
  # repository but not tag one, so a template with tags failed EVERY first pull:
  # "ECRPullThroughCacheWithTemplate is not authorized to perform:
  # ecr:TagResource". The pod waited on the image until its deadline, and nothing
  # was ever cached (second-account rerun, 2026-10-15; seen in CloudTrail). Tags
  # would need a `custom_role_arn` able to tag; a Name tag is not worth owning one.
  # The reference deployment pulls from its private registry and never reaches
  # this path.
}

# What a node needs beyond AmazonEC2ContainerRegistryReadOnly / PullOnly to pull
# a digest the cache does not hold yet: create the repository on first use and
# import the image from upstream. Scoped to the cache prefix, so it cannot create
# or import into any other repository.
data "aws_iam_policy_document" "image_cache_pull" {
  count = local.image_cache_enabled ? 1 : 0

  statement {
    sid    = "PullThroughCacheFirstPull"
    effect = "Allow"
    actions = [
      "ecr:BatchImportUpstreamImage",
      "ecr:CreateRepository",
    ]
    resources = [
      "arn:aws:ecr:${var.region}:${data.aws_caller_identity.current.account_id}:repository/${var.image_cache_prefix}/*",
    ]
  }
}

resource "aws_iam_policy" "image_cache_pull" {
  count = local.image_cache_enabled ? 1 : 0

  name        = "${var.name}-image-cache-pull"
  description = "First pull of a published pipeline image through the ECR pull-through cache"
  policy      = data.aws_iam_policy_document.image_cache_pull[0].json
}

# Every node role, not only Karpenter's: the ops CronWorkflows and the Prefect
# flow runner pull from the same registry, and which nodes they land on is the
# scheduler's choice, not this file's.
locals {
  image_cache_node_roles = {
    karpenter         = module.karpenter.node_iam_role_name
    backend_nodegroup = "${var.name}-backend-nodegroup-iam-role"
    karpenter_managed = "${var.name}-karpenter-nodegroup-iam-role"
    argo_nodegroup    = "${var.name}-argo-nodegroup-iam-role"
  }
}

resource "aws_iam_role_policy_attachment" "image_cache_pull" {
  for_each = local.image_cache_enabled ? local.image_cache_node_roles : {}

  role       = each.value
  policy_arn = aws_iam_policy.image_cache_pull[0].arn

  # The three managed node group roles are named here, not read from the module,
  # so they must exist before the attachment.
  depends_on = [module.eks]
}
