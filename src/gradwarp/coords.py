"""Scanner-coordinate mapping for the gradwarp pilot (add-fasttrack-gradwarp-recovery 2.2).

Conventions:
- Every point here is physical RAS, in mm. nibabel affines are RAS; ANTs/ITK works in LPS,
  so ANTs transforms and points are flipped on the way in and out (`itk_to_ras`).
- The raw T1w is converted by `dcm2niix`, whose affine is DICOM patient coordinates in RAS.
  For a head-first supine (HFS) patient those axes are fixed in the magnet, with the origin
  at isocentre when the table is at the landmark position (checked in task 2.5). The field is
  learned and applied in that frame, so only HFS sessions are used (`require_hfs`).
- The registration is raw (moving) to DAIC's T1w (fixed). ANTs' forward transform maps a
  fixed point to the moving point it samples, so for a DAIC point `x`:
  - the rigid part gives `R(x)`, the corrected scanner position, up to the session's own
    small rigid error, which the fit absorbs per session (`shfield`);
  - the full rigid+SyN composite gives `q`, the acquired scanner position.

  The sample is the pull-back displacement `d = q − R(x)` at position `p = R(x)`.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

#: RAS <-> LPS: negate x and y. Its own inverse.
FLIP = np.diag([-1.0, -1.0, 1.0, 1.0])


def apply(affine: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Apply a 4x4 affine to (N, 3) points."""
    pts = np.asarray(points, dtype=float)
    return pts @ affine[:3, :3].T + affine[:3, 3]


def flip_lps_ras(points: np.ndarray) -> np.ndarray:
    """RAS points to LPS, or LPS to RAS (the same operation)."""
    out = np.array(points, dtype=float, copy=True)
    out[..., :2] *= -1.0
    return out


def itk_to_ras(matrix: np.ndarray, translation: np.ndarray, center: np.ndarray) -> np.ndarray:
    """An ITK `MatrixOffsetTransformBase` (LPS) as a 4x4 RAS-to-RAS affine.

    ITK maps `y = M (x − c) + c + t`, in LPS.
    """
    m = np.asarray(matrix, dtype=float).reshape(3, 3)
    c = np.asarray(center, dtype=float).reshape(3)
    t = np.asarray(translation, dtype=float).reshape(3)
    lps = np.eye(4)
    lps[:3, :3] = m
    lps[:3, 3] = t + c - m @ c
    return FLIP @ lps @ FLIP


def read_ants_mat(path: str | Path) -> np.ndarray:
    """antsRegistration's `0GenericAffine.mat` (MATLAB v4) as a 4x4 RAS-to-RAS affine."""
    from scipy.io import loadmat

    mat = loadmat(str(path))
    key = next(k for k in mat if k.startswith("AffineTransform_") or k.startswith("MatrixOffset"))
    params = np.asarray(mat[key], dtype=float).ravel()
    center = np.asarray(mat["fixed"], dtype=float).ravel()
    return itk_to_ras(params[:9], params[9:12], center)


def is_rigid(affine: np.ndarray, tol: float = 1e-6) -> bool:
    """True if the 3x3 part is a proper rotation."""
    r = affine[:3, :3]
    return bool(np.allclose(r.T @ r, np.eye(3), atol=tol) and np.linalg.det(r) > 0)


def grid_points(mask: np.ndarray, affine: np.ndarray, step: int) -> np.ndarray:
    """Physical RAS points of every `step`-th voxel along each axis where `mask` is true."""
    if step < 1:
        raise ValueError("step must be >= 1")
    sub = np.zeros(mask.shape, dtype=bool)
    sub[::step, ::step, ::step] = True
    ijk = np.argwhere(np.asarray(mask, dtype=bool) & sub).astype(float)
    return apply(affine, ijk)


def displacement_samples(
    fixed_points: np.ndarray, rigid: np.ndarray, mapped_points: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """(positions, displacements) in scanner RAS mm, from one session's registration.

    `fixed_points` are DAIC-space points; `rigid` is the rigid stage as a RAS affine; and
    `mapped_points` are the same points through the full composite (rigid then SyN), as
    `antsApplyTransformsToPoints` returns them (flipped back to RAS).
    """
    if not is_rigid(rigid):
        raise ValueError("the rigid stage is not a proper rotation plus translation")
    fixed = np.asarray(fixed_points, dtype=float)
    mapped = np.asarray(mapped_points, dtype=float)
    if fixed.shape != mapped.shape or fixed.ndim != 2 or fixed.shape[1] != 3:
        raise ValueError(f"shape mismatch: {fixed.shape} vs {mapped.shape}")
    p = apply(rigid, fixed)
    return p, mapped - p


def require_hfs(patient_position: str | None) -> None:
    """The field's frame is patient RAS for a head-first supine patient; nothing else."""
    if (patient_position or "").strip().upper() != "HFS":
        raise ValueError(f"patient position {patient_position!r} is not HFS")
