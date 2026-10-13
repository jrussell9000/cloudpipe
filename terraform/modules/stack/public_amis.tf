################################################################################
# AMI block public access, declared closed in each of public_ami_regions
################################################################################
# Nothing in this account shares an AMI publicly, and nothing lifts this block.
# A public GPU AMI was planned and dropped on 2026-10-12: EBS encryption by default
# is on here, so every AMI this account makes is encrypted, and AWS will not make
# an encrypted AMI public (openspec/changes/publish-images-for-deployers, design
# decisions 4 and 5).
#
# The declaration is kept as a guardrail. A block lifted by hand shows as drift on
# the next plan, and no CI role holds ec2:DisableImageBlockPublicAccess, which
# tests/test_terraform_publish_gate.py holds to.
#
# Publisher-only. Another deployment has no reason to manage its account's
# setting, and declaring one would override whatever that account chose.
#
# Removing this resource leaves the setting where it is (the provider only drops
# it from state), so turning publishing off does not lift any block.
resource "aws_ec2_image_block_public_access" "publish_regions" {
  for_each = var.publish_public_artifacts ? toset(var.public_ami_regions) : toset([])

  region = each.key
  state  = "block-new-sharing"
}
