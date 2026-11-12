"""The pre-registered final analysis: one decision per declared endpoint (task 3.5, D7, D19).

Reads the session table `equivalence.collect` writes (one row per endpoint per
session, `value` on the endpoint's test scale) and applies each endpoint's
committed margin with the test its margin declares:

- **log-ratio TOST** (`tsnr_median`, `gcor`, `aor`, `aqi`): `tost` on the session
  log ratios against ±ln(ratio_upper), reported back as a ratio of geometric
  means with its 90% interval. Beside it, always, the proportionality check and
  the absolute-scale interval (spec, "Ratio-scale endpoint"), and for `aqi` the
  pre-specified robust variant, the 20%-trimmed-mean TOST (one-sample trimmed t). Neither changes the
  decision.
- **exact** (`n_cosines`, a TOST at margin 0): identical in every session, or a
  defect in one implementation. Not a statistical test.
- **Fisher-z non-inferiority** (`mean_image_correlation`, `tsnr_map_correlation`):
  the session values are already mean Fisher z, tested one-sided against
  arctanh(threshold) and reported back on r.
- **identity non-inferiority** (`t1w_mni_label_dice`): the session differences,
  cloudpipe minus fMRIPrep, one-sided against the threshold.
- **binomial ceiling** (`mean_image_low_share`): the share of sessions below the
  cut-off, by the one-sided exact binomial.

Reporting is per endpoint, never a single pass or fail (spec, "Endpoint fails
equivalence"). Each endpoint is tested at the committed alpha with no
adjustment: the claim is per endpoint, and a reader wanting all of them at once
has the intersection-union rule, which needs none. Where equivalence is not
established the report says which arm's value is higher, and never calls a
direction better.

It refuses margins that are not committed, an amendment still pending, a table
whose sessions differ between endpoints, and a session count other than the
final n recorded in task 4.15.

    python -m equivalence.analyze --values final_values.csv --expected-n 100 [--json result.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .equivalence import (
    FISHER_Z,
    CeilingResult,
    EquivalenceResult,
    NonInferiorityResult,
    ProportionalityResult,
    binomial_ceiling,
    non_inferiority,
    proportionality,
    tost,
    trimmed_tost,
)
from .margins import (
    BINOMIAL_CEILING,
    DEFAULT_MARGINS,
    LOG_RATIO,
    NON_INFERIORITY,
    TOST,
    Margin,
    Margins,
    MarginsNotCommitted,
    load_margins,
)

#: Endpoints whose robust sensitivity analysis was pre-specified (D18, margins.yaml).
ROBUST_SENSITIVITY = frozenset({"aqi"})

IDENTICAL = "identical in every session"
DEFECT = "differs between the arms: a defect to investigate"


class AnalysisInputError(ValueError):
    """The session table cannot be analysed as the pre-registration requires."""


@dataclass(frozen=True)
class Row:
    session: str
    value: float
    cloudpipe: float | None
    reference: float | None
    n_runs: int


@dataclass(frozen=True)
class RatioReport:
    """A log-ratio TOST, reported on the ratio scale."""

    test: EquivalenceResult
    ratio: float
    ratio_ci: tuple[float, float]
    bounds: tuple[float, float]
    cloudpipe_geomean: float
    reference_geomean: float
    proportionality: ProportionalityResult
    robust: EquivalenceResult | None = None
    robust_ratio_ci: tuple[float, float] | None = None


@dataclass(frozen=True)
class ExactReport:
    n: int
    differing: int
    verdict: str


@dataclass(frozen=True)
class EndpointReport:
    endpoint: str
    tier: int
    test: str
    margin: str
    ratio: RatioReport | None = None
    exact: ExactReport | None = None
    non_inferiority: NonInferiorityResult | None = None
    ceiling: CeilingResult | None = None
    #: Both arms' session means on the natural scale, where the endpoint has arms.
    arms: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Report:
    n_sessions: int
    alpha: float
    margins_source: str
    endpoints: list[EndpointReport]


def read_table(path: Path) -> dict[str, list[Row]]:
    """The session table, by endpoint. One row per endpoint per session."""
    table: dict[str, list[Row]] = defaultdict(list)
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        missing = {"endpoint", "session", "value", "cloudpipe", "reference", "n_runs"} - set(
            reader.fieldnames or []
        )
        if missing:
            raise AnalysisInputError(f"{path}: missing columns {sorted(missing)}")
        seen = set()
        for r in reader:
            key = (r["endpoint"], r["session"])
            if key in seen:
                raise AnalysisInputError(f"{path}: {key} appears twice; one value per session")
            seen.add(key)
            table[r["endpoint"]].append(
                Row(
                    session=r["session"],
                    value=float(r["value"]),
                    cloudpipe=float(r["cloudpipe"]) if r["cloudpipe"] else None,
                    reference=float(r["reference"]) if r["reference"] else None,
                    n_runs=int(r["n_runs"] or 0),
                )
            )
    return dict(table)


def check_table(margins: Margins, table: dict[str, list[Row]], expected_n: int) -> None:
    """Every declared endpoint present, on the same sessions, at the recorded final n."""
    absent = sorted(set(margins.entries) - set(table))
    if absent:
        raise AnalysisInputError(f"no session values for declared endpoint(s) {absent}")
    sessions = {name: {r.session for r in table[name]} for name in margins.entries}
    reference_set = sessions[min(sessions)]
    for name, s in sessions.items():
        if s != reference_set:
            raise AnalysisInputError(
                f"{name} has {len(s)} sessions and {min(sessions)} {len(reference_set)}; "
                f"they differ in {len(s ^ reference_set)}. Every endpoint is analysed on "
                "the same sessions, or the per-endpoint claims rest on different samples."
            )
    if len(reference_set) != expected_n:
        raise AnalysisInputError(
            f"{len(reference_set)} sessions, but the final n recorded at the interim look "
            f"is {expected_n} (task 4.15). Analysing a different n is a deviation to "
            "declare first, not a flag to pass."
        )
    for name, rows in table.items():
        if name in margins.entries and not all(math.isfinite(r.value) for r in rows):
            raise AnalysisInputError(f"{name}: a session value is not finite")


def _geomean(values: list[float], offset: float) -> float:
    return float(np.exp(np.mean(np.log(np.asarray(values) + offset))) - offset)


def _ratio(margin: Margin, rows: list[Row], alpha: float) -> RatioReport:
    logs = [r.value for r in rows]
    result = tost(logs, margin.value, margin.name, alpha)
    a = [r.cloudpipe for r in rows]
    b = [r.reference for r in rows]
    if any(v is None for v in a + b):
        raise AnalysisInputError(f"{margin.name}: a session lacks an arm's value")
    robust = robust_ci = None
    if margin.name in ROBUST_SENSITIVITY:
        robust = trimmed_tost(logs, margin.value, margin.name, alpha)
        robust_ci = (math.exp(robust.ci_low), math.exp(robust.ci_high))
    upper = math.exp(margin.value)
    return RatioReport(
        test=result,
        ratio=math.exp(result.estimate),
        ratio_ci=(math.exp(result.ci_low), math.exp(result.ci_high)),
        bounds=(1 / upper, upper),
        cloudpipe_geomean=_geomean(a, margin.floor_offset),
        reference_geomean=_geomean(b, margin.floor_offset),
        proportionality=proportionality(a, b, margin.name, margin.floor_offset, alpha),
        robust=robust,
        robust_ratio_ci=robust_ci,
    )


def _fisher_ni(margin: Margin, rows: list[Row], alpha: float) -> NonInferiorityResult:
    """The session values are already mean Fisher z: test on z, report on r."""
    z = non_inferiority(
        [r.value for r in rows], math.atanh(margin.value), margin.name, alpha, transform=None
    )
    return NonInferiorityResult(
        endpoint=z.endpoint,
        n=z.n,
        estimate=math.tanh(z.estimate),
        lower_bound=math.tanh(z.lower_bound),
        threshold=margin.value,
        p_value=z.p_value,
        alpha=alpha,
        scale=FISHER_Z,
        verdict=z.verdict,
    )


def _arms(rows: list[Row]) -> dict[str, float]:
    a = [r.cloudpipe for r in rows if r.cloudpipe is not None]
    b = [r.reference for r in rows if r.reference is not None]
    out = {}
    if len(a) == len(rows):
        out["cloudpipe"] = float(np.mean(a))
    if len(b) == len(rows):
        out["fmriprep"] = float(np.mean(b))
    return out


def analyse_endpoint(margin: Margin, rows: list[Row], alpha: float) -> EndpointReport:
    base = {
        "endpoint": margin.name,
        "tier": margin.tier,
        "test": margin.test,
        "margin": str(margin),
    }
    if margin.test == TOST and margin.is_exact:
        differing = sum(1 for r in rows if r.value != 0)
        return EndpointReport(
            **base,
            exact=ExactReport(len(rows), differing, IDENTICAL if differing == 0 else DEFECT),
            arms=_arms(rows),
        )
    if margin.test == TOST and margin.scale == LOG_RATIO:
        return EndpointReport(**base, ratio=_ratio(margin, rows, alpha))
    if margin.test == NON_INFERIORITY and margin.scale == FISHER_Z:
        return EndpointReport(**base, non_inferiority=_fisher_ni(margin, rows, alpha))
    if margin.test == NON_INFERIORITY:
        ni = non_inferiority([r.value for r in rows], margin.value, margin.name, alpha)
        return EndpointReport(**base, non_inferiority=ni, arms=_arms(rows))
    if margin.test == BINOMIAL_CEILING:
        return EndpointReport(
            **base,
            ceiling=binomial_ceiling([r.value for r in rows], margin.value, margin.name, alpha),
        )
    raise AnalysisInputError(  # pragma: no cover - margins.py admits no other shape
        f"{margin.name}: no analysis for test {margin.test!r} on scale {margin.scale!r}"
    )


def analyse(margins: Margins, table: dict[str, list[Row]], expected_n: int) -> Report:
    """Every declared endpoint's pre-registered test. Refuses unsettled margins."""
    if not margins.is_committed:
        raise MarginsNotCommitted(f"{margins.source}: the margins are not committed")
    margins.require_settled()
    check_table(margins, table, expected_n)
    order = sorted(margins.entries.values(), key=lambda m: (m.tier, m.name))
    return Report(
        n_sessions=expected_n,
        alpha=margins.alpha,
        margins_source=str(margins.source),
        endpoints=[analyse_endpoint(m, table[m.name], margins.alpha) for m in order],
    )


def _line(e: EndpointReport) -> list[str]:
    head = f"[tier {e.tier}] {e.endpoint}"
    if e.ratio:
        r = e.ratio
        p = r.proportionality
        lines = [
            f"{head}: ratio {r.ratio:.4f} [{r.ratio_ci[0]:.4f}, {r.ratio_ci[1]:.4f}] vs "
            f"bounds {r.bounds[0]:.4f}-{r.bounds[1]:.4f} (n={r.test.n}) — {r.test.verdict}"
            + ("" if r.test.departure == "none" else f" ({r.test.departure})"),
            f"    geometric means: cloudpipe {r.cloudpipe_geomean:.4g}, fMRIPrep {r.reference_geomean:.4g}",
            f"    proportionality: log-scale slope {p.log_scale.slope:+.3f} "
            f"[{p.log_scale.ci_low:+.3f}, {p.log_scale.ci_high:+.3f}], absolute-scale slope "
            f"{p.absolute_scale.slope:+.3f} [{p.absolute_scale.ci_low:+.3f}, "
            f"{p.absolute_scale.ci_high:+.3f}] — "
            + ("consistent with proportional" if p.proportional else "NOT proportional"),
            f"    absolute scale (descriptive): {p.absolute_mean:+.4g} "
            f"[{p.absolute_ci[0]:+.4g}, {p.absolute_ci[1]:+.4g}]",
        ]
        if r.robust:
            lines.append(
                f"    robust sensitivity (20%-trimmed TOST): ratio {math.exp(r.robust.estimate):.4f} "
                f"[{r.robust_ratio_ci[0]:.4f}, {r.robust_ratio_ci[1]:.4f}] — {r.robust.verdict}"
            )
        return lines
    if e.exact:
        return [f"{head}: {e.exact.verdict} ({e.exact.differing} of {e.exact.n} sessions differ)"]
    # The results' own __str__ starts with the endpoint name; `head` already has it.
    if e.non_inferiority:
        arms = "".join(f", {k} {v:.4g}" for k, v in e.arms.items())
        return [f"[tier {e.tier}] {e.non_inferiority}{arms}"]
    return [f"[tier {e.tier}] {e.ceiling}"]


def render(report: Report) -> str:
    lines = [
        f"{report.n_sessions} sessions, each endpoint at one-sided alpha {report.alpha}, "
        f"margins {report.margins_source}. Reported per endpoint; no single verdict."
    ]
    for e in report.endpoints:
        lines += _line(e)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--values", type=Path, required=True, help="equivalence.collect's session table")
    p.add_argument("--margins", type=Path, default=DEFAULT_MARGINS)
    p.add_argument(
        "--expected-n",
        type=int,
        required=True,
        help="the final n recorded at the interim look (task 4.15)",
    )
    p.add_argument("--json", type=Path)
    args = p.parse_args(argv)
    try:
        report = analyse(load_margins(args.margins), read_table(args.values), args.expected_n)
    except (MarginsNotCommitted, AnalysisInputError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(render(report))
    if args.json:
        args.json.write_text(json.dumps(asdict(report), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
