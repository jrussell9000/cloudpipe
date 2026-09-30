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
  fastsurfer_ami_digest = "sha256:db570b9aa47328cd284c7e970517d2609c1604a929e434abe1da727c33d04ba9"
  fireants_ami_digest   = "sha256:78e95ce1263d98485c55eab70c8f36bf91f734cc83f210eda09abbd860a51e38"

  # Git-sha tags corresponding to the digests above. Informational: nothing
  # selects or pulls on these, they exist so a digest can be traced to a commit.
  fastsurfer_ami_tag = "sha-0b0aaaaa9c27915b8e742144b2b8c3b71d2a23d4"
  fireants_ami_tag   = "sha-a6c5d8f8ac47dd1e9cffcf54ffda0d0b6d34fc1a"
}
