# ---------------------------------------------------------------------------
# Stage 1: rebuild mri_normalize from pinned FreeSurfer source with the
# HISTOalloc bias-outlier sentinel applied (freesurfer/freesurfer#1452).
#
# WHY WE COMPILE AT ALL. The failure is a ~2e9 voxel manufactured in-memory by
# an unguarded division in MRIapplyBiasCorrectionSameGeometry, which then makes
# MRIhistogram request ~2e9 bins and HISTOalloc ask for 31.7 GiB. Every
# non-source mitigation is dead, and each was MEASURED dead, not assumed:
#
#   - An external clamp is impossible: norm.mgz on disk is uint8 with max 160,
#     so the value cannot be bounded before the process reads it (probe
#     histoalloc-confirm-rw94n, 2026-08-15).
#   - Raising the memory cap does not work: at 24 GiB it still aborts, in the
#     SECOND histogram (HISTOsmooth -> HISTOcopy, mrinorm.cpp:3379). The real
#     requirement is ~36 GiB per session, x k=4 ~= 140 GiB to rescue ~1%.
#   - A FreeSurfer version bump does not work: utils/mrihisto.cpp,
#     utils/histo.cpp and utils/mrinorm.cpp are byte-identical across v7.4.1,
#     v8.0.0, v8.1.0, v8.2.0 and dev.
#
# so the only remaining levers are rejecting the timepoint (the status quo,
# which is what strands these subjects) or fixing the code.
#
# THE BINARY IS FULLY STATIC, like every binary beside it. The FreeSurfer tree in
# deepmi/fastsurfer is the released freesurfer-linux-ubuntu22_x86_64-7.4.1 build,
# and its binaries are static executables (`ldd mri_segment`: "not a dynamic
# executable"). The runtime base is NOT the platform that tarball names:
# cu128-v2.5.4 is Ubuntu 24.04. A dynamically linked binary from this stage would
# depend on whatever libraries that base happens to ship. The first version of this
# stage did exactly that: it linked Ubuntu's shared ITK, which the runtime image
# does not carry, so the replaced mri_normalize could not even load
# ("libITKTransformFactory-4.13.so.1: cannot open shared object file"). Every
# surface run would have died at its first mri_normalize call. A static binary has
# no runtime dependencies at all, so the runtime base's distro cannot break it.
# The runtime stage below EXECUTES the binary to prove it.
#
# ubuntu:22.04 stays the builder because it is the platform FreeSurfer 7.4.1 was
# released for (GCC 11, cmake 3.22), and it ships static libz/libblas/liblapack/
# libgfortran/libquadmath/libgomp, which CMakeLists.txt:147-154 already prefers.
#
# THREE OF THESE PACKAGES CONTRIBUTE NOTHING TO THE BINARY and exist only to get
# the build through configure, generate and link. Each was found the hard way,
# and each fails in a different phase with an error that does not name it:
#   gfortran           - project() at CMakeLists.txt:31 declares Fortran as a
#                        project language unconditionally, BEFORE -DBUILD_FORTRAN
#                        is ever consulted. Fails CONFIGURE.
#   python3-dev        - packages/pybind11 is added unconditionally at
#                        CMakeLists.txt:380 and pybind11::module's
#                        INTERFACE_INCLUDE_DIRECTORIES names /usr/include/python3.10,
#                        which cmake validates. Fails GENERATE.
#   xxd                - add_help() (cmake/functions.cmake:135) embeds
#                        mri_normalize.help.xml into a generated header. Fails LINK.
# Do not prune them.
#
# ITK IS REQUIRED even though nothing under utils/ or mri_normalize/ includes an
# ITK header directly: include/mri.h:38 pulls in itkImage.h to declare
# ITKImageType and MRI::toITKImage / MRI::loadITKImage. The only callers of those
# two methods in the entire tree are AntsDenoiseImageFs and
# AntsN4BiasFieldCorrectionFs, neither of which mri_normalize reaches. ITK
# satisfies the linker, not the maths. Ubuntu's libinsighttoolkit4-dev ships
# shared libraries only, so a static link needs ITK built here. 4.13.2 is the
# version FreeSurfer 7.4.1 pins (CMakeLists.txt:172-183). Its own recipe,
# packages/source/build_itk.sh, cannot be used as-is: the itk-4.13.2.tar.gz beside
# it is a git-annex symlink with no content in the release tarball. ITKGroup_Core
# is all that utils/ links (`${ITK_LIBRARIES}`, utils/CMakeLists.txt:237).
#
# Digest-pinned like the runtime base below (#648). This stage's compiler and static
# libraries become part of a binary that runs on DUA data, so a floating tag would let
# an upstream push change that binary with no diff here. The digest is the multi-arch
# index, as resolved 2026-10-29.
# tag: 22.04
FROM ubuntu:22.04@sha256:076e7bf82319619f28dd0aa9c02ab34b055ad37100395b4f49a2ffba04fb5e11 AS mrinorm-builder

ARG DEBIAN_FRONTEND=noninteractive
ARG FS_GIT_TAG=v7.4.1
ARG ITK_VERSION=4.13.2
# sha256 of the upstream GitHub release asset InsightToolkit-4.13.2.tar.gz,
# measured 2026-10-29. A release asset is mutable, unlike a git tag, so it is pinned.
ARG ITK_SHA256=d8760b279de20497c432e7cdf97ed349277da1ae435be1f6f0f00fbe8d4938c1

# Without pipefail a failed compile inside a `make ... | tail` pipeline exits 0
# and produces an image with no binary in it. This bit twice during development.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git \
        build-essential cmake \
        zlib1g-dev libblas-dev liblapack-dev gfortran \
        python3 python3-dev xxd \
    && rm -rf /var/lib/apt/lists/*

# Static ITK, Core group only. BUILD_SHARED_LIBS=OFF is ITK's default; it is set
# explicitly because a shared ITK is the exact failure described above. C++11 and
# the SSE flags are build_itk.sh's own.
#
# ONE SOURCE EDIT, and it is inert. ITK 4.13.2's bundled vxl identifies the
# compiler in vcl/vcl_compiler.h by an __GNUC__ ladder that stops at GCC 8 and
# ends in `#error "Dunno about this gcc"`; jammy's GCC 11 hits it. 4.13.3 is no
# better, its ladder stops at GCC 10, which is why Ubuntu patches its own package.
# The ladder only defines per-version macros (VCL_GCC_8, VCL_GCC_83, ...) that
# nothing else in ITK reads. The rest of the tree tests only the generic VCL_GCC,
# which is set before the ladder. So the #error becomes a define, and the grep
# fails the build if a different ITK_VERSION no longer has that line to replace.
RUN mkdir -p /src/itk \
    && curl -fsSL -o /tmp/itk.tar.gz \
       "https://github.com/InsightSoftwareConsortium/ITK/releases/download/v${ITK_VERSION}/InsightToolkit-${ITK_VERSION}.tar.gz" \
    && echo "${ITK_SHA256}  /tmp/itk.tar.gz" | sha256sum -c - \
    && tar xzf /tmp/itk.tar.gz --strip-components=1 -C /src/itk \
    && rm /tmp/itk.tar.gz \
    && vcl=/src/itk/Modules/ThirdParty/VNL/src/vxl/vcl/vcl_compiler.h \
    && sed -i 's|^#  error "Dunno about this gcc"$|#  define VCL_GCC_UNLISTED|' "$vcl" \
    && grep -q '^#  define VCL_GCC_UNLISTED$' "$vcl" \
    && ! grep -q 'Dunno about this gcc' "$vcl" \
    && cmake -S /src/itk -B /src/itk/build \
         -DCMAKE_BUILD_TYPE=Release \
         -DCMAKE_INSTALL_PREFIX=/opt/itk \
         -DCMAKE_CXX_STANDARD=11 \
         -DCMAKE_C_STANDARD=11 \
         -DCMAKE_CXX_FLAGS="-msse2 -mfpmath=sse" \
         -DCMAKE_C_FLAGS="-msse2 -mfpmath=sse" \
         -DBUILD_SHARED_LIBS=OFF \
         -DITK_BUILD_DEFAULT_MODULES=OFF \
         -DITKGroup_Core=ON \
         -DBUILD_TESTING=OFF \
         -DBUILD_EXAMPLES=OFF \
         > /tmp/itk-cmake.log 2>&1 \
    || { tail -40 /tmp/itk-cmake.log; exit 1; }
RUN cmake --build /src/itk/build -j"$(nproc)" --target install > /tmp/itk-make.log 2>&1 \
    || { grep -E '\*\*\*|error:' /tmp/itk-make.log | head -40; exit 1; } \
    && rm -rf /src/itk

# Fetch the source FIRST, so touching the dependency list below does not
# invalidate this (slow) layer.
#
# The release TARBALL, not `git clone --depth 1`. Even shallow, the clone carries
# a .git we never read: 654 MB on disk for the clone against 480 MB extracted
# from the tarball, both measured. `--strip-components=1` saves having to name the
# extracted directory, which the tarball spells without the tag's leading "v"
# (v7.4.1 -> freesurfer-7.4.1).
#
# Trimming further is possible but NOT worth it. The build compiles ~36 MB
# (utils, packages, include, mri_normalize, cmake), but cmake CONFIGURES the whole
# project before `make` walks a single target's dependency graph, and
# add_subdirectories() (cmake/functions.cmake:46-50) is a bare loop with no
# existence check -- so distribution/, vtkutils/, lineprof/, fem_elastic/ and
# gems/ must all be PRESENT or configure hard-fails. gems alone is 59 MB and is
# never compiled here. A sparse checkout reaches ~96 MB and saves perhaps a
# minute, at the price of a directory list that a future FS_GIT_TAG bump breaks
# with a confusing error. The download is not the expensive part of this stage;
# the 194-file compile below is.
#
# No checksum pin, deliberately, unlike the AFNI tarball elsewhere in images/:
# this is an immutable git tag rather than a mutable release asset, and the
# `git apply` below fails hard if mri_normalize.cpp has drifted -- which is the
# file whose content the patch actually depends on.
WORKDIR /src
RUN mkdir -p /src/freesurfer \
    && curl -fsSL "https://github.com/freesurfer/freesurfer/archive/refs/tags/${FS_GIT_TAG}.tar.gz" \
       | tar xz --strip-components=1 -C /src/freesurfer

COPY images/fastsurfer/patches/0001-mri_normalize-bias-outlier-sentinel.patch \
     /tmp/sentinel.patch

# `git apply` (not `patch`) so a context drift after an FS_GIT_TAG bump is a
# hard build failure rather than a fuzzy-matched hunk landing somewhere else.
RUN cd /src/freesurfer && git apply --verbose /tmp/sentinel.patch

# FS_VERSION/BUILD_STAMP default to "$ENV{USER}-local" and
# "freesurfer-local-build-<date>" (CMakeLists.txt:84,88). Overriding them makes
# the binary SELF-IDENTIFYING: mri_normalize writes its version into the MGH tag
# block of every volume it produces, so any brain.mgz can be traced to this
# build rather than to stock 7.4.1. Keep the upstream version as the prefix --
# this IS 7.4.1, plus one bounded guard.
#
# `-static` plus static zlib and libgomp make the link fully static. FindOpenMP
# resolves OpenMP::OpenMP_CXX to libgomp.so, and ld then refuses: "attempted
# static link of dynamic object". BLAS, LAPACK and gfortran need no override:
# CMakeLists.txt:147-154 already asks for their .a. The link prints glibc's
# getpwuid/getpwnam/dlopen warnings for static binaries. They are expected and
# shared by every stock binary in the tree, which is static too.
RUN cmake -S /src/freesurfer -B /src/freesurfer/build \
      -DCMAKE_BUILD_TYPE=Release \
      -DITK_DIR=/opt/itk/lib/cmake/ITK-4.13 \
      -DCMAKE_EXE_LINKER_FLAGS="-static" \
      -DZLIB_LIBRARY=/usr/lib/x86_64-linux-gnu/libz.a \
      -DOpenMP_gomp_LIBRARY=/usr/lib/gcc/x86_64-linux-gnu/11/libgomp.a \
      -DFS_VERSION="7.4.1-cloudpipe-histoalloc1452" \
      -DBUILD_STAMP="freesurfer-7.4.1-cloudpipe-histoalloc1452" \
      -DBUILD_GUIS=OFF \
      -DMINIMAL=ON \
      -DINSTALL_PYTHON_DEPENDENCIES=OFF \
      -DDISTRIBUTE_FSPYTHON=OFF \
      -DBUILD_FORTRAN=OFF \
      -DQATOOLS_MODULE=OFF \
      -DWARNING_AS_ERROR=OFF \
      -DMARTINOS_BUILD=OFF \
      > /tmp/cmake.log 2>&1 \
    || { tail -40 /tmp/cmake.log; exit 1; }

# ITK 4.13's headers use `export` as a template keyword (vcl_compiler.h:114),
# which GCC 11 warns about in every translation unit that includes mri.h -- i.e.
# almost all of utils. A raw `| tail` of this build shows thousands of those and
# hides the actual error, so log to a file and print diagnostics on failure.
# Grep `***` (make's own marker) as well as `error:`: a missing link input fails
# with "No rule to make target" and NO compiler diagnostic attached.
RUN make -C /src/freesurfer/build -j"$(nproc)" mri_normalize \
      > /tmp/make.log 2>&1 \
    || { echo "=== make diagnostics ==="; \
         grep -E '\*\*\*|error:|undefined reference' /tmp/make.log | head -40; \
         exit 1; }

# Fail the build if the patch silently did not take effect. `git apply` above
# would error on a bad patch, but a patch that applies to the wrong place, or a
# stale object file, would not be caught -- and the resulting image would look
# fine while fixing nothing.
#
# `grep -a` on the binary directly, NOT `strings ... | grep -q`. Under the
# pipefail SHELL above, `grep -q` exits on its first match and closes the pipe,
# `strings` dies of SIGPIPE, and pipefail reports the pipeline as failed -- so
# that form fails exactly when the string IS present, with an error message
# claiming it is absent. Do not "simplify" this back into a pipeline.
RUN grep -aq 'cloudpipe: bounded' \
      /src/freesurfer/build/mri_normalize/mri_normalize \
    || { echo "ERROR: sentinel string absent from the built binary"; exit 1; } \
    && echo "sentinel present in mri_normalize"

# Fail the build if the link is not fully static. `file` would also say so, but
# it is not installed; the ELF program headers are authoritative. A PT_INTERP
# header means a dynamic loader, and so runtime library dependencies.
RUN if readelf -l /src/freesurfer/build/mri_normalize/mri_normalize | grep -q INTERP; then \
        echo "ERROR: mri_normalize is dynamically linked:"; \
        readelf -d /src/freesurfer/build/mri_normalize/mri_normalize | grep NEEDED; \
        exit 1; \
    fi \
    && echo "mri_normalize is a static executable"

# ---------------------------------------------------------------------------
# Stage 2: the runtime image.
#
# Digest-pinned, not `:latest` (#648). This base carries the segmentation code
# that runs on DUA data: the built image is baked into the GPU AMI and runs with
# the runner role, which can write and delete across the data bucket. On a
# floating tag an upstream push silently changes that code with no diff in this
# repo and no rebuild of our own — the pin is what makes a base change a
# reviewable commit.
#
# FastSurfer 2.5.4 (`2.5.4+cdfccea`), with FreeSurfer 7.4.1
# (`freesurfer-linux-ubuntu22_x86_64-7.4.1-20230614-7eb8460`) for its surface
# stage: the build that wrote the cohort's anatomy. A census of every tree in
# derivatives/fastsurfer/ on 2026-10-26 (each tree's scripts/build.log and
# scripts/build-stamp.txt) found 45,219 of 45,246 trees on this build, written
# August-September 2026. This image's /fastsurfer/BUILD.info was diffed against a
# tree's build.log (same version, model checkpoints and package list) and its
# FreeSurfer build stamp checked, before it was pinned.
#
# Held here for the cohort, so that every subject's anatomy comes from one
# build (operator, 2026-10-26). Dependabot moved this base to v2.6.0 on
# 2026-10-14 (#817), which carries FreeSurfer 8.2 and so changes segmentation
# and surface output; the 27 trees it wrote (every tree of 8 subjects) are
# re-run on this build. .github/dependabot.yml ignores deepmi/fastsurfer, so a
# release bump is a deliberate change, never a weekly PR. Moving to a new
# release means re-running the cohort's anatomy, or reporting two builds.
#
# The tag comment is on its own line, NOT trailing the FROM: a Dockerfile has
# no inline comments, so `FROM image@digest # tag` parses the comment as
# arguments and fails with "FROM requires either one or three arguments".
# tag: cu128-v2.5.4
FROM deepmi/fastsurfer:cu128-v2.5.4@sha256:11d594ae6fea7b1dde5b66ba5124b6295c4eedf1f401e21c7319c22eefd55c50

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

# Replace mri_normalize with the patched build from stage 1
# (freesurfer/freesurfer#1452 -- see the long note on that stage).
#
# THE VERSION ASSERTION HAS TEETH, and it guards a real hazard. The patch is
# generated against v7.4.1 source and its reasoning is tied to specific lines in
# that tree (mrinorm.cpp:3652, mri_normalize.cpp:805, :647, :971). If deepmi
# ever rebases onto a different FreeSurfer, dropping a 7.4.1-derived binary into
# that tree could silently pair our mri_normalize with a differently-versioned
# recon-surf. That would not fail loudly -- it would produce subtly wrong
# anatomy, thousands of subjects into a cohort leg. Fail the build instead.
#
# If this fires: check what FreeSurfer the new base ships, re-derive the patch
# against that tag (FS_GIT_TAG above), and re-verify the line references in the
# patch comment before bumping this assertion. Do NOT just widen the match.
RUN set -eu; \
    stamp=""; \
    for f in /opt/freesurfer/build-stamp.txt /opt/freesurfer/VERSION; do \
        if [ -s "$f" ]; then stamp="$(cat "$f")"; echo "read version from $f"; break; fi; \
    done; \
    if [ -z "$stamp" ]; then \
        echo "ERROR: could not read the base image's FreeSurfer version from"; \
        echo "       build-stamp.txt or VERSION under /opt/freesurfer. Find where"; \
        echo "       this base records its version and add it to the loop above --"; \
        echo "       do not delete the check. Replacing mri_normalize in a tree of"; \
        echo "       unknown version is the hazard this guards."; \
        exit 1; \
    fi; \
    case "$stamp" in \
      *7.4.1*) echo "base FreeSurfer build stamp: $stamp" ;; \
      *) echo "ERROR: base image FreeSurfer is '$stamp', expected 7.4.1."; \
         echo "       The mri_normalize patch is derived from v7.4.1 source."; \
         exit 1 ;; \
    esac

COPY --from=mrinorm-builder --chown=1000:1000 \
     /src/freesurfer/build/mri_normalize/mri_normalize \
     /opt/freesurfer/bin/mri_normalize

# Match the tree's permissions (set by the chown/chmod above) and prove the
# binary that actually landed is the patched one -- a COPY from the wrong stage
# path would otherwise ship stock silently.
#
# Then EXECUTE it, here in the runtime image. A string match proves the bytes are
# ours and nothing more: the first version of this stage passed that check with a
# binary that could not load its shared libraries, in an image where every surface
# run would have failed. `--version` exercises the loader and the program's own
# startup, and its output carries FS_VERSION, so it also proves which build ran.
RUN set -eu; \
    chmod 770 /opt/freesurfer/bin/mri_normalize; \
    if ! grep -aq 'cloudpipe: bounded' /opt/freesurfer/bin/mri_normalize; then \
        echo "ERROR: /opt/freesurfer/bin/mri_normalize is not the patched build."; \
        exit 1; \
    fi; \
    if ! out="$(/opt/freesurfer/bin/mri_normalize --version 2>&1)"; then \
        echo "ERROR: /opt/freesurfer/bin/mri_normalize does not run in this image:"; \
        echo "$out"; \
        exit 1; \
    fi; \
    case "$out" in \
      *cloudpipe-histoalloc1452*) ;; \
      *) echo "ERROR: mri_normalize --version did not report the patched build: $out"; exit 1 ;; \
    esac; \
    echo "mri_normalize: patched build in place and runs: $out"

# Time-bound FreeSurfer's two topology fixers (issue #958). recon-all's -fix
# stage runs mris_fix_topology and, when the result still has euler != 2, falls
# back to mris_topo_fixer; neither has a bound, and on a hemisphere whose
# surface is not a brain (a saturated T1w gives one defect spanning ~all of its
# vertices) both grind for hours. recon-surf.sh waits for both hemispheres, so
# the parcellation pod sat on the hung one until its 3 h activeDeadlineSeconds
# killed it, with no error anywhere — four subjects on 2026-10-30 alone.
#
# The real binaries move aside as `<name>.unbounded` and the wrapper takes each
# name, so recon-all's bare-name calls (scripts/recon-all:3486, :3563) resolve
# to it with no PATH games — FreeSurfer's own scripts prepend $FREESURFER_HOME/bin
# to PATH, so a shim directory ahead of it could not be relied on. The wrapper
# runs the real binary under coreutils `timeout` (30 min by default,
# CLOUDPIPE_TOPOLOGY_TIMEOUT_S from the template) and leaves a marker the
# WorkflowTemplate turns into exit 70. Sizing and the output contract are in the
# wrapper's header.
#
# This does NOT change the cohort build: a hemisphere that finishes inside the
# bound runs the identical binary with identical arguments. FastSurfer's
# build.log and the FreeSurfer build stamp are untouched, so the one-build census
# (the runtime-stage note above) still holds.
#
# The run below proves the wrapper is what the name resolves to AND that the
# real binary still runs through it: `--version` is FreeSurfer's standard
# handle_version_option, so its output carries the 7.4.1 stamp only if the
# binary behind the wrapper executed.
COPY --chown=1000:1000 images/fastsurfer/topology_bound.sh /app/topology_bound.sh
RUN set -eu; \
    chmod 770 /app/topology_bound.sh; \
    for prog in mris_fix_topology mris_topo_fixer; do \
        bin="/opt/freesurfer/bin/${prog}"; \
        if [ ! -x "$bin" ]; then \
            echo "ERROR: ${bin} is missing from the base image; the topology bound has nothing to wrap."; \
            exit 1; \
        fi; \
        if [ -e "${bin}.unbounded" ]; then \
            echo "ERROR: ${bin}.unbounded already exists; the base image changed shape (#958)."; \
            exit 1; \
        fi; \
        mv "$bin" "${bin}.unbounded"; \
        cp /app/topology_bound.sh "$bin"; \
        chown 1000:1000 "$bin" "${bin}.unbounded"; \
        chmod 770 "$bin" "${bin}.unbounded"; \
    done; \
    command -v timeout > /dev/null || { echo "ERROR: coreutils timeout is missing"; exit 1; }; \
    out="$(/opt/freesurfer/bin/mris_fix_topology --version 2>&1)" || true; \
    case "$out" in \
      *"cloudpipe: mris_fix_topology bounded"*) ;; \
      *) echo "ERROR: /opt/freesurfer/bin/mris_fix_topology is not the bounded wrapper: $out"; exit 1 ;; \
    esac; \
    case "$out" in \
      *7.4.1*) echo "topology bound in place; mris_fix_topology runs through it: $out" ;; \
      *) echo "ERROR: the real mris_fix_topology did not run behind the wrapper: $out"; exit 1 ;; \
    esac

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


