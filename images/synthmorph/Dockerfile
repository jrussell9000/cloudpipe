FROM freesurfer/synthmorph:latest

USER root

RUN ln -sf python3 /usr/bin/python && \
    groupadd --gid 1000 nonroot && \
    useradd --uid 1000 --gid 1000 -m nonroot

COPY bold_to_t1w.py /app/bold_to_t1w.py

USER nonroot
