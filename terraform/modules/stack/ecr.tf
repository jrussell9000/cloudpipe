################################################################################
# ECR Private — Build cache repository
################################################################################

resource "aws_ecr_repository" "build_cache" {
  name                 = "${var.name}-cache"
  image_tag_mutability = "MUTABLE"

  # Scanning is configured at the REGISTRY level, in
  # aws_ecr_registry_scanning_configuration.this — not here. This repo is
  # deliberately matched by no rule there and is therefore unscanned: it holds
  # buildx cache manifests rather than runnable images. There is no
  # `image_scanning_configuration` block because that is the BASIC-tier knob and
  # is inert under ENHANCED; stating `scan_on_push = false` in it would read as a
  # decision to disable scanning that this file does not actually make.

  lifecycle {
    prevent_destroy = false
  }

  tags = {
    Name = "${var.name}-cache"
  }
}

# Expire cache manifests after 30 days so stale image layers don't accumulate.
resource "aws_ecr_lifecycle_policy" "build_cache" {
  repository = aws_ecr_repository.build_cache.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire cache tags older than 30 days"
      selection = {
        tagStatus   = "any"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
}

output "ecr_cache_registry" {
  description = "Private ECR registry URL for the build cache repo — used in GHA workflows"
  value       = aws_ecr_repository.build_cache.repository_url
}

################################################################################
# ECR Public Repositories
################################################################################

locals {
  # One repository per image directory. This is the CANONICAL set and drives the
  # private registry plus vulnerability scanning:
  #   <acct>.dkr.ecr.<region>.amazonaws.com/cloudpipe/<name>  (aws_ecr_repository.images)
  #
  # It is NOT the public set any more — see local.ecr_images_public below. Never
  # remove a name from here to retire a public repo: this same list drives
  # aws_ecr_repository.images, its lifecycle policy, AND the scan-frequency
  # tiers, so a deletion would destroy the private repo and silently drop the
  # image out of Inspector coverage (the RA-5 claim behind finding N5) while
  # leaving the check blocks below satisfied, since a removed name partitions
  # perfectly well.
  ecr_images = [
    "afni",
    "diffusion",
    "fastsurfer",
    "fireants",
    "fmriprep",
    "freesurfer",
    "fsl",
    "fsqc",
    "globus",
    "python",
    "synthmorph",
    "workbench",
    "fmri-first-level-proc",
    "cloudpipe-flow-runner",
  ]

  # Step 6 of handoffs/ecr-private-registry-migration.md, one image at a time.
  # Adding a name here DESTROYS its ECR Public repository and every image in it.
  # Preconditions before a name goes in, all of which must be verified live and
  # not just read off the repo:
  #
  #   1. No consumer resolves the image through public.ecr.aws. A digest pin in
  #      a workflow template counts only if the digest is present in PRIVATE ECR
  #      (`aws ecr batch-get-image`); a Prefect deployment counts only per
  #      `prefect deployment inspect`, never per prefect.yaml, because the image
  #      is stored server-side.
  #   2. No IN-FLIGHT Argo workflow references it. A running workflow freezes its
  #      image refs in its stored spec, so this is not answerable from git.
  #   3. The push side no longer dual-pushes it, or CI breaks on the next build
  #      when it pushes to a repo that no longer exists.
  #
  # Retiring is one-way for the images: ECR Public repos are recreatable, but
  # their contents are not, so rollback means a rebuild rather than a revert.
  ecr_images_public_retired = [
    # fmri-first-level-proc: the Argo template has pinned
    # <acct>.dkr.ecr.<region>.amazonaws.com/cloudpipe/fmri-first-level-proc
    # @sha256:1423bedb... since the 2026-08-16 build, dual-push is removed from
    # build-fmri-first-level-proc.yaml in this same change, and nothing else in
    # the tree references the image.
    "fmri-first-level-proc",
  ]

  # The public set is DERIVED, so a name can only ever leave it by being listed
  # as retired above.
  ecr_images_public = setsubtract(toset(local.ecr_images), toset(local.ecr_images_public_retired))

  # Scan-frequency tiers, consumed by aws_ecr_registry_scanning_configuration
  # below. Sizes are the compressed registry size of the most recent push as of
  # 2026-08-16 (`aws ecr describe-images ... imageSizeInBytes`); the split is at
  # roughly 1 GB.
  #
  # Four repos have never been pushed to, so they are placed by what their
  # Dockerfile pulls in rather than by measurement: fsl and fmriprep are
  # full upstream neuroimaging distributions (large), diffusion and synthmorph
  # build on ubuntu:24.04 + pixi (small). Re-check these once they are real.
  #
  # Every repo in ecr_images must appear in exactly one tier — the check block
  # below enforces that, so adding a repo without tiering it fails the plan.
  ecr_images_continuous_scan = [
    "fastsurfer", # 5.90 GB
    "fireants",   # 5.35 GB
    "freesurfer", # 6.77 GB
    "fsl",        # unbuilt
    "fmriprep",   # unbuilt
  ]

  ecr_images_scan_on_push = [
    "afni",                  # 593 MB
    "cloudpipe-flow-runner", # 380 MB
    "diffusion",             # never built; ubuntu:24.04 + pixi
    "fmri-first-level-proc", # 578 MB
    "fsqc",                  # 215 MB
    "globus",                # 56 MB
    "python",                # 42 MB
    "synthmorph",            # unbuilt
    "workbench",             # 485 MB
  ]
}

check "ecr_public_retired_names_are_real" {
  # setsubtract silently ignores a name that is not in the set, so a typo in
  # ecr_images_public_retired would leave the public repo standing and the plan
  # would report no change -- reading as "already retired" when nothing was.
  assert {
    condition = length(setsubtract(
      toset(local.ecr_images_public_retired),
      toset(local.ecr_images),
    )) == 0
    error_message = "A name in ecr_images_public_retired is not in ecr_images; it retires nothing. Check the spelling."
  }
}

check "ecr_scan_tiers_partition_images" {
  assert {
    condition = length(setsubtract(
      toset(local.ecr_images),
      setunion(toset(local.ecr_images_continuous_scan), toset(local.ecr_images_scan_on_push)),
    )) == 0
    error_message = "Every repo in local.ecr_images needs a scan tier: add it to ecr_images_continuous_scan or ecr_images_scan_on_push."
  }

  assert {
    condition = length(setintersection(
      toset(local.ecr_images_continuous_scan),
      toset(local.ecr_images_scan_on_push),
    )) == 0
    error_message = "A repo appears in both scan tiers; ECR rule precedence across overlapping filters is undefined, so tiers must be disjoint."
  }

  # ECR repository filters match by PREFIX, not exact name: a filter of "prod"
  # also matches "prod1" and "prodtest". Names that are disjoint as strings can
  # therefore still overlap as filters, which would put one repo under two rules.
  assert {
    condition = length([
      for pair in setproduct(local.ecr_images_continuous_scan, local.ecr_images_scan_on_push) :
      pair if startswith(pair[0], pair[1]) || startswith(pair[1], pair[0])
    ]) == 0
    error_message = "Two repos in different scan tiers have a prefix relationship; ECR filters match by prefix, so one repo would match both rules."
  }
}

################################################################################
# ECR Registry — vulnerability scanning
################################################################################
# scan_type is a REGISTRY-wide setting: BASIC and ENHANCED cannot coexist across
# repositories in the same account+region. The per-repository
# `image_scanning_configuration.scan_on_push` field is a BASIC-only knob and is
# inert while this is ENHANCED, which is why no repository in this file declares
# one.
#
# ENHANCED delegates to Amazon Inspector, which is what we want for the images
# that matter: it reads OS packages *and* language packages (pip, conda), which
# BASIC does not, and it is the only tier that finds anything in a
# FreeSurfer/FastSurfer image where nearly all of the risk is in the Python
# environment rather than the Ubuntu base. Inspector must be enabled for ECR at
# the account level for this to take effect; it is (inspector2 resourceState.ecr
# = ENABLED), enabled outside Terraform.
#
# Scan frequency is then split per repository:
#
#   SCAN_ON_PUSH    — one scan when the image is pushed, then nothing. Applied
#                     to the small images (<1 GB), which are thin layers over a
#                     current base and get rebuilt often enough that the next
#                     push is the practical refresh.
#   CONTINUOUS_SCAN — rescanned as new CVEs are published, for up to 30 days
#                     past the last pull. Applied to the large images
#                     (5-7 GB), which carry the FreeSurfer/AFNI/FSL
#                     distributions, rebuild rarely, and stay pinned by digest
#                     in workflow templates for months. Those are exactly the
#                     images where a CVE published after the push would
#                     otherwise never be surfaced.
#
# A repository matched by NO rule is not scanned. That deliberately excludes
# aws_ecr_repository.build_cache: it holds buildx cache manifests, not runnable
# images, and Inspector reports every one of them as UNSUPPORTED_MEDIA_TYPE —
# 204 such entries under the previous wildcard-everything rule.
resource "aws_ecr_registry_scanning_configuration" "this" {
  scan_type = "ENHANCED"

  rule {
    scan_frequency = "SCAN_ON_PUSH"

    dynamic "repository_filter" {
      for_each = toset(local.ecr_images_scan_on_push)
      content {
        filter      = "${var.name}/${repository_filter.value}"
        filter_type = "WILDCARD"
      }
    }
  }

  rule {
    scan_frequency = "CONTINUOUS_SCAN"

    dynamic "repository_filter" {
      for_each = toset(local.ecr_images_continuous_scan)
      content {
        filter      = "${var.name}/${repository_filter.value}"
        filter_type = "WILDCARD"
      }
    }
  }
}

resource "aws_ecrpublic_repository" "images" {
  # Deliberately local.ecr_images_public, NOT local.ecr_images: the public
  # registry is being retired image by image while the private one keeps every
  # repo. See local.ecr_images_public_retired for the preconditions.
  for_each = local.ecr_images_public
  provider = aws.us_east_1

  repository_name = "${var.name}/${each.key}"

  catalog_data {
    description       = "CloudPipe pipeline image: ${each.key}"
    architectures     = ["x86-64"]
    operating_systems = ["Linux"]
  }

  tags = {
    Name = "${var.name}/${each.key}"
  }
}

# Extract the registry prefix (public.ecr.aws/<alias>) from any one repo URI.
# URI format: public.ecr.aws/<alias>/<repo-name>
locals {
  ecr_public_registry = regex("public\\.ecr\\.aws/[^/]+", values(aws_ecrpublic_repository.images)[0].repository_uri)
}

################################################################################
# ECR Private — Pipeline image repositories
################################################################################
# Migration target for the ECR Public repos above. Pods pull these over the S3
# gateway endpoint (layer blobs are served from S3), so pulls no longer traverse
# the NAT gateway — which is what was driving 300 GB-1.8 TB of NAT ingress on
# batch days. See handoffs/ecr-private-registry-migration.md.
#
# Node pull auth needs no change: the Karpenter/EKS node role already carries
# AmazonEC2ContainerRegistryReadOnly and the kubelet's ECR credential provider
# handles token exchange. No imagePullSecrets required.

resource "aws_ecr_repository" "images" {
  for_each = toset(local.ecr_images)

  name = "${var.name}/${each.key}"

  # Vulnerability scanning for these repos is configured at the REGISTRY level,
  # in aws_ecr_registry_scanning_configuration.this. There is deliberately no
  # `image_scanning_configuration` block here: `scan_on_push` is the BASIC-tier
  # knob and is inert while the registry runs ENHANCED, so setting it either way
  # would be a no-op that misrepresents the posture. It previously read
  # `scan_on_push = false` while every image was in fact being scanned
  # continuously by Inspector — see finding N1 in
  # tools/nist-800-171-assessment.md, where that line caused an assessment to
  # conclude scanning was disabled.

  # MUTABLE despite CI only ever pushing sha-<git-sha> tags. IMMUTABLE was
  # considered and rejected: Docker builds are not bit-reproducible, so
  # re-running a failed build at the same git SHA yields a different layer
  # digest, and the push would fail with ImageAlreadyExistsException. That
  # would break re-running a failed matrix leg, the workflow_dispatch
  # "build all" recovery path, and force_rebuild — all of which rebuild at an
  # unchanged commit by design.
  #
  # Integrity is provided by DIGEST PINNING instead, which is strictly stronger
  # than an immutable tag: workflow templates reference images as
  # repo@sha256:..., which is content-addressed and cannot be repointed by
  # anyone, whereas IMMUTABLE is a registry policy that an admin can lift.
  # build-images.yaml resolves the tag to its digest and writes that into the
  # templates. See the update-image-refs job there, and finding C10 in
  # tools/nist-800-171-assessment.md.
  #
  # This is why the sha- tag must KEEP being pushed even though nothing pins to
  # it: the lifecycle policy below expires UNTAGGED images after 7 days, so a
  # digest-pinned image that carried no tag would be garbage-collected out from
  # under the templates that reference it.
  image_tag_mutability = "MUTABLE"

  tags = {
    Name = "${var.name}/${each.key}"
  }
}

# Expire only UNTAGGED images. Tagged images are intentionally kept forever:
# workflow templates pin exact sha- tags, and an old pin may still be referenced
# by a template, a pre-baked AMI, or an in-flight workflow. Age is not a safe
# proxy for "unused" here, so garbage-collecting tagged images is a manual call.
resource "aws_ecr_lifecycle_policy" "images" {
  for_each = aws_ecr_repository.images

  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after 7 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 7
      }
      action = { type = "expire" }
    }]
  })
}

locals {
  ecr_private_registry = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}

################################################################################
# GitHub Actions OIDC — allows GHA to push images without long-lived credentials
################################################################################

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # Thumbprint list is managed by AWS and rotated automatically; the value below
  # is the current well-known thumbprint but does not need manual updates.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_actions_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      # This used to be `repo:${var.github_repo}:*`, with a comment claiming it
      # restricted access to main-branch pushes. It did not: `*` also matches
      # `:pull_request`, `:ref:refs/heads/<any-branch>`, `:ref:refs/tags/<tag>`,
      # and `:environment:<name>`. The repository was pinned; the ref was not.
      #
      # That was latent rather than exploitable — no workflow that assumes these
      # roles triggers on pull_request, and the two PR-triggered workflows
      # (ci.yaml, pr-image-build.yaml) request no OIDC token at all, the latter
      # documenting that as deliberate. But this document is the assume-role
      # policy for BOTH github_actions_ecr and github_actions_packer, and the
      # Packer role can RunInstances and manage packer-* IAM roles. Adding
      # `id-token: write` to one PR workflow would have handed that to
      # fork-authored code, with nothing in review to catch it.
      #
      # Now enumerated instead of wildcarded. See var.github_oidc_allowed_subs
      # for how to add a branch temporarily.
      values = var.github_oidc_allowed_subs
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "github_actions_ecr" {
  name               = "${var.name}-github-actions-ecr"
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume.json
  description        = "Assumed by GitHub Actions to push images to private ECR"
}

# No ecr-public PUSH grant. The ECRPublicAuth and ECRPublicPush statements were
# removed 2026-08-17 with the transitional dual-push (Step 6, push side). Dropping
# the workflow refs alone would not have stopped a public push -- any future edit
# could have re-added one and it would have succeeded silently. Revoking the grant
# is the enforcement point, and it is what makes the RA-5 claim ("every image we
# publish is inside the Inspector scanning configuration") true by construction
# rather than by convention. The 13 public repositories still exist as a frozen
# rollback target; nothing can write to them.
#
# ECRPublicPullAuth below is NOT that grant coming back. It lets build-images.yaml
# log Docker in to public.ecr.aws so the base-image pulls (`FROM public.ecr.aws/
# docker/library/...`) are authenticated: anonymous ones are metered per source
# IP, GitHub-hosted runners share their IPs, and builds were failing with
# `429 Too Many Requests ... toomanyrequests: Data limit exceeded`. The token
# only identifies the caller. Every push API -- InitiateLayerUpload,
# UploadLayerPart, CompleteLayerUpload, PutImage -- is still authorized per call
# against this role, and none is granted, so a push with this token is denied
# exactly as before.
data "aws_iam_policy_document" "github_actions_ecr" {
  statement {
    sid    = "ECRPrivateAuth"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
    ]
    resources = ["*"]
  }
  # Both are account-level calls that cannot be resource-scoped, and ECR Public
  # serves them only from us-east-1 (amazon-ecr-login handles that itself).
  # These two are the whole of what an authenticated PULL needs.
  statement {
    sid    = "ECRPublicPullAuth"
    effect = "Allow"
    actions = [
      "ecr-public:GetAuthorizationToken",
      "sts:GetServiceBearerToken",
    ]
    resources = ["*"]
  }
  # Covers both the buildx cache repo and the pipeline image repos. Read actions
  # (BatchGetImage/GetDownloadUrlForLayer) are required for cache-from, not just
  # pushes.
  statement {
    sid    = "ECRPrivateReadWrite"
    effect = "Allow"
    actions = [
      "ecr:BatchGetImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = concat(
      [aws_ecr_repository.build_cache.arn],
      [for repo in aws_ecr_repository.images : repo.arn],
    )
  }
}

resource "aws_iam_role_policy" "github_actions_ecr" {
  name   = "${var.name}-github-actions-ecr"
  role   = aws_iam_role.github_actions_ecr.name
  policy = data.aws_iam_policy_document.github_actions_ecr.json
}

output "github_actions_ecr_role_arn" {
  description = "IAM role ARN to set as GHA secret AWS_ROLE_ARN"
  value       = aws_iam_role.github_actions_ecr.arn
}

################################################################################
# GitHub Actions — Packer AMI builder role
# Used by build-gpu-nodeclass-ami.yaml to build pre-baked node AMIs.
# Set this ARN as GHA secret AWS_PACKER_ROLE_ARN.
################################################################################

resource "aws_iam_role" "github_actions_packer" {
  name               = "${var.name}-github-actions-packer"
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume.json
  description        = "Assumed by GitHub Actions to build pre-baked EKS node AMIs with Packer"
}

data "aws_iam_policy_document" "github_actions_packer" {
  # ── EC2: full Packer build lifecycle ───────────────────────────────────────
  statement {
    sid    = "PackerEC2"
    effect = "Allow"
    actions = [
      "ec2:RunInstances",
      "ec2:TerminateInstances",
      "ec2:StopInstances",
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus",
      "ec2:CreateImage",
      "ec2:RegisterImage",
      "ec2:DeregisterImage",
      "ec2:DescribeImages",
      "ec2:DescribeImageAttribute",
      "ec2:ModifyImageAttribute",
      "ec2:CopyImage",
      "ec2:CreateSnapshot",
      "ec2:DeleteSnapshot",
      "ec2:DescribeSnapshots",
      "ec2:ModifySnapshotAttribute",
      "ec2:CreateTags",
      "ec2:DescribeTags",
      "ec2:CreateKeyPair",
      "ec2:DeleteKeyPair",
      "ec2:DescribeKeyPairs",
      "ec2:DescribeSubnets",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeVpcs",
      "ec2:DescribeVolumes",
      "ec2:DescribeRegions",
      "ec2:GetPasswordData",
    ]
    resources = ["*"]
  }

  # ── IAM: hand the pre-created builder profile to the instance ─────────────
  #
  # This used to grant iam:CreateRole, iam:PutRolePolicy, iam:AttachRolePolicy
  # and iam:PassRole on role/packer-* so Packer could build its own temporary
  # instance profile. The packer-* prefix was not the control the old comment
  # claimed: a job holding this role could create packer-anything, give it an
  # inline admin policy with PutRolePolicy, pass it to an instance, and own the
  # account (#640).
  #
  # Narrowing those actions does not fix it. PutRolePolicy alone is sufficient
  # for the escalation, and Packer requires PutRolePolicy to apply a temporary
  # policy document — so the only real fix is for Packer not to write IAM. The
  # builder profiles are now pre-created in packer_builder_profiles.tf and both
  # templates reference them by name.
  #
  # What remains is PassRole on exactly those two roles, which RunInstances
  # needs to launch with a profile. It is not escalatable: both roles' policies
  # are Terraform-managed and hold only SSM messaging plus, for the GPU builder,
  # read-only pulls from this deployment's ECR repositories. A job with this
  # role can launch an instance as the builder, which it could already do, and
  # can no longer decide what the builder is allowed to do.
  statement {
    sid     = "PackerPassBuilderProfile"
    effect  = "Allow"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.packer_builder_gpu.arn,
      aws_iam_role.packer_builder_globus.arn,
    ]
  }

  # Packer VALIDATES a static `iam_instance_profile` before it launches, so the
  # read is mandatory, not a convenience. Removing the whole PackerIAM statement
  # took these two with the writes and broke the next GPU bake:
  #
  #   Couldn't find specified instance profile: AccessDenied: ... not authorized
  #   to perform: iam:GetInstanceProfile on resource: instance profile
  #   packer-builder-gpu
  #
  # Reads, not escalation: Get* cannot change what a role may do, which is the
  # whole reason the writes were removed. Scoped to the two builder identities
  # anyway so this does not become a general IAM-enumeration grant.
  statement {
    sid    = "PackerReadBuilderProfile"
    effect = "Allow"
    actions = [
      "iam:GetInstanceProfile",
      "iam:GetRole",
    ]
    resources = [
      aws_iam_role.packer_builder_gpu.arn,
      aws_iam_role.packer_builder_globus.arn,
      aws_iam_instance_profile.packer_builder_gpu.arn,
      aws_iam_instance_profile.packer_builder_globus.arn,
    ]
  }

  # ── SSM: session-manager communicator (SSH tunnel, no open ports) ───────────
  statement {
    sid    = "PackerSSM"
    effect = "Allow"
    actions = [
      "ssm:StartSession",
      "ssm:TerminateSession",
      "ssm:DescribeSessions",
      "ssm:GetConnectionStatus",
      "ssm:DescribeInstanceInformation",
      "ssm:DescribeInstanceProperties",
    ]
    resources = ["*"]
  }

  # ── SSM Parameter Store: Canonical's published "current stable" AMI id ──────
  # `packer/globus-gcs/` selects its Ubuntu 24.04 base from Canonical's public
  # parameter rather than an AMI-name filter, because the parameter is
  # Canonical's own statement of which image is current while a name filter can
  # match one they have since superseded. Without this the build fails before it
  # starts, on the data source rather than on anything it builds.
  #
  # The path is confined to Canonical's. These parameters are public and owned by
  # the `ssm` service account, which is why the ARN carries a region and no
  # account id — an account id here would match nothing.
  statement {
    sid    = "PackerReadCanonicalAMIParameter"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
    ]
    resources = [
      "arn:aws:ssm:${var.region}::parameter/aws/service/canonical/*",
    ]
  }

  # ── ECR Public: check that the fastsurfer image exists before building ──────
  # Retained for rollback; the existence check moves to private ECR below.
  statement {
    sid    = "ECRPublicRead"
    effect = "Allow"
    actions = [
      "ecr-public:GetAuthorizationToken",
      "ecr-public:DescribeImageTags",
      "ecr-public:DescribeImages",
      "sts:GetServiceBearerToken",
    ]
    resources = ["*"]
  }

  # ── ECR Private: existence check + the builder's authenticated `ctr` pull ───
  # GetAuthorizationToken cannot be resource-scoped. The token it returns is what
  # the Packer builder feeds to `ctr images pull --user AWS:<token>`; containerd
  # has no ECR credential helper of its own (that is a kubelet feature).
  statement {
    sid       = "ECRPrivateAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "ECRPrivateRead"
    effect = "Allow"
    actions = [
      "ecr:DescribeImages",
      "ecr:BatchGetImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [for repo in aws_ecr_repository.images : repo.arn]
  }

}

resource "aws_iam_role_policy" "github_actions_packer" {
  name   = "${var.name}-github-actions-packer"
  role   = aws_iam_role.github_actions_packer.name
  policy = data.aws_iam_policy_document.github_actions_packer.json
}

# The Packer role deliberately has NO EKS access. It used to hold
# eks:DescribeCluster plus a cluster-scoped AmazonEKSEditPolicy access entry, so
# that build-gpu-nodeclass-ami.yaml could kubectl-apply the gpu-nodeclass after
# baking an AMI. That step never worked -- the EKS endpoint is private-only and
# GitHub-hosted runners cannot route to it -- and was removed from the workflow.
# The grant it needed was broad (cluster-wide edit) for a step that only rolled
# one nodeclass, so it went with it. The roll now happens via terraform apply.
# Restoring this is only warranted alongside a runner that can actually reach the
# API endpoint; see docs/pre-baked-amis.md.

output "github_actions_packer_role_arn" {
  description = "IAM role ARN to set as GHA secret AWS_PACKER_ROLE_ARN"
  value       = aws_iam_role.github_actions_packer.arn
}

output "ecr_registry" {
  description = "ECR Public registry prefix (e.g. public.ecr.aws/<alias>) — legacy; being retired"
  value       = local.ecr_public_registry
}

output "ecr_private_registry" {
  description = "Private ECR registry host (<acct>.dkr.ecr.<region>.amazonaws.com) — set as GHA variable ECR_REGISTRY"
  value       = local.ecr_private_registry
}
