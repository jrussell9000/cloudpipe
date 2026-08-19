# ── Stage 1: AFNI from NIMH's official precompiled binaries ─────────────────────
#
# NOT from conda-forge, which is where this image got AFNI until now. That
# feedstock has stalled at 25.0.00 (build _3, uploaded 2025-09-07) while upstream
# ships 26.2.x, so no version spec on that channel can produce a current AFNI —
# staying there means shipping a year-old build indefinitely, which is exactly the
# unsupported-version shape of NIST 800-171 finding C6. conda-forge also packages
# only ~71 of AFNI's ~600 programs, and a missing one surfaces as a PermissionError
# rather than a clean "not found" — which is how preproc.py acquired a no-op guard
# around 3dTshift, a binary that was simply never present.
#
# A separate stage because the pixi base has no curl and this is the only thing
# that needs one. The 161 MB download and the 270 MB extracted tree stay here; only
# the allow-listed programs and AFNI's five bundled libraries are carried forward.
FROM public.ecr.aws/docker/library/debian:bookworm-slim AS afni-dist

# The upstream URL is UNVERSIONED and rolls in place — there is no versioned
# filename, no GitHub release asset (the repo publishes tags only), and no archive
# of previous builds. So it is pinned two ways instead:
#
#   AFNI_SHA256   byte-pins this exact tarball. When NIMH rolls it (roughly
#                 monthly) the build fails loudly with the observed digest
#                 printed — a deliberate two-minute chore: verify, bump both ARGs.
#                 An unpinned fetch would instead change AFNI version silently, on
#                 whatever unrelated PR happened to trigger a rebuild.
#   AFNI_VERSION  asserted against the tarball's own AFNI_version.txt, so a digest
#                 bump cannot quietly land a different release than the one
#                 recorded here.
#
# Trade-off to know about: unlike a conda package, the previous tarball is GONE
# once upstream rolls, so a cold build of an old commit stops being reproducible
# at that point. That is the price of currency here and there is no third option —
# NIMH publishes no versioned artifact. `--build-arg AFNI_SHA256=` (empty) skips
# the digest check for an emergency build; the version assertion still applies.
ARG AFNI_URL=https://afni.nimh.nih.gov/pub/dist/tgz/linux_ubuntu_24_64.tgz
ARG AFNI_SHA256=5471e0271c788a79a4e8919de447e2ef05612430b09490174c54ec5d3801a0c2
ARG AFNI_VERSION=AFNI_26.2.03

# Programs lifted out of the tarball. preproc.py shells out to exactly ONE AFNI
# binary — 3dcalc, for Stage 5's MNI masking — so that is all that ships. Adding
# another is a one-word edit here and nothing else: the five bundled libraries
# copied below cover every program in the distribution, and the link check at the
# end of this stage fails the build if a new binary needs something they lack.
#
# Shipping the whole tree instead was considered and rejected. 270 MB of unused
# binaries is scanner surface with no consumer, and unused AFNI programs are a
# large part of what the current image is being flagged for.
ARG AFNI_PROGRAMS="3dcalc"

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    curl -fsSL -o /tmp/afni.tgz "$AFNI_URL"; \
    if [ -n "$AFNI_SHA256" ]; then \
        got=$(sha256sum /tmp/afni.tgz | cut -d' ' -f1); \
        if [ "$got" != "$AFNI_SHA256" ]; then \
            echo "FATAL: AFNI tarball digest mismatch."; \
            echo "  expected $AFNI_SHA256"; \
            echo "  observed $got"; \
            echo "Upstream rolled the unversioned tarball. Verify the new one, then"; \
            echo "set AFNI_SHA256 to the observed digest and AFNI_VERSION to match."; \
            exit 1; \
        fi; \
    fi; \
    mkdir -p /tmp/tree /afni; \
    tar xzf /tmp/afni.tgz -C /tmp/tree --strip-components=1; \
    ver=$(sed -n 1p /tmp/tree/AFNI_version.txt); \
    if [ "$ver" != "$AFNI_VERSION" ]; then \
        echo "FATAL: tarball reports $ver but AFNI_VERSION says $AFNI_VERSION"; \
        exit 1; \
    fi; \
    cp /tmp/tree/AFNI_version.txt /afni/; \
    cp /tmp/tree/lib*.so /tmp/tree/lib*.so.* /afni/; \
    for p in $AFNI_PROGRAMS; do \
        if [ ! -x "/tmp/tree/$p" ]; then \
            echo "FATAL: $p is not in the AFNI distribution"; exit 1; \
        fi; \
        cp "/tmp/tree/$p" /afni/; \
    done; \
    rm -rf /tmp/afni.tgz /tmp/tree; \
    echo "AFNI $ver: extracted $AFNI_PROGRAMS + $(ls /afni/lib* | wc -l) libraries"

# ── Stage 2: runtime ────────────────────────────────────────────────────────────
FROM ghcr.io/prefix-dev/pixi:0.41.4 AS build

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
# libexpat1 is the one runtime dependency AFNI's binaries need that this base lacks.
# Measured with ldd against 3dcalc's full transitive closure on this exact base:
# libmri.so and libf2c.so resolve out of /opt/afni via $ORIGIN, libz/libm/libc are
# already present, and libexpat.so.1 was the only "not found".
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# AFNI's binaries and its bundled libraries, from stage 1. Kept out of the pixi
# environment on purpose: pixi owns /app/.pixi and rewrites it on any lock change,
# and AFNI is not a conda package any more.
COPY --from=afni-dist /afni/ /opt/afni/
ENV PATH="/opt/afni:$PATH"

# Fail the build if AFNI does not actually run here. A link check first: these are
# Debian-built binaries landing in an Ubuntu base with only libexpat1 added, so a
# missing soname is the realistic failure mode, and left to runtime it would surface
# as a PermissionError inside an already-scheduled pod rather than as a build error.
#
# It also asserts that the version 3dcalc reports matches AFNI_version.txt, which
# stage 1 carried across as the single source of truth — so a half-applied ARG bump
# cannot ship. Deliberately compared against the file, not against a second copy of
# the ARG default, which would just be one more thing to keep in sync.
RUN set -eu; \
    want=$(sed -n 1p /opt/afni/AFNI_version.txt); \
    3dcalc -help > /dev/null; \
    got=$(3dcalc 2>&1 | sed -n 's/.*AFNI version=\([A-Za-z0-9_.]*\).*/\1/p' | sed -n 1p); \
    if [ "$got" != "$want" ]; then \
        echo "FATAL: 3dcalc reports '$got' but AFNI_version.txt says '$want'"; exit 1; \
    fi; \
    echo "AFNI $got: 3dcalc links and runs"

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

COPY images/afni/preproc.py images/afni/surf_geometry.py ./

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

RUN echo "python preproc.py" >> /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
