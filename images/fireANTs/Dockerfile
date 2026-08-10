# fireANTs image — T1w→MNI SyN registration on the GPU.
#
# Two stages exist only to get `fireants_fused_ops` (FireANTs' fused CUDA kernels)
# into a `-runtime` base. That package is NOT on PyPI — only the pure-Python
# `fireants` wheel is published — so it must be compiled from the git tag matching
# the installed wheel, which needs `nvcc` and therefore a `-devel` base. Building
# in-place would add ~2-3 GB to an image that is baked into the GPU node AMI
# (`fireants_ami_tag`, terraform/karpenter.tf), so the compiler stage is discarded
# and only the built wheel is carried forward.
#
# See handoffs/fireants-fused-ops.md for the measured baseline this must be
# validated against, and images/fireANTs/scripts/fst1w_to_mni.py for the USE_FFO
# runtime switch that selects the fused / non-fused arm.
ARG CUDA_TAG=12.1.1-cudnn8
# Pinned together, deliberately. The extension compiled below links against this
# exact libtorch ABI, and it only builds from the matching `fireants` checkout, so
# these versions are one unit — bump them together or not at all.
ARG TORCH_VERSION=2.5.1
ARG TORCHVISION_VERSION=0.20.1
ARG FIREANTS_VERSION=1.5.0

# ── Stage 1: compile the fused CUDA extension ───────────────────────────────────
FROM nvidia/cuda:${CUDA_TAG}-devel-ubuntu22.04 AS ffo-builder
ARG TORCH_VERSION
ARG FIREANTS_VERSION

# sm_75 = g4dn (T4), sm_86 = g5 (A10G), sm_89 = g6/g6e (L4/L40S) — every family
# gpu-nodepool allows (terraform/modules/karpenter/helm-values/gpu-nodepool.yaml).
# Karpenter picks by price and we do not control which we get, so all three must be
# in the fatbin; an arch that is missing fails at kernel launch ("no kernel image is
# available"), i.e. after the pod has already pulled and started.
ARG TORCH_CUDA_ARCH_LIST="7.5;8.6;8.9"

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-dev git ninja-build unzip \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir torch==${TORCH_VERSION} \
        --index-url https://download.pytorch.org/whl/cu121

RUN git clone --depth 1 --branch v${FIREANTS_VERSION} \
        https://github.com/rohitrango/FireANTs.git /src/fireants

# --no-build-isolation is load-bearing: fused_ops/pyproject.toml declares
# `requires = [..., "torch>=2.3.0"]`, and an isolated build env would fetch a fresh
# CPU-only torch from PyPI and compile against the wrong libtorch instead of using
# the cu121 wheel above.
#
# MAX_JOBS caps ninja, which otherwise fans out to every core: nine translation
# units, each compiled for three archs, and the largest (FusedGridSampler.cu, 93 KB
# of templates) peaks at several GB in nvcc. The GitHub runner has 4 cores / 16 GB;
# raise MAX_JOBS via --build-arg for a local build on a bigger machine.
ARG MAX_JOBS=2
RUN cd /src/fireants/fused_ops \
    && MAX_JOBS="${MAX_JOBS}" TORCH_CUDA_ARCH_LIST="${TORCH_CUDA_ARCH_LIST}" \
       pip wheel . --no-build-isolation --no-deps -w /wheels

# Assert the fatbin really covers all three archs before shipping. This is a
# build-time check on purpose — the runtime alternative is a pod that pulls, starts,
# and only then dies on whichever card Karpenter chose. Uses cuobjdump (devel-only,
# driver-free) rather than importing the module, which would need a GPU driver that
# a build runner does not have.
RUN set -eu; \
    unzip -o -q /wheels/fireants_fused_ops-*.whl -d /tmp/whlcheck; \
    so=$(find /tmp/whlcheck -name 'fireants_fused_ops*.so' | head -1); \
    [ -n "$so" ] || { echo 'no fireants_fused_ops .so in the built wheel'; exit 1; }; \
    elf=$(cuobjdump --list-elf "$so"); \
    echo "$elf"; \
    for arch in sm_75 sm_86 sm_89; do \
        echo "$elf" | grep -q "$arch" \
            || { echo "FATAL: $arch missing from fatbin — check TORCH_CUDA_ARCH_LIST"; exit 1; }; \
    done

# ── Stage 2: runtime ────────────────────────────────────────────────────────────
FROM nvidia/cuda:${CUDA_TAG}-runtime-ubuntu22.04
ARG TORCH_VERSION
ARG TORCHVISION_VERSION
ARG FIREANTS_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
    && ln -sf python3 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
        torch==${TORCH_VERSION} torchvision==${TORCHVISION_VERSION} \
        --index-url https://download.pytorch.org/whl/cu121 \
    && pip install --no-cache-dir fireants==${FIREANTS_VERSION} nibabel numpy scipy

# --no-deps: the wheel declares `torch>=2.3.0`, which pip would happily satisfy by
# moving torch off the pin above and invalidating the ABI it was compiled against.
COPY --from=ffo-builder /wheels/*.whl /tmp/wheels/
RUN pip install --no-cache-dir --no-deps /tmp/wheels/*.whl && rm -rf /tmp/wheels

COPY images/fireANTs/scripts/fst1w_to_mni.py /app/fst1w_to_mni.py
COPY images/shared/registration_qc.py /app/registration_qc.py
