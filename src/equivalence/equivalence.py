"""Equivalence by TOST, agreement by ICC and Bland-Altman, and n as a constraint.

A two-sided test that fails to reject is not evidence of equivalence, so the
decision rule is two one-sided tests against a margin committed before any result
is computed (design D7). Reporting is per endpoint: there is no single pass/fail,
and "equivalence not established" for one endpoint is a result to publish.

The order D7 fixes, and this module's three parts follow it:

1. the margin exists first (`margins.py` reads the committed file);
2. the pilot's variance converts a margin into the sample size it requires
   (`required_n`);
3. that n is checked against the budget *before* the main run — a shortfall is a
   finding to report, not a reason to run underpowered.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats

#: TOST at this one-sided alpha corresponds to the 90% interval the spec reports.
DEFAULT_ALPHA = 0.05

EQUIVALENT = "equivalent"
NOT_ESTABLISHED = "equivalence not established"
NON_INFERIOR = "non-inferior"
NI_NOT_ESTABLISHED = "non-inferiority not established"

#: Correlations are tested on the Fisher z scale. Near the ceiling r is badly
#: skewed — dz/dr is 10 at r = 0.95 and 25 at r = 0.98 — so a symmetric interval
#: on r is not a symmetric interval on the quantity that is approximately normal.
FISHER_Z = "fisher_z"
#: |r| = 1 sends arctanh to infinity. A per-run endpoint can land there on a
#: degenerate run, so it is clipped rather than allowed to poison the mean.
_R_CLIP = 1.0 - 1e-7

# Where a departure from equivalence points. Strictly FACTUAL: which arm's value
# is higher, never which arm is better.
#
# The study claims equivalence and does not contest fMRIPrep, which is the field's
# reference implementation. What this adds is only the ability to tell apart the
# reasons equivalence was not established — without it, a results table records a
# difference in either direction as one undifferentiated failure, which penalises
# cloudpipe for a difference a reader might well read in its favour. The reading
# is left to the reader: both arms' values are reported side by side and no
# direction is labelled as the better one.
#
# One fact the write-up states so a reader does not supply the wrong
# interpretation: the arms' `mean_fd`, `max_fd` and `mean_dvars` come from
# different motion estimators (ABCD's `3dvolreg` against fMRIPrep's `mcflirt`), so
# a difference there is an estimator difference and not a data-quality one.
DEPARTURE_NONE = "none"
DEPARTURE_CLOUDPIPE_HIGHER = "cloudpipe higher"
DEPARTURE_REFERENCE_HIGHER = "reference higher"
DEPARTURE_UNRESOLVED = "wider than the margin, direction unresolved"


@dataclass(frozen=True)
class EquivalenceResult:
    """One endpoint's verdict, with everything a reader needs to check it.

    `estimate` is signed, `cloudpipe - reference`. `departure` says where a
    departure points and is descriptive only — `verdict` carries the
    pre-registered decision and is computed from the margin alone.
    """

    endpoint: str
    n: int
    estimate: float
    ci_low: float
    ci_high: float
    margin: float
    p_value: float
    alpha: float
    verdict: str
    departure: str = DEPARTURE_NONE

    @property
    def is_equivalent(self) -> bool:
        return self.verdict == EQUIVALENT

    @property
    def cloudpipe_higher(self) -> bool:
        return self.departure == DEPARTURE_CLOUDPIPE_HIGHER

    def __str__(self) -> str:
        tail = "" if self.departure == DEPARTURE_NONE else f" ({self.departure})"
        return (
            f"{self.endpoint}: {self.estimate:+.4g} "
            f"[{self.ci_low:+.4g}, {self.ci_high:+.4g}] "
            f"vs ±{self.margin:.4g} (n={self.n}) — {self.verdict}{tail}"
        )


def classify_departure(
    ci_low: float,
    ci_high: float,
    margin: float,
    verdict: str,
) -> str:
    """Which arm's value is higher, read off the same interval.

    No extra test and no alpha spent: each bound of the `1 - 2*alpha` interval is
    already a one-sided `1 - alpha` statement, so the interval that decides
    equivalence also says which side a departure sits on.

    Factual only. No direction is called better, for any metric — the study does
    not contest fMRIPrep, and a reader with a view on which direction is
    preferable has both arms' values in front of them.
    """
    if verdict == EQUIVALENT:
        return DEPARTURE_NONE
    if ci_low > margin:
        return DEPARTURE_CLOUDPIPE_HIGHER
    if ci_high < -margin:
        return DEPARTURE_REFERENCE_HIGHER
    return DEPARTURE_UNRESOLVED


def tost(
    differences,
    margin: float,
    endpoint: str = "",
    alpha: float = DEFAULT_ALPHA,
) -> EquivalenceResult:
    """Two one-sided tests on paired arm differences against ±`margin`.

    `differences` are per-unit `cloudpipe - reference` values, so the test is
    paired by construction — both arms processed the same run.

    The result carries a `departure` saying which arm is higher where equivalence
    was not established (see `classify_departure`). It does not touch the decision.

    The reported interval is the `1 - 2*alpha` confidence interval (90% at the
    default alpha), which is the interval TOST's decision is equivalent to: the
    verdict is `equivalent` exactly when that interval lies wholly inside the
    margin. Reporting the interval rather than only the p-value is what lets a
    reader see an inconclusive result as inconclusive rather than as a null.
    """
    d = np.asarray(differences, dtype=np.float64)
    d = d[np.isfinite(d)]
    n = d.size
    if n < 2:
        raise ValueError(f"{endpoint or 'endpoint'}: TOST needs at least 2 paired values, got {n}")
    if margin <= 0:
        raise ValueError(f"{endpoint or 'endpoint'}: margin must be positive, got {margin}")

    mean = float(d.mean())
    se = float(d.std(ddof=1) / np.sqrt(n))
    df = n - 1

    if se == 0:
        # Every pair identical. The interval is a point, and the verdict follows
        # from whether that point is inside the margin; no test is needed.
        verdict = EQUIVALENT if abs(mean) < margin else NOT_ESTABLISHED
        return EquivalenceResult(
            endpoint,
            n,
            mean,
            mean,
            mean,
            margin,
            0.0 if verdict == EQUIVALENT else 1.0,
            alpha,
            verdict,
            classify_departure(mean, mean, margin, verdict),
        )

    t_lower = (mean + margin) / se
    t_upper = (mean - margin) / se
    p_lower = float(stats.t.sf(t_lower, df))  # H0: difference <= -margin
    p_upper = float(stats.t.cdf(t_upper, df))  # H0: difference >= +margin
    p_value = max(p_lower, p_upper)

    half_width = float(stats.t.ppf(1 - alpha, df)) * se
    ci_low, ci_high = mean - half_width, mean + half_width
    verdict = EQUIVALENT if (ci_low > -margin and ci_high < margin) else NOT_ESTABLISHED

    return EquivalenceResult(
        endpoint=endpoint,
        n=n,
        estimate=mean,
        ci_low=ci_low,
        ci_high=ci_high,
        margin=margin,
        p_value=p_value,
        alpha=alpha,
        verdict=verdict,
        departure=classify_departure(ci_low, ci_high, margin, verdict),
    )


@dataclass(frozen=True)
class IccResult:
    """ICC(2,1) — two-way random effects, single measurement, absolute agreement."""

    endpoint: str
    n: int
    icc: float
    ci_low: float
    ci_high: float
    confidence: float

    def __str__(self) -> str:
        return (
            f"{self.endpoint}: ICC(2,1) {self.icc:.3f} "
            f"[{self.ci_low:.3f}, {self.ci_high:.3f}] (n={self.n})"
        )


def icc_2_1(arm_a, arm_b, endpoint: str = "", confidence: float = 0.95) -> IccResult:
    """ICC(2,1) with a confidence interval, for two arms measured on the same runs.

    Absolute agreement, not consistency: a constant offset between the two
    pipelines is a disagreement for this comparison's purposes, and ICC(3,1)
    would score it as perfect.

    The interval follows McGraw & Wong (1996), the ICC(A,1) row — F-based, with
    the Satterthwaite degrees of freedom their table gives. Shrout & Fleiss
    (1979) is the ICC(2,1) naming.
    """
    a = np.asarray(arm_a, dtype=np.float64)
    b = np.asarray(arm_b, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"{endpoint or 'endpoint'}: arms differ in length, {a.size} vs {b.size}")
    paired = np.isfinite(a) & np.isfinite(b)
    a, b = a[paired], b[paired]
    n = a.size
    if n < 3:
        raise ValueError(f"{endpoint or 'endpoint'}: ICC needs at least 3 paired values, got {n}")

    ratings = np.column_stack([a, b])
    k = 2  # two arms
    grand_mean = ratings.mean()
    # Two-way ANOVA without replication: subject rows, arm columns.
    ss_rows = k * float(((ratings.mean(axis=1) - grand_mean) ** 2).sum())
    ss_cols = n * float(((ratings.mean(axis=0) - grand_mean) ** 2).sum())
    ss_total = float(((ratings - grand_mean) ** 2).sum())
    ss_error = ss_total - ss_rows - ss_cols

    ms_rows = ss_rows / (n - 1)
    ms_cols = ss_cols / (k - 1)
    ms_error = ss_error / ((n - 1) * (k - 1))

    denominator = ms_rows + (k - 1) * ms_error + k * (ms_cols - ms_error) / n
    icc = (ms_rows - ms_error) / denominator if denominator != 0 else 0.0

    alpha = 1 - confidence
    if ms_error == 0 or denominator == 0:
        # Perfect agreement leaves no residual to build an interval from.
        return IccResult(endpoint, n, float(icc), float(icc), float(icc), confidence)

    f_observed = ms_rows / ms_error
    # Satterthwaite df for the mixed denominator (McGraw & Wong 1996, Table 7).
    a_term = k * icc / (n * (1 - icc)) if icc < 1 else np.inf
    b_term = 1 + k * icc * (n - 1) / (n * (1 - icc)) if icc < 1 else np.inf
    if not np.isfinite(a_term) or not np.isfinite(b_term):
        return IccResult(endpoint, n, float(icc), float(icc), float(icc), confidence)
    v_numerator = (a_term * ms_cols + b_term * ms_error) ** 2
    v_denominator = (a_term * ms_cols) ** 2 / (k - 1) + (b_term * ms_error) ** 2 / (
        (n - 1) * (k - 1)
    )
    v = v_numerator / v_denominator if v_denominator > 0 else (n - 1) * (k - 1)

    f_lower = float(stats.f.ppf(1 - alpha / 2, n - 1, v))
    f_upper = float(stats.f.ppf(1 - alpha / 2, v, n - 1))

    shared = k * ms_cols + (k * n - k - n) * ms_error
    ci_low = n * (ms_rows - f_lower * ms_error) / (f_lower * shared + n * ms_rows)
    ci_high = n * (f_upper * ms_rows - ms_error) / (shared + n * f_upper * ms_rows)

    del f_observed  # kept above only to document the F the interval is built on
    return IccResult(
        endpoint=endpoint,
        n=n,
        icc=float(icc),
        ci_low=float(min(ci_low, ci_high)),
        ci_high=float(max(ci_low, ci_high)),
        confidence=confidence,
    )


@dataclass(frozen=True)
class BlandAltmanResult:
    """Bias and limits of agreement, with the limits' own confidence interval."""

    endpoint: str
    n: int
    bias: float
    bias_ci_low: float
    bias_ci_high: float
    loa_low: float
    loa_high: float
    loa_low_ci: tuple[float, float]
    loa_high_ci: tuple[float, float]
    sd_differences: float

    def __str__(self) -> str:
        return (
            f"{self.endpoint}: bias {self.bias:+.4g} "
            f"[{self.bias_ci_low:+.4g}, {self.bias_ci_high:+.4g}], "
            f"LoA [{self.loa_low:+.4g}, {self.loa_high:+.4g}] (n={self.n})"
        )


def bland_altman(arm_a, arm_b, endpoint: str = "", confidence: float = 0.95) -> BlandAltmanResult:
    """Bias and 95% limits of agreement for two arms measured on the same runs.

    The limits are `bias ± 1.96 sd`, and their standard error is
    `sd * sqrt(3/n)` (Bland & Altman 1986, 1999) — reported because at a
    budget-bound n the limits are themselves imprecise, and a reader comparing
    them against a margin needs to see that.
    """
    a = np.asarray(arm_a, dtype=np.float64)
    b = np.asarray(arm_b, dtype=np.float64)
    paired = np.isfinite(a) & np.isfinite(b)
    d = a[paired] - b[paired]
    n = d.size
    if n < 2:
        raise ValueError(f"{endpoint or 'endpoint'}: needs at least 2 paired values, got {n}")

    bias = float(d.mean())
    sd = float(d.std(ddof=1))
    df = n - 1
    t_crit = float(stats.t.ppf(1 - (1 - confidence) / 2, df))

    se_bias = sd / np.sqrt(n)
    se_loa = sd * np.sqrt(3.0 / n)
    loa_low, loa_high = bias - 1.96 * sd, bias + 1.96 * sd

    return BlandAltmanResult(
        endpoint=endpoint,
        n=n,
        bias=bias,
        bias_ci_low=bias - t_crit * se_bias,
        bias_ci_high=bias + t_crit * se_bias,
        loa_low=loa_low,
        loa_high=loa_high,
        loa_low_ci=(loa_low - t_crit * se_loa, loa_low + t_crit * se_loa),
        loa_high_ci=(loa_high - t_crit * se_loa, loa_high + t_crit * se_loa),
        sd_differences=sd,
    )


@dataclass(frozen=True)
class NonInferiorityResult:
    """One tier-1 endpoint against a one-sided threshold.

    Tier-1 endpoints are agreement statistics — a correlation, a Dice — where
    only one direction is a failure: an agreement *above* the threshold is never
    a worse result. The test is therefore non-inferiority, not equivalence, and
    the reported quantity is a one-sided lower confidence bound.

    `estimate` and `lower_bound` are on the reported scale (back-transformed when
    a transform was used); `scale` names the scale the test ran on.
    """

    endpoint: str
    n: int
    estimate: float
    lower_bound: float
    threshold: float
    p_value: float
    alpha: float
    scale: str
    verdict: str

    @property
    def is_non_inferior(self) -> bool:
        return self.verdict == NON_INFERIOR

    def __str__(self) -> str:
        scale = "" if self.scale == "identity" else f", on {self.scale}"
        return (
            f"{self.endpoint}: {self.estimate:.4f} "
            f"(one-sided {1 - self.alpha:.0%} lower bound {self.lower_bound:.4f}) "
            f"vs threshold {self.threshold:.4f} (n={self.n}{scale}) — {self.verdict}"
        )


def fisher_z(values):
    """arctanh with the endpoints clipped, for correlation-valued endpoints."""
    r = np.clip(np.asarray(values, dtype=np.float64), -_R_CLIP, _R_CLIP)
    return np.arctanh(r)


def non_inferiority(
    values,
    threshold: float,
    endpoint: str = "",
    alpha: float = DEFAULT_ALPHA,
    transform: str | None = None,
) -> NonInferiorityResult:
    """Is the endpoint's mean above `threshold`, by a one-sided t test?

    `values` are per-unit endpoint values — one correlation or Dice per run, not
    per voxel. The verdict is `NON_INFERIOR` exactly when the one-sided
    `1 - alpha` lower confidence bound clears the threshold, so the bound and the
    verdict cannot disagree.

    Pass `transform=FISHER_Z` for a correlation. The test then runs on z, and the
    bound is back-transformed with `tanh` for reporting: the interval is
    asymmetric in r, which is the point — at r = 0.98 a 0.01 step in r is 0.26 in
    z, so treating r as normal understates the upper half and overstates the
    lower.
    """
    raw = np.asarray(values, dtype=np.float64)
    raw = raw[np.isfinite(raw)]
    n = raw.size
    if n < 2:
        raise ValueError(f"{endpoint or 'endpoint'}: needs at least 2 values, got {n}")

    if transform == FISHER_Z:
        tested = fisher_z(raw)
        tested_threshold = float(np.arctanh(np.clip(threshold, -_R_CLIP, _R_CLIP)))
        back = np.tanh
        scale = FISHER_Z
    elif transform is None:
        tested = raw
        tested_threshold = float(threshold)

        def back(x):
            return x

        scale = "identity"
    else:
        raise ValueError(f"unknown transform {transform!r}; expected None or {FISHER_Z!r}")

    mean = float(tested.mean())
    se = float(tested.std(ddof=1) / np.sqrt(n))
    df = n - 1

    if se == 0:
        verdict = NON_INFERIOR if mean > tested_threshold else NI_NOT_ESTABLISHED
        return NonInferiorityResult(
            endpoint=endpoint,
            n=n,
            estimate=float(back(mean)),
            lower_bound=float(back(mean)),
            threshold=float(threshold),
            p_value=0.0 if verdict == NON_INFERIOR else 1.0,
            alpha=alpha,
            scale=scale,
            verdict=verdict,
        )

    t_stat = (mean - tested_threshold) / se
    p_value = float(stats.t.sf(t_stat, df))
    lower = mean - float(stats.t.ppf(1 - alpha, df)) * se
    verdict = NON_INFERIOR if lower > tested_threshold else NI_NOT_ESTABLISHED

    return NonInferiorityResult(
        endpoint=endpoint,
        n=n,
        estimate=float(back(mean)),
        lower_bound=float(back(lower)),
        threshold=float(threshold),
        p_value=p_value,
        alpha=alpha,
        scale=scale,
        verdict=verdict,
    )


def non_inferiority_power(
    n: int,
    gap: float,
    sd: float,
    alpha: float = DEFAULT_ALPHA,
) -> float:
    """Exact power of the one-sided test, for a true `gap` above the threshold.

    `gap` and `sd` are on the tested scale — pass Fisher z units for a
    correlation endpoint, which `fisher_z_gap` computes.
    """
    if n < 2 or sd <= 0:
        return 0.0
    df = n - 1
    t_crit = float(stats.t.ppf(1 - alpha, df))
    return float(stats.nct.sf(t_crit, df, gap * np.sqrt(n) / sd))


def required_n_one_sided(
    gap: float,
    sd: float,
    power: float = 0.8,
    alpha: float = DEFAULT_ALPHA,
    max_n: int = 100_000,
) -> int:
    """The n a one-sided test needs to put the lower bound above the threshold.

    Roughly 72% of the n the symmetric TOST needs for the same gap — the
    multiplier is `z_(1-alpha) + z_(1-beta)` rather than
    `z_(1-alpha) + z_(1-beta/2)`, and `(2.4865 / 2.9265)**2 = 0.722`. Using TOST
    on a tier-1 endpoint therefore buys sessions it does not need.
    """
    if gap <= 0:
        raise ValueError(
            f"gap must be positive, got {gap}: a mean at or below the threshold is not a "
            "power problem, and no sample size establishes non-inferiority against it"
        )
    if sd <= 0:
        raise ValueError(f"sd must be positive, got {sd}")

    z_alpha = float(stats.norm.ppf(1 - alpha))
    z_beta = float(stats.norm.ppf(power))
    n = max(2, int(np.ceil(((sd * (z_alpha + z_beta)) / gap) ** 2)) - 5)
    while n <= max_n:
        if non_inferiority_power(n, gap, sd, alpha) >= power:
            return n
        n += 1
    raise ValueError(f"no n <= {max_n} reaches power {power} for gap {gap} at sd {sd}")


def fisher_z_gap(mean_r: float, threshold_r: float) -> float:
    """The gap between a correlation and its threshold, in Fisher z units.

    The unit `required_n_one_sided` and `non_inferiority_power` want for a
    correlation endpoint. Near the ceiling this is much larger than the gap in r,
    which is why powering a correlation endpoint off r units overstates the n
    required.
    """
    return float(
        np.arctanh(np.clip(mean_r, -_R_CLIP, _R_CLIP))
        - np.arctanh(np.clip(threshold_r, -_R_CLIP, _R_CLIP))
    )


#: The within-session ICC the sample-size calculations plan against, chosen by the
#: operator on 2026-10-08. **Planning only.** The primary analysis tests session
#: means (`aggregate_by_cluster`), whose standard error needs no assumed
#: correlation, so this value never reaches a published interval — it decides how
#: many sessions to buy, and a higher value is the conservative choice there. High rather than low on purpose: runs in a session share
#: subject, anatomy and the T1w->MNI target, and D4 holds anatomy constant between
#: the arms, so the design shares exactly what drives the correlation. At 5 runs per
#: session this is a design effect of 4.2 — each session buys 1.19 effective runs, so
#: power is set by the session count and extra runs are nearly free. The pilot
#: measures the real value per endpoint with `icc_one_way` (task 4.15); until then
#: this is the planning figure, and a measured value replaces it rather than
#: averaging with it.
PLANNING_ICC = 0.8


@dataclass(frozen=True)
class EndpointSizing:
    """What one endpoint needs, in sessions."""

    endpoint: str
    gap: float
    sd: float
    one_sided: bool
    required_sessions: int

    def __str__(self) -> str:
        kind = "non-inferiority" if self.one_sided else "TOST"
        return (
            f"{self.endpoint}: {self.required_sessions} sessions "
            f"(gap {self.gap:.4g}, SD {self.sd:.4g}, {kind})"
        )


def required_sessions(
    endpoints: dict[str, tuple[float, float, bool]],
    power: float = 0.8,
    alpha: float = DEFAULT_ALPHA,
) -> tuple[list[EndpointSizing], EndpointSizing]:
    """Sessions needed per endpoint, and the endpoint that binds.

    `endpoints` maps a name to `(gap, sd, one_sided)`, where `gap` is the distance
    between the observed mean and the endpoint's threshold or margin and `sd` is
    the **between-session** standard deviation of that endpoint — both on the
    tested scale, so Fisher z units for a correlation (`fisher_z_gap`).

    One sample has to serve every endpoint, so the study's size is the MAXIMUM
    over endpoints, and the returned binding endpoint is the one that sets it.
    Sizing to the mean or the median would leave some endpoints underpowered,
    which is the failure this function exists to prevent.

    Because the analysis tests session means (`aggregate_by_cluster`), the SD this
    wants is measured directly by the pilot across its sessions. No within-session
    ICC enters the calculation — `PLANNING_ICC` is only needed to guess a
    between-session SD before any sessions have been run.
    """
    if not endpoints:
        raise ValueError("no endpoints to size for")

    sized = []
    for name, (gap, sd, one_sided) in endpoints.items():
        n = (
            required_n_one_sided(gap=gap, sd=sd, power=power, alpha=alpha)
            if one_sided
            else required_n(margin=gap, sd=sd, power=power, alpha=alpha)
        )
        sized.append(EndpointSizing(name, gap, sd, one_sided, n))

    sized.sort(key=lambda e: e.required_sessions, reverse=True)
    return sized, sized[0]


def design_effect(runs_per_cluster: float, icc: float) -> float:
    """`1 + (m - 1) * rho`, the variance inflation from clustering.

    Runs within a session share subject, anatomy and the T1w->MNI target, so they
    are not independent observations of an arm difference. Holding anatomy
    constant (D4) shares exactly the thing that drives the correlation, so a high
    `icc` is the expected case rather than the pathological one.
    """
    if runs_per_cluster < 1:
        raise ValueError(f"runs_per_cluster must be >= 1, got {runs_per_cluster}")
    if not 0.0 <= icc <= 1.0:
        raise ValueError(f"icc must be in [0, 1], got {icc}")
    return 1.0 + (runs_per_cluster - 1.0) * icc


def effective_n(n_runs: float, runs_per_cluster: float, icc: float) -> float:
    """Runs divided by the design effect: what the power functions should be given.

    At 5 runs per session and an icc of 0.5, each session buys 1.67 effective
    runs rather than 5, and 20 sessions behave like n = 33 rather than n = 100.
    """
    return float(n_runs) / design_effect(runs_per_cluster, icc)


def aggregate_by_cluster(values, clusters) -> tuple[np.ndarray, np.ndarray]:
    """One value per cluster — the analysis unit for every clustered endpoint.

    Testing on session means rather than on runs removes the clustering problem
    instead of correcting for it: the standard error of a mean over `S` session
    means is exactly the standard error the design-effect correction computes, so
    no within-session correlation has to be assumed, estimated or defended. The
    only cost is degrees of freedom, `S - 1` rather than `n_eff - 1`, which is
    worth 0-1.6% on the detectable gap across the plausible range of rho.

    Means are **unweighted** by run count. Weighting looks appealing and is wrong
    at the correlations this design expects: at rho = 0.8 the variance of a
    session mean is 0.900 sigma^2 at two runs and 0.833 at six, so a six-run
    session carries barely more information than a two-run session and weighting
    by run count would overstate it.

    Returns `(means, labels)` with labels sorted, so two endpoints aggregated
    separately stay aligned.
    """
    values = np.asarray(values, dtype=np.float64)
    labels = np.asarray(clusters)
    if values.shape != labels.shape:
        raise ValueError(f"values and clusters differ in length, {values.size} vs {labels.size}")

    finite = np.isfinite(values)
    values, labels = values[finite], labels[finite]
    if values.size == 0:
        raise ValueError("no finite values to aggregate")

    unique = np.unique(labels)
    means = np.array([float(values[labels == label].mean()) for label in unique])
    return means, unique


def icc_one_way(values, clusters) -> float:
    """ICC(1) — the share of variance between clusters, for the design effect.

    One-way random effects over an unbalanced design, using the harmonic-style
    mean cluster size the standard formula assumes. Returns 0.0 where the
    between-cluster term does not exceed the within-cluster term, since a
    negative variance component is an estimate of zero, not a negative
    correlation.
    """
    values = np.asarray(values, dtype=np.float64)
    labels = np.asarray(clusters)
    if values.shape != labels.shape:
        raise ValueError(f"values and clusters differ in length, {values.size} vs {labels.size}")

    groups = [values[labels == label] for label in np.unique(labels)]
    groups = [g for g in groups if g.size]
    k = len(groups)
    if k < 2:
        raise ValueError(f"ICC(1) needs at least 2 clusters, got {k}")
    total = sum(g.size for g in groups)
    if total <= k:
        raise ValueError("ICC(1) needs at least one cluster with more than one member")

    grand_mean = float(values.mean())
    ss_between = sum(g.size * (float(g.mean()) - grand_mean) ** 2 for g in groups)
    ss_within = sum(float(((g - g.mean()) ** 2).sum()) for g in groups)
    ms_between = ss_between / (k - 1)
    ms_within = ss_within / (total - k)

    # The mean cluster size the one-way ICC assumes, corrected for imbalance.
    sizes = np.array([g.size for g in groups], dtype=np.float64)
    m_bar = (total - float((sizes**2).sum()) / total) / (k - 1)
    if ms_within == 0 or m_bar <= 0:
        return 1.0 if ms_between > 0 else 0.0

    icc = (ms_between - ms_within) / (ms_between + (m_bar - 1.0) * ms_within)
    return float(max(0.0, min(1.0, icc)))


def tost_power(
    n: int,
    margin: float,
    sd: float,
    true_difference: float = 0.0,
    alpha: float = DEFAULT_ALPHA,
) -> float:
    """A conservative lower bound on TOST power at sample size `n`.

    Both one-sided tests must reject, and `P(A and B) >= P(A) + P(B) - 1`. Each
    term is an exact noncentral-t probability, so the bound is never optimistic:
    the n that `required_n` returns from it is never smaller than the n the exact
    power (Owen's Q) requires. That direction is the one that matters here —
    D7 uses this number to decide whether the study is affordable at the
    precision the margins demand, and an underestimate of n would answer that
    question wrongly.
    """
    if n < 2:
        return 0.0
    df = n - 1
    se_scale = np.sqrt(n) / sd
    t_crit = float(stats.t.ppf(1 - alpha, df))

    nc_lower = (margin + true_difference) * se_scale
    nc_upper = (true_difference - margin) * se_scale
    p_lower = float(stats.nct.sf(t_crit, df, nc_lower))
    p_upper = float(stats.nct.cdf(-t_crit, df, nc_upper))
    return float(max(0.0, min(1.0, p_lower + p_upper - 1.0)))


def required_n(
    margin: float,
    sd: float,
    true_difference: float = 0.0,
    power: float = 0.8,
    alpha: float = DEFAULT_ALPHA,
    max_n: int = 100_000,
) -> int:
    """The paired sample size TOST needs to establish equivalence within `margin`.

    `sd` is the standard deviation of the per-run arm *difference*, which is what
    the pilot measures. `true_difference` is the difference assumed to exist: a
    non-zero value is the honest input whenever the pilot shows an offset, and it
    raises the required n sharply as it approaches the margin.

    Raises if the margin cannot be met at any n below `max_n`, which is the real
    answer when `|true_difference| >= margin` — no sample size establishes
    equivalence against a margin the true difference sits outside.
    """
    if margin <= 0:
        raise ValueError(f"margin must be positive, got {margin}")
    if sd <= 0:
        raise ValueError(f"sd must be positive, got {sd}")
    if abs(true_difference) >= margin:
        raise ValueError(
            f"assumed difference {true_difference} is not inside the margin ±{margin}; "
            "no sample size establishes equivalence here, and reporting one would be "
            "a scope question rather than a power question"
        )

    # Normal approximation for the starting point, then step up until the exact
    # bound clears `power`. Cheap: the approximation is within a few of the answer.
    z_alpha = float(stats.norm.ppf(1 - alpha))
    z_beta = float(stats.norm.ppf(1 - (1 - power) / 2))
    start = max(2, int(np.ceil(((sd * (z_alpha + z_beta)) / (margin - abs(true_difference))) ** 2)))

    n = max(2, start - 10)
    while n <= max_n:
        if tost_power(n, margin, sd, true_difference, alpha) >= power:
            return n
        n += 1
    raise ValueError(f"no n <= {max_n} reaches power {power} for margin ±{margin} at sd {sd}")
