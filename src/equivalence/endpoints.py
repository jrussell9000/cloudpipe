"""The two tiers of endpoint, computed on a shared mask and a shared grid.

Tier 1 is spatial agreement of the preprocessed image; tier 2 is nuisance and
quality agreement. Both carry the claim — the derived-analysis tier is deferred
(design D6), so these are the result rather than its explanation.

Three rules hold throughout, and each exists because of a way this comparison can
be made to look better than it is:

- **The mask is the intersection of the two arms' brain masks, and its Dice is
  reported alongside every masked number.** A high correlation computed over a
  small intersection is a high correlation over the easy voxels.
- **Neither arm is resampled onto a third grid.** Both pipelines were asked to
  write `MNI152NLin2009cAsym` at native BOLD resolution (D5), so a grid
  disagreement is an error to raise, not something to interpolate away.
- **Subcortical grayordinates are not an endpoint.** fMRIPrep samples them on a
  fixed grid derived from `MNI152NLin6Asym` and cloudpipe's are on
  `MNI152NLin2009cAsym` by design; no `--output-spaces` value reconciles them, so
  a whole-brain grayordinate figure is refused rather than reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import nibabel as nib
import numpy as np

from .confound_map import (
    CLOUDPIPE,
    FMRIPREP,
    columns_in_family,
    principal_angles,
    subspace_similarity,
)
from .equivalence import bland_altman, icc_2_1, tost

#: The cortical block of a 91k-grayordinate CIFTI: standard fsLR, both arms.
CORTICAL_GRAYORDINATES = 59412

#: The declared grayordinate endpoint is the lower TAIL of the per-grayordinate
#: correlation, not its median. The median sits at ~0.99 with negligible
#: between-run spread, so it certifies agreement whatever the arms did; the tail
#: is where two pipelines actually differ and where the between-run variance that
#: powers the test lives. Declared here, before any result, because an endpoint
#: swapped once the numbers are known is exploratory (spec).
GRAYORDINATE_TAIL_PERCENTILE = 5

#: Tier-2 metrics, from the spec. Order is the order the results table reports.
TIER2_METRICS: tuple[str, ...] = (
    "mean_fd",
    "max_fd",
    "mean_dvars",
    "gcor",
    "aor",
    "aqi",
    "tsnr_median",
)


class GridMismatch(ValueError):
    """Two arms' images are not on the same grid, so they were not compared."""


class RefusedEndpoint(ValueError):
    """A comparison the harness will not perform, with the reason it will not."""


@dataclass(frozen=True)
class MaskedComparison:
    """One masked spatial endpoint, with the intersection it was computed over."""

    name: str
    correlation: float
    dice: float
    n_voxels: int

    def __str__(self) -> str:
        return (
            f"{self.name}: r={self.correlation:.4f} over {self.n_voxels} voxels "
            f"(mask Dice {self.dice:.4f})"
        )


@dataclass(frozen=True)
class Tier1Result:
    """Spatial agreement for one run."""

    mean_image: MaskedComparison
    tsnr_map: MaskedComparison
    mask_dice: float
    # Signed, cloudpipe - reference. The spec's tier-1 figure is the absolute
    # difference, which is `abs()` of this; keeping the sign costs nothing and an
    # absolute value would tabulate a cloudpipe tSNR advantage identically to a
    # shortfall of the same size.
    tsnr_median_difference: float

    @property
    def tsnr_median_absolute_difference(self) -> float:
        return abs(self.tsnr_median_difference)

    def __str__(self) -> str:
        return (
            f"{self.mean_image}\n{self.tsnr_map}\n"
            f"brain-mask Dice {self.mask_dice:.4f}; "
            f"Δ tsnr_median {self.tsnr_median_difference:+.3f}"
        )


def dice(mask_a: np.ndarray, mask_b: np.ndarray) -> float:
    """Dice coefficient of two masks binarized at > 0."""
    a = (np.asarray(mask_a) > 0).ravel()
    b = (np.asarray(mask_b) > 0).ravel()
    denominator = float(a.sum() + b.sum())
    return float(2 * np.count_nonzero(a & b) / denominator) if denominator > 0 else 0.0


def _require_same_grid(a: nib.Nifti1Image, b: nib.Nifti1Image, what: str) -> None:
    if a.shape[:3] != b.shape[:3]:
        raise GridMismatch(
            f"{what}: shapes {a.shape[:3]} and {b.shape[:3]} differ. Both arms were "
            "asked to write MNI152NLin2009cAsym at native BOLD resolution (D5); "
            "resampling one onto the other here would hide that they did not."
        )
    if not np.allclose(a.affine, b.affine, atol=1e-3):
        raise GridMismatch(
            f"{what}: affines differ by more than 1e-3 mm. The comparison is defined "
            "on a shared grid, and an interpolation belonging to neither pipeline is "
            "not a correction for this."
        )


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    """Pearson correlation, 0.0 where either side is constant."""
    x = np.asarray(x, dtype=np.float64).ravel()
    y = np.asarray(y, dtype=np.float64).ravel()
    x = x - x.mean()
    y = y - y.mean()
    denominator = float(np.sqrt(float(x @ x) * float(y @ y)))
    return float(x @ y) / denominator if denominator > 0 else 0.0


def temporal_mean(bold: np.ndarray) -> np.ndarray:
    return np.asarray(bold, dtype=np.float32).mean(axis=-1)


def tsnr_map(bold: np.ndarray) -> np.ndarray:
    """Voxelwise mean / population std over time, 0 where std is 0."""
    data = np.asarray(bold, dtype=np.float32)
    with np.errstate(divide="ignore", invalid="ignore"):
        std = data.std(axis=-1)
        return np.where(std > 0, data.mean(axis=-1) / std, 0.0)


def tier1(
    bold_a,
    mask_a,
    bold_b,
    mask_b,
    tsnr_median_a: float | None = None,
    tsnr_median_b: float | None = None,
) -> Tier1Result:
    """Tier-1 spatial endpoints for one run, over the two masks' intersection.

    Arguments are paths or loaded images; nothing here knows which arm is which,
    beyond the order of the pair.
    """
    image_a = bold_a if hasattr(bold_a, "get_fdata") else nib.load(bold_a)
    image_b = bold_b if hasattr(bold_b, "get_fdata") else nib.load(bold_b)
    mask_image_a = mask_a if hasattr(mask_a, "get_fdata") else nib.load(mask_a)
    mask_image_b = mask_b if hasattr(mask_b, "get_fdata") else nib.load(mask_b)

    _require_same_grid(image_a, image_b, "MNI BOLD")
    _require_same_grid(mask_image_a, mask_image_b, "brain mask")

    binary_a = np.asanyarray(mask_image_a.dataobj) > 0
    binary_b = np.asanyarray(mask_image_b.dataobj) > 0
    shared = binary_a & binary_b
    mask_dice = dice(binary_a, binary_b)
    if not shared.any():
        raise RefusedEndpoint(
            "the two arms' brain masks do not intersect; there is no shared mask to "
            f"compare on (Dice {mask_dice:.4f})"
        )

    data_a = np.asanyarray(image_a.dataobj)
    data_b = np.asanyarray(image_b.dataobj)

    mean_comparison = MaskedComparison(
        name="mean image voxelwise correlation",
        correlation=_pearson(temporal_mean(data_a)[shared], temporal_mean(data_b)[shared]),
        dice=mask_dice,
        n_voxels=int(shared.sum()),
    )
    tsnr_comparison = MaskedComparison(
        name="tSNR map voxelwise correlation",
        correlation=_pearson(tsnr_map(data_a)[shared], tsnr_map(data_b)[shared]),
        dice=mask_dice,
        n_voxels=int(shared.sum()),
    )

    if tsnr_median_a is None:
        tsnr_median_a = float(np.median(tsnr_map(data_a)[shared]))
    if tsnr_median_b is None:
        tsnr_median_b = float(np.median(tsnr_map(data_b)[shared]))

    return Tier1Result(
        mean_image=mean_comparison,
        tsnr_map=tsnr_comparison,
        mask_dice=mask_dice,
        tsnr_median_difference=float(tsnr_median_a) - float(tsnr_median_b),
    )


@dataclass(frozen=True)
class GrayordinateResult:
    """Cortical grayordinate agreement: 59412 of them, and no more.

    `tail_temporal_correlation` is the declared endpoint — the
    `GRAYORDINATE_TAIL_PERCENTILE`th percentile of the per-grayordinate
    correlation. The median is descriptive: near the ceiling it discriminates
    nothing.
    """

    n_grayordinates: int
    tail_temporal_correlation: float
    tail_percentile: int
    median_temporal_correlation: float
    temporal_correlation_percentiles: dict[int, float]
    mean_agreement: float

    def __str__(self) -> str:
        return (
            f"{self.n_grayordinates} cortical grayordinates: "
            f"p{self.tail_percentile} temporal r={self.tail_temporal_correlation:.4f} "
            f"(the endpoint); median {self.median_temporal_correlation:.4f} "
            f"and temporal-mean r={self.mean_agreement:.4f} (descriptive)"
        )


def cortical_grayordinates(
    dtseries_a, dtseries_b, n_cortical: int = CORTICAL_GRAYORDINATES
) -> GrayordinateResult:
    """Per-grayordinate temporal correlation over the cortical block only.

    Both arms' CIFTI carries the standard fsLR cortical block first, so the first
    `n_cortical` rows are comparable positionally. Anything past them is not: see
    `refuse_subcortical_comparison`.

    The declared endpoint is the tail percentile, not the median: a median near 1
    can sit above a tail of grayordinates where the two arms disagree completely,
    and that tail is both the finding and the only part of this distribution with
    enough between-run variance to test.
    """
    a = _dtseries_array(dtseries_a)
    b = _dtseries_array(dtseries_b)

    for array, label in ((a, "first"), (b, "second")):
        if array.shape[0] < n_cortical:
            raise RefusedEndpoint(
                f"the {label} dtseries has {array.shape[0]} grayordinates, fewer than the "
                f"{n_cortical} cortical ones this endpoint is defined on"
            )
    if a.shape[1] != b.shape[1]:
        raise GridMismatch(
            f"frame counts differ, {a.shape[1]} and {b.shape[1]}; a per-grayordinate "
            "temporal correlation is not defined across different time bases"
        )

    cortex_a = a[:n_cortical]
    cortex_b = b[:n_cortical]
    correlations = np.array(
        [_pearson(cortex_a[i], cortex_b[i]) for i in range(n_cortical)], dtype=np.float64
    )

    percentiles = {p: float(np.percentile(correlations, p)) for p in (1, 5, 25, 50, 75, 95, 99)}
    return GrayordinateResult(
        n_grayordinates=n_cortical,
        tail_temporal_correlation=percentiles[GRAYORDINATE_TAIL_PERCENTILE],
        tail_percentile=GRAYORDINATE_TAIL_PERCENTILE,
        median_temporal_correlation=percentiles[50],
        temporal_correlation_percentiles=percentiles,
        mean_agreement=_pearson(cortex_a.mean(axis=1), cortex_b.mean(axis=1)),
    )


def refuse_subcortical_comparison() -> None:
    """Always raises. The refusal the spec requires, with its reason attached."""
    raise RefusedEndpoint(
        "the subcortical grayordinate blocks are not comparable: fMRIPrep samples "
        "subcortical time series on a fixed grid derived from MNI152NLin6Asym, while "
        "cloudpipe's subcortical block is on MNI152NLin2009cAsym by design, which is why "
        "its grayordinate total is not 91282. No --output-spaces value reconciles them, "
        "so no whole-brain grayordinate agreement figure spanning both blocks is reported."
    )


def _dtseries_array(dtseries) -> np.ndarray:
    """`(n_grayordinates, n_frames)` from a CIFTI path, image or array.

    CIFTI dtseries are stored time-major, so a loaded image transposes.
    """
    if isinstance(dtseries, np.ndarray):
        return dtseries
    image = dtseries if hasattr(dtseries, "get_fdata") else nib.load(dtseries)
    return np.asarray(image.get_fdata()).T


@dataclass(frozen=True)
class Tier2Result:
    """Nuisance and quality agreement across runs, per metric.

    `equivalence` holds the pre-registered decision per metric, where a margin was
    supplied. Each result carries a `departure` naming which arm is higher, so a
    tabulated "equivalence not established" separates a difference one way from a
    difference the other and from an interval too wide to place.
    """

    icc: dict
    bland_altman: dict
    equivalence: dict = field(default_factory=dict)
    acompcor_principal_angles: np.ndarray | None = None
    acompcor_subspace_similarity: float | None = None
    n_cosines_agreement: float | None = None
    n_cosines_disagreements: tuple[tuple[int, int], ...] = ()


def tier2(
    records_a: list[dict],
    records_b: list[dict],
    metrics: tuple[str, ...] = TIER2_METRICS,
    margins: dict[str, float] | None = None,
) -> Tier2Result:
    """ICC(2,1) and Bland-Altman for each tier-2 metric, plus `n_cosines` agreement.

    The two record lists must be in the same run order — they are paired, one
    record per run per arm, which is what the pairing in ICC and Bland-Altman
    means here. `records_a` is cloudpipe, so every difference is
    `cloudpipe - reference`.

    Where `margins` carries a metric, its pre-registered TOST decision is computed
    too. Each result records which arm is higher, factually, so a tabulated
    "equivalence not established" is not one undifferentiated failure — the study
    still claims equivalence and calls no direction better.
    """
    if len(records_a) != len(records_b):
        raise ValueError(f"paired records differ in length, {len(records_a)} vs {len(records_b)}")

    icc_results = {}
    agreement_results = {}
    equivalence_results = {}
    for metric in metrics:
        values_a = [float(r[metric]) for r in records_a]
        values_b = [float(r[metric]) for r in records_b]
        icc_results[metric] = icc_2_1(values_a, values_b, endpoint=metric)
        agreement_results[metric] = bland_altman(values_a, values_b, endpoint=metric)
        if margins and metric in margins:
            equivalence_results[metric] = tost(
                [a - b for a, b in zip(values_a, values_b, strict=True)],
                margin=margins[metric],
                endpoint=metric,
            )

    cosines = [
        (int(a["n_cosines"]), int(b["n_cosines"]))
        for a, b in zip(records_a, records_b, strict=True)
    ]
    disagreements = tuple((x, y) for x, y in cosines if x != y)
    agreement = 1.0 - len(disagreements) / len(cosines) if cosines else None

    return Tier2Result(
        icc=icc_results,
        bland_altman=agreement_results,
        equivalence=equivalence_results,
        n_cosines_agreement=agreement,
        n_cosines_disagreements=disagreements,
    )


def acompcor_agreement(confounds_a, confounds_b) -> tuple[np.ndarray, float]:
    """Principal angles and mean cosine between the two arms' aCompCor subspaces.

    Never a column pairing: cloudpipe runs PCA per tissue mask and fMRIPrep on a
    merged mask, so component *i* of one is not component *i* of the other (D3).
    """
    columns_a = columns_in_family(list(confounds_a.columns), "a_comp_cor", CLOUDPIPE)
    columns_b = columns_in_family(list(confounds_b.columns), "a_comp_cor", FMRIPREP)
    if not columns_a or not columns_b:
        raise RefusedEndpoint(
            f"no aCompCor regressors on one side: {len(columns_a)} and {len(columns_b)} "
            "columns found, so there is no subspace to compare"
        )
    a = confounds_a[columns_a].to_numpy(dtype=np.float64)
    b = confounds_b[columns_b].to_numpy(dtype=np.float64)
    return principal_angles(a, b), subspace_similarity(a, b)
