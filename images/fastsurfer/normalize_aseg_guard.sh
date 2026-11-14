#!/bin/bash
# Wrapper around mri_normalize for Deep-MI/FastSurfer#956 (the cause of HISTOalloc).
#
# Installed by images/fastsurfer/Dockerfile at $FREESURFER_HOME/bin/mri_normalize,
# with the real (sentinel-patched, #1452) binary beside it as
# `mri_normalize.unguarded`. recon-all's -normalization2 calls it by bare name as
#     mri_normalize ... -aseg aseg.presurf.mgz -mask brainmask.mgz norm.mgz brain.mgz
#
# WHY. FastSurfer 2.5.4 can leave segmentation labels outside the brain mask
# (fixed upstream in 2.6.1, #957). On the -aseg path their white matter becomes
# control points at intensity 0, the bias field is 0 there, and the correction
# produces NaN/Inf: the HISTOalloc crash, which the sentinel patch turns into a
# bounded run that keeps going. Clearing those labels removes the non-finite
# voxels entirely (proved on 5 victims, 2026-11-13; see fs_guards.py).
#
# HOW. The real binary always runs first, with the caller's arguments, untouched.
# Only when its output shows the failure (the sentinel's `cloudpipe: bounded` line,
# or a NaN/Inf white-matter peak) does this clear the labels outside the -mask
# volume from the -aseg volume, in place, and run it a second time. So a healthy
# session runs the stock command once and produces identical bytes, which keeps
# the cohort's one-build guarantee; only an affected session changes, and it
# changes from a bounded result to a correct one. The cleared aseg.presurf.mgz is
# what every later stage reads (mri_edit_wm_with_aseg, mris_place_surface --seg,
# the hypointensity relabel that makes aseg.mgz), so they agree with each other.
# aseg.auto.mgz and the deep-segmentation stats are not touched.
#
# A marker, scripts/aseg-outside-mask-cleaned, records that the second run
# happened, and is published with the tree so a census can count it.
set -u
set -o pipefail

real="${FREESURFER_HOME:-/opt/freesurfer}/bin/mri_normalize.unguarded"
guards="${CLOUDPIPE_FS_GUARDS:-/app/fs_guards.py}"
python="${CLOUDPIPE_GUARD_PYTHON:-/venv/bin/python3}"

if [[ ! -x "$real" ]]; then
  echo "cloudpipe: ${real} is missing; the image did not install the mri_normalize guard correctly" >&2
  exit 127
fi

aseg=""
mask=""
args=("$@")
for ((i = 0; i < ${#args[@]} - 1; i++)); do
  case "${args[i]}" in
    -aseg) aseg="${args[i+1]}" ;;
    -mask) mask="${args[i+1]}" ;;
  esac
done

# Not the -aseg path (e.g. recon-surf's `-g 1 nu.mgz T1.mgz`): nothing to guard.
if [[ -z "$aseg" || -z "$mask" ]]; then
  exec "$real" "$@"
fi

log="$(mktemp)"
trap 'rm -f "$log"' EXIT

"$real" "$@" 2>&1 | tee "$log"
rc=${PIPESTATUS[0]}

"$python" -I "$guards" aseg-clean --log "$log" --aseg "$aseg" --mask "$mask"
decision=$?
case "$decision" in
  0)
    "$real" "$@"
    exit $?
    ;;
  10|11)
    # 10: no failure in the output. 11: the failure without labels outside the
    # mask, so there is nothing to clear and the bounded result stands.
    exit "$rc"
    ;;
  *)
    echo "cloudpipe: fs_guards.py aseg-clean failed (exit ${decision}); keeping mri_normalize's own result" >&2
    exit "$rc"
    ;;
esac
