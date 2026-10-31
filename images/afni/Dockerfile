# The func-preproc image. The name is historical: it no longer ships any AFNI.
#
# AFNI was here for ONE program, 3dcalc, Stage 5's MNI mask multiply. Since
# 2026-10-24 preproc.py does that multiply in numpy as it writes the MNI BOLD
# (write_mni_bold), so nothing calls AFNI and its install stage is gone. It was
# a recurring build failure: NIMH rolls the unversioned tarball in place, so
# every roll failed the pinned digest on a cold build and needed a re-pin that
# split the cohort's outputs by AFNI patch version (#338, #591).
# preproc.py's other names for AFNI (3dvolreg motion, AFNI-equivalent aor/aqi/
# gcor) describe upstream ABCD processing and numpy reimplementations, not binaries.
FROM ghcr.io/prefix-dev/pixi:0.83.0 AS build

# Same-release security upgrade (NIST SP 800-171 SI-2, finding N1). The pixi base is
# Ubuntu 24.04 and this is the stage that ships, so that base layer goes out as-is and
# its apt packages are what the scanner reports (glibc 2.39 → 2.39-0ubuntu8.8 and
# similar). `upgrade`, not `dist-upgrade`: it only moves already-installed packages to
# a newer revision of the SAME release and never installs or removes one.
#
# This reaches the apt-managed OS layer ONLY. The conda-forge packages that pixi
# installs below are invisible to apt and unaffected — their findings are a separate
# class needing a pixi.lock change, not an OS revision bump. Placed before the pixi
# layers so it does not invalidate the (slow) `pixi install`.
#
# (libexpat1 used to be installed here for AFNI's binaries, the only thing that
# needed it; it went with them.)
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy pixi manifest files
COPY images/afni/pixi.toml images/afni/pixi.lock ./

# Install the environment. No longer carries afni — see the note in pixi.toml — so
# this went from 118 locked packages to 76: dropping the conda AFNI package also
# dropped its X11/Motif tail (xorg-libxmu, libxp, libxpm, libxrender, libxt), none of
# which anything else here needs. ANTs, nibabel, numpy/scipy and pigz are unchanged.
RUN pixi install --locked

# Create a shell-hook script to actiave the environment
RUN pixi shell-hook > /shell-hook.sh
RUN echo "#!/bin/bash" > /entrypoint.sh && \
    echo "source /shell-hook.sh" >> /entrypoint.sh && \
    echo "exec \"\$@\"" >> /entrypoint.sh && \
    chmod +x /entrypoint.sh

COPY images/afni/preproc.py images/afni/surf_geometry.py images/afni/tissue_masks.py ./
# The one-off probseg backfill (probseg-backfill WorkflowTemplate); runs tissue_masks.py.
COPY images/afni/probseg_backfill.py ./
# The one-off confounds-only re-run (confounds-rerun WorkflowTemplate); runs preproc.py.
COPY images/afni/confounds_rerun.py ./

# The IQMs and FuncQC record preproc.py imports. Shared with src/equivalence, which
# recomputes the same record from another pipeline's outputs with this same code.
COPY images/shared/func_iqm.py /app/func_iqm.py

# Exploded-derivative link replay (ADR 017). ONLY restore_links.py — this image
# has no boto3 and no AWS CLI, and deliberately keeps it that way: Argo does the
# download (`archive: none` against the exploded prefix) and this replays the
# symlink manifest afterwards. Adding an S3 client here would mean a new
# pixi.toml dependency and a pixi.lock regeneration under the builder's pinned
# pixi 0.41.4, which is a lot of blast radius for a read path.
#
# restore_links.py is standard-library only, so it runs under the pixi env's
# python (3.14) with nothing installed. Its own test asserts that property.
COPY images/shared/restore_links.py /app/restore_links.py
# The exploded func publish: func_derivatives builds on fs_derivatives' transfer
# helpers and marker reader (ADR 017).
COPY images/shared/fs_derivatives.py /app/fs_derivatives.py
COPY images/shared/func_derivatives.py /app/func_derivatives.py

# The StepOutcome taxonomy and writer the func-preproc driver records with
# (#646). Standard-library only, same as restore_links.py.
COPY images/shared/step_outcome.py /app/step_outcome.py

RUN echo "python preproc.py" >> /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
