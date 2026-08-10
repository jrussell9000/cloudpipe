"""Registration QC metric computation.

Shared by images/fireANTs (fst1w_to_mni.py) and images/synthmorph (bold_to_t1w.py).
Each Dockerfile COPYs this file to /app/registration_qc.py.
"""

from __future__ import annotations

import nibabel as nib
import numpy as np


def dice(a: np.ndarray, b: np.ndarray) -> float:
    """Dice coefficient between two arrays binarized at > 0."""
    ma = (a > 0).ravel()
    mb = (b > 0).ravel()
    denom = float(ma.sum() + mb.sum())
    return float(2 * (ma & mb).sum() / denom) if denom > 0 else 0.0


def modified_hausdorff_distance(
    a: np.ndarray, b: np.ndarray, voxel_spacing: tuple[float, float, float] = (1.0, 1.0, 1.0)
) -> float:
    """Modified Hausdorff distance (mm) between the surfaces of two binary masks.

    Dubuisson & Jain (1994): MHD(A, B) = max(d(A, B), d(B, A)), where
    d(A, B) = mean over surface voxels a in A of the distance to the nearest
    surface voxel in B. The mean (not the max, as in the classic Hausdorff
    distance) makes it robust to a single stray voxel — one bad point can shift
    max-Hausdorff by centimetres while MHD barely moves. Both masks must share a
    grid; `voxel_spacing` scales voxel distances to mm (pass the header zooms).

    This is a geometric BOUNDARY-agreement metric, orthogonal to the
    intensity-agreement of (n)mutual_information: NMI can be high while a boundary
    is locally displaced, and vice versa. For bold_to_t1w it compares the T1w
    brain mask against a brain mask derived from the warped BOLD reference — see
    the caveats at that call site, since the BOLD mask is a soft-edged EPI
    skull-strip and sets the achievable floor.

    Surface voxels are the mask minus its binary erosion (the one-voxel shell).
    Distances use the exact Euclidean transform of the complementary surface, so
    every voxel's distance to the nearest opposite-surface voxel is available
    without an O(N²) point-to-point scan.

    Returns -1.0 (an impossible MHD, which is always >= 0) as the "could not
    compute" sentinel when either mask has no surface — e.g. the EPI skull-strip
    collapsed to empty. Unlike 0.0, that sentinel cannot be mistaken for a perfect
    boundary match; filter it out in analysis the way `nmi > 0` filters failures.
    """
    from scipy.ndimage import binary_erosion, distance_transform_edt

    ba = a > 0
    bb = b > 0
    surf_a = ba & ~binary_erosion(ba)
    surf_b = bb & ~binary_erosion(bb)
    if not surf_a.any() or not surf_b.any():
        return -1.0

    # distance_transform_edt gives, for each voxel, the distance to the nearest
    # zero. Inverting each surface makes that "distance to the nearest surface
    # voxel"; sampling those fields at the *other* surface gives the directed
    # nearest-neighbour distances without a pairwise scan.
    dist_to_b = distance_transform_edt(~surf_b, sampling=voxel_spacing)
    dist_to_a = distance_transform_edt(~surf_a, sampling=voxel_spacing)
    d_ab = float(dist_to_b[surf_a].mean())
    d_ba = float(dist_to_a[surf_b].mean())
    return max(d_ab, d_ba)


def rigid_transform_metrics(matrix: np.ndarray, points_ras: np.ndarray) -> dict:
    """Magnitude of a rigid transform: brain displacement (mm) and rotation (deg).

    A characterisation of the SynthMorph bold_to_t1w transform ITSELF — it reads
    no image intensities and needs no segmentation, so it is orthogonal to `nmi`
    and immune to the EPI skull-strip fragility that limits the mask metrics. It
    exists because bold_to_t1w is deliberately RIGID (ADR 002): SDC already ran
    upstream, so the true BOLD↔T1w offset is a same-session head-position
    difference only — small by construction. A registration implying a large
    shift or rotation is therefore implausible on physical grounds, independent
    of how its intensities happen to score (cf. 2655eea, where a broken run
    scored the HIGHEST raw MI of its siblings).

    `matrix` is the 4×4 RAS→RAS rigid transform; `points_ras` is an (N, 3) array
    of source-space RAS coordinates (pass the T1w brain-mask voxel centres in mm).
    The induced displacement is position-dependent — rotation moves distant
    voxels farther — so it is measured over the actual brain, not inferred from
    the translation column (which conflates rotation-about-origin with true
    translation and is not the physical brain shift).

      rigid_disp_mean_mm : mean over brain voxels of ‖T·x − x‖
      rigid_disp_max_mm  : worst-case brain-voxel displacement
      rigid_rot_deg      : rotation angle from R, arccos((tr R − 1) / 2)

    Gate the mean displacement if you gate anything: it already folds in the
    rotation's effect on the brain, so rigid_rot_deg is diagnostic rather than a
    second independent gate (cf. how ice_mean_mm is gated and its percentiles are
    not). All three are recorded ungated pending batch calibration.
    """
    r = np.asarray(matrix, dtype=np.float64)[:3, :3]
    t = np.asarray(matrix, dtype=np.float64)[:3, 3]
    pts = np.asarray(points_ras, dtype=np.float64)
    if pts.size == 0:
        return {'rigid_disp_mean_mm': 0.0, 'rigid_disp_max_mm': 0.0, 'rigid_rot_deg': 0.0}
    moved = pts @ r.T + t
    disp = np.linalg.norm(moved - pts, axis=1)
    cos_theta = (np.trace(r) - 1.0) / 2.0
    rot_deg = float(np.degrees(np.arccos(np.clip(cos_theta, -1.0, 1.0))))
    return {
        'rigid_disp_mean_mm': float(disp.mean()),
        'rigid_disp_max_mm': float(disp.max()),
        'rigid_rot_deg': rot_deg,
    }


def _joint_entropies(
    a: np.ndarray, b: np.ndarray, mask: np.ndarray | None, bins: int
) -> tuple[float, float, float] | None:
    """Marginal and joint Shannon entropies (H_a, H_b, H_ab) in nats.

    Shared core of mutual_information and normalized_mutual_information so the
    two can never disagree about masking, background handling or binning.
    Returns None when there is no tissue to score (empty mask/overlap), which
    each caller maps to its own failure sentinel.

    Background/air voxels (a <= 0) are dropped so the joint histogram reflects
    tissue, not the constant-zero region outside the field of view.
    """
    if mask is not None:
        a, b = a[mask], b[mask]
    a = a.ravel().astype(np.float64)
    b = b.ravel().astype(np.float64)
    sel = a > 0
    a, b = a[sel], b[sel]
    if a.size == 0:
        return None
    h, _, _ = np.histogram2d(a, b, bins=bins)
    p = h / h.sum()
    pa = p.sum(axis=1)
    pb = p.sum(axis=0)

    def _entropy(q: np.ndarray) -> float:
        nz = q > 0
        return float(-(q[nz] * np.log(q[nz])).sum())

    return _entropy(pa), _entropy(pb), _entropy(p)


def mutual_information(
    a: np.ndarray, b: np.ndarray, mask: np.ndarray | None = None, bins: int = 64
) -> float:
    """Mutual information (nats) between two images, optionally within a mask.

    This is the appropriate similarity metric for cross-contrast registration
    (e.g. EPI↔T1w): it is polarity-independent, where intensity NCC is not.
    For EPI↔T1w, NCC stays near zero regardless of alignment because the two
    modalities have opposite gray/white contrast; MI rises as anatomy aligns.

    Higher is better; the absolute value depends on `bins` so compare
    like-for-like. Note this raw form is NOT overlap-invariant — it is computed
    over surviving (a > 0) voxels, so it *inflates* as the overlap shrinks. The
    pipeline QC metric is `normalized_mutual_information`, which removes exactly
    that artefact; this raw form is retained for characterisation and tests.
    """
    ent = _joint_entropies(a, b, mask, bins)
    if ent is None:
        return 0.0
    h_a, h_b, h_ab = ent
    return h_a + h_b - h_ab


def normalized_mutual_information(
    a: np.ndarray, b: np.ndarray, mask: np.ndarray | None = None, bins: int = 64
) -> float:
    """Studholme normalized mutual information, (H_a + H_b) / H_ab.

    The cross-contrast quality metric for bold_to_t1w. Ranges from 1.0 when the
    images are statistically independent (H_ab = H_a + H_b) to 2.0 when one
    determines the other (H_ab = H_a = H_b); higher is better.

    Why normalized rather than raw MI: NMI = 1 + MI / H_ab, and it is
    *overlap-invariant* by construction (Studholme et al. 1999, "An overlap
    invariant entropy measure of 3D medical image alignment"). Raw MI is
    computed over surviving voxels, so a registration whose brain overlap
    collapses scores a spuriously HIGH MI on the shrinking set — the exact
    pathology behind the empty-mask failure in 2655eea, where the broken session
    scored the highest MI of its three. Dividing by the joint entropy cancels
    that. It is also far less sensitive to `bins` than raw MI.

    Returns 0.0 (an impossible NMI, which is always >= 1 for real tissue) as the
    failure sentinel when there is no overlap or no intensity variation, so the
    `> 0` filter that excludes failed runs still holds.
    """
    ent = _joint_entropies(a, b, mask, bins)
    if ent is None:
        return 0.0
    h_a, h_b, h_ab = ent
    if h_ab <= 0.0:  # both images constant over the overlap — no signal
        return 0.0
    return (h_a + h_b) / h_ab


def lncc(a: np.ndarray, b: np.ndarray, mask: np.ndarray, radius: int = 4) -> float:
    """Mean local normalized cross-correlation within `mask`.

    Windowed NCC (box window of side 2*radius+1) between two same-grid images,
    averaged over masked voxels. This is the intra-modal T1w↔T1w-template
    alignment metric (cf. ANTs SyN's CC). Identical textured images → ~1.0;
    uncorrelated → ~0.0. scipy is imported lazily so the shared module stays
    importable in images without scipy (e.g. bold_to_t1w never calls this).
    """
    from scipy.ndimage import uniform_filter

    a = a.astype(np.float64)
    b = b.astype(np.float64)
    size = 2 * radius + 1
    mu_a = uniform_filter(a, size)
    mu_b = uniform_filter(b, size)
    var_a = uniform_filter(a * a, size) - mu_a * mu_a
    var_b = uniform_filter(b * b, size) - mu_b * mu_b
    cov = uniform_filter(a * b, size) - mu_a * mu_b
    denom = np.sqrt(np.clip(var_a * var_b, 1e-10, None))
    ncc = cov / denom
    sel = mask & np.isfinite(ncc)
    return float(ncc[sel].mean()) if sel.any() else 0.0


# FreeSurfer/FastSurfer aseg label groups used by segmentation_alignment().
# aseg.auto.mgz ships in the templated FastSurfer tarball the bold_to_t1w step
# already unpacks, on the same conformed 256^3 1 mm grid as T1.mgz — so the
# warped BOLD needs no extra resampling to be indexed by these.
_ASEG_WM = (2, 41)  # left/right cerebral white matter
_ASEG_GM = (3, 42)  # left/right cerebral cortex
# (Ventricle labels 4/43/14/15 were used by seg_ventricle_ratio, removed in
# schema 2.6 — see segmentation_alignment's docstring.)


def _label_mask(aseg: np.ndarray, labels: tuple[int, ...]) -> np.ndarray:
    """Boolean mask of voxels whose aseg label is in `labels`."""
    return np.isin(np.rint(aseg).astype(np.int32), np.asarray(labels, dtype=np.int32))


def segmentation_alignment(warped: np.ndarray, aseg: np.ndarray, shell_radius: int = 2) -> dict:
    """Tissue-contrast metrics of a warped BOLD sampled through an aseg segmentation.

    This is the BBR cost *measured* rather than optimised. bbregister itself was
    removed in 61ccff7 because ABCD's 2.4 mm EPI lacks the gray/white contrast to
    DRIVE an optimisation, but that is a much higher bar than scoring an already-
    fitted transform: a metric only has to separate a good alignment from a bad
    one, not supply a usable gradient at every step. So the BBR *principle*
    returns here as QC even though the BBR *optimiser* stays gone.

    The premise is physical. In T2*-weighted EPI gray matter is brighter than
    white matter, so if the transform is correct the aseg GM label lands on
    bright BOLD voxels and the WM label on dark ones. Misalignment mixes the two
    populations and drives their contrast toward zero — monotonically, and with a
    far wider dynamic range than `normalized_mutual_information`, whose alignment
    signal is diluted across the whole-brain joint histogram (see
    docs/nmi-interpretation.md: the entire usable NMI range here is ~0.009 wide).

    Sampling is restricted to a shell of `shell_radius` voxels either side of the
    WM/GM interface, because that is the only place misregistration changes
    anything. Deep white matter and gyral crowns sit millimetres from any
    boundary, so including them adds a large alignment-insensitive constant to
    both means and blunts the metric — a whole-label version of the dilution that
    already limits NMI.

    Returns:
      seg_bbr_contrast   (mean_GM - mean_WM) / (0.5 * (mean_GM + mean_WM)) in the
                         boundary shell. Positive and larger is better. This is a
                         Michelson-style relative contrast, so it is invariant to
                         the arbitrary global scaling of BOLD intensity units —
                         a raw difference of means is not, and would not be
                         comparable across runs, let alone sessions.
    Returns the -999.0 sentinel when it cannot be computed (an empty shell, an
    empty label, or a zero mean that would divide by zero). Unlike 0.0 — a
    legitimate value meaning the tissues are indistinguishable — that sentinel
    cannot be mistaken for a real measurement.

    A companion `seg_ventricle_ratio` (mean BOLD in ventricle labels over mean in
    cerebral WM) was returned by schema 2.5 on the hypothesis that a shift
    dragging bright CSF into parenchyma would move it while leaving the GM/WM
    shell contrast plausible. Phase A measured it and the hypothesis did not hold:
    Spearman -0.158 against induced misregistration where this metric scores
    -0.90, and NON-MONOTONE — separation peaked at 10 mm then fell at 20 mm, so
    grosser misalignment scored healthier. Removed in 2.6 rather than kept as a
    field with no defensible reading. See
    handoffs/bold-t1w-qc-gate-calibration/RESULTS.md.
    """
    from scipy.ndimage import binary_dilation

    if warped.shape != aseg.shape:
        raise ValueError(
            f"warped and aseg must share the same grid; got {warped.shape} vs {aseg.shape}"
        )

    wm = _label_mask(aseg, _ASEG_WM)
    gm = _label_mask(aseg, _ASEG_GM)

    # The WM/GM interface, as the region where dilating each label overlaps the
    # other. Dilating BOTH and intersecting gives a shell straddling the boundary
    # symmetrically; intersecting each with its own label then splits it back into
    # the WM side and the GM side, which is what the contrast compares.
    if shell_radius > 0 and wm.any() and gm.any():
        near_gm = binary_dilation(gm, iterations=shell_radius)
        near_wm = binary_dilation(wm, iterations=shell_radius)
        wm_side = wm & near_gm
        gm_side = gm & near_wm
    else:
        wm_side, gm_side = wm, gm

    out = {'seg_bbr_contrast': -999.0}

    if wm_side.any() and gm_side.any():
        mu_wm = float(warped[wm_side].mean())
        mu_gm = float(warped[gm_side].mean())
        denom = 0.5 * (mu_gm + mu_wm)
        if denom != 0.0:
            out['seg_bbr_contrast'] = (mu_gm - mu_wm) / denom

    return out


def normalized_gradient_field(
    a: np.ndarray,
    b: np.ndarray,
    mask: np.ndarray,
    smooth_sigma: float = 1.5,
    edge_percentile: float = 75.0,
) -> float:
    """Normalized gradient field alignment (Haber & Modersitzki 2006), in [0, 1].

    An EDGE-alignment metric: it compares the *directions* of the two images'
    intensity gradients, ignoring their magnitudes and their signs.

      g_hat = grad(I) / sqrt(|grad(I)|^2 + eta^2)
      ngf   = mean over evaluation voxels of (g_hat_a . g_hat_b)^2

    The square is what makes this usable at all here. EPI and T1w have opposite
    gray/white polarity, so at a correctly-aligned boundary their gradients are
    ANTI-parallel — dot product -1. Squaring scores that 1.0, exactly as
    `mutual_information` is polarity-blind and for the same reason; an unsquared
    (or NCC-style) gradient metric would rank a perfect registration as maximally
    wrong. Perpendicular gradients — no shared boundary — score 0.

    Why this is not redundant with NMI: NMI asks how deterministic the global
    voxel-to-voxel intensity RELATIONSHIP is, pooling every brain voxel into one
    histogram, and is blind to where a given intensity sits. NGF asks whether
    the two images' boundaries point the same way, voxel by voxel. A few
    millimetres of cortical-ribbon slip barely perturbs a whole-brain histogram
    but flips the local gradient directions outright.

    `eta`, the edge-noise floor that keeps flat regions from contributing
    normalized noise, is set per-image from the median gradient magnitude inside
    `mask` rather than as a tuned constant — it must scale with each image's own
    intensity units, and BOLD and T1w units differ by orders of magnitude.

    Two constraints matter for 2.4 mm EPI resampled onto a 1 mm grid:

      `smooth_sigma` (voxels) blurs BOTH images before differentiating. Without
      it, every fine T1w edge is a high-frequency structure the EPI physically
      cannot resolve, so the score collapses toward chance for reasons that have
      nothing to do with registration. The default ~1.5 voxels brings the 1 mm
      T1w toward the EPI's effective resolution.

      `edge_percentile` restricts evaluation to voxels above that percentile of
      T1w gradient magnitude within `mask` — the real anatomical edges. Most of
      the brain is flat, and flat voxels contribute direction noise that dilutes
      the signal toward its chance value of ~1/3 (the mean of cos^2 over random
      3D directions).

    Returns 0.0 as the failure sentinel when no evaluation voxels survive.
    """
    from scipy.ndimage import gaussian_filter

    if a.shape != b.shape:
        raise ValueError(f"a and b must share the same grid; got {a.shape} vs {b.shape}")

    fa = gaussian_filter(a.astype(np.float64), smooth_sigma)
    fb = gaussian_filter(b.astype(np.float64), smooth_sigma)
    ga = np.stack(np.gradient(fa), axis=-1)
    gb = np.stack(np.gradient(fb), axis=-1)

    mag_a = np.linalg.norm(ga, axis=-1)
    mag_b = np.linalg.norm(gb, axis=-1)

    sel = mask & (mag_b > 0)
    if not sel.any():
        return 0.0
    # Evaluate only on real T1w edges. `b` is the fixed image (T1w) by
    # convention at the call site, so its gradient defines where the anatomy is.
    thresh = float(np.percentile(mag_b[sel], edge_percentile))
    # mag_a > 0 as well: a flat BOLD voxel has no gradient direction to compare,
    # and would divide by zero below. Dropping it is correct rather than scoring
    # it 0 — an absent gradient is an absent measurement, not a disagreement.
    sel = sel & (mag_b >= thresh) & (mag_a > 0)
    if not sel.any():
        return 0.0

    # Per-image noise floor: the median edge strength over the evaluation set.
    # Scale-free, so BOLD and T1w intensity units need no reconciliation.
    eta_a = float(np.median(mag_a[sel])) or 1e-10
    eta_b = float(np.median(mag_b[sel])) or 1e-10

    ma = mag_a[sel]
    mb = mag_b[sel]
    # cos of the angle between the gradients, squared to be polarity-blind.
    cos2 = ((ga[sel] * gb[sel]).sum(axis=1) / (ma * mb)) ** 2

    # eta enters as a WEIGHT, not as a rescaling of the directions. Damping the
    # vectors and then renormalizing them would cancel eta identically, leaving
    # plain cos^2 and no noise floor at all; weighting keeps the intended effect
    # — a voxel whose gradient is comparable to its image's noise floor barely
    # counts, one on a strong edge counts fully — while preserving the property
    # that a perfectly aligned pair scores exactly 1.0 regardless of eta.
    w = (ma / np.sqrt(ma**2 + eta_a**2)) * (mb / np.sqrt(mb**2 + eta_b**2))
    total = float(w.sum())
    if total <= 0.0:
        return 0.0
    return float((w * cos2).sum() / total)


def centroid_displacement_mm(
    fixed: np.ndarray,
    warped: np.ndarray,
    affine: np.ndarray,
    mask: np.ndarray,
) -> float:
    """Intensity-weighted centroid displacement between two images (mm).

    Both arrays must already be on the same voxel grid. Centroids are computed
    within `mask`, weighted by voxel intensity, then converted to RAS mm via
    `affine`. Returns the Euclidean distance between the two centroids.
    """
    if fixed.shape != warped.shape:
        raise ValueError(
            f"fixed and warped must share the same grid; got {fixed.shape} vs {warped.shape}"
        )

    def _weighted_centroid(arr: np.ndarray) -> np.ndarray:
        vals = np.where(mask, arr, 0.0).astype(np.float64)
        total = vals.sum()
        if total == 0:
            # Fallback: unweighted centroid of the mask
            coords = np.argwhere(mask)
            return coords.mean(axis=0) if len(coords) else np.zeros(3)
        idxs = np.indices(arr.shape)  # (3, X, Y, Z)
        return np.array([(idxs[i] * vals).sum() / total for i in range(3)])

    c_fixed = _weighted_centroid(fixed)
    c_warped = _weighted_centroid(warped)

    # Convert voxel-space centroids to RAS mm via the affine (drop translation
    # for the difference; use it for absolute positions).
    rot = affine[:3, :3]
    t = affine[:3, 3]
    ras_fixed = rot @ c_fixed + t
    ras_warped = rot @ c_warped + t
    return float(np.linalg.norm(ras_fixed - ras_warped))


# T1w-to-MNI QC gate. One bound per failure family, fail-only — no warn bands.
#
# Narrowed from five gated metrics to three (2026-07-25). Every added gate
# multiplies the FALSE-fail rate, and a false fail costs a GPU re-registration
# plus operator time; it buys nothing unless it detects a failure mode the other
# gates miss. These three are each the only cheap detector of their family:
#
#   lncc                   intensity agreement with the template
#   jac_det_frac_negative  local folding (the transform is not a diffeomorphism)
#   ice_mean_mm            global invertibility (a property of the TRANSFORM
#                          alone — no intensity, no template — so it catches a
#                          warp that matches intensities well while being
#                          globally non-invertible)
#
# Warn bands are gone. Warn exited 0, promoted outputs, and had no consumer — but
# all three recorded recalibrations (lncc 2026-07-03, jac_det_frac_negative,
# mask_dice 2026-07-23) were driven by a WARN band firing on healthy subjects.
# The fail bands were never the problem. Raw values are in Athena, so any "warn"
# band anyone actually wants stays derivable post-hoc without a tuned constant
# here. _BOLD_T1W_THRESHOLDS dropped its warn band too (2026-07-30), so no table
# declares one and verdict() no longer implements warn at all.
_T1W_MNI_THRESHOLDS = {
    # Individual T1w vs the MNI *average* template caps diffeomorphically at
    # lncc ~0.83 (10-subject post-fix distribution: mean 0.80, sd 0.017,
    # min 0.78); broken registrations score ~0.19. 0.65 sits ~9 sd below the
    # healthy mean and ~35 sd above the broken anchor — the single most
    # discriminating metric here, which is why the other intensity/geometry
    # gates add so little. (The original 0.85/0.75 was above the achievable
    # ceiling, so every subject warned; recalibrated 2026-07-03, Part E.)
    'lncc': {'fail': 0.65, 'direction': 'below'},
    # Folding budget: at most 0.5% of brain voxels may have det(J) < 0. The
    # prior 0.001 sat *inside* the healthy distribution (10-subject batch:
    # mean 0.0004, sd 0.0002, max 0.0013), so a normal registration tripped it
    # ~17% of the time — it was measuring the population mean, not detecting
    # anomalies.
    'jac_det_frac_negative': {'fail': 0.005, 'direction': 'above'},
    # Forward∘inverse round-trip residual. Sub-half-voxel on 1 mm MNI152. Gated
    # on the mean; p95/p99/max are recorded for diagnosis but not gated. Absent
    # when the inverse warp could not be saved — verdict() skips absent keys, so
    # this gate fails open rather than failing a session on a missing measurement.
    'ice_mean_mm': {'fail': 0.5, 'direction': 'above'},
    # DELIBERATELY NOT GATED, recorded only:
    #
    #   mask_dice — near-inert by construction. Whole-brain overlap saturates for
    #     affine+SyN to a template (10-subject batch: mean 0.9818, sd 0.0019,
    #     range 0.9759-0.9843), so a bound that can fire at all has to sit far
    #     below the achievable floor; the 2026-07-23 recalibration already had to
    #     drop it to 0.950, ~17 sd below the mean. At that distance it fires only
    #     on registrations lncc has already rejected. It never caught the one
    #     genuine failure in that batch (which failed on deformation folding).
    #   centroid_displacement_mm — global rigid offset, a strict subset of what
    #     lncc and the folding gate already see; no independent failure family.
    #   log_jac_* — the log-Jacobian distribution, recorded by jacobian_stats()
    #     for characterisation. Folding is adjudicated by jac_det_frac_negative;
    #     volume-change magnitude is descriptive only.
}


# BOLD→T1w gate (bold_to_t1w.py). Gates on `nmi_gain` — how much NMI the fitted
# transform buys over resampling the same BOLD reference with identity — and on
# nothing else. Everything else (nmi, the rigid_* trio, mhd_mm) is recorded-only.
#
# Why not the rigid_* magnitude metrics (they gated in schema 2.3, reverted 2.4):
# the premise was that "bold_to_t1w is rigid, so the true offset is a small
# same-session head shift". That is false for ABCD minimally preprocessed input.
# Hagler 2019 keeps the BOLD in its ORIGINAL scanner space and merely ships a
# registration matrix alongside — the BOLD is never resampled into anatomical
# space — so the BOLD→T1w offset is the field-of-view prescription difference,
# set by how the operator positioned the two acquisitions, not by head motion.
#
# Measured on the 2026-07-29 10-subject batch (110 runs), which the 15/20 bounds
# failed 92 of and passed 0 of:
#   - rigid_disp_max_mm is a SESSION-LEVEL CONSTANT: pooled within-session SD
#     0.29 mm vs between-session SD 19.75 mm — a 69x ratio. All six runs of
#     sub-WGVKC3KK/ses-04A score 17.58 +/- 0.37 mm; all six of that same
#     subject's ses-00A score 96.97 +/- 0.64 mm. A per-run *quality* metric
#     cannot be a per-session constant; it was measuring the prescription.
#   - It anti-correlates with quality: corr(nmi, rigid_disp_max_mm) = +0.425.
#     Larger corrections are BETTER registrations, and the batch's largest
#     offset (96.97 mm) had its second-highest mean nmi. The gate ranked runs
#     backwards and discarded whole sessions on scanner positioning.
# See docs/investigations/2026-07-29-bold-to-t1w-qc-handoff.md section 0.
#
# Why `nmi_gain` rather than an absolute `nmi` bound: absolute NMI is not
# portable. Its scale depends on bin count, masking and interpolation, and the
# literature is explicit that these metrics have no established thresholds and
# are highly dependent on site, vendor and acquisition parameters (MRIQC). It is
# not even portable BETWEEN SESSIONS here: the identity baseline itself moved
# 1.00947-1.01329 across three measured sessions, a 0.0038 spread comparable to
# the entire 0.0046-0.0089 improvement signal. Scoring each run against its own
# identity baseline cancels that, and makes the gate self-calibrating across
# site, protocol and population.
#
# THE BOUND IS STRUCTURAL, NOT CALIBRATED. `nmi_gain <= 0` means the fitted
# transform scores no better than resampling with identity — the registration
# bought nothing, or actively made alignment worse. That is not a judgement about
# where "good" stops; it is the point at which the transform stops being an
# improvement at all, which is a fact about what the number means rather than a
# percentile of some observed population.
#
# This distinction is the whole reason the bound sits here. Every previous
# threshold on this step (and the lncc, jac_det_frac_negative and mask_dice
# recalibrations before it) was a constant fitted to a healthy-only sample, and
# every one of them was later moved because it fired on healthy subjects from a
# scanner or protocol the sample did not cover. A structural bound cannot drift
# that way: no site, vendor or acquisition change makes "worse than doing
# nothing" acceptable.
#
# Prior bounds here were fitted constants: schema 2.4 used fail 0.0005 / warn
# 0.002, chosen as ~9x and ~2.3x below the minimum of an 18-run healthy sample
# (range 0.00456-0.00894, mean 0.00702, 18/18 beating identity). Both are
# withdrawn. The observed healthy floor is ~0.0046, so 0.0 sits far below
# anything ever measured and the false-fail rate is effectively nil.
#
# `inclusive` matters: a degenerate SynthMorph result that returns the identity
# transform scores nmi_gain of EXACTLY 0.0, so a strict `<` would pass precisely
# the failure this gate exists to catch.
#
# WHY NOTHING ELSE GATES — and specifically why no *quality* bound is set here.
# The two error types are not symmetric:
#   - A false fail is PERMANENT. SynthMorph is deterministic, so a resubmit
#     re-registers to the identical fail, and exit 65 is deliberately excluded
#     from the workflow's retry expression. The run's outputs are discarded and
#     the ABCD session timepoint cannot be re-acquired.
#   - A false pass is RECOVERABLE. Every metric below lands in Athena, so a
#     consumer can filter a poor run out at any later point.
# When one error is irreversible and the other is a query away, the irreversible
# one does not go behind a threshold fitted from healthy-only data. Quality
# metrics are therefore recorded, not gated; see docs/investigations/ and
# handoffs/bold-t1w-qc-gate-calibration/PREREGISTRATION.md.
_BOLD_T1W_THRESHOLDS = {
    'nmi_gain': {'fail': 0.0, 'direction': 'below', 'inclusive': True},
}


def verdict(metrics: dict, thresholds: dict | None = None) -> str:
    """Return 'pass' or 'fail' from a composite registration gate.

    Evaluate every threshold in `thresholds` present in `metrics`: 'fail' if any
    metric crosses its fail bound, else 'pass'. Unrecognised or missing keys are
    ignored (they do not trip the gate). `thresholds` defaults to the T1w→MNI
    table; pass `_BOLD_T1W_THRESHOLDS` for the bold_to_t1w gate.

    PASS THE WHOLE METRICS RECORD. This function selects what gates by consulting
    `thresholds` alone, so the threshold table is the single source of truth for
    which metrics are gated. Callers must NOT pre-filter the record down to the
    gated keys: that creates a second place to edit when a gate changes, and the
    two drift silently because a missing key here fails open rather than raising.

    THERE IS NO 'warn'. Both tables declare fail bounds only. A warn exited 0 and
    promoted the outputs exactly as a pass did, so it had no consumer — while
    being the trigger for every historical recalibration on both registration
    types (lncc 2026-07-03, jac_det_frac_negative and mask_dice 2026-07-23,
    rigid_disp_max_mm 2026-07-29). Raw values are all in Athena, so any warn band
    a consumer actually wants stays derivable post-hoc. Historical records still
    contain 'warn' and queries over them are unaffected; this removes only the
    ability to emit a new one.

    A spec may set `'inclusive': True` to close its bound, so a value landing
    exactly ON the bound fails. Bounds are exclusive by default.
    """
    thresholds = thresholds if thresholds is not None else _T1W_MNI_THRESHOLDS
    for key, spec in thresholds.items():
        if key not in metrics:
            continue
        val, bound = metrics[key], spec['fail']
        inclusive = spec.get('inclusive', False)
        if spec['direction'] == 'below':
            if (val <= bound) if inclusive else (val < bound):
                return 'fail'
        else:  # 'above'
            if (val >= bound) if inclusive else (val > bound):
                return 'fail'
    return 'pass'


def jacobian_stats(warp_path: str, mask_path: str | None = None) -> dict:
    """Jacobian determinant statistics of an ANTs displacement field (NIfTI format).

    Returns jac_det_{min,max,mean,std,frac_negative} plus the log-Jacobian
    distribution (log_jac_*). A healthy SyN warp has all determinants > 0 (no
    folding) and mean ≈ 1.

    Why log(det J) and not det J alone: det J is multiplicative and asymmetric —
    a doubling of local volume is 2.0 but a halving is 0.5, so expansion and
    compression are not comparable on one scale and summary statistics are
    dragged by the expansion tail. log(det J) centres at 0 (no volume change)
    and makes the two symmetric: +0.69 doubling, −0.69 halving. For a healthy
    adult T1w→MNI152 warp local volume change rarely exceeds a factor of 3–4,
    so the vast majority of values should fall within ±1.5. Values approaching
    ±3 (a ~20× volume change) mean the warp is aggressively squashing or
    ballooning tissue to force an intensity match — usually failed
    skull-stripping, or pathology the template cannot accommodate.

    log() is undefined at det <= 0, so folded voxels are excluded from every
    log_jac_* statistic and reported separately as jac_det_frac_negative; the
    log_jac_frac_* denominators are the non-folded voxels only. If nothing is
    left after masking, or no determinant is positive, the log_jac_* fields are
    returned as 0.0 — that case is catastrophic and is caught by
    jac_det_frac_negative rather than being encoded here.

    `mask_path` restricts every statistic to voxels where that image is > 0 —
    pass the MNI template so the numbers describe brain tissue. Outside the
    brain the warp is unconstrained extrapolation whose extreme values would
    otherwise dominate the beyond-threshold fractions. The mask is read here
    with nibabel so it shares the warp's (x, y, z) axis order; a
    SimpleITK-derived mask is (z, y, x) and would mis-index silently.
    """
    img = nib.load(warp_path)
    d = img.get_fdata(dtype=np.float32)
    if d.ndim == 5:
        d = d[:, :, :, 0, :]  # (X, Y, Z, 1, 3) → (X, Y, Z, 3)
    vox = np.abs(img.header.get_zooms()[:3]).tolist()

    ux, uy, uz = d[..., 0], d[..., 1], d[..., 2]

    j11 = 1 + np.gradient(ux, vox[0], axis=0)
    j12 = np.gradient(ux, vox[1], axis=1)
    j13 = np.gradient(ux, vox[2], axis=2)
    j21 = np.gradient(uy, vox[0], axis=0)
    j22 = 1 + np.gradient(uy, vox[1], axis=1)
    j23 = np.gradient(uy, vox[2], axis=2)
    j31 = np.gradient(uz, vox[0], axis=0)
    j32 = np.gradient(uz, vox[1], axis=1)
    j33 = 1 + np.gradient(uz, vox[2], axis=2)

    det = (
        j11 * (j22 * j33 - j32 * j23)
        - j12 * (j21 * j33 - j31 * j23)
        + j13 * (j21 * j32 - j31 * j22)
    )

    sel = np.isfinite(det)
    if mask_path is not None:
        mask = nib.load(mask_path).get_fdata() > 0
        if mask.shape != det.shape:
            raise ValueError(
                f'mask shape {mask.shape} does not match warp grid {det.shape}; '
                'the mask must be on the same grid as the displacement field'
            )
        sel &= mask

    det = det[sel]
    log_fields = {
        'log_jac_mean': 0.0,
        'log_jac_std': 0.0,
        'log_jac_p01': 0.0,
        'log_jac_p99': 0.0,
        'log_jac_min': 0.0,
        'log_jac_max': 0.0,
        'log_jac_frac_beyond_1p5': 0.0,
        'log_jac_frac_beyond_3': 0.0,
    }
    if det.size == 0:
        return {
            'jac_det_min': 0.0,
            'jac_det_max': 0.0,
            'jac_det_mean': 0.0,
            'jac_det_std': 0.0,
            'jac_det_frac_negative': 0.0,
            **log_fields,
        }

    positive = det[det > 0]
    if positive.size:
        log_det = np.log(positive)
        abs_log = np.abs(log_det)
        log_fields = {
            'log_jac_mean': float(log_det.mean()),
            'log_jac_std': float(log_det.std()),
            # Robust range: the tails of a deformation field are single-voxel
            # noise, so percentiles describe "where the distribution lives"
            # while min/max below record how bad the worst voxel got.
            'log_jac_p01': float(np.percentile(log_det, 1)),
            'log_jac_p99': float(np.percentile(log_det, 99)),
            'log_jac_min': float(log_det.min()),
            'log_jac_max': float(log_det.max()),
            # Volume change beyond ~4.5x (e^1.5) — outside the plausible range
            # for healthy adult anatomy against MNI152.
            'log_jac_frac_beyond_1p5': float((abs_log > 1.5).mean()),
            # Beyond ~20x (e^3) — squashing/ballooning, not anatomy.
            'log_jac_frac_beyond_3': float((abs_log > 3.0).mean()),
        }

    return {
        'jac_det_min': float(det.min()),
        'jac_det_max': float(det.max()),
        'jac_det_mean': float(det.mean()),
        'jac_det_std': float(det.std()),
        'jac_det_frac_negative': float((det < 0).mean()),
        **log_fields,
    }


def _load_displacement_field(path: str) -> tuple[np.ndarray, np.ndarray]:
    """Load an ANTs displacement field as (X, Y, Z, 3) mm, plus voxel spacing."""
    img = nib.load(path)
    d = img.get_fdata(dtype=np.float32)
    if d.ndim == 5:
        d = d[:, :, :, 0, :]  # (X, Y, Z, 1, 3) → (X, Y, Z, 3)
    vox = np.abs(img.header.get_zooms()[:3]).astype(np.float32)
    return d, vox


def inverse_consistency_error(
    forward_warp_path: str,
    inverse_warp_path: str,
    mask_path: str | None = None,
) -> dict:
    """Round-trip error of composing the forward and inverse warps, in mm.

    Displace each voxel by the forward field, then displace the result by the
    inverse field sampled at that (off-grid) location. A true inverse pair
    returns to the starting point, so the residual magnitude is the error:

        ICE(x) = ‖ F(x) + I(x + F(x)) ‖

    This is the only QC metric here that is a property of the TRANSFORM alone —
    it reads no image intensities and needs no template comparison. That makes it
    orthogonal to mask_dice/lncc (which measure intensity agreement and can be
    satisfied by a geometrically implausible warp) and to the Jacobian statistics
    (which are local: every voxel can have positive det(J) while the mapping is
    not globally invertible). Target is sub-voxel — under 0.5 mm on 1 mm MNI152.

    Both fields must be ANTs-convention displacements on the SAME grid — for
    ANTs/FireANTs, Warp and InverseWarp are both defined on the fixed (template)
    grid, so this holds. Displacement components are assumed to correspond to
    array axes in order and to be expressed in mm, which is the same assumption
    jacobian_stats() already makes when it differentiates with voxel spacing;
    it holds for axis-aligned volumes and would need an affine rotation term
    otherwise.

    `mask_path` restricts the statistics to voxels where that image is > 0 — pass
    the template so the numbers describe brain tissue rather than the
    unconstrained extrapolation outside it.
    """
    from scipy.ndimage import map_coordinates

    fwd, vox = _load_displacement_field(forward_warp_path)
    inv, _ = _load_displacement_field(inverse_warp_path)
    if fwd.shape != inv.shape:
        raise ValueError(f'forward {fwd.shape} and inverse {inv.shape} fields must share a grid')

    # Where each voxel lands under the forward field, in VOXEL units (the
    # displacements are mm, so divide by spacing before adding to indices).
    grid = np.indices(fwd.shape[:3]).astype(np.float32)  # (3, X, Y, Z)
    target = grid + np.stack([fwd[..., k] / vox[k] for k in range(3)])

    # Sample the inverse field at those positions. order=1 is trilinear;
    # mode='nearest' clamps points pushed outside the field rather than
    # wrapping them, which would fabricate huge residuals at the border.
    sampled = np.stack(
        [map_coordinates(inv[..., k], target, order=1, mode='nearest') for k in range(3)]
    )

    residual = np.stack([fwd[..., k] for k in range(3)]) + sampled
    ice = np.sqrt((residual**2).sum(axis=0))

    sel = np.isfinite(ice)
    if mask_path is not None:
        mask = nib.load(mask_path).get_fdata() > 0
        if mask.shape != ice.shape:
            raise ValueError(
                f'mask shape {mask.shape} does not match warp grid {ice.shape}; '
                'the mask must be on the same grid as the displacement field'
            )
        sel &= mask

    ice = ice[sel]
    if ice.size == 0:
        return {
            'ice_mean_mm': 0.0,
            'ice_p95_mm': 0.0,
            'ice_p99_mm': 0.0,
            'ice_max_mm': 0.0,
        }

    return {
        'ice_mean_mm': float(ice.mean()),
        'ice_p95_mm': float(np.percentile(ice, 95)),
        'ice_p99_mm': float(np.percentile(ice, 99)),
        'ice_max_mm': float(ice.max()),
    }
