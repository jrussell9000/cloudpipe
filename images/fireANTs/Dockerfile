FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
    && ln -sf python3 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cu121 \
    && pip install --no-cache-dir fireants nibabel numpy
COPY ./scripts/fst1w_to_mni.py /app/fst1w_to_mni.py