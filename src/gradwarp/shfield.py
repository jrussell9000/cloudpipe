"""One spherical-harmonic displacement field across sessions (add-fasttrack-gradwarp-recovery 2.3).

The model, per session `s` and sample `i` at scanner position `p` (RAS mm about isocentre):

    d_si = F(p_si) + t_s + w_s × p_si + noise

- `F` is the shared field. Each of its three components is a sum of real regular solid
  harmonics, `(r/r0)^n Y_nm`, for orders `n = 1..order`, with `r0` = 250 mm (Siemens'
  coefficient convention).
- Order 0 (a constant) and the antisymmetric part of order 1 (a rotation) are left out of
  `F`. The order-1 part is a symmetric 3x3 matrix: scale and shear, which a rigid fit cannot
  absorb.
- `t_s`, `w_s` are a per-session translation and rotation, estimated alongside `F` and
  eliminated per session. They are needed, not optional: the raw-to-DAIC rigid registration
  fits the warped image, so it already absorbed the field's own translation and rotation at
  that session's head position. Without them the fit would be biased toward each head's
  local rigid part. With them, order 0 and the rotation part of order 1 are not
  identifiable, which is why `F` leaves them out.

The fit solves the normal equations with each session's 6 rigid parameters eliminated by a
Schur complement, so memory is (parameters)², not (samples × parameters).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
from scipy.special import sph_harm_y

R0_MM = 250.0
#: Order-1 symmetric pairs (i, j), i <= j; each is one parameter setting C[i, j] and C[j, i].
_SYM = [(0, 0), (1, 1), (2, 2), (0, 1), (0, 2), (1, 2)]


@dataclass(frozen=True)
class Session:
    """One session's samples: positions and pull-back displacements, (N, 3) mm each.

    `group` is an opaque integer for the subject, never an ID: sessions of one subject are
    always in the same cross-validation fold.
    """

    points: np.ndarray
    disp: np.ndarray
    group: int


def n_terms(order: int) -> int:
    """Basis functions per component for orders 1..order: sum of (2n + 1)."""
    return (order + 1) ** 2 - 1


def term_index(order: int) -> list[tuple[int, int]]:
    """(n, m) of each basis column. Order 1 is listed as Cartesian x, y, z (m = 1, -1, 0)."""
    out = [(1, 1), (1, -1), (1, 0)]
    for n in range(2, order + 1):
        out.extend((n, m) for m in range(-n, n + 1))
    return out


def _real_ylm(n: int, m: int, theta: np.ndarray, phi: np.ndarray) -> np.ndarray:
    if m == 0:
        return sph_harm_y(n, 0, theta, phi).real
    y = sph_harm_y(n, abs(m), theta, phi)
    return np.sqrt(2.0) * (-1.0) ** m * (y.real if m > 0 else y.imag)


def basis(points: np.ndarray, order: int, r0: float = R0_MM) -> np.ndarray:
    """(N, n_terms(order)) design matrix at RAS mm points."""
    if order < 1:
        raise ValueError("order must be >= 1")
    p = np.asarray(points, dtype=float) / r0
    x, y, z = p[:, 0], p[:, 1], p[:, 2]
    r = np.sqrt(x * x + y * y + z * z)
    with np.errstate(invalid="ignore", divide="ignore"):
        theta = np.where(r > 0, np.arccos(np.clip(z / np.where(r > 0, r, 1.0), -1, 1)), 0.0)
    phi = np.arctan2(y, x)
    cols = [x, y, z]
    for n in range(2, order + 1):
        rn = r**n
        cols.extend(rn * _real_ylm(n, m, theta, phi) for m in range(-n, n + 1))
    return np.column_stack(cols)


def _constraint(order: int) -> np.ndarray:
    """Z: free parameters -> vec(C), with vec index `c * K + k` (component-major).

    The order-1 block (rows 0..2 of C) is symmetric; every other entry is free.
    """
    k = n_terms(order)
    cols = []
    for i, j in _SYM:
        v = np.zeros(3 * k)
        v[j * k + i] = 1.0
        v[i * k + j] = 1.0
        cols.append(v)
    for c in range(3):
        for t in range(3, k):
            v = np.zeros(3 * k)
            v[c * k + t] = 1.0
            cols.append(v)
    return np.column_stack(cols)


def _rigid_design(points: np.ndarray, r0: float) -> np.ndarray:
    """(3N, 6): translation (3) and rotation w × p (3), stacked component-major."""
    p = np.asarray(points, dtype=float) / r0
    n = len(p)
    j = np.zeros((3, n, 6))
    for c in range(3):
        j[c, :, c] = 1.0
    x, y, z = p[:, 0], p[:, 1], p[:, 2]
    # w × p = (wy z − wz y, wz x − wx z, wx y − wy x); columns wx, wy, wz.
    j[0, :, 4], j[0, :, 5] = z, -y
    j[1, :, 3], j[1, :, 5] = -z, x
    j[2, :, 3], j[2, :, 4] = y, -x
    return j.reshape(3 * n, 6)


@dataclass(frozen=True)
class Field:
    """A fitted field: coefficients C, (n_terms(order), 3), in mm per basis unit."""

    order: int
    coef: np.ndarray
    r0: float = R0_MM

    def __call__(self, points: np.ndarray) -> np.ndarray:
        """Pull-back displacement (corrected -> acquired), (N, 3) mm."""
        return basis(points, self.order, self.r0) @ self.coef

    def acquired(self, corrected: np.ndarray) -> np.ndarray:
        p = np.asarray(corrected, dtype=float)
        return p + self(p)

    def corrected(self, acquired: np.ndarray, iters: int = 50, tol: float = 1e-6) -> np.ndarray:
        """Inverse map by fixed-point iteration, p = q − F(p). Converges where |∇F| < 1."""
        q = np.asarray(acquired, dtype=float)
        p = q.copy()
        for _ in range(iters):
            nxt = q - self(p)
            if np.max(np.abs(nxt - p)) < tol:
                return nxt
            p = nxt
        return p

    def to_dict(self) -> dict:
        return {
            "basis": "real regular solid harmonics (r/r0)^n Y_nm, per RAS component",
            "frame": "DICOM patient RAS mm, head-first supine, origin at isocentre",
            "direction": "pull-back: corrected scanner position -> acquired position",
            "excluded_terms": "order 0; antisymmetric (rotation) part of order 1",
            "order": self.order,
            "r0_mm": self.r0,
            "terms": [list(t) for t in term_index(self.order)],
            "coef_mm": np.asarray(self.coef).tolist(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Field:
        return cls(order=int(d["order"]), coef=np.asarray(d["coef_mm"], dtype=float), r0=d["r0_mm"])


def _session_normal(s: Session, order: int, z: np.ndarray, r0: float):
    """One session's contribution to the reduced normal equations (rigid eliminated)."""
    x = basis(s.points, order, r0)
    k = x.shape[1]
    d = np.asarray(s.disp, dtype=float)
    dvec = d.T.reshape(-1)  # component-major
    j = _rigid_design(s.points, r0)
    jr = j.reshape(3, len(d), 6)
    # (I3 ⊗ X)ᵀ J and (I3 ⊗ X)ᵀ d, without forming I3 ⊗ X.
    xtj = np.einsum("nk,cnj->ckj", x, jr).reshape(3 * k, 6)
    xtd = (x.T @ d).T.reshape(-1)
    g = x.T @ x
    ata = z.T @ np.kron(np.eye(3), g) @ z
    atj = z.T @ xtj
    atd = z.T @ xtd
    jtj = j.T @ j
    jtd = j.T @ dvec
    sol = np.linalg.solve(jtj, np.column_stack([atj.T, jtd]))
    return ata - atj @ sol[:, :-1], atd - atj @ sol[:, -1]


def fit(sessions: Iterable[Session], order: int, r0: float = R0_MM) -> Field:
    """Least-squares field over sessions, each with its own rigid nuisance."""
    z = _constraint(order)
    m = np.zeros((z.shape[1], z.shape[1]))
    r = np.zeros(z.shape[1])
    count = 0
    for s in sessions:
        dm, dr = _session_normal(s, order, z, r0)
        m += dm
        r += dr
        count += 1
    if count == 0:
        raise ValueError("no sessions")
    theta, *_ = np.linalg.lstsq(m, r, rcond=None)
    coef = (z @ theta).reshape(3, n_terms(order)).T
    return Field(order=order, coef=coef, r0=r0)


def residual_rms(field: Field | None, s: Session) -> float:
    """RMS vector length of what is left after the field and the session's own rigid fit.

    `field=None` gives the rigid-only residual.
    """
    d = np.asarray(s.disp, dtype=float)
    e = d - field(s.points) if field is not None else d
    j = _rigid_design(s.points, field.r0 if field is not None else R0_MM)
    evec = e.T.reshape(-1)
    beta, *_ = np.linalg.lstsq(j, evec, rcond=None)
    left = (evec - j @ beta).reshape(3, -1).T
    return float(np.sqrt(np.mean(np.sum(left * left, axis=1))))


def folds_by_group(sessions: Sequence[Session], k: int, seed: int) -> list[list[int]]:
    """Session indices per fold; every session of a group lands in the same fold."""
    groups = sorted({s.group for s in sessions})
    if len(groups) < k:
        raise ValueError(f"{len(groups)} groups for {k} folds")
    order = np.random.default_rng(seed).permutation(groups)
    fold_of = {g: i % k for i, g in enumerate(order)}
    out: list[list[int]] = [[] for _ in range(k)]
    for i, s in enumerate(sessions):
        out[fold_of[s.group]].append(i)
    return out


def choose_order(
    sessions: Sequence[Session],
    orders: Iterable[int] = range(3, 10),
    k: int = 5,
    seed: int = 0,
    r0: float = R0_MM,
) -> tuple[int, dict[int, tuple[float, float]]]:
    """Order by grouped k-fold cross-validation, with the one-standard-error rule.

    Each order's score is the mean over folds of the held-out sessions' residual RMS, with its
    standard error over folds. The choice is the lowest order whose mean is within one
    standard error of the best mean: a higher order must earn its extra terms.
    Returns (order, {order: (mean, se)}).
    """
    folds = folds_by_group(sessions, k, seed)
    scores: dict[int, tuple[float, float]] = {}
    for order in orders:
        per_fold = []
        for held in folds:
            held_set = set(held)
            train = [s for i, s in enumerate(sessions) if i not in held_set]
            f = fit(train, order, r0)
            per_fold.append(np.mean([residual_rms(f, sessions[i]) for i in held]))
        a = np.asarray(per_fold)
        scores[order] = (float(a.mean()), float(a.std(ddof=1) / np.sqrt(len(a))))
    best = min(scores, key=lambda o: scores[o][0])
    bar = scores[best][0] + scores[best][1]
    return min(o for o in scores if scores[o][0] <= bar), scores


def sphere_points(radius: float = 100.0, step: float = 5.0) -> np.ndarray:
    """A regular grid inside a sphere about isocentre, RAS mm."""
    ax = np.arange(-radius, radius + step / 2, step)
    g = np.stack(np.meshgrid(ax, ax, ax, indexing="ij"), axis=-1).reshape(-1, 3)
    return g[np.sum(g * g, axis=1) <= radius * radius]


def rms(field: Field, points: np.ndarray) -> float:
    v = field(points)
    return float(np.sqrt(np.mean(np.sum(v * v, axis=1))))


def rms_difference(a: Field, b: Field, points: np.ndarray) -> float:
    v = a(points) - b(points)
    return float(np.sqrt(np.mean(np.sum(v * v, axis=1))))
