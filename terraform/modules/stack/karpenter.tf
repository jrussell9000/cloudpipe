module "karpenter" {
  source = "../karpenter"

  cluster_name      = module.eks.cluster_name
  cluster_endpoint  = module.eks.cluster_endpoint
  zones             = local.azs
  eks_version       = var.kubernetes_version
  karpenter_version = "1.13.0"

  # Digests of the images pre-baked into the GPU AMI. These select the AMI (via
  # amiSelectorTerms) AND are what packer pulls, so they are the load-bearing
  # values here — the _tag values below are for human traceability only.
  #
  # Each MUST equal the digest pinned in the corresponding workflow template.
  # kubelet matches cached images by full reference string, so if these drift the
  # pre-bake is dead weight and every GPU node re-pulls at pod start. The
  # fastsurfer bake had drifted for ~2 months before #123; build-gpu-nodeclass-ami.yaml
  # now resolves both from ECR and warns when they disagree.
  #
  # Updated automatically by build-gpu-nodeclass-ami.yaml after each AMI build;
  # a terraform apply is still required to roll the nodeclass to the new AMI.
  fastsurfer_ami_digest = "sha256:50317282787d76eabcb139c255341ea0317fb71a50cf3f959c8f80390c0a97a9"
  fireants_ami_digest   = "sha256:074d91905eb65016b51c0e41feb3a2304e77a0d214a3cf74af8422435b8bef86"

  # Git-sha tags corresponding to the digests above. Informational: nothing
  # selects or pulls on these, they exist so a digest can be traced to a commit.
  fastsurfer_ami_tag = "sha-8621ce52cbf8b8097f9681cb9440476efe672441"
  fireants_ami_tag   = "sha-6d954b039097afba85b45679d39fd51864a2837c"
}
