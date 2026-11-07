"""The variance-only sample-size re-estimation: the study's one interim look (design D19).

Sessions run in the order of the stratified draw. After the first `interim` of
them (30), this module reads each primary endpoint's session values and computes
**only their standard deviation**. From that SD's upper one-sided confidence
limit it computes the sessions each endpoint needs at the pre-registered power
and alpha, under the assumed true value `margins.yaml` fixes in advance. The
study then runs min(cap, max(floor, the largest requirement)) sessions.

What it never does, by construction: compute or print a mean, a difference
between arms, a confidence interval or a verdict. Re-estimating a nuisance
variance without looking at the effect leaves the type I error essentially
unchanged (Wittes and Brittain, 1990; Kieser and Friede, 2003), which is what
lets the final analysis go unadjusted. A look that showed a mean would not.

The session values come in on each endpoint's test scale:

- a log-ratio TOST: log(cloudpipe / fMRIPrep) of the session's value;
- a Fisher-z non-inferiority: arctanh of the session's agreement r;
- an identity-scale non-inferiority: the session's difference, cloudpipe minus
  fMRIPrep (the registration Dice);
- a binomial ceiling needs no session values: its requirement follows from the
  ceiling and the assumed share alone.

The input file is produced by the endpoint computation and is read by this
module, not by a person.

    python -m equivalence.reestimate --values interim_values.csv [--json plan.json]

CSV columns: `endpoint`, `session`, `value`. `--allow-draft` runs it against the
draft margins as a rehearsal; its output says so and is not the study's look.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from scipy import stats

from .equivalence import FISHER_Z, fisher_z_gap, required_n, required_n_one_sided
from .margins import (
    BINOMIAL_CEILING,
    DEFAULT_MARGINS,
    LOG_RATIO,
    NON_INFERIORITY,
    TOST,
    Margin,
    Margins,
    MarginsNotCommitted,
    SampleSizeRule,
    load_margins,
)

#: Fewer session values than this cannot estimate an SD worth sizing a study on.
MIN_SESSIONS = 10


@dataclass(frozen=True)
class EndpointPlan:
    """One endpoint's requirement. Deliberately carries no mean."""

    endpoint: str
    test: str
    n_sessions: int
    sd: float | None
    sd_upper: float | None
    required: int

    def __str__(self) -> str:
        if self.sd is None:
            return f"{self.endpoint:28s} {self.test:17s} required {self.required:5d}"
        return (
            f"{self.endpoint:28s} {self.test:17s} sessions {self.n_sessions:3d}  "
            f"SD {self.sd:.4g} (upper {self.sd_upper:.4g})  required {self.required:5d}"
        )


@dataclass(frozen=True)
class Plan:
    endpoints: list[EndpointPlan]
    binding: str
    final_n: int
    capped: bool
    rule: SampleSizeRule
    rehearsal: bool


def sample_sd(values) -> tuple[float, int]:
    """The SD (ddof=1) of the session values, and their count. Nothing else."""
    v = np.asarray(values, dtype=np.float64)
    v = v[np.isfinite(v)]
    if v.size < MIN_SESSIONS:
        raise ValueError(f"{v.size} finite session values; at least {MIN_SESSIONS} are needed")
    return float(np.std(v, ddof=1)), int(v.size)


def upper_sd(sd: float, n: int, confidence: float) -> float:
    """The upper one-sided `confidence` limit of a normal SD estimated from n values.

    sigma_U = s * sqrt((n - 1) / chi2_{1 - confidence, n - 1}). Planning on it
    rather than on s protects against an interim SD that came out low by chance.
    """
    return sd * math.sqrt((n - 1) / float(stats.chi2.ppf(1 - confidence, n - 1)))


def ceiling_required(margin: Margin, assumed_share: float, rule: SampleSizeRule) -> int:
    """The smallest n at which the exact one-sided binomial reaches the rule's power.

    The test rejects "share >= ceiling" when the count below `margin.below` is at
    most k, the largest count with P(X <= k | ceiling) <= alpha. Power is
    P(X <= k | assumed share). Searched up to 10 x the cap; beyond it the endpoint
    is reported as needing more than the study can run.
    """
    for n in range(2, 10 * rule.cap + 1):
        ks = np.arange(n + 1)
        allowed = ks[stats.binom.cdf(ks, n, margin.value) <= rule.alpha]
        if allowed.size and stats.binom.cdf(allowed.max(), n, assumed_share) >= rule.power:
            return n
    return 10 * rule.cap + 1


def endpoint_required(margin: Margin, values, assumed: float, rule: SampleSizeRule) -> EndpointPlan:
    """What one primary endpoint needs, from its interim session values."""
    if margin.test == BINOMIAL_CEILING:
        return EndpointPlan(
            margin.name, margin.test, 0, None, None, ceiling_required(margin, assumed, rule)
        )

    sd, n = sample_sd(values)
    sd_u = upper_sd(sd, n, rule.sd_confidence)
    if margin.test == TOST:
        true_difference = math.log(assumed) if margin.scale == LOG_RATIO else assumed
        need = required_n(margin.value, sd_u, true_difference, rule.power, rule.alpha)
    elif margin.test == NON_INFERIORITY:
        gap = (
            fisher_z_gap(assumed, margin.value)
            if margin.scale == FISHER_Z
            else assumed - margin.value
        )
        need = required_n_one_sided(gap, sd_u, rule.power, rule.alpha)
    else:  # pragma: no cover - margins.py admits no other test
        raise ValueError(f"{margin.name}: no sizing for test {margin.test!r}")
    return EndpointPlan(margin.name, margin.test, n, sd, sd_u, need)


def reestimate(margins: Margins, values: dict[str, list[float]]) -> Plan:
    """Size every declared endpoint from its interim values and set the final n.

    Exact endpoints (a TOST at margin 0) are not sized. Refuses draft margins: the
    look is pre-registered only if the rule it applies was committed first.
    Callers rehearsing against the draft pass a `Margins` loaded with
    `allow_draft=True`, and the returned plan is marked as a rehearsal.
    """
    rule = margins.sample_size
    if rule is None:  # pragma: no cover - load_margins requires the block
        raise ValueError("the margins carry no sample_size rule")
    plans = []
    for name, margin in sorted(margins.entries.items()):
        if margin.is_exact:
            continue
        if margin.test != BINOMIAL_CEILING and name not in values:
            raise ValueError(f"no interim session values for {name!r}")
        plans.append(endpoint_required(margin, values.get(name, []), rule.assumed[name], rule))
    binding = max(plans, key=lambda p: p.required)
    final = min(rule.cap, max(rule.floor, binding.required))
    return Plan(
        endpoints=plans,
        binding=binding.endpoint,
        final_n=final,
        capped=binding.required > rule.cap,
        rule=rule,
        rehearsal=not margins.is_committed or bool(margins.pending_amendments),
    )


def read_values(path: Path) -> dict[str, list[float]]:
    values: dict[str, list[float]] = defaultdict(list)
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        missing = {"endpoint", "session", "value"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing columns {sorted(missing)}")
        seen = set()
        for row in reader:
            key = (row["endpoint"], row["session"])
            if key in seen:
                raise ValueError(f"{path}: {key} appears twice; one value per session")
            seen.add(key)
            values[row["endpoint"]].append(float(row["value"]))
    return dict(values)


def render(plan: Plan) -> str:
    lines = []
    if plan.rehearsal:
        lines.append("REHEARSAL against DRAFT margins: this is not the study's interim look.")
    lines += [str(p) for p in plan.endpoints]
    r = plan.rule
    lines.append(
        f"binding: {plan.binding}. Final n = min({r.cap}, max({r.floor}, required)) = {plan.final_n}"
        + ("; the binding endpoint needs more than the cap" if plan.capped else "")
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--values", type=Path, required=True, help="CSV: endpoint, session, value")
    p.add_argument("--margins", type=Path, default=DEFAULT_MARGINS)
    p.add_argument("--allow-draft", action="store_true", help="rehearse against draft margins")
    p.add_argument("--json", type=Path)
    args = p.parse_args(argv)
    try:
        margins = load_margins(args.margins, allow_draft=args.allow_draft)
        margins.require_settled(allow_pending=args.allow_draft)
    except MarginsNotCommitted as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    plan = reestimate(margins, read_values(args.values))
    print(render(plan))
    if args.json:
        out = asdict(plan)
        out["rule"] = asdict(plan.rule)
        args.json.write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
