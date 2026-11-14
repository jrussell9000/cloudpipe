#!/bin/bash
# Wrapper around mris_autodet_gwstats for the gray-mode clip spike.
#
# Installed by images/fastsurfer/Dockerfile at $FREESURFER_HOME/bin/mris_autodet_gwstats,
# with the real binary beside it as `mris_autodet_gwstats.unguarded`. recon-all's
# -autodetgwstats calls it by bare name as
#     mris_autodet_gwstats --o ../surf/autodet.gw.stats.<hemi>.dat --i brain.finalsurfs.mgz
#                          --wm wm.mgz --surf ../surf/<hemi>.orig
#
# WHY. The binary replaces the gray-matter mean with the gray MODE of samples just
# outside ?h.orig, after clipping white matter to 110. In about 0.24% of sessions
# the clip spike wins, gray_mode is 110, and every gray-derived threshold is
# computed from 110. mris_place_surface then pulls the white surface inward and
# the pial onto it: cortex volume about halves, thickness drops ~1 mm, and every
# step reports success (80 sessions in the 2.5.4 cohort; see fs_guards.py).
#
# HOW. The real binary always runs first, with the caller's arguments, untouched.
# If the resulting gray_mode is a normal value this exits with its code, and the
# session is byte-identical to the cohort build. Only if gray_mode is the spike
# does it run the binary again with the five gray-derived thresholds overridden
# from FreeSurfer's own class mean (printed by the first run, before the mode
# overwrite) plus the healthy-cohort mode offset. The overrides are not enough on
# their own: mris_place_surface places the WHITE surface from the .dat's
# gray_mean, not its white_border_low, so gray_mean is then rewritten to the same
# value. Finally both are checked, and the hemisphere fails if either is still
# ~110. gray_mode keeps 110 as the record of what the binary found.
#
# A marker, scripts/autodet-gm-fallback.<hemi>, records the old and new values and
# is published with the tree. A refusal writes scripts/autodet-gm-collapsed.<hemi>
# and exits non-zero, so recon-all fails the hemisphere instead of placing a
# collapsed surface.
set -u
set -o pipefail

real="${FREESURFER_HOME:-/opt/freesurfer}/bin/mris_autodet_gwstats.unguarded"
guards="${CLOUDPIPE_FS_GUARDS:-/app/fs_guards.py}"
python="${CLOUDPIPE_GUARD_PYTHON:-/venv/bin/python3}"

if [[ ! -x "$real" ]]; then
  echo "cloudpipe: ${real} is missing; the image did not install the autodet guard correctly" >&2
  exit 127
fi

dat=""
surf=""
args=("$@")
for ((i = 0; i < ${#args[@]} - 1; i++)); do
  case "${args[i]}" in
    --o) dat="${args[i+1]}" ;;
    --surf) surf="${args[i+1]}" ;;
  esac
done

# --help, --version, or a shape we do not know: nothing to guard.
if [[ -z "$dat" ]]; then
  exec "$real" "$@"
fi

hemi="$(basename "${surf:-?.orig}")"
hemi="${hemi%%.*}"

log="$(mktemp)"
trap 'rm -f "$log"' EXIT

"$real" "$@" 2>&1 | tee "$log"
rc=${PIPESTATUS[0]}
if (( rc != 0 )); then
  exit "$rc"
fi

overrides="$("$python" -I "$guards" autodet-check --dat "$dat" --log "$log" --hemi "$hemi")"
decision=$?

collapsed() {
  local msg="cloudpipe: mris_autodet_gwstats ${hemi}: $1; refusing to place surfaces on gray-matter thresholds of ~110"
  echo "$msg" >&2
  local scripts
  scripts="$(dirname "$(dirname "$(realpath -m "$dat")")")/scripts"
  if [[ -d "$scripts" ]]; then
    echo "$msg" > "${scripts}/autodet-gm-collapsed.${hemi}"
    echo "cloudpipe: wrote ${scripts}/autodet-gm-collapsed.${hemi}" >&2
  fi
  exit 1
}

case "$decision" in
  10)
    exit 0
    ;;
  0)
    mapfile -t extra <<< "$overrides"
    gray=""
    for ((i = 0; i < ${#extra[@]} - 1; i++)); do
      [[ "${extra[i]}" == --min_border_white ]] && gray="${extra[i+1]}"
    done
    [[ -n "$gray" ]] || collapsed "fs_guards.py autodet-check returned no --min_border_white"
    "$real" "$@" "${extra[@]}" || collapsed "the re-run with fallback thresholds failed"
    "$python" -I "$guards" autodet-finish --dat "$dat" --gray "$gray" || collapsed "the fallback did not take"
    exit 0
    ;;
  *)
    collapsed "fs_guards.py autodet-check exited ${decision}"
    ;;
esac
