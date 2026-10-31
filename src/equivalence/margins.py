"""The pre-registered margins, loaded from `margins.yaml` and gated on their status.

D7 requires a margin per endpoint, declared before any result, with its
justification recorded beside it. The spec makes the file's commit date the
evidence that the margins preceded the results.

That evidence is only worth something if a draft cannot be used by accident, so
`load_margins` refuses a file whose `status` is not `committed` unless the caller
explicitly asks for a draft. A comparison run against draft margins is not
pre-registered, and nothing here should let that be an oversight.

Two further rules, both enforced rather than documented:

- every entry carries a non-empty justification, because D7's failure mode is a
  margin whose only defence is that the observed interval fits inside it;
- an entry's `test` decides its shape — `non_inferiority` for an agreement
  statistic, with `threshold`; `tost` for a difference, with `margin` (or, on the
  `log_ratio` scale, `ratio_upper`); and `binomial_ceiling` for the share of
  sessions below a value, with `below` and `ceiling` — so an endpoint cannot be
  tested the wrong way round by a caller's mistake.

The file also carries the variance-only sample-size re-estimation rule (design
D19) in a `sample_size` block. It is pre-registered with the margins, so it is
validated here, and every primary endpoint the rule sizes must have an assumed
true value declared in it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .equivalence import FISHER_Z

#: Shipped beside this module, so the margins travel with the code that reads them.
DEFAULT_MARGINS = Path(__file__).with_name("margins.yaml")

COMMITTED = "committed"
DRAFT = "draft"

NON_INFERIORITY = "non_inferiority"
TOST = "tost"
BINOMIAL_CEILING = "binomial_ceiling"
TESTS = (NON_INFERIORITY, TOST, BINOMIAL_CEILING)

#: A TOST on the log of a ratio of session geometric means. Declared with
#: `ratio_upper` (e.g. 1.25); the margin is ln(ratio_upper), symmetric on the log
#: scale, so 1.25 means bounds 0.80–1.25.
LOG_RATIO = "log_ratio"

#: A justification shorter than this is not one.
MIN_JUSTIFICATION = 80


class MarginsNotCommitted(RuntimeError):
    """The margin file is still a draft, so a primary analysis may not use it."""


class MarginsInvalid(ValueError):
    """The margin file is malformed, or an entry is missing its justification."""


@dataclass(frozen=True)
class Margin:
    """One endpoint's committed margin and the reasoning behind it."""

    name: str
    tier: int
    test: str
    scale: str
    value: float
    unit: str
    justification: str
    confidence: str = ""
    anchor: str = ""
    #: binomial_ceiling only: the per-session value a session must not fall below.
    below: float | None = None
    #: log_ratio only: added to every run's value before its log is taken, so a
    #: run whose metric is exactly 0 (aor with no outlier frames) has one. 0 means
    #: none, and a zero value is then refused rather than dropped.
    floor_offset: float = 0.0

    @property
    def is_one_sided(self) -> bool:
        return self.test in (NON_INFERIORITY, BINOMIAL_CEILING)

    @property
    def uses_fisher_z(self) -> bool:
        return self.scale == FISHER_Z

    @property
    def is_exact(self) -> bool:
        """A TOST with margin 0: any difference is a defect, so nothing to size."""
        return self.test == TOST and self.value == 0

    def __str__(self) -> str:
        if self.test == BINOMIAL_CEILING:
            return (
                f"{self.name} (tier {self.tier}): at most {self.value:.0%} of sessions "
                f"below {self.below:g}"
            )
        kind = "threshold" if self.test == NON_INFERIORITY else "margin ±"
        return f"{self.name} (tier {self.tier}): {kind}{self.value:g} {self.unit}"


@dataclass(frozen=True)
class SampleSizeRule:
    """The pre-registered variance-only re-estimation (design D19).

    The first `interim` sessions of the ordered draw are run, one look reads each
    primary endpoint's between-session SD (never a mean, interval or verdict),
    and the final n is min(cap, max(floor, the largest required n)).
    """

    floor: int
    cap: int
    interim: int
    power: float
    alpha: float
    #: The SD used is the upper one-sided confidence limit at this level.
    sd_confidence: float
    #: Endpoint -> the assumed true value the required n is computed under, on the
    #: endpoint's own scale: a ratio for log-ratio TOSTs, r for Fisher-z endpoints,
    #: a difference for identity-scale non-inferiority, a share for the ceiling.
    assumed: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Margins:
    """The whole declared set, plus the endpoints recommended as descriptive."""

    status: str
    power: float
    alpha: float
    entries: dict[str, Margin]
    recommended_descriptive: dict[str, str]
    source: Path
    sample_size: SampleSizeRule | None = None
    #: Ids of amendments declared but not yet carried into the values above.
    pending_amendments: tuple[str, ...] = ()

    @property
    def is_committed(self) -> bool:
        return self.status == COMMITTED

    def require_settled(self, allow_pending: bool = False) -> None:
        """Refuse a comparison while an amendment is pending, unless rehearsing.

        A pending amendment has declared that some values above will change, so a
        result computed against them now would be against margins already known
        to be superseded.
        """
        if self.pending_amendments and not allow_pending:
            raise MarginsNotCommitted(
                f"{self.source}: amendment(s) {list(self.pending_amendments)} are pending. "
                "Record their values (state: recorded) before computing a result; "
                "pass the rehearsal flag only for a run whose result is not reported."
            )

    def __getitem__(self, endpoint: str) -> Margin:
        try:
            return self.entries[endpoint]
        except KeyError:
            raise MarginsInvalid(
                f"{endpoint!r} has no declared margin. Declared: {sorted(self.entries)}. "
                f"Recommended descriptive: {sorted(self.recommended_descriptive)}. An "
                "endpoint without a margin cannot contribute to the equivalence decision."
            ) from None

    def tier(self, tier: int) -> list[Margin]:
        return [m for m in self.entries.values() if m.tier == tier]


def _entry(raw: dict, source: Path) -> Margin:
    for required in ("name", "tier", "test", "scale", "unit", "justification"):
        if not raw.get(required):
            raise MarginsInvalid(f"{source}: an entry is missing {required!r}: {raw!r}")

    value, below = _value(raw, source)
    floor_offset = float(raw.get("floor_offset", 0.0))
    if floor_offset and raw["scale"] != LOG_RATIO:
        raise MarginsInvalid(
            f"{source}: {raw['name']!r} has a floor_offset, which only a {LOG_RATIO} "
            "endpoint takes."
        )
    if floor_offset < 0:
        raise MarginsInvalid(f"{source}: {raw['name']!r} has a negative floor_offset")

    justification = " ".join(str(raw["justification"]).split())
    if len(justification) < MIN_JUSTIFICATION:
        raise MarginsInvalid(
            f"{source}: {raw['name']!r} has a {len(justification)}-character "
            f"justification, under the {MIN_JUSTIFICATION} this file requires. D7 exists "
            "because an unjustified margin is the first thing a reviewer attacks."
        )

    return Margin(
        name=raw["name"],
        tier=int(raw["tier"]),
        test=raw["test"],
        scale=raw["scale"],
        value=value,
        unit=raw["unit"],
        justification=justification,
        confidence=raw.get("confidence", ""),
        anchor=raw.get("anchor", ""),
        below=below,
        floor_offset=floor_offset,
    )


def _value(raw: dict, source: Path) -> tuple[float, float | None]:
    """The entry's margin, threshold or ceiling, shaped by its test; and `below`."""
    test = raw["test"]
    below = None
    if test == NON_INFERIORITY:
        if "threshold" not in raw:
            raise MarginsInvalid(
                f"{source}: {raw['name']!r} is a {NON_INFERIORITY} endpoint and needs a "
                "'threshold', not a 'margin' — it is tested one-sided against a floor."
            )
        value = float(raw["threshold"])
    elif test == TOST and raw["scale"] == LOG_RATIO:
        if "ratio_upper" not in raw or "margin" in raw:
            raise MarginsInvalid(
                f"{source}: {raw['name']!r} is a {LOG_RATIO} {TOST} endpoint and needs "
                "'ratio_upper' (e.g. 1.25 for bounds 0.80-1.25), not a 'margin'."
            )
        upper = float(raw["ratio_upper"])
        if upper <= 1:
            raise MarginsInvalid(f"{source}: {raw['name']!r} has ratio_upper {upper}; must be > 1")
        value = math.log(upper)
    elif test == TOST:
        if "margin" not in raw:
            raise MarginsInvalid(
                f"{source}: {raw['name']!r} is a {TOST} endpoint and needs a 'margin', "
                "not a 'threshold' — it is tested two-sided against ±margin."
            )
        value = float(raw["margin"])
        if value < 0:
            raise MarginsInvalid(f"{source}: {raw['name']!r} has a negative margin")
    elif test == BINOMIAL_CEILING:
        if "ceiling" not in raw or "below" not in raw:
            raise MarginsInvalid(
                f"{source}: {raw['name']!r} is a {BINOMIAL_CEILING} endpoint and needs "
                "'below' (the per-session value) and 'ceiling' (the share allowed below it)."
            )
        value = float(raw["ceiling"])
        below = float(raw["below"])
        if not 0 < value < 1:
            raise MarginsInvalid(
                f"{source}: {raw['name']!r} has ceiling {value}; must be in (0, 1)"
            )
    else:
        raise MarginsInvalid(
            f"{source}: {raw['name']!r} has test {test!r}; expected one of {', '.join(TESTS)}"
        )
    return value, below


def _sample_size(raw: dict | None, entries: dict[str, Margin], source: Path) -> SampleSizeRule:
    """Validate the re-estimation rule against the declared endpoints."""
    if not isinstance(raw, dict):
        raise MarginsInvalid(
            f"{source}: no 'sample_size' block. The re-estimation rule (design D19) is "
            "pre-registered with the margins, so the file must carry it."
        )
    try:
        rule = SampleSizeRule(
            floor=int(raw["floor"]),
            cap=int(raw["cap"]),
            interim=int(raw["interim"]),
            power=float(raw["power"]),
            alpha=float(raw["alpha"]),
            sd_confidence=float(raw["sd_confidence"]),
            assumed={k: float(v) for k, v in (raw.get("assumed") or {}).items()},
        )
    except KeyError as missing:
        raise MarginsInvalid(f"{source}: sample_size is missing {missing}") from None
    if not 0 < rule.interim < rule.floor <= rule.cap:
        raise MarginsInvalid(
            f"{source}: sample_size needs 0 < interim < floor <= cap; got interim "
            f"{rule.interim}, floor {rule.floor}, cap {rule.cap}"
        )
    for name, level in (
        ("power", rule.power),
        ("alpha", rule.alpha),
        ("sd_confidence", rule.sd_confidence),
    ):
        if not 0 < level < 1:
            raise MarginsInvalid(f"{source}: sample_size {name} {level} must be in (0, 1)")
    unknown = set(rule.assumed) - set(entries)
    if unknown:
        raise MarginsInvalid(
            f"{source}: sample_size assumes values for undeclared {sorted(unknown)}"
        )
    unsized = sorted(n for n, m in entries.items() if not m.is_exact and n not in rule.assumed)
    if unsized:
        raise MarginsInvalid(
            f"{source}: sample_size declares no assumed true value for {unsized}. Every "
            "declared endpoint except an exact one is sized at the interim, and its "
            "assumed value must be fixed in advance, never taken from the interim's outcomes."
        )
    return rule


def load_margins(path: Path = DEFAULT_MARGINS, allow_draft: bool = False) -> Margins:
    """Load the margins, refusing a draft unless one is explicitly asked for.

    `allow_draft=True` is for inspecting or reporting on a draft — never for a
    comparison whose result will be presented as pre-registered.
    """
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise MarginsInvalid(f"{path}: expected a mapping at the top level")

    status = raw.get("status")
    if status not in (COMMITTED, DRAFT):
        raise MarginsInvalid(f"{path}: status is {status!r}; expected {COMMITTED!r} or {DRAFT!r}")
    if status != COMMITTED and not allow_draft:
        raise MarginsNotCommitted(
            f"{path}: status is {status!r}. A comparison run against draft margins is not "
            "pre-registered, and D7 makes the commit date of the committed file the "
            "evidence that the margins preceded the results. Review the entries, set "
            "status: committed, and commit before computing the first result. Pass "
            "allow_draft=True only to inspect or report on the draft."
        )

    entries = [_entry(e, path) for e in raw.get("endpoints") or []]
    if not entries:
        raise MarginsInvalid(f"{path}: no endpoints declared")

    names = [e.name for e in entries]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise MarginsInvalid(f"{path}: duplicate endpoints {sorted(duplicates)}")

    descriptive = {}
    for item in raw.get("recommended_descriptive") or []:
        if not item.get("name") or not item.get("reason"):
            raise MarginsInvalid(f"{path}: a descriptive entry needs a name and a reason")
        descriptive[item["name"]] = " ".join(str(item["reason"]).split())

    overlap = set(descriptive) & set(names)
    if overlap:
        raise MarginsInvalid(
            f"{path}: {sorted(overlap)} appear both as declared endpoints and as "
            "recommended descriptive. An endpoint is one or the other."
        )

    by_name = {e.name: e for e in entries}
    pending = _amendments(raw.get("amendments"), path)
    return Margins(
        status=status,
        power=float(raw.get("power", 0.8)),
        alpha=float(raw.get("alpha", 0.05)),
        entries=by_name,
        recommended_descriptive=descriptive,
        source=path,
        sample_size=_sample_size(raw.get("sample_size"), by_name, path),
        pending_amendments=pending,
    )


AMENDMENT_STATES = ("pending", "recorded")


def _amendments(raw: list | None, source: Path) -> tuple[str, ...]:
    """Validate the amendment log; return the ids still pending."""
    pending = []
    for item in raw or []:
        for required in ("id", "declared", "state", "section", "entries", "reason"):
            if not item.get(required):
                raise MarginsInvalid(f"{source}: an amendment is missing {required!r}: {item!r}")
        if item["state"] not in AMENDMENT_STATES:
            raise MarginsInvalid(
                f"{source}: amendment {item['id']!r} has state {item['state']!r}; "
                f"expected one of {AMENDMENT_STATES}"
            )
        if item["state"] == "recorded" and not item.get("recorded"):
            raise MarginsInvalid(
                f"{source}: amendment {item['id']!r} is recorded but has no 'recorded' date"
            )
        if item["state"] == "pending":
            pending.append(item["id"])
    return tuple(pending)
