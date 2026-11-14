"""Decision logic for the two FreeSurfer input guards in the fastsurfer image.

Each guard is a bash wrapper installed over a FreeSurfer binary (like
topology_bound.sh, #958). The wrapper runs the real binary first; only if its
output shows the known failure does it call this helper and run the binary a
second time with corrected inputs. A healthy run is therefore the stock
command, unchanged, and the cohort's one-build guarantee holds for every tree
that never hit the failure.

aseg-clean  (wrapper: normalize_aseg_guard.sh over mri_normalize)
    FastSurfer 2.5.0-2.6.0 builds aseg.auto.mgz from the UNMASKED network
    prediction (Deep-MI/FastSurfer#956, fixed upstream in 2.6.1 by #957), so a
    region mislabelled outside the brain survives into aseg.presurf.mgz.
    `mri_normalize -aseg aseg.presurf.mgz -mask brainmask.mgz` then uses its
    white matter as control points where the masked intensity is 0. The bias
    field is 0 there and the bias correction makes NaN/Inf: this is the
    HISTOalloc crash (freesurfer/freesurfer#1452) that the sentinel patch in
    mri_normalize turns into a bounded run. Proved 2026-11-13 on 5 victims:
    clearing the labels outside the mask took the non-finite count to 0 on
    every one, and removing as many white-matter voxels INSIDE the mask did
    not. Trigger: the sentinel's `cloudpipe: bounded` line, or a NaN/Inf
    white-matter peak (upstream's silent-collapse signature).

autodet-check / autodet-verify  (wrapper: autodet_gm_guard.sh over mris_autodet_gwstats)
    mris_autodet_gwstats replaces the gray-matter mean with the gray MODE of
    samples 1 mm outside ?h.orig, after MRIclipBrightWM has clipped white
    matter to 110 (utils/mrisurf_mri.cpp, AutoDetectStats, v7.4.1). In some
    sessions that clip spike becomes the gray mode, so gray_mode = 110 and
    every gray-derived threshold (min_border_white = gray_mean, the CSF-border
    values, max_csf) is computed from 110. mris_place_surface then pulls the
    white surface inward and the pial onto it: cortex volume about halves and
    every step reports success. Census 2026-11-13: 80 of 33,466 sessions
    (132 hemispheres); gray_mode is 60-87 or exactly 110 across 90,558
    hemispheres, nothing in between. The class statistics FreeSurfer prints
    BEFORE the mode overwrite are intact on the failing hemispheres (gray mean
    68.5-79.3, same spread as healthy), and on 626 healthy hemispheres the mode
    sits a median 3.1 below that class mean. So the fallback is the class mean
    + GM_MODE_OFFSET, passed back through the binary's own override options,
    and then written into the .dat's gray_mean, which is what mris_place_surface
    places the white surface from (see autodet_finish).
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import tempfile
from pathlib import Path

# --- aseg-clean ------------------------------------------------------------

# A line printed only when mri_normalize produced non-finite voxels or a
# non-finite white-matter peak. The first comes from our sentinel patch
# (images/fastsurfer/patches/0001-*.patch); the second is the symptom
# Deep-MI/FastSurfer#956 names for the silent collapse.
NORMALIZE_TRIGGER = re.compile(
    r"cloudpipe: bounded \d+ bias-correction outlier|white matter peak found at -?(nan|inf)",
    re.IGNORECASE,
)

# The corpus callosum is painted after the mask is made and upstream never
# masks it (#957); keep that.
CC_LABELS = (251, 252, 253, 254, 255)
WHITE_MATTER_LABELS = (2, 41)

EXIT_NO_TRIGGER = 10
EXIT_NOTHING_TO_CLEAN = 11
EXIT_UNPARSEABLE = 12
EXIT_STILL_COLLAPSED = 13


def _subject_dir(path: str) -> Path:
    """`<sd>/<subject>/mri/x.mgz` or `<sd>/<subject>/surf/x.dat` -> `<sd>/<subject>`."""
    return Path(os.path.abspath(path)).parent.parent


def _write_marker(subject_dir: Path, name: str, text: str) -> None:
    scripts = subject_dir / "scripts"
    if scripts.is_dir():
        (scripts / name).write_text(text)
        print(f"cloudpipe: wrote {scripts / name}", file=sys.stderr)


def aseg_clean(log: str, aseg: str, mask: str) -> int:
    if not NORMALIZE_TRIGGER.search(Path(log).read_text(errors="replace")):
        return EXIT_NO_TRIGGER

    import nibabel as nib
    import numpy as np

    img = nib.load(aseg)
    seg = np.asarray(img.dataobj)
    brain = np.asarray(nib.load(mask).dataobj) > 0
    if seg.shape != brain.shape:
        print(
            f"cloudpipe: {aseg} {seg.shape} and {mask} {brain.shape} differ in shape",
            file=sys.stderr,
        )
        return EXIT_UNPARSEABLE
    outside = (seg > 0) & ~brain & ~np.isin(seg, CC_LABELS)
    n = int(outside.sum())
    if n == 0:
        print(
            f"cloudpipe: mri_normalize hit the #956 failure but {aseg} has no label outside {mask}; "
            "keeping the bounded result",
            file=sys.stderr,
        )
        return EXIT_NOTHING_TO_CLEAN
    n_wm = int((outside & np.isin(seg, WHITE_MATTER_LABELS)).sum())
    voxel_ml = float(np.prod(img.header.get_zooms()[:3])) / 1000.0
    cleaned = seg.copy()
    cleaned[outside] = 0

    # Atomic, and replaces a symlink rather than writing through it.
    out_dir = os.path.dirname(os.path.abspath(aseg))
    fd, tmp = tempfile.mkstemp(suffix=".mgz", dir=out_dir)
    os.close(fd)
    try:
        nib.save(nib.MGHImage(cleaned.astype(seg.dtype), img.affine, img.header), tmp)
        os.replace(tmp, aseg)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    msg = (
        f"cloudpipe: cleared {n} labelled voxel(s) ({n * voxel_ml:.2f} mL; {n_wm} white matter) "
        f"outside {os.path.basename(mask)} from {os.path.basename(aseg)} and re-running mri_normalize "
        "-- Deep-MI/FastSurfer#956"
    )
    print(msg)
    _write_marker(_subject_dir(aseg), "aseg-outside-mask-cleaned", msg + "\n")
    return 0


# --- autodet ---------------------------------------------------------------

# gray_mode at or above this is the clip spike, not gray matter. The cohort has
# nothing between 87 and 110, so anything in [88, 110) would do; 100 is the
# middle of the gap.
GM_MODE_MAX = float(os.environ.get("CLOUDPIPE_AUTODET_GM_MAX", "100"))
# Healthy hemispheres: gray_mode - class gray mean, median over 626 (IQR -4.2..-2.0).
GM_MODE_OFFSET = float(os.environ.get("CLOUDPIPE_AUTODET_GM_OFFSET", "-3.1"))
# AutoDetGWStats defaults in v7.4.1 (include/mrisurf.h): variablesigma 3, so
# max_csf = gray - MAX(.5, variablesigma-1) * std = gray - 2 * std.
VARIABLESIGMA = 3.0

CLASS_STATS = re.compile(
    r"white_mean = ([-\d.eE+]+) \+/- ([-\d.eE+]+), gray_mean = ([-\d.eE+]+) \+/- ([-\d.eE+]+)"
)


def read_dat(path: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return out


def gray_overrides(gray: float, gray_std: float) -> list[str]:
    """The five gray-derived thresholds, exactly as AutoDetectStats derives them
    from gray_mean, but from `gray`. Sanity floors still apply inside the binary."""
    vals = {
        "--min_border_white": gray,
        "--min_gray_at_white_border": gray - gray_std,
        "--max_csf": gray - max(0.5, VARIABLESIGMA - 1.0) * gray_std,
        "--max_gray_at_csf_border": gray - 1.0 * gray_std,
        "--min_gray_at_csf_border": gray - VARIABLESIGMA * gray_std,
    }
    args: list[str] = []
    for k, v in vals.items():
        args += [k, f"{v:.4f}"]
    return args


def autodet_check(dat: str, log: str, hemi: str) -> int:
    d = read_dat(dat)
    mode = d.get("gray_mode")
    if mode is None or not math.isfinite(mode):
        print(f"cloudpipe: {dat} has no gray_mode", file=sys.stderr)
        return EXIT_UNPARSEABLE
    if mode < GM_MODE_MAX:
        return EXIT_NO_TRIGGER

    # Pair the class-statistics line with this run by its gray std, which the
    # .dat records unchanged (std_scale 1).
    std = d.get("gray_std", float("nan"))
    hits = [
        m
        for m in CLASS_STATS.finditer(Path(log).read_text(errors="replace"))
        if math.isclose(float(m.group(4)), std, rel_tol=0, abs_tol=1e-3)
    ]
    if not hits:
        print(
            f"cloudpipe: gray_mode {mode:g} in {dat} but no class-statistics line with "
            f"gray std {std:g} in the log",
            file=sys.stderr,
        )
        return EXIT_UNPARSEABLE
    class_gray = float(hits[-1].group(3))
    gray = class_gray + GM_MODE_OFFSET
    if not (40.0 < gray < GM_MODE_MAX):
        print(
            f"cloudpipe: class gray mean {class_gray:g} gives fallback {gray:g}, outside (40, "
            f"{GM_MODE_MAX:g}); refusing",
            file=sys.stderr,
        )
        return EXIT_UNPARSEABLE

    msg = (
        f"cloudpipe: mris_autodet_gwstats {hemi} gray_mode {mode:g} is the white-matter clip spike "
        f"(white_mode {d.get('white_mode', float('nan')):g}); re-running with gray {gray:.2f} = class "
        f"mean {class_gray:.2f} {GM_MODE_OFFSET:+g}, gray std {std:.2f}"
    )
    print(msg, file=sys.stderr)
    _write_marker(
        _subject_dir(dat),
        f"autodet-gm-fallback.{hemi}",
        f"{msg}\nold_gray_mode={mode:g} class_gray_mean={class_gray:g} "
        f"fallback_gray={gray:.4f} gray_std={std:g}\n",
    )
    print("\n".join(gray_overrides(gray, std)))
    return 0


def autodet_finish(dat: str, gray: float) -> int:
    """After the re-run: set gray_mean in the .dat, then check both values
    mris_place_surface reads.

    The re-run's overrides fix every threshold the binary writes, but
    mris_place_surface does not read white_border_low for the WHITE surface. It
    recomputes border_low = f*gray_mean + (1-f)*white_mean with
    white_border_low_factor = 1, i.e. gray_mean itself
    (mris_place_surface.cpp:639-642, v7.4.1), and gray_mean in the .dat is still
    the 110 mode. Measured 2026-11-13: with the overrides alone the pial came out
    right and the white surface was byte-identical to the collapsed one. So
    gray_mean is rewritten here, which is what AutoDetectStats itself would have
    written (gray_mean = gray mode) had the mode been `gray`. AutoDetGWStats::Read
    is a positional fscanf, so only the value changes, never the line order.
    gray_mode is left at 110 as the record of what the binary found.
    """
    lines = Path(dat).read_text().splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.split()[:1] == ["gray_mean"]]
    if len(hits) != 1:
        print(f"cloudpipe: {dat} has {len(hits)} gray_mean lines, expected 1", file=sys.stderr)
        return EXIT_UNPARSEABLE
    lines[hits[0]] = f"gray_mean {gray:f}\n"
    tmp = f"{dat}.cloudpipe-tmp"
    Path(tmp).write_text("".join(lines))
    os.replace(tmp, dat)

    d = read_dat(dat)
    bad = {
        k: d.get(k, float("nan"))
        for k in ("gray_mean", "white_border_low")
        if not (math.isfinite(d.get(k, float("nan"))) and d[k] < GM_MODE_MAX)
    }
    if not bad:
        return 0
    print(f"cloudpipe: {dat} still has {bad} after the fallback", file=sys.stderr)
    return EXIT_STILL_COLLAPSED


# --- surface-check ---------------------------------------------------------

# Pial area / white area, healthy hemispheres (800, 2026-11-13): min 1.176, 0.5% 1.194,
# median 1.250, max 1.309. The band sits outside that on both sides.
PIAL_WHITE_RATIO_BAND = (1.15, 1.35)


def _surface_area(path: Path) -> float:
    import nibabel.freesurfer as fsio
    import numpy as np

    v, f = fsio.read_geometry(str(path))
    cross = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    return float(0.5 * np.linalg.norm(cross, axis=1).sum())


def surface_check(surf_dir: str, hemi: str) -> int:
    """For a hemisphere the autodet guard corrected: are the placed surfaces sane?

    The gray-mode fallback is only right when the surface sampled is on this
    session's anatomy. On a session misregistered to its template (one 2026-11-13
    case is rotated 93.5 deg against ~21 deg for its siblings) the class
    statistics look normal, the fallback runs, and the white surface ends up
    LARGER than the pial (pial/white area 0.97-0.98). That is no longer the 110
    signature, so without this check it would be published. A good fallback
    sits inside the healthy band (1.285 on the validated case).
    """
    d = Path(surf_dir)
    pial = d / f"{hemi}.pial.T1"
    if not pial.exists():
        pial = d / f"{hemi}.pial"
    try:
        ratio = _surface_area(pial) / _surface_area(d / f"{hemi}.white")
    except Exception as e:  # missing or unreadable surface: not sane
        print(f"cloudpipe: surface-check {hemi}: cannot read surfaces in {d}: {e}", file=sys.stderr)
        return EXIT_STILL_COLLAPSED
    lo, hi = PIAL_WHITE_RATIO_BAND
    if lo <= ratio <= hi:
        print(f"cloudpipe: surface-check {hemi}: pial/white area {ratio:.3f} within [{lo}, {hi}]")
        return 0
    print(
        f"cloudpipe: surface-check {hemi}: pial/white area {ratio:.3f} outside [{lo}, {hi}] after the "
        "gray-mode fallback; the session is likely misregistered to its template",
        file=sys.stderr,
    )
    return EXIT_STILL_COLLAPSED


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("aseg-clean")
    a.add_argument("--log", required=True)
    a.add_argument("--aseg", required=True)
    a.add_argument("--mask", required=True)
    c = sub.add_parser("autodet-check")
    c.add_argument("--dat", required=True)
    c.add_argument("--log", required=True)
    c.add_argument("--hemi", required=True)
    f = sub.add_parser("autodet-finish")
    f.add_argument("--dat", required=True)
    f.add_argument("--gray", required=True, type=float)
    s = sub.add_parser("surface-check")
    s.add_argument("--surf-dir", required=True)
    s.add_argument("--hemi", required=True)
    ns = p.parse_args(argv)
    if ns.cmd == "aseg-clean":
        return aseg_clean(ns.log, ns.aseg, ns.mask)
    if ns.cmd == "autodet-check":
        return autodet_check(ns.dat, ns.log, ns.hemi)
    if ns.cmd == "surface-check":
        return surface_check(ns.surf_dir, ns.hemi)
    return autodet_finish(ns.dat, ns.gray)


if __name__ == "__main__":
    sys.exit(main())
