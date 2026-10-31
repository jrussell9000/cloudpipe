#!/bin/bash
# Time-bounded wrapper around FreeSurfer's two topology fixers (issue #958).
#
# Installed by images/fastsurfer/Dockerfile at BOTH
#     $FREESURFER_HOME/bin/mris_fix_topology
#     $FREESURFER_HOME/bin/mris_topo_fixer
# with the real binaries moved beside them as `<name>.unbounded`. recon-all calls
# each by bare name (scripts/recon-all:3486, :3563 in v7.4.1), so this is what it
# runs, and the arguments pass through untouched.
#
# WHY. `recon-all -fix` runs mris_fix_topology; if the result still has
# euler != 2 it reverts to orig.nofix and runs mris_topo_fixer. Neither has a
# bound, and on a hopeless surface both grind: a hemisphere whose white-matter
# surface is not a brain (a saturated T1w, one 2026-10-30 subject) comes out of
# marching cubes with eno=-1954 and ONE defect of 311,919 of its 314,570
# vertices, where 600 healthy hemispheres sampled from the cohort never exceed
# eno=-82 or a 902-vertex defect. The genetic search over that defect ran 3.0 h
# before failing on cloudpipe-qqtg7 (2026-08-18) and then handed off to
# mris_topo_fixer, which spun at 100% CPU with no output. recon-surf.sh runs the
# hemispheres as sibling processes and waits for both, so the pod sits on the
# bad one until the template's 3 h activeDeadlineSeconds kills it — with no
# error anywhere, because the hung hemisphere's log only reaches stdout when it
# finishes.
#
# The bound is sized from those 600 healthy hemispheres (300 long-template
# trees, 2026-10-30): the whole `recon-all -fix` stage, which includes this
# program plus the euler check and remesh, took median 1.3 min, p99 4.6 min,
# max 6.9 min. 30 min is >4x the observed maximum and 6x short of the 3 h
# deadline — a hemisphere that needs longer than that has defects far outside
# the cohort's band and has never gone on to succeed. CLOUDPIPE_TOPOLOGY_TIMEOUT_S
# overrides it from the WorkflowTemplate, so tuning needs no image rebuild.
#
# OUTPUT CONTRACT. On a timeout this writes a marker file
#     $SUBJECTS_DIR/<subject>/scripts/topology-unfixable.<hemi>
# and exits non-zero, which recon-all turns into `goto error_exit` (exit 1) and
# recon-surf.sh into a failed hemisphere. The exit code itself does not survive
# that path, so the WorkflowTemplate looks for the marker after run_fastsurfer.sh
# returns and exits TOPOLOGY_UNFIXABLE_EXIT_CODE (images/shared/step_outcome.py)
# — a code the retry expression does not match, because the input is identical
# on every attempt. The marker's text names the hemisphere, the program and the
# bound, and `recon-all -fix`'s own output above it in the appended hemisphere
# log carries the euler number and defect size.
#
# NOT A DETERMINISM CHANGE. A hemisphere that finishes inside the bound sees the
# identical binary with identical arguments and produces identical bytes; the
# wrapper only turns a 3 h silent deadline kill into a 30 min named failure.
set -u

name="$(basename "$0")"
real="${FREESURFER_HOME:-/opt/freesurfer}/bin/${name}.unbounded"
bound="${CLOUDPIPE_TOPOLOGY_TIMEOUT_S:-1800}"

if [[ ! -x "$real" ]]; then
  echo "cloudpipe: ${real} is missing; the image did not install the topology bound correctly" >&2
  exit 127
fi

# Both programs take `<subject> <hemi>` as their last two positional arguments.
# The marker is best-effort: if the arguments are not in that shape the bound
# still applies, only the marker's name degrades.
subject=""
hemi=""
if (( $# >= 2 )); then
  subject="${*: -2:1}"
  hemi="${*: -1}"
fi

echo "cloudpipe: ${name} bounded at ${bound}s (issue #958)"

# --kill-after: mris_fix_topology ignores nothing, but a process deep in a
# genetic-search generation can take a while to die on SIGTERM; SIGKILL follows.
timeout --kill-after=60 "$bound" "$real" "$@"
rc=$?

# 124: the bound fired and SIGTERM ended it. 137: it had to be SIGKILLed.
if (( rc == 124 || rc == 137 )); then
  msg="cloudpipe: ${name} ${hemi:-?} exceeded its ${bound}s bound and was killed; this hemisphere's surface has a topology no fixer closes (issue #958)"
  echo "$msg" >&2
  if [[ -n "${SUBJECTS_DIR:-}" && -n "$subject" && -d "${SUBJECTS_DIR}/${subject}/scripts" ]]; then
    marker="${SUBJECTS_DIR}/${subject}/scripts/topology-unfixable.${hemi}"
    { echo "$msg"; echo "program=${name} bound_s=${bound} killed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"; } > "$marker"
    echo "cloudpipe: wrote ${marker}" >&2
  fi
  exit $rc
fi

exit $rc
