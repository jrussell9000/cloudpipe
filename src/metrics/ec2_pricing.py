"""
ec2_pricing.py — On-demand list price per EC2 instance type, from the AWS
Pricing API.

Exists to make the no-spot counterfactual computable. Every Karpenter nodepool
in this cluster is spot-only (ADR 007), so Kubecost only ever observes spot
rates; the on-demand side of "what would this batch have cost without spot" has
to come from outside the cluster.

WHY THE API RATHER THAN A TABLE IN THE REPO
    A hardcoded {instance_type: price} map would be smaller and need no IAM
    grant, but it rots silently. Karpenter's price-capacity-optimized strategy
    reaches across whole families and picks up new ones as AWS ships them: a
    single day (2026-08-11) placed pipeline pods on c5, t3, c7i, c7i-flex, c8i,
    c8i-flex and g4dn, and the -flex variants did not exist when the nodepools
    were written. An unmatched type in a static map yields no counterfactual for
    that node, and nothing would flag it.

    The API is queried once per distinct instance type per scrape run and cached
    in process — a handful of calls a night, not one per pod.

LIST PRICE, NOT BILLED PRICE
    This returns the public on-demand rate. If the account holds a savings plan
    or reserved instances, real on-demand spend would be lower, so the
    counterfactual built on this is "on-demand at list", i.e. the conservative
    upper bound on what abandoning spot would cost. That is the intended
    reading, but it is a reading — do not present it as a billed figure.

The Pricing API is only served from a few regions (us-east-1 among them)
regardless of which region you are pricing, hence PRICING_API_REGION.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# The Pricing API has no regional endpoint in <YOUR_AWS_REGION>. Querying it from
# <YOUR_AWS_REGION> fails to connect; the `regionCode` filter is what selects the
# region being priced, not the client's region.
PRICING_API_REGION = "us-east-1"

# Shared-tenancy Linux, no pre-installed software, and capacitystatus=Used —
# the combination that yields exactly one price per instance type. Without
# capacitystatus the same type also returns its Reserved and
# CapacityBlock/UnusedCapacityReservation SKUs, and picking "the first" among
# those is a coin flip.
_BASE_FILTERS = {
    "tenancy": "Shared",
    "operatingSystem": "Linux",
    "preInstalledSw": "NA",
    "capacitystatus": "Used",
}

_cache: dict[tuple[str, str], float | None] = {}


def _extract_hourly(price_list_entry: str) -> float | None:
    """Pull the USD/hour figure out of one Pricing API PriceList entry.

    Entries arrive as JSON *strings*, not objects. The OnDemand term nests two
    levels of opaque SKU keys before reaching priceDimensions, so both are
    walked positionally rather than by name.
    """
    try:
        product = json.loads(price_list_entry)
    except (TypeError, ValueError):
        return None

    for term in (product.get("terms", {}).get("OnDemand") or {}).values():
        for dim in (term.get("priceDimensions") or {}).values():
            if dim.get("unit") not in ("Hrs", "Hours"):
                continue
            raw = (dim.get("pricePerUnit") or {}).get("USD")
            try:
                price = float(raw)
            except (TypeError, ValueError):
                continue
            # A $0.00 dimension is a free-tier or placeholder row, not a real
            # rate; treating it as one would zero out the counterfactual.
            if price > 0:
                return price
    return None


def ondemand_hourly_usd(
    instance_type: str,
    region: str = "<YOUR_AWS_REGION>",
    client: Any = None,
) -> float | None:
    """On-demand list price in USD/hour, or None if it cannot be determined.

    Never raises. A missing price is a missing counterfactual for one node,
    which callers record as NULL; it must not be able to fail the cost scrape
    that surrounds it, so every failure mode — unknown instance type, missing
    pricing:GetProducts grant, throttling, network — collapses to None and a
    log line.

    Results are memoized per (instance_type, region) for the life of the
    process, including negative results: a type AWS does not price will not be
    re-queried on every node that used it.
    """
    if not instance_type:
        return None

    cache_key = (instance_type, region)
    if cache_key in _cache:
        return _cache[cache_key]

    price: float | None = None
    try:
        if client is None:
            import boto3

            client = boto3.client("pricing", region_name=PRICING_API_REGION)

        filters = [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        ]
        filters += [
            {"Type": "TERM_MATCH", "Field": field, "Value": value}
            for field, value in _BASE_FILTERS.items()
        ]

        resp = client.get_products(ServiceCode="AmazonEC2", Filters=filters, MaxResults=10)
        for entry in resp.get("PriceList", []):
            price = _extract_hourly(entry)
            if price is not None:
                break

        if price is None:
            log.warning(
                "No on-demand list price found for %s in %s — its nodes will carry a NULL "
                "counterfactual rate",
                instance_type,
                region,
            )
    except Exception as exc:  # noqa: BLE001 — see docstring: never fatal
        log.warning("Pricing lookup failed for %s in %s: %s", instance_type, region, exc)
        price = None

    _cache[cache_key] = price
    return price


def clear_cache() -> None:
    """Drop the memo table. For tests, and for long-lived processes that want
    to pick up a price change across runs."""
    _cache.clear()
