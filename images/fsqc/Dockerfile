# fsqc anatomical QC — Deep-MI/fsqc (MIT) run against FastSurfer + subregion output.
#
# Deliberately NOT built FROM deepmi/fastsurfer. fsqc reads FreeSurfer *output
# files* (through nibabel), not FreeSurfer *binaries*: the package contains no
# subprocess/shutil.which calls at all, its only FREESURFER_HOME reference is a
# docstring scoped to the `--shape` module (out of scope here), and even the
# color LUT is a hardcoded Python array rather than a read of
# $FREESURFER_HOME/FreeSurferColorLUT.txt. Basing this on the 16 GB fastsurfer
# image would carry a CUDA runtime and a full FreeSurfer tree that no enabled
# module touches. If the shape module is ever enabled, revisit — that one does
# need a sourced FreeSurfer.
FROM public.ecr.aws/docker/library/python:3.10-slim-bookworm

# 3.10 matches the runtime of the other neuroimaging images in this repo. fsqc
# requires >=3.10; the wheels below are all manylinux, so no compiler is needed.

ARG FSQC_VERSION=2.1.7

# libgomp1 is needed by scipy/scikit-image's OpenMP-threaded kernels.
#
# `apt-get upgrade` is the same-release security upgrade (NIST SP 800-171 SI-2,
# finding N1) — deliberately not `dist-upgrade`. `upgrade` only moves already-installed
# packages to a newer revision of the SAME Debian release (bookworm here) and never
# installs or removes one, so it collects backported CVE fixes with no version jump.
# The wheels installed below are manylinux and link against glibc's stable ABI, so a
# revision bump underneath them is not a rebuild trigger.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# fsqc is installed --no-deps with its runtime requirements listed explicitly, so
# that the OpenGL/Qt rendering stack can be left out.
#
# TRIMMED: whippersnappy, pyopengl, glfw, pyrr, PyQt6. fsqc declares these as
# hard install_requires, but they are reached only from createSurfacePlots.py
# (the --surfaces module), which is out of scope. They would add a system
# OpenGL/Qt dependency surface to an image that never renders a surface.
#
# Consequence to know about if --surfaces is ever enabled: it does NOT fail the
# run. Verified — fsqc catches the ImportError per-module, logs "surfaces module
# failed ... No module named 'whippersnappy'", writes `surfaces:1` to
# status.txt, and exits 0 with every other module's metrics intact. So enabling
# the flag without also adding these packages back yields a quietly incomplete
# record, not a crash. Add the deps in the same change as the flag.
#
# KEPT, despite each looking trimmable at first glance:
#   brainprint — fsqcMain._check_packages() raises ImportError on a missing
#                brainprint at STARTUP, unconditionally: the check is NOT gated
#                on --shape. So even a core-metrics-only run refuses to start
#                without it. Cheap to satisfy — brainprint's own deps are
#                numpy/scipy/pandas/lapy/psutil, no OpenGL. (A plain import
#                smoke-test does not catch this; only invoking run_fsqc does.)
#   lapy       — required two ways: _check_packages() hard-fails without it, and
#                checkTopology.py does `from lapy import TriaMesh`. The topology
#                module IS invoked under this change (its fields come back NaN
#                because FastSurfer doesn't retain surf/[lr]h.orig.nofix, but the
#                code path still runs).
#   psutil     — imported at fsqc's package __init__ (via fsqc.utils._config) but
#                NOT declared in fsqc's own install_requires; it normally arrives
#                transitively. Listed explicitly so the trim can't strand it.
#
# The enabled modules (core SNR/CC-size/contrast/rotation/topology, outlier
# detection, hippocampus, hypothalamus) import only this set;
# createScreenshots.py, which the hippocampus/hypothalamus overlays go through,
# is pure matplotlib. After any version bump, re-run the smoke test below AND a
# real run_fsqc invocation — the startup package gate is only reachable that way.
#
# The final pip call pins boto3/botocore/urllib3. This image stages exploded
# derivatives via fs_derivatives.py, the same publish path that segfaulted in
# #268, so it must pin the SAME versions as images/fastsurfer/Dockerfile and
# images/freesurfer/Dockerfile — see the note in the former for why.
RUN pip install --disable-pip-version-check --root-user-action=ignore --no-cache-dir \
        nibabel \
        numpy \
        pandas \
        scipy \
        scikit-image \
        matplotlib \
        transforms3d \
        pillow \
        psutil \
        packaging \
        'lapy>=1.1.1' \
        'brainprint==0.4.0' \
    && pip install --disable-pip-version-check --root-user-action=ignore --no-cache-dir \
        --no-deps "fsqc==${FSQC_VERSION}" \
    && pip install --disable-pip-version-check --root-user-action=ignore --no-cache-dir \
        boto3==1.43.72 \
        botocore==1.43.72 \
        urllib3==2.7.0

# Fail the build if the trimmed dependency set above is insufficient. Three
# distinct checks, because they catch different things:
#   1. Importing every enabled module — catches a missing module-level import.
#   2. run_fsqc --help — catches a broken console entry point.
#   3. fsqcMain._check_packages() — catches fsqc's UNCONDITIONAL startup package
#      gate, which is what the trim actually trips over (it demanded brainprint
#      even though --shape is never passed). Checks 1 and 2 both pass while this
#      one fails, so it has to be asserted separately or the image ships broken
#      and only fails once it is scheduled against real data.
RUN python -c "\
import fsqc, importlib; \
[importlib.import_module('fsqc.' + m) for m in ( \
    'checkSNR', 'checkCCSize', 'checkContrast', 'checkRotation', 'checkTopology', \
    'outlierDetection', 'evaluateHippocampalSegmentation', \
    'evaluateHypothalamicSegmentation', 'createScreenshots', 'fsqcMain')]; \
print('fsqc', fsqc.get_version(), 'imports OK')" \
    && run_fsqc --help > /dev/null \
    && echo "run_fsqc entry point OK" \
    && python -c "\
from fsqc.fsqcMain import _check_packages; \
_check_packages(); \
print('fsqc startup package gate OK')"

# Matplotlib and fontconfig both want a writable HOME; the pod runs as uid 1000
# with a read-only-ish filesystem, so point their caches at /tmp (mirrors the
# MPLCONFIGDIR/XDG_CACHE_HOME treatment in the fastsurfer base image).
ENV MPLCONFIGDIR=/tmp/matplotlib-config \
    XDG_CACHE_HOME=/tmp/xdgcache \
    MPLBACKEND=Agg \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 1000 nonroot && \
    useradd --uid 1000 --gid 1000 -m nonroot

# Staging/bridge/emit driver. Copied after the pip layers so editing it doesn't
# invalidate the (slow) dependency install.
COPY images/fsqc/stage_and_run.py /app/stage_and_run.py

# Exploded-derivative staging (ADR 017). stage_and_run.py imports fs_derivatives
# directly (it has boto3 already), and fs_derivatives imports restore_links, so
# both must be present and importable from /app.
COPY images/shared/fs_derivatives.py /app/fs_derivatives.py
COPY images/shared/restore_links.py /app/restore_links.py

ENV PYTHONPATH=/app

RUN python -c "import ast, pathlib; ast.parse(pathlib.Path('/app/stage_and_run.py').read_text())" \
    && echo "stage_and_run.py parses OK" \
    && python -c "import sys; sys.path.insert(0, '/app'); import stage_and_run; print('stage_and_run imports OK')"

USER nonroot
