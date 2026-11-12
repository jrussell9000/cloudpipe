"""Reprocess-audit bucket pairs and workflow-name prefixes (openspec add-reprocess-audit).

The audit runs the production `cloudpipe` WorkflowTemplate into its own bucket
pairs so nothing it writes reaches production derivatives, metrics or cost
tables (design D1–D3). Two pairs:

  primary  the main sample, and each noise-floor subject's first run
  repeat   each noise-floor subject's second run (a second run into the
           primary pair would find the first run's outputs and skip)

A workflow's pair is named by its generateName prefix, and that prefix is
the ONLY thing the cost scraper sees, so the queue manager and the scraper
must agree on it: both import it from here. The bucket names live in one
Prefect Variable, PAIRS_VARIABLE, as JSON:

    {"primary": {"bucket": "...", "metrics_bucket": "..."},
     "repeat":  {"bucket": "...", "metrics_bucket": "..."}}

Standard library only: the flow runner imports this from src/metrics/.
"""

from __future__ import annotations

import json
from typing import Any

PAIRS_VARIABLE = "cloudpipe-audit-buckets"

PRIMARY = "primary"
REPEAT = "repeat"

# Longest first: every repeat name also starts with the primary prefix.
PREFIXES: dict[str, str] = {
    "cloudpipe-audit-repeat-": REPEAT,
    "cloudpipe-audit-": PRIMARY,
}


class AuditConfigError(ValueError):
    """A submission or a pairs value that would let audit and production mix."""


def pair_of(workflow_name: str) -> str | None:
    """PRIMARY / REPEAT for an audit workflow name or prefix; None for anything else."""
    for prefix, pair in PREFIXES.items():
        if workflow_name.startswith(prefix):
            return pair
    return None


def is_audit(workflow_name: str) -> bool:
    return pair_of(workflow_name) is not None


def parse_pairs(raw: Any) -> dict[str, dict[str, str]]:
    """Validate the PAIRS_VARIABLE value; {} when it is unset or empty.

    Refuses a value naming the same bucket twice, because one bucket in two
    roles is how a repeat run would land on the primary run's outputs.
    """
    if raw in (None, "", {}):
        return {}
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict) or set(value) - {PRIMARY, REPEAT}:
        raise AuditConfigError(f"{PAIRS_VARIABLE} must map 'primary'/'repeat' to bucket pairs")
    pairs: dict[str, dict[str, str]] = {}
    for pair, spec in value.items():
        if not isinstance(spec, dict) or not spec.get("bucket") or not spec.get("metrics_bucket"):
            raise AuditConfigError(
                f"{PAIRS_VARIABLE}[{pair!r}] needs 'bucket' and 'metrics_bucket'"
            )
        pairs[pair] = {"bucket": spec["bucket"], "metrics_bucket": spec["metrics_bucket"]}
    names = [b for spec in pairs.values() for b in spec.values()]
    if len(names) != len(set(names)):
        raise AuditConfigError(f"{PAIRS_VARIABLE} names one bucket more than once: {names}")
    return pairs


def audit_buckets(pairs: dict[str, dict[str, str]]) -> set[str]:
    return {b for spec in pairs.values() for b in spec.values()}


def audit_metrics_buckets(pairs: dict[str, dict[str, str]]) -> set[str]:
    return {spec["metrics_bucket"] for spec in pairs.values()}


def check_submission(
    name_prefix: str,
    bucket: str,
    metrics_bucket: str,
    workflow_template: str,
    pairs: dict[str, dict[str, str]],
) -> None:
    """Refuse any prefix / bucket combination that does not belong together (spec audit-isolation).

    - An audit prefix needs exactly its own pair's buckets, both of them, and
      the production `cloudpipe` template: the audit measures what production
      runs, so it must not run a copy (design D2).
    - Any other prefix (including the default, empty) must not name an audit
      bucket. Empty buckets mean the ConfigMap's production defaults.
    """
    pair = pair_of(name_prefix)
    if pair is None:
        stray = {bucket, metrics_bucket} & audit_buckets(pairs)
        if stray:
            raise AuditConfigError(
                f"name_prefix {name_prefix!r} is not an audit prefix but the buckets name an "
                f"audit bucket ({sorted(stray)}); audit runs must use "
                f"{' or '.join(repr(p) for p in PREFIXES)}"
            )
        return
    if name_prefix not in PREFIXES:
        raise AuditConfigError(
            f"name_prefix {name_prefix!r} must be exactly one of {list(PREFIXES)}"
        )
    if pair not in pairs:
        raise AuditConfigError(
            f"{name_prefix!r} needs the {pair!r} pair in Prefect Variable {PAIRS_VARIABLE}"
        )
    want = pairs[pair]
    if (bucket, metrics_bucket) != (want["bucket"], want["metrics_bucket"]):
        raise AuditConfigError(
            f"{name_prefix!r} must use the {pair!r} pair ({want['bucket']}, "
            f"{want['metrics_bucket']}), not ({bucket or '<default>'}, "
            f"{metrics_bucket or '<default>'})"
        )
    if workflow_template != "cloudpipe":
        raise AuditConfigError(
            f"audit runs use the production 'cloudpipe' template, not {workflow_template!r}"
        )


def metrics_bucket_for(
    workflow_name: str, default_bucket: str, pairs: dict[str, dict[str, str]]
) -> str | None:
    """Where a workflow's metrics and cost records belong.

    Production workflows: `default_bucket`. Audit workflows: their pair's
    metrics bucket, or None when that pair is not configured. None means DROP,
    never "fall back to production" (spec audit-isolation).
    """
    pair = pair_of(workflow_name)
    if pair is None:
        return default_bucket
    spec = pairs.get(pair)
    return spec["metrics_bucket"] if spec else None
