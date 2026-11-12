# fMRIPrep, the reference arm of the cloudpipe / fMRIPrep equivalence benchmark
# (openspec/changes/add-fmriprep-equivalence-benchmark, task 4.1). Not a pipeline
# step: nothing in cloudpipe_minproc runs it.
#
# The version is the study's protocol, not a dependency to keep fresh (design
# D12). Every figure the benchmark publishes is reported against this release and
# this image's digest, so a bump mid-study would split the reference arm across
# two programs. That is why .github/dependabot.yml ignores nipreps/fmriprep: a
# bump is a protocol amendment, decided and recorded in the change, never a
# weekly maintenance PR.
#
# 25.2.5 (2026-03-10) is the newest tagged release and part of the 25.2 LTS
# series (supported to 2029-10). Tag AND digest: the digest is what is pulled,
# the tag keeps the line readable. Single-arch (amd64) upstream.
# tag: 25.2.5
FROM nipreps/fmriprep:25.2.5@sha256:15cbf8dcd17440d26ff5e80e9f7313f1cb3c54f13673f1ec4aed4465e8e12d77

# Carries FSL (upstream installs it from fslconda into this prefix). Restated
# here so publish_images.py's FSL rule sees it: FSL may not be redistributed
# publicly, so this image must never reach ECR Public (its public repository is
# retired in terraform/modules/stack/ecr.tf).
ENV FSLDIR=/app/.pixi/envs/fmriprep

# Upstream's own TemplateFlow home. Never DataLad: the cluster has no git-annex
# remote to reach, and every file must already be here (design D10).
ENV TEMPLATEFLOW_HOME=/home/fmriprep/.cache/templateflow \
    TEMPLATEFLOW_USE_DATALAD=false

COPY images/fmriprep/bake_templates.py /opt/cloudpipe/bake_templates.py

RUN python /opt/cloudpipe/bake_templates.py fetch \
    && chmod -R go=u "$TEMPLATEFLOW_HOME"

# The proof, with the network switched off: a file a run would have to download
# fails the BUILD here rather than a pilot session hours in.
RUN --network=none python /opt/cloudpipe/bake_templates.py verify

# No FreeSurfer license in the image (publish_images.py scans for one anyway): the
# workflow mounts config/fslicense and passes --fs-license-file.

# The benchmark's driver and the two standard-library modules it imports
# (argo/workflows/fmriprep_bench runs `python /opt/cloudpipe/run_bench.py`):
# provenance reads fMRIPrep's own report into the run record, and step_outcome
# writes the StepOutcome rows (task 4.8). Copied, not imported from the
# repository at run time, so the image digest recorded with every figure also
# fixes the code that produced it. restore_links left with the shared-anatomy
# mode (design D19): no FastSurfer tree is staged any more.
COPY images/fmriprep/run_bench.py /opt/cloudpipe/run_bench.py
COPY images/shared/step_outcome.py /opt/cloudpipe/step_outcome.py
COPY src/equivalence/provenance.py /opt/cloudpipe/provenance.py

# The registration endpoint's label warping (task 5.9), argo/workflows/fmriprep_bench's
# fmriprep-bench-labels: this image is the one that ships antsApplyTransforms.
COPY images/fmriprep/warp_labels.py /opt/cloudpipe/warp_labels.py
