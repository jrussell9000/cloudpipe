FROM deepmi/fastsurfer:latest

USER root

RUN groupadd --gid 1000 nonroot 2>/dev/null || true && \
    useradd --uid 1000 --gid 1000 -m nonroot 2>/dev/null || true && \
    chown -R 1000:1000 /opt/freesurfer && \
    chmod -R 770 /opt/freesurfer

COPY extract_qc.py /app/extract_qc.py

# Switch to the non-privileged user to run the application.
USER nonroot


