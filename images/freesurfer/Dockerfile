FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG FS_VERSION=7.4.1
ARG FS_URL=https://surfer.nmr.mgh.harvard.edu/pub/dist/freesurfer/${FS_VERSION}/freesurfer-linux-ubuntu22_amd64-${FS_VERSION}.tar.gz

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        bc \
        aria2 \
        ca-certificates \
        tcsh \
        python3 \
        python3-numpy \
        python3-scipy \
        python3-nibabel \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

# python3-boto3 from apt (Ubuntu 22.04) ships a botocore old enough to predate
# EKS Pod Identity: it rejects the credentials endpoint host
# (169.254.170.23) with "Unsupported host", since its allow-list only knows
# the older ECS-style 169.254.170.2. pip installs a current botocore instead.
# No --root-user-action: apt's pip here is 22.0.2, older than the 22.1 release
# that flag requires.
RUN pip install --disable-pip-version-check --no-cache-dir boto3 botocore

RUN groupadd --gid 1000 nonroot && \
    useradd --uid 1000 --gid 1000 -m nonroot

# Download FreeSurfer 7.4.1 and extract only what subregion segmentation needs:
#
# Kept:
#   python/           — bundled Python interpreter + gems/surfa/samseg packages
#   average/HippoSF/atlas/         — probabilistic atlas for hippo-amygdala
#   average/ThalamicNuclei/atlas/  — probabilistic atlas for thalamus
#   average/BrainstemSS/atlas/     — probabilistic atlas for brainstem
#   bin/segment_subregions, bin/fspython, bin/mri_convert + supporting utils
#   setup scripts and color LUTs
#   models/           — included in full; DL tools look here at runtime
#
# Excluded:
#   average/*.gca     — large recon-all GCA atlases
#   bin/Segment*      — old MATLAB-compiled subregion binaries (replaced by segment_subregions)
#   lib/cuda/qt/tcl/vtk  — GUI and GPU libraries
#   matlab/, mni/, diffusion, fsfast, etc.
#   subjects/         — template subjects not read by segment_subregions
#   python/include/, python/share/  — build headers and docs
RUN aria2c -x 16 -s 16 --file-allocation=none -q -o /tmp/freesurfer.tar.gz "$FS_URL" && \
    tar zx --no-same-owner -C /opt \
      --exclude='freesurfer/average/*.gca' \
      --exclude='freesurfer/average/Buckner_JNeurophysiol11_MNI152' \
      --exclude='freesurfer/average/Choi_JNeurophysiol12_MNI152' \
      --exclude='freesurfer/average/mult-comp-cor' \
      --exclude='freesurfer/average/samseg' \
      --exclude='freesurfer/average/Yeo_Brainmap_MNI152' \
      --exclude='freesurfer/average/Yeo_JNeurophysiol11_MNI152' \
      --exclude='freesurfer/bin/freeview.bin' \
      --exclude='freesurfer/bin/freeview' \
      --exclude='freesurfer/bin/fs_spmreg.glnxa64' \
      --exclude='freesurfer/bin/mris_decimate_gui.bin' \
      --exclude='freesurfer/bin/mris_decimate_gui' \
      --exclude='freesurfer/bin/qdec_glmfit' \
      --exclude='freesurfer/bin/qdec.bin' \
      --exclude='freesurfer/bin/qdec' \
      --exclude='freesurfer/bin/SegmentSubfieldsT1Longitudinal' \
      --exclude='freesurfer/bin/SegmentSubjectT1_autoEstimateAlveusML' \
      --exclude='freesurfer/bin/SegmentSubjectT1T2_autoEstimateAlveusML' \
      --exclude='freesurfer/bin/SegmentSubjectT2_autoEstimateAlveusML' \
      --exclude='freesurfer/bin/run_SegmentSubfieldsT1Longitudinal.sh' \
      --exclude='freesurfer/bin/run_segmentSubjectT1_autoEstimateAlveusML.sh' \
      --exclude='freesurfer/bin/run_segmentSubjectT1T2_autoEstimateAlveusML.sh' \
      --exclude='freesurfer/bin/run_segmentSubjectT2_autoEstimateAlveusML.sh' \
      --exclude='freesurfer/bin/run_SegmentThalamicNuclei.sh' \
      --exclude='freesurfer/bin/SegmentThalamicNuclei' \
      --exclude='freesurfer/bin/run_SegmentSubject.sh' \
      --exclude='freesurfer/bin/segmentSubject' \
      --exclude='freesurfer/diffusion' \
      --exclude='freesurfer/fsafd' \
      --exclude='freesurfer/fsfast' \
      --exclude='freesurfer/lib/cuda' \
      --exclude='freesurfer/lib/images' \
      --exclude='freesurfer/lib/qt' \
      --exclude='freesurfer/lib/tcl' \
      --exclude='freesurfer/lib/tktools' \
      --exclude='freesurfer/lib/vtk' \
      --exclude='freesurfer/matlab' \
      --exclude='freesurfer/mni-1.4' \
      --exclude='freesurfer/mni' \
      --exclude='freesurfer/python/include' \
      --exclude='freesurfer/python/share' \
      --exclude='freesurfer/subjects' \
      --exclude='freesurfer/tktools' \
      --exclude='freesurfer/trctrain' \
      -f /tmp/freesurfer.tar.gz && \
    rm /tmp/freesurfer.tar.gz && \
    chown -R 1000:1000 /opt/freesurfer

ENV FREESURFER_HOME=/opt/freesurfer
ENV SUBJECTS_DIR=/opt/freesurfer/subjects
ENV PATH=$FREESURFER_HOME/bin:$FREESURFER_HOME/python/bin:$PATH
ENV PYTHONPATH=/app:$FREESURFER_HOME/python/packages

# bold_to_t1w wrapper and shared QC library
COPY --chown=1000:1000 images/freesurfer/bold_to_t1w.py /app/bold_to_t1w.py
COPY --chown=1000:1000 images/shared/registration_qc.py /app/registration_qc.py

USER nonroot
