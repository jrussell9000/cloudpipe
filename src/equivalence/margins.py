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
- an entry's `test` decides its shape — `non_inferiority` for a tier-1 agreement
  statistic, with `threshold`, and `tost` for a difference, with `margin` — so an
  endpoint cannot be tested the wrong way round by a caller's mistake.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .equivalence import FISHER_Z

#: Shipped beside this module, so the margins travel with the code that reads them.
DEFAULT_MARGINS = Path(__file__).with_name("margins.yaml")

COMMITTED = "committed"
DRAFT = "draft"

NON_INFERIORITY = "non_inferiority"
TOST = "tost"

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

    @property
    def is_one_sided(self) -> bool:
        return self.test == NON_INFERIORITY

    @property
    def uses_fisher_z(self) -> bool:
        return self.scale == FISHER_Z

    def __str__(self) -> str:
        kind = "threshold" if self.is_one_sided else "margin ±"
        return f"{self.name} (tier {self.tier}): {kind}{self.value:g} {self.unit}"


@dataclass(frozen=True)
class Margins:
    """The whole declared set, plus the endpoints recommended as descriptive."""

    status: str
    power: float
    alpha: float
    entries: dict[str, Margin]
    recommended_descriptive: dict[str, str]
    source: Path

    @property
    def is_committed(self) -> bool:
        return self.status == COMMITTED

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

    test = raw["test"]
    if test == NON_INFERIORITY:
        if "threshold" not in raw:
            raise MarginsInvalid(
                f"{source}: {raw['name']!r} is a {NON_INFERIORITY} endpoint and needs a "
                "'threshold', not a 'margin' — it is tested one-sided against a floor."
            )
        value = float(raw["threshold"])
    elif test == TOST:
        if "margin" not in raw:
            raise MarginsInvalid(
                f"{source}: {raw['name']!r} is a {TOST} endpoint and needs a 'margin', "
                "not a 'threshold' — it is tested two-sided against ±margin."
            )
        value = float(raw["margin"])
        if value < 0:
            raise MarginsInvalid(f"{source}: {raw['name']!r} has a negative margin")
    else:
        raise MarginsInvalid(
            f"{source}: {raw['name']!r} has test {test!r}; expected {NON_INFERIORITY!r} or {TOST!r}"
        )

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
        test=test,
        scale=raw["scale"],
        value=value,
        unit=raw["unit"],
        justification=justification,
        confidence=raw.get("confidence", ""),
        anchor=raw.get("anchor", ""),
    )


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

    return Margins(
        status=status,
        power=float(raw.get("power", 0.8)),
        alpha=float(raw.get("alpha", 0.05)),
        entries={e.name: e for e in entries},
        recommended_descriptive=descriptive,
        source=path,
    )
