"""Registration endpoints, from the transforms and warped labels themselves (D17, D19).

Three measures, none of which looks at transform magnitude. ABCD's BOLD is never
resampled out of native scanner space, so the BOLD→T1w offset is field-of-view
prescription (16–97 mm) and says nothing about quality.

- **Warped-label Dice** (T1w→MNI, primary). Each pipeline warps its own
  segmentation by its own transform; each is scored against the template as
  segmented by its own engine. The labels are fixed here, before any result: the
  14 bilateral subcortical structures and the 31 DKT parcels per hemisphere both
  engines produce. The session value tested is the signed difference of the two
  mean Dices, cloudpipe minus fMRIPrep, against a non-inferiority margin.
- **Folding fraction** (descriptive). The share of template-mask voxels where a
  warp's Jacobian determinant is ≤ 0, from its displacement field.
- **Average absolute displacement** between two rigid transforms over a set of
  points (descriptive since D19). Over the white-surface vertices in world
  coordinates, so the measure is centred on the brain, not on a distant origin.
  Both transforms must map the same source space to the same target space; under
  D19 the two arms' T1w spaces differ, so the caller supplies that mapping.

Everything here is arrays in, numbers out: no file layout, no arm identity.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

#: The aseg structures both engines label, left then right: thalamus, caudate,
#: putamen, pallidum, hippocampus, amygdala, nucleus accumbens.
SUBCORTICAL_LABELS: tuple[int, ...] = (10, 11, 12, 13, 17, 18, 26, 49, 50, 51, 52, 53, 54, 58)

#: The 31 DKT (Klein and Tourville, 2012) cortical parcels per hemisphere, as
#: FreeSurfer's `aparc.DKTatlas` and FastSurfer's `aparc.DKTatlas.mapped` number
#: them. DKT drops Desikan's banks of the STS (1001), corpus callosum (1004),
#: frontal pole (1032) and temporal pole (1033).
_DKT_PARCELS: tuple[int, ...] = (
    2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18,
    19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 34, 35,
)  # fmt: skip
DKT_LABELS: tuple[int, ...] = tuple(1000 + p for p in _DKT_PARCELS) + tuple(
    2000 + p for p in _DKT_PARCELS
)

#: The registration endpoint's label set: 14 + 62 = 76 labels.
ENDPOINT_LABELS: tuple[int, ...] = SUBCORTICAL_LABELS + DKT_LABELS


class RegistrationInputError(ValueError):
    """Inputs that cannot define the measure, refused rather than guessed at."""


@dataclass(frozen=True)
class LabelDice:
    """Per-label Dice of one warped segmentation against one target."""

    per_label: dict[int, float]
    missing: tuple[int, ...]

    @property
    def mean(self) -> float:
        """Mean over every endpoint label. A label absent from one side scores 0."""
        return float(np.mean(list(self.per_label.values())))


def label_dice(warped, target, labels: Iterable[int] = ENDPOINT_LABELS) -> LabelDice:
    """Dice per label between a warped segmentation and a target on one grid.

    A label present in neither image is recorded in `missing` and scores 0: the
    label set is fixed in advance, so a structure an engine failed to produce
    counts against it rather than leaving the mean.
    """
    warped = np.asarray(warped)
    target = np.asarray(target)
    if warped.shape != target.shape:
        raise RegistrationInputError(
            f"segmentations on different grids, {warped.shape} and {target.shape}"
        )
    per_label: dict[int, float] = {}
    missing = []
    for label in labels:
        a = warped == label
        b = target == label
        total = int(a.sum() + b.sum())
        if total == 0:
            missing.append(label)
            per_label[label] = 0.0
        else:
            per_label[label] = 2.0 * float(np.count_nonzero(a & b)) / total
    return LabelDice(per_label, tuple(missing))


def jacobian_determinant(displacement, affine) -> np.ndarray:
    """det J of x → x + u(x) on a voxel grid, from a world-space displacement field.

    `displacement` is `(X, Y, Z, 3)` in millimetres, world axes; `affine` maps
    voxel indices to the same world. J = I + (∂u/∂index) · A⁻¹, A being the
    affine's 3×3, so an oblique or flipped grid is handled rather than assumed
    away. Central differences inside, one-sided at the edges (`np.gradient`).
    """
    u = np.asarray(displacement, dtype=np.float64)
    if u.ndim != 4 or u.shape[-1] != 3:
        raise RegistrationInputError(f"displacement must be (X, Y, Z, 3), got {u.shape}")
    a_inv = np.linalg.inv(np.asarray(affine, dtype=np.float64)[:3, :3])
    # grad[..., c, k] = ∂u_c / ∂index_k
    grad = np.stack(
        [np.stack(np.gradient(u[..., c], axis=(0, 1, 2)), axis=-1) for c in range(3)], axis=-2
    )
    jac = np.eye(3) + grad @ a_inv
    return np.linalg.det(jac)


def folding_fraction(displacement, affine, mask=None) -> float:
    """Share of voxels (inside `mask` if given) where det J ≤ 0."""
    det = jacobian_determinant(displacement, affine)
    if mask is not None:
        mask = np.asarray(mask) > 0
        if mask.shape != det.shape:
            raise RegistrationInputError(f"mask {mask.shape} is not on the warp's grid {det.shape}")
        det = det[mask]
    if det.size == 0:
        raise RegistrationInputError("no voxels to measure folding over")
    return float(np.count_nonzero(det <= 0) / det.size)


def average_absolute_displacement(transform_a, transform_b, points) -> float:
    """Mean distance between where two 4×4 transforms send each point.

    `points` is `(N, 3)` in the transforms' common source space, in mm. The
    transforms must share source and target; nothing here can check that, so it
    is the caller's statement.
    """
    a = np.asarray(transform_a, dtype=np.float64)
    b = np.asarray(transform_b, dtype=np.float64)
    pts = np.asarray(points, dtype=np.float64)
    if a.shape != (4, 4) or b.shape != (4, 4):
        raise RegistrationInputError("transforms must be 4x4 homogeneous matrices")
    if pts.ndim != 2 or pts.shape[1] != 3 or len(pts) == 0:
        raise RegistrationInputError(f"points must be a non-empty (N, 3) array, got {pts.shape}")
    homogeneous = np.c_[pts, np.ones(len(pts))]
    moved_a = homogeneous @ a.T
    moved_b = homogeneous @ b.T
    return float(np.linalg.norm(moved_a[:, :3] - moved_b[:, :3], axis=1).mean())


def labels_on_grid(labels, reference) -> np.ndarray:
    """A label volume on `reference`'s grid by nearest neighbour in world coordinates.

    For putting each engine's segmentation of the template, in its own conformed
    256³ space, onto the 1 mm template grid the warped labels are on. Both engines'
    targets go through this one function, so the ≤0.5 mm lattice shift it costs
    is the same for both and cannot favour either arm. Voxels that fall outside
    `labels` are 0.
    """
    import nibabel as nib

    src = labels if hasattr(labels, "affine") else nib.load(labels)
    ref = reference if hasattr(reference, "affine") else nib.load(reference)
    data = np.asanyarray(src.dataobj)
    shape = ref.shape[:3]
    ijk = np.indices(shape, dtype=np.float64).reshape(3, -1)
    world = ref.affine[:3, :3] @ ijk + ref.affine[:3, 3:4]
    src_ijk = np.linalg.inv(src.affine)[:3, :3] @ world + np.linalg.inv(src.affine)[:3, 3:4]
    idx = np.rint(src_ijk).astype(np.int64)
    inside = np.all((idx >= 0) & (idx < np.array(data.shape[:3])[:, None]), axis=0)
    out = np.zeros(idx.shape[1], dtype=np.int32)
    out[inside] = data[idx[0, inside], idx[1, inside], idx[2, inside]]
    return out.reshape(shape)
