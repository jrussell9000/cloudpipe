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
  fastsurfer_ami_digest = "sha256:ad0dbabe3f99d8532851119338d0a87d338f97625e95cf18a62bf99ad8ef87ff"
  fireants_ami_digest   = "sha256:a6e850ab91c3e9b9cb7ca15f7b06df35bd9a0d5ed4129414ca2de9fa5a204003"

  # Git-sha tags corresponding to the digests above. Informational: nothing
  # selects or pulls on these, they exist so a digest can be traced to a commit.
  fastsurfer_ami_tag = "sha-970a3b546cbad72fae9e66225014a266d797def7"
  fireants_ami_tag   = "sha-ac9a6eb62384b22ffc315b229399bf29a43af466"

  # Which AMI the GPU node class launches when this account has no bake of its
  # own; see ../gpu-ami-choice.
  gpu_ami_selection = var.gpu_ami_selection
  gpu_ami_alias     = var.gpu_ami_alias

  # The ingress node pool's network (ingress_network.tf), by ID.
  ingress_subnet_ids        = [for s in aws_subnet.ingress : s.id]
  ingress_security_group_id = aws_security_group.ingress_node.id
}
