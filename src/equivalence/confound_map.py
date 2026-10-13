"""The two pipelines' confound vocabularies, as a reviewed table with a refusal list.

Name matching across the two confounds TSVs is wrong in at least three places and
two of them are silent (design D3), so the mapping is data, reviewed once, and an
unmapped pair raises instead of falling back to the names.

The one case that must never be mapped: cloudpipe's `FuncQC.dvars_std` is
`mean_dvars / mean_global_signal * 100`, a single percent-signal-change scalar,
while fMRIPrep's `std_dvars` is a per-frame standardized series. Same-looking
name, different quantity, different shape.

aCompCor is mapped as a *subspace*, never column by column: cloudpipe runs PCA
per tissue mask and fMRIPrep runs it on a merged mask, so component *i* of one is
not component *i* of the other — component order follows each input covariance
matrix. `principal_angles` is the comparison that holds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

CLOUDPIPE = "cloudpipe"
FMRIPREP = "fmriprep"
VOCABULARIES = (CLOUDPIPE, FMRIPREP)


class UnmappedColumn(KeyError):
    """A column, or a column pair, that the reviewed table does not cover."""


class RefusedComparison(ValueError):
    """A pair that looks comparable by name and is not the same measurement."""


@dataclass(frozen=True)
class ColumnPair:
    """One row of the reviewed table.

    `canonical` is the name the harness measures under. `pattern` members are
    regular expressions where a pipeline emits a numbered family, so the count
    rather than the index is what is comparable.
    """

    canonical: str
    cloudpipe: str
    fmriprep: str
    relationship: str
    is_family: bool = False

    def column_for(self, vocabulary: str) -> str:
        if vocabulary == CLOUDPIPE:
            return self.cloudpipe
        if vocabulary == FMRIPREP:
            return self.fmriprep
        raise UnmappedColumn(f"unknown vocabulary {vocabulary!r}; expected one of {VOCABULARIES}")


#: The reviewed table. Every pair the spec names, and nothing inferred.
MAPPING: tuple[ColumnPair, ...] = (
    ColumnPair(
        canonical="framewise_displacement",
        cloudpipe="framewise_displacement",
        fmriprep="framewise_displacement",
        relationship=(
            "Same definition (Power et al. 2012), different motion estimates upstream: "
            "cloudpipe reads ABCD's 3dvolreg parameters, fMRIPrep re-estimates with mcflirt. "
            "That difference is a finding, which is why both arms are recomputed from "
            "outputs rather than compared on their self-reported records."
        ),
    ),
    ColumnPair(
        canonical="dvars",
        cloudpipe="dvars",
        fmriprep="dvars",
        relationship="Same quantity.",
    ),
    ColumnPair(
        canonical="global_signal",
        cloudpipe="global_signal",
        fmriprep="global_signal",
        relationship="Same quantity, different brain mask. The masks' Dice is reported with it.",
    ),
    ColumnPair(
        canonical="cosine",
        cloudpipe=r"^cosine_\d+$",
        fmriprep=r"^cosine\d+$",
        relationship=(
            "Same discrete-cosine basis; the count depends on run length and TR, so the "
            "comparable quantity is the number of regressors, not a column pairing."
        ),
        is_family=True,
    ),
    ColumnPair(
        canonical="a_comp_cor",
        cloudpipe=r"^a_comp_cor_(wm|csf)_\d+$",
        fmriprep=r"^a_comp_cor_\d+$",
        relationship=(
            "NOT the same regressors: cloudpipe runs PCA per tissue mask (WM and CSF "
            "separately), fMRIPrep runs it on a merged mask. Comparable as subspaces via "
            "principal_angles, never column by column."
        ),
        is_family=True,
    ),
    ColumnPair(
        canonical="t_comp_cor",
        cloudpipe=r"^t_comp_cor_\d+$",
        fmriprep=r"^t_comp_cor_\d+$",
        relationship=(
            "Same idea (PCA over the highest-variance voxels), different voxel selection. "
            "Comparable as subspaces, like a_comp_cor."
        ),
        is_family=True,
    ),
)

#: Pairs that name-matching would join and that are not the same measurement.
#: Keyed `(cloudpipe name, fMRIPrep name)`; the value is what the raise must say.
REFUSALS: dict[tuple[str, str], str] = {
    ("dvars_std", "std_dvars"): (
        "cloudpipe's dvars_std is one percent-signal-change scalar "
        "(mean_dvars / mean_global_signal * 100, FuncQC schema 1.1); fMRIPrep's std_dvars "
        "is a per-frame standardized DVARS series. Different quantities and different "
        "shapes — there is no comparison to make between them."
    ),
    ("a_comp_cor_wm_00", "a_comp_cor_00"): (
        "cloudpipe's aCompCor components come from a per-tissue PCA and fMRIPrep's from a "
        "merged-mask PCA, so component order is not shared. Compare the subspaces with "
        "principal_angles instead of pairing components by index."
    ),
}


def _pair_for_canonical(canonical: str) -> ColumnPair:
    for pair in MAPPING:
        if pair.canonical == canonical:
            return pair
    raise UnmappedColumn(
        f"{canonical!r} is not in the reviewed mapping table. "
        f"Known: {sorted(p.canonical for p in MAPPING)}"
    )


def canonical_name(column: str, vocabulary: str) -> str:
    """The canonical name for one pipeline's column.

    Raises rather than guessing: a column absent from the table is a column whose
    cross-pipeline meaning has not been reviewed.
    """
    for pair in MAPPING:
        own = pair.column_for(vocabulary)
        if pair.is_family:
            if re.match(own, column):
                return pair.canonical
        elif own == column:
            return pair.canonical
    raise UnmappedColumn(
        f"{column!r} ({vocabulary}) is not in the reviewed mapping table "
        f"({[p.canonical for p in MAPPING]}). Add a reviewed row, or put the pair on "
        "the refusal list; do not fall back to matching names."
    )


def columns_in_family(columns: list[str], canonical: str, vocabulary: str) -> list[str]:
    """Every column of a numbered family, in the order the TSV carries them."""
    pair = _pair_for_canonical(canonical)
    if not pair.is_family:
        own = pair.column_for(vocabulary)
        return [c for c in columns if c == own]
    pattern = pair.column_for(vocabulary)
    return [c for c in columns if re.match(pattern, c)]


def comparable_pair(cloudpipe_column: str, fmriprep_column: str) -> ColumnPair:
    """The table row licensing a comparison of these two columns.

    Raises `RefusedComparison` for a pair on the refusal list, naming both
    quantities, and `UnmappedColumn` for a pair the table does not cover.
    """
    refusal = REFUSALS.get((cloudpipe_column, fmriprep_column))
    if refusal is not None:
        raise RefusedComparison(
            f"refusing to compare cloudpipe {cloudpipe_column!r} against fMRIPrep "
            f"{fmriprep_column!r}: {refusal}"
        )

    left = canonical_name(cloudpipe_column, CLOUDPIPE)
    right = canonical_name(fmriprep_column, FMRIPREP)
    if left != right:
        raise UnmappedColumn(
            f"cloudpipe {cloudpipe_column!r} maps to {left!r} and fMRIPrep "
            f"{fmriprep_column!r} to {right!r}; the table does not license this pair"
        )
    return _pair_for_canonical(left)


def to_canonical(confounds, vocabulary: str):
    """Return a confounds table holding only reviewed columns, canonically named.

    Columns outside the table are dropped rather than carried under a name that
    has not been reviewed; the dropped names are on the returned frame's
    `attrs["dropped_columns"]`, so what was left behind is visible rather than
    absent. fMRIPrep's `std_dvars` leaves this way: it is on the refusal list, and
    carrying it beside cloudpipe's `dvars_std` is the mistake D3 exists to stop.

    `confounds` is a pandas DataFrame; pandas is not imported here, because this
    module is also read inside the reference image, which has its own environment.
    """
    scalar_renames: dict[str, str] = {}
    families: dict[tuple[str, str], list[str]] = {}
    for column in confounds.columns:
        try:
            canonical = canonical_name(column, vocabulary)
        except UnmappedColumn:
            continue
        pair = _pair_for_canonical(canonical)
        if pair.is_family:
            families.setdefault((canonical, _tissue_of(column)), []).append(column)
        else:
            scalar_renames[column] = canonical

    # Families keep each pipeline's own ordering but take cloudpipe's prefix
    # spelling, which is what func_iqm counts (`a_comp_cor_wm_`, `cosine_`, …).
    # Numbering is per tissue, so cloudpipe's WM and CSF components keep their own
    # indices. fMRIPrep's aCompCor comes from a merged mask and so has no tissue;
    # it lands under the WM prefix purely so the count has somewhere to go. The
    # count is not the aCompCor comparison — principal_angles is.
    family_renames: dict[str, str] = {}
    for (canonical, tissue), columns in families.items():
        prefix = f"a_comp_cor_{tissue}" if canonical == "a_comp_cor" else canonical
        for index, column in enumerate(sorted(columns)):
            family_renames[column] = f"{prefix}_{index:02d}"

    keep = [c for c in confounds.columns if c in scalar_renames or c in family_renames]
    out = confounds[keep].rename(columns={**scalar_renames, **family_renames})
    out.attrs["dropped_columns"] = [c for c in confounds.columns if c not in keep]
    return out


def _tissue_of(column: str) -> str:
    """`wm`, `csf`, or `wm` for a merged-mask component that names no tissue."""
    match = re.search(r"_(wm|csf)_", column)
    return match.group(1) if match else "wm"


def principal_angles(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Principal angles (radians, ascending) between the column spaces of `a` and `b`.

    Björck & Golub (1973): orthonormalise both bases, take the singular values of
    `Qa.T @ Qb` — those are the cosines of the principal angles. The first angle
    is the smallest rotation between the subspaces; the last bounds how much of
    one subspace the other fails to span.

    This is the honest aCompCor comparison. Pairing component *i* against
    component *i* would measure the stability of PCA's component ordering under a
    different input covariance matrix, which is not a property of either pipeline.

    Returns `min(rank(a), rank(b))` angles. Constant or empty regressors are
    dropped before orthonormalisation, so a column of zeros neither contributes a
    dimension nor raises.

    One numerical caveat worth knowing before reading a small number here: for
    nearly coincident subspaces the cosines sit at `1 - O(eps)`, and `arccos`
    turns that into an angle of order `sqrt(eps)` ~ 1e-8 rad rather than zero. So
    angles below about 1e-7 rad mean "indistinguishable", not a measured
    rotation. `subspace_similarity` reads the cosines directly and does not carry
    that amplification.
    """
    qa = _orthonormal_basis(a)
    qb = _orthonormal_basis(b)
    if qa.size == 0 or qb.size == 0:
        return np.empty(0)
    cosines = np.linalg.svd(qa.T @ qb, compute_uv=False)
    return np.arccos(np.clip(cosines, -1.0, 1.0))


def subspace_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Mean cosine of the principal angles: 1.0 for identical subspaces, 0.0 for orthogonal.

    A single number for the results table. The angles themselves are what the
    report carries, because a high mean can hide one direction that is missed
    entirely.
    """
    angles = principal_angles(a, b)
    return float(np.mean(np.cos(angles))) if angles.size else 0.0


def _orthonormal_basis(matrix: np.ndarray) -> np.ndarray:
    """An orthonormal basis for the columns of `matrix`, rank-truncated.

    Deliberately NOT demeaned. aCompCor regressors are PCA scores and are already
    mean-centred by construction, and removing a mean here would change the span
    being compared — two subspaces that are orthogonal would stop reading as
    orthogonal. Rank truncation drops directions a constant or duplicated
    regressor contributes, so a column of zeros neither raises nor counts.
    """
    m = np.asarray(matrix, dtype=np.float64)
    if m.ndim == 1:
        m = m[:, None]
    if m.size == 0:
        return np.empty((m.shape[0] if m.ndim == 2 else 0, 0))
    keep = np.linalg.norm(m, axis=0) > 0
    m = m[:, keep]
    if m.shape[1] == 0:
        return np.empty((m.shape[0], 0))
    u, s, _ = np.linalg.svd(m, full_matrices=False)
    rank = int(np.sum(s > s[0] * 1e-10)) if s.size else 0
    return u[:, :rank]
