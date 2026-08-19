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
#
# Was 12.1.1-cudnn8 + torch 2.5.1 (cu121) until #255. `torch==2.5.1` carries a hard
# `nvidia-cudnn-cu12==9.1.0.70` requirement and NVIDIA garbage-collected that patch
# release from both pypi.nvidia.com and download.pytorch.org, so every cold-cache
# build of this image failed. The dead pin spans torch 2.4.0–2.6.0 on BOTH the cu121
# and cu124 wheel lines, so there is no sideways escape; cu126 + torch 2.6.0 is the
# nearest live combination going forward, pinning cudnn 9.5.1.17 (present).
#
# Going BACKWARD also resolves and was rejected on merit, not availability: cu121
# torch 2.3.0/2.3.1 pin cudnn 8.9.2.26, which is still published, and would have kept
# this base tag byte-identical. It buys nothing durable — 2.3.x is a two-minor
# downgrade of the whole stack, is further from upstream FireANTs' own pin, and its
# CUDA-12.1 wheel line is closer to the next round of NVIDIA garbage collection, not
# further from it. `fireants==1.5.0` itself only asks for `torch>=2.3.0`.
#
# CUDA 12.6 base tags drop the cudnn major from the name ("-cudnn", not "-cudnn9");
# 12.6.3 is the last 12.6 patch (no 12.6.4 exists on Docker Hub).
ARG CUDA_TAG=12.6.3-cudnn
# Pinned together, deliberately. The extension compiled below links against this
# exact libtorch ABI, and it only builds from the matching `fireants` checkout, so
# these versions are one unit — bump them together or not at all. TORCH_INDEX must
# track CUDA_TAG's major.minor: a cu126 wheel on a 12.1 base loads the wrong
# libcudart at import time.
ARG TORCH_VERSION=2.6.0
ARG TORCHVISION_VERSION=0.21.0
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu126
ARG FIREANTS_VERSION=1.5.0

# ── Stage 1: compile the fused CUDA extension ───────────────────────────────────
FROM nvidia/cuda:${CUDA_TAG}-devel-ubuntu22.04 AS ffo-builder
ARG TORCH_VERSION
ARG TORCH_INDEX
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
        --index-url ${TORCH_INDEX}

RUN git clone --depth 1 --branch v${FIREANTS_VERSION} \
        https://github.com/rohitrango/FireANTs.git /src/fireants

# --no-build-isolation is load-bearing: fused_ops/pyproject.toml declares
# `requires = [..., "torch>=2.3.0"]`, and an isolated build env would fetch a fresh
# CPU-only torch from PyPI and compile against the wrong libtorch instead of using
# the CUDA wheel above.
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
ARG TORCH_INDEX
ARG FIREANTS_VERSION

# Same-release security upgrade (NIST SP 800-171 SI-2, finding N1). `upgrade`, not
# `dist-upgrade`: it only moves already-installed packages to a newer revision of the
# SAME Ubuntu release and never installs or removes one, so it picks up backported CVE
# fixes (libc6 2.35 → 2.35-0ubuntu3.14) without a version jump. This image carried the
# largest single share of the fleet's OS-class critical findings.
#
# NVIDIA's apt repo is moved aside for the upgrade, and only for it. That repo is the
# only source of the CUDA runtime libraries, so with its list file gone those packages
# have no upgrade candidate and cannot move — while everything from the Ubuntu archive
# still does. Left in place, an unrestricted `upgrade` would happily walk
# libcudart/libcudnn off the ${CUDA_TAG} that stage 1's fused-ops wheel was linked
# against, which is precisely the coupling the file header warns about. Restored
# afterwards so a later editor who adds an apt install here is not silently missing it.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends python3 python3-pip; \
    ln -sf python3 /usr/bin/python; \
    mkdir -p /tmp/nvrepo; \
    for f in /etc/apt/sources.list.d/cuda*.list /etc/apt/sources.list.d/nvidia*.list; do \
        { [ -e "$f" ] && mv "$f" /tmp/nvrepo/; } || true; \
    done; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y; \
    mv /tmp/nvrepo/* /etc/apt/sources.list.d/ 2>/dev/null || true; \
    rmdir /tmp/nvrepo; \
    rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
        torch==${TORCH_VERSION} torchvision==${TORCHVISION_VERSION} \
        --index-url ${TORCH_INDEX} \
    && pip install --no-cache-dir fireants==${FIREANTS_VERSION} nibabel numpy scipy

# --no-deps: the wheel declares `torch>=2.3.0`, which pip would happily satisfy by
# moving torch off the pin above and invalidating the ABI it was compiled against.
COPY --from=ffo-builder /wheels/*.whl /tmp/wheels/
RUN pip install --no-cache-dir --no-deps /tmp/wheels/*.whl && rm -rf /tmp/wheels

COPY images/fireANTs/scripts/fst1w_to_mni.py /app/fst1w_to_mni.py
COPY images/shared/registration_qc.py /app/registration_qc.py
