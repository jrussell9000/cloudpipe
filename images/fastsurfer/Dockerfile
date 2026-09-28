FROM deepmi/fastsurfer:latest

USER root

# Same-release security upgrade (NIST SP 800-171 SI-2, finding N1). `upgrade`, not
# `dist-upgrade`: it only moves already-installed packages to a newer revision of the
# SAME distro release and never installs or removes one, so it collects backported CVE
# fixes without a version jump. This is the only lever available on an upstream base we
# do not build — the FreeSurfer tree and the /venv torch stack inside it are vendored
# and untouched by apt, so their findings are a separate class.
#
# The NVIDIA apt repo is moved aside for the upgrade in case this base carries one (it
# is a GPU image); with the list file gone the CUDA packages have no upgrade candidate
# and cannot move off the torch ABI. Same reasoning, and the same guard, as
# images/fireANTs/Dockerfile — a no-op if the repo is not present.
RUN set -eu; \
    mkdir -p /tmp/nvrepo; \
    for f in /etc/apt/sources.list.d/cuda*.list /etc/apt/sources.list.d/nvidia*.list; do \
        { [ -e "$f" ] && mv "$f" /tmp/nvrepo/; } || true; \
    done; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y; \
    mv /tmp/nvrepo/* /etc/apt/sources.list.d/ 2>/dev/null || true; \
    rmdir /tmp/nvrepo; \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 nonroot 2>/dev/null || true && \
    useradd --uid 1000 --gid 1000 -m nonroot 2>/dev/null || true && \
    chown -R 1000:1000 /opt/freesurfer && \
    chmod -R 770 /opt/freesurfer

# fsaverage comes from the BASE IMAGE — assert it, do not fetch it (#372).
#
# All four FastSurfer steps used to run a `download-fsaverage` init container
# that pulled config/fsaverage/ out of S3 into SUBJECTS_DIR: 312 objects,
# ~480 MiB, sequentially, per pod. On the two GPU steps that is pure waste of a
# time-slice, which is allocated at pod ADMISSION and so is held by every init
# container. Nothing ever read the result. Every fsaverage reference in
# FastSurfer resolves under $FREESURFER_HOME, never in SUBJECTS_DIR:
#
#   recon_surf/recon-surf.sh:944-945   rotate_sphere.py --trgsphere/--trgaparc
#   recon_surf/recon-surfreg.sh:343-344
#   recon_surf/fs_balabels.py:199      invoked at recon-surf.sh:1404 as
#                                      `--sd $SUBJECTS_DIR --sid $subject`, with
#                                      no --fsaverage, so it defaults to
#                                      $FREESURFER_HOME/subjects/fsaverage
#
# and `git log -S 'SUBJECTS_DIR/fsaverage' --all -- recon_surf/` upstream
# returns nothing — that path has never been read. The tree the base image
# ships is upstream's own declaration of sufficiency: tools/build/
# install_fs_pruned.sh:270-271,409-496 copies exactly the label/ and surf/
# files those three call sites open, pruned from FreeSurfer 7.4.1 (the same
# version and download URL images/freesurfer/Dockerfile pins). The released
# deepmi image runs our argstrings (--base, --surf_only --fsaparc, --long)
# against that pruned set and nothing else.
#
# That makes this an UPSTREAM dependency with no local copy to fall back on, so
# it gets teeth HERE. A base-image change that prunes harder, renames, or moves
# FREESURFER_HOME would otherwise surface as a failed surface registration
# thousands of subjects into a cohort leg, on steps whose failure costs the
# whole anatomical phase. Fail the build instead — a broken build is cheap.
#
# If this ever fires, find out what upstream changed. Do NOT restore the S3
# download: it wrote to a path FastSurfer does not read, so it would not have
# fixed a missing file here either.
RUN set -eu; \
    if [ "${FREESURFER_HOME:-}" != /opt/freesurfer ]; then \
        echo "ERROR: FREESURFER_HOME is '${FREESURFER_HOME:-unset}', expected /opt/freesurfer."; \
        echo "       The workflow templates link \$FREESURFER_HOME/subjects/fsaverage into"; \
        echo "       SUBJECTS_DIR; a moved FREESURFER_HOME needs that link updated too."; \
        exit 1; \
    fi; \
    for f in surf/lh.sphere surf/rh.sphere \
             surf/lh.sphere.reg surf/rh.sphere.reg \
             label/lh.aparc.annot label/rh.aparc.annot \
             label/lh.BA1_exvivo.label label/rh.BA1_exvivo.thresh.label \
             label/lh.FG1.mpm.vpnl.label; do \
        if [ ! -s "/opt/freesurfer/subjects/fsaverage/$f" ]; then \
            echo "ERROR: /opt/freesurfer/subjects/fsaverage/$f is missing or empty."; \
            echo "       The base image no longer ships the fsaverage files the FastSurfer"; \
            echo "       surface steps read (#372). See the note above this RUN."; \
            exit 1; \
        fi; \
    done; \
    echo "fsaverage present: $(find /opt/freesurfer/subjects/fsaverage -type f | wc -l) files"

COPY images/fastsurfer/extract_qc.py /app/extract_qc.py

# Exploded-derivative publish (ADR 017). This is the ONE dependency the whole
# change adds: the base image ships a /venv python 3.12 with no boto3 and no
# AWS CLI, and the long-phase pod must write derivatives/fastsurfer/ itself —
# publishing cannot be an Argo artifact, because Argo does not order artifact
# uploads and `_complete.json` must be written last. Mirrors the equivalent
# line in images/freesurfer/Dockerfile.
#
# PINNED, and urllib3 named explicitly rather than left to boto3's resolver.
# Unpinned, the exact botocore/urllib3/OpenSSL combination in this venv is not
# reproducible from the repo and drifts between builds — so when publish
# segfaulted in the 2026-08-14 batch (#268) there was no way to say WHICH
# combination did it, or to tell a fixed image from a lucky one.
#
# These versions are deliberately NOT lock-matched, and cannot be: pixi resolves
# boto3/botocore from conda-forge, which lags PyPI (newest conda-forge 1.43.70 vs
# PyPI 1.43.72 as of 2026-08-15), and pixi.lock holds two different resolutions
# anyway because the `default` environment is solved separately from the
# lint/imaging/docs solve group. No single version matches everything. What
# matters instead is that all three images running fs_derivatives.py's publish
# path — fastsurfer, freesurfer, fsqc — pin the SAME versions, so a crash
# reproduces identically in each. Keep them in step: if you bump one, bump all
# three. Both are inside pixi.toml's declared `>=1.43.49,<2` band.
RUN pip install --disable-pip-version-check --no-cache-dir \
        boto3==1.43.72 \
        botocore==1.43.72 \
        urllib3==2.7.0

COPY images/shared/fs_derivatives.py /app/fs_derivatives.py
COPY images/shared/restore_links.py /app/restore_links.py

# Switch to the non-privileged user to run the application.
USER nonroot


