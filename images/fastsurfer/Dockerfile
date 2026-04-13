FROM deepmi/fastsurfer:latest

USER root

RUN groupadd --gid 1000 nonroot && \
    useradd --uid 1000 --gid 1000 -m nonroot && \
    chown -R 1000:1000 /opt/freesurfer && \
    chmod -R 770 /opt/freesurfer

# Switch to the non-privileged user to run the application.
USER nonroot


