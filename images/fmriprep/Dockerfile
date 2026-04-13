FROM nipreps/fmriprep:latest

USER root

RUN mkdir -p /home/fmriprep/input && \
    mkdir -p /home/fmriprep/output && \
    chown -R 1000:1000 /home/fmriprep && \
    chmod -R 770 /home/fmriprep

RUN micromamba install -n fmriprep -c conda-forge PySocks

# fmriprep doesn't limit python warnings in the output....there's a lot
ENV PYTHONWARNINGS="ignore::UserWarning,ignore::DeprecationWarning,ignore::PendingDeprecationWarning,ignore::FutureWarning"

USER fmriprep
