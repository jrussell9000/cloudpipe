"""One value per session per endpoint, on the endpoint's test scale (spec, D19).

The analysis unit is the subject-session. A session's value is the unweighted
mean over its runs, taken on the scale the endpoint is tested on, and the scale
comes from the endpoint's committed margin, never from the caller:

- **Fisher z** (agreement correlations): the mean of arctanh(r) over the runs.
  Back-transformed, tanh of it is the session's r.
- **Log ratio** (`tsnr_median`, `gcor`, `aor`, `aqi`): log(cloudpipe's session
  geometric mean / fMRIPrep's), which is the mean over runs of log(x + offset)
  for cloudpipe minus the same for fMRIPrep. The offset is the margin's
  `floor_offset`; with none, a value ≤ 0 is refused, never dropped, because a
  dropped run would make the two arms' geometric means span different runs.
- **Identity** (`n_cosines`): the mean over runs of cloudpipe − fMRIPrep.
- **Binomial ceiling** (`mean_image_low_share`): 1 when the session's mean-image r
  (tanh of its mean Fisher z) is below the margin's `below`, else 0.

Session-grain endpoints (the registration Dice, one value per session per arm)
are passed in already at session grain. Both arms' session values are kept on
the natural scale beside the tested value, for the side-by-side table (D13).
"""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .equivalence import FISHER_Z
from .margins import BINOMIAL_CEILING, LOG_RATIO, NON_INFERIORITY, Margin, Margins

#: The binomial companion reads its source endpoint's session r.
COMPANION_SOURCE = {"mean_image_low_share": "mean_image_correlation"}

#: Agreement endpoints measured per run, as one r for the pair.
_R_CLIP = 1.0 - 1e-7


class SessionValueError(ValueError):
    """A session's runs cannot produce a value for an endpoint on its test scale."""


@dataclass(frozen=True)
class RunMeasures:
    """One run's measures: agreement statistics, and each arm's own record."""

    task: str
    run: str
    #: endpoint -> r, for the agreement endpoints (one number per pair).
    agreement: dict[str, float]
    #: FuncQC-shaped fields, measured by the one ruler on each arm's outputs.
    cloudpipe: dict[str, float]
    reference: dict[str, float]


@dataclass(frozen=True)
class SessionValue:
    """One endpoint's session value on its test scale, and both arms' values."""

    endpoint: str
    value: float
    #: Natural scale: the session's r for agreement, each arm's geometric mean
    #: (or plain mean) for the per-arm endpoints. None where the endpoint is one
    #: number for the pair.
    cloudpipe: float | None = None
    reference: float | None = None
    n_runs: int = 0


@dataclass(frozen=True)
class Session:
    subject: str
    session: str
    values: dict[str, SessionValue] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.subject}_{self.session}"


def _fisher_mean(rs: list[float]) -> float:
    return float(np.mean(np.arctanh(np.clip(rs, -_R_CLIP, _R_CLIP))))


def _log_geometric_mean(values: list[float], offset: float, endpoint: str, arm: str) -> float:
    shifted = np.asarray(values, dtype=np.float64) + offset
    if (shifted <= 0).any():
        raise SessionValueError(
            f"{endpoint}: {arm} has a run with value {min(values)} and the margin's "
            f"floor_offset is {offset}, so its log is undefined. Dropping the run would "
            "make the two arms' geometric means span different runs; declare an offset."
        )
    return float(np.mean(np.log(shifted)))


def _per_run(runs: list[RunMeasures], name: str, attr: str) -> list[float]:
    try:
        return [float(getattr(r, attr)[name]) for r in runs]
    except KeyError:
        raise SessionValueError(f"a run has no {attr} value for {name!r}") from None


def session_value(margin: Margin, runs: list[RunMeasures]) -> SessionValue:
    """The session's value for one run-grain endpoint, from its margin's scale."""
    if not runs:
        raise SessionValueError(f"{margin.name}: no runs")
    name, n = margin.name, len(runs)

    if margin.test == BINOMIAL_CEILING:
        source = COMPANION_SOURCE.get(name)
        if source is None:
            raise SessionValueError(f"{name}: no source endpoint declared for the ceiling")
        r = math.tanh(_fisher_mean(_per_run(runs, source, "agreement")))
        return SessionValue(name, float(r < margin.below), cloudpipe=r, n_runs=n)

    if margin.scale == FISHER_Z:
        z = _fisher_mean(_per_run(runs, name, "agreement"))
        return SessionValue(name, z, cloudpipe=math.tanh(z), n_runs=n)

    a = _per_run(runs, name, "cloudpipe")
    b = _per_run(runs, name, "reference")
    if margin.scale == LOG_RATIO:
        log_a = _log_geometric_mean(a, margin.floor_offset, name, "cloudpipe")
        log_b = _log_geometric_mean(b, margin.floor_offset, name, "fMRIPrep")
        return SessionValue(
            name,
            log_a - log_b,
            cloudpipe=math.exp(log_a) - margin.floor_offset,
            reference=math.exp(log_b) - margin.floor_offset,
            n_runs=n,
        )
    return SessionValue(
        name,
        float(np.mean(np.subtract(a, b))),
        cloudpipe=float(np.mean(a)),
        reference=float(np.mean(b)),
        n_runs=n,
    )


def session_grain_value(margin: Margin, cloudpipe: float, reference: float) -> SessionValue:
    """An identity-scale non-inferiority endpoint measured once per session per arm."""
    if margin.test != NON_INFERIORITY or margin.scale == FISHER_Z:
        raise SessionValueError(f"{margin.name} is not an identity-scale session endpoint")
    return SessionValue(margin.name, cloudpipe - reference, cloudpipe, reference, n_runs=0)


def build_session(
    margins: Margins,
    subject: str,
    session: str,
    runs: list[RunMeasures],
    session_grain: dict[str, tuple[float, float]] | None = None,
) -> Session:
    """Every declared endpoint's session value. An endpoint with no input is absent.

    `session_grain` maps a session-grain endpoint to (cloudpipe, fMRIPrep).
    """
    session_grain = session_grain or {}
    values = {}
    for name, margin in margins.entries.items():
        if name in session_grain:
            values[name] = session_grain_value(margin, *session_grain[name])
            continue
        try:
            values[name] = session_value(margin, runs)
        except SessionValueError:
            if _has_input(margin, runs):
                raise
    return Session(subject, session, values)


def _has_input(margin: Margin, runs: list[RunMeasures]) -> bool:
    source = COMPANION_SOURCE.get(margin.name, margin.name)
    return any(
        source in r.agreement or source in r.cloudpipe or source in r.reference for r in runs
    )


VALUE_COLUMNS = ["endpoint", "session", "value", "cloudpipe", "reference", "n_runs"]


def write_values(path: Path, sessions: Iterable[Session]) -> None:
    """The per-session table, read by `equivalence.reestimate --values` and the analysis.

    `value` is on the test scale; `cloudpipe` and `reference` on the natural one.
    """
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(VALUE_COLUMNS)
        for s in sessions:
            for v in sorted(s.values.values(), key=lambda v: v.endpoint):
                w.writerow(
                    [
                        v.endpoint,
                        s.key,
                        repr(v.value),
                        _cell(v.cloudpipe),
                        _cell(v.reference),
                        v.n_runs,
                    ]
                )


def _cell(x: float | None) -> str:
    return "" if x is None else repr(x)
