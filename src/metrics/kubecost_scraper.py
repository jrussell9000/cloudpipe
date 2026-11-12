"""
kubecost_scraper.py — Scrapes yesterday's per-subject cost data from Kubecost
and writes CostAllocation JSON records to S3.

Kubecost Allocation API endpoint (in-cluster) — see KUBECOST_BASE below, which is
the authority; this docstring named a `kubecost-cost-analyzer` service that does not
exist in the kubecost namespace (verified 2026-07-31):
  http://kubecost-frontend.kubecost.svc.cluster.local:9090/model/allocation
  ?window=yesterday&accumulate=true&aggregate=label:subjectid

The response groups cost by the Kubernetes label `subjectid`, which must be
set on all cloudpipe workflow pods (see podMetadata in the master workflow
template).  Each allocation entry is mapped to a CostAllocation record and
written to s3://{bucket}/metrics/costs/dt={date}/{date}_{workflow_name}_cost_allocation.json.

TWO GRAINS, TWO PASSES, ONE WINDOW
    scrape_and_upload()           aggregate=label:<workflow>  -> metrics/costs/
    scrape_pod_costs_and_upload() aggregate=pod               -> metrics/pod-costs/

The pod pass exists to answer "which workflow COMPONENT cost that", which the
workflow totals cannot: it keeps the per-template `cloudpipe.io/step` and
`cloudpipe.io/phase` pod labels on each row, so cost rolls up by component in
SQL while per-pod variance (BOLD run count, node placement) stays visible.
Both passes share _resolve_window(), so pod costs always sum to their
workflow's CostAllocation for the same date.

The passes are independent by design — the pod pass is newer and its label
parsing is the only part that can be wrong, so it must never be able to take
the validated workflow-grain scrape down with it. The Prefect flow runs it
best-effort for that reason.

NO-SPOT COUNTERFACTUAL
    The pod pass makes a third call, to the Assets API (filterTypes=Node), to
    learn each node's own hourly rate — the Allocation API prices pod SHARES
    and never exposes it. That rate, paired with the instance type's on-demand
    list price from ec2_pricing, lands on every PodCost row as
    node_effective_usd_per_hour / node_ondemand_usd_per_hour, which is what
    makes "what would this batch have cost without spot" answerable at all:
    every nodepool is spot-only (ADR 007), so no on-demand pipeline cost is
    ever observed directly. This third call is best-effort — it can only ever
    NULL three columns, never fail the scrape.

    Pods are matched to an INSTANCE, not a hostname (#1060): private IPs are
    reused within a day, so a hostname can name several instances. A pod whose
    instance cannot be proven gets NULL rates — see resolve_node, and why
    the settled day+3 read proves fewer than the day+1 read. The day+1 read
    records the instance (node_instance_id, schema 1.2), and the settled read
    carries it forward with the drift-free columns where it cannot prove one
    itself (_node_columns).

SETTLED RE-SCRAPE
    The nightly pass reads each report-date at day+1, before Kubecost has
    finished reconciling it against the AWS CUR, so it overstates settled cost
    by a median ~51% (8-240% across 10 measured dates — wide, not a constant
    you can correct for; see kubecost_drift_probe.py for the table).
    Reconciliation converges at age SETTLED_AGE_DAYS (3) and then
    never moves, so the flow re-scrapes each report-date once at that age and
    overwrites the day+1 records in place; scrape_age_days on the record says
    which read it came from. Overwrites are guarded — see PARTIAL_READ_FRACTION.

Usage:
    python kubecost_scraper.py --bucket "$CLOUDPIPE_METRICS_BUCKET"
    python kubecost_scraper.py --bucket "$CLOUDPIPE_METRICS_BUCKET" --settled   # re-scrape day-3

The metrics bucket, not the data bucket: writes to the data bucket's metrics/
prefix are denied. --region defaults to this deployment's own region.

This module is also imported by prefect/flows/cost_scraper.py.
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Any

import audit_pairs  # type: ignore[import-not-found]
import requests
from ec2_pricing import ondemand_hourly_usd  # type: ignore[import-not-found]
from schemas import CostAllocation, PodCost  # type: ignore[import-not-found]
from writer import emit_jsonl_to_s3, emit_to_s3  # type: ignore[import-not-found]

log = logging.getLogger(__name__)

KUBECOST_BASE = "http://kubecost-frontend.kubecost.svc.cluster.local:9090"
ALLOCATION_PATH = "/model/allocation"

# Node-grain costs live on a different endpoint than pod-grain ones: the
# Allocation API prices POD SHARES of a node and never exposes the node's own
# rate, so the no-spot counterfactual is unanswerable from it. The Assets API
# is where `totalCost` for the node itself, its `minutes`, and its
# `preemptible` flag come from.
ASSETS_PATH = "/model/assets"

# Kubecost has no per-allocation "reconciled yet?" flag (checked against the
# current Allocation API docs — the UI's "unreconciled" highlighting is a
# fixed 36h-since-window heuristic on the frontend, not a queryable field).
# These *CostAdjustment fields are the real, per-allocation signal: nonzero
# means reconciliation against cloud billing has already moved this
# allocation's cost. Also used by kubecost_drift_probe.py, which imports this
# constant rather than keeping its own copy.
ADJUSTMENT_FIELDS = [
    "cpuCostAdjustment",
    "gpuCostAdjustment",
    "ramCostAdjustment",
    "networkCostAdjustment",
    "loadBalancerCostAdjustment",
    "pvCostAdjustment",
]

# Label used to aggregate the workflow-grain pass. Argo stamps it on every
# workflow pod automatically.
WORKFLOW_LABEL = "workflows.argoproj.io/workflow"

# Kubecost sanitizes Kubernetes label keys before exposing them in
# properties.labels: dots and slashes both become underscores. These are the
# sanitized forms of the per-template pod labels the workflow templates set
# (`cloudpipe.io/step`, `cloudpipe.io/phase`, `session`, `subjectid`) — the
# raw keys will never appear in a response, so looking them up unsanitized
# silently yields "" on every pod.
LABEL_WORKFLOW = "workflows_argoproj_io_workflow"
LABEL_STEP = "cloudpipe_io_step"
LABEL_PHASE = "cloudpipe_io_phase"
LABEL_SESSION = "session"
LABEL_SUBJECT = "subjectid"

# Karpenter stamps this on every node it provisions; Kubecost propagates node
# labels onto both allocations and assets. It is absent (not "on-demand") on
# the EKS managed nodegroup nodes, which Karpenter did not create — see
# _capacity_type, which falls back to the `preemptible` field for those.
LABEL_CAPACITY_TYPE = "karpenter_sh_capacity_type"

# Workflows whose cost is deliberately not recorded, by name prefix (Argo names a
# workflow `{template}-{suffix}`). The confounds-only re-run (confounds-rerun,
# 2026-10-28) only repairs TSVs a cloudpipe workflow already produced and was
# costed for, and writes no WorkflowRun row; operator decision: drop its cost
# rather than add it to the subject's. Both scrape grains skip these, and
# cost_gaps does not count them as unbilled.
UNRECORDED_WORKFLOW_PREFIXES = ("confounds-rerun-",)


def cost_is_recorded(workflow_name: str) -> bool:
    return not workflow_name.startswith(UNRECORDED_WORKFLOW_PREFIXES)


_BYTES_PER_GB = 1024**3

# Kubecost reconciliation against the AWS CUR converges at age 3 and then never
# moves again (measured longitudinally from metrics/cost-drift-probe/ — see
# kubecost_drift_probe.py's docstring and GitHub #171). So a report-date is
# re-scraped exactly once, at this age, and that read is final. Ages 1-2 move in
# both directions and are not safe to treat as settled.
SETTLED_AGE_DAYS = 3

# Floor for overwriting an already-stored report-date. Kubecost intermittently
# answers a window with a near-empty allocation set: the drift probe caught
# 2026-07-28 returning 1 workflow / $0.222 at age 2, against 103 workflows /
# $42.5 from every other read of that date. A blind re-scrape landing on such a
# response would replace good stored records with garbage, and nothing
# downstream would flag it. Real read-to-read variation in workflow count is
# small (worst observed: 109 -> 101, 0.93x), so a fetch below this fraction of
# what is already stored is a partial response, not new information.
PARTIAL_READ_FRACTION = 0.5


class PartialReadError(RuntimeError):
    """Kubecost returned far fewer workflows than are already stored for a date.

    Raised instead of writing, so a partial response can never overwrite a
    good record. Callers that treat this as fatal get a retry; the nightly
    flow's settled re-scrape treats it as best-effort — the stored day+1
    record stays in place and the next run tries again.
    """


def fetch_allocations(
    window: str = "yesterday",
    aggregate: str = f"label:{WORKFLOW_LABEL}",
    base_url: str = KUBECOST_BASE,
    timeout: int = 60,
    verify_ssl: bool = True,
) -> dict[str, Any]:
    """Call the Kubecost Allocation API and return the raw response dict."""
    url = f"{base_url}{ALLOCATION_PATH}"
    params = {
        "window": window,
        "accumulate": "true",
        "aggregate": aggregate,
        "includeIdle": "false",
    }
    resp = requests.get(url, params=params, timeout=timeout, verify=verify_ssl)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(f"Kubecost API error: {data.get('message', data)}")
    return data


def _first_allocation_set(api_response: dict[str, Any], grain: str) -> dict[str, dict] | None:
    """Return the accumulated allocation set, or None when the window has no data.

    Kubecost expresses "no data for this window" in three different shapes, and
    only the first two are the obvious ones:

        {"data": []}      — empty list
        {"data": null}    — null in place of the list
        {"data": [null]}  — a ONE-ELEMENT list holding a nil set

    The third is the dangerous one. A plain `if not sets` guard passes it (the
    list is non-empty), `sets[0]` is then None, and iterating it raises
    `AttributeError: 'NoneType' object has no attribute 'items'`. That is not
    hypothetical: the aggregator OOMKilled at 2026-09-05T10:38:58Z, lost its
    in-flight ingestion of that day, and served `data: [null]` for it
    afterwards. The nightly scrape died on that shape at its first task, before
    writing anything — so the run lost both the 2026-09-05 window it was
    scraping and the settled re-scrape of 2026-09-03 that shared the flow run.
    One bad window, two missing days, and 09-03 is stuck at scrape_age_days=1
    permanently because no third read is ever scheduled.

    A window with no data is a normal condition (an idle cluster, or a day the
    aggregator has not ingested yet). It should skip the day and let the rest of
    the flow proceed, never abort the run.
    """
    sets = api_response.get("data") or []
    allocation_set = sets[0] if sets else None
    if not allocation_set:
        log.warning(
            "Kubecost returned no %s allocation data for window (data=%s); skipping",
            grain,
            "[null]" if sets else api_response.get("data"),
        )
        return None
    return allocation_set


def parse_allocations(
    api_response: dict[str, Any],
    report_date: date,
    pipeline: str = "cloudpipe_minproc",
    scrape_date: date | None = None,
) -> list[CostAllocation]:
    """Convert Kubecost allocation response to a list of CostAllocation records.

    Kubecost returns a list of allocation sets; we take the first (accumulated) set.
    Each key is a workflow name (from label workflows.argoproj.io/workflow).
    The subject ID is pulled from properties.labels.subjectid on the allocation.
    Entries with no workflow label appear as "__unallocated__" or similar — skip them.

    scrape_date defaults to report_date + 1 day (the normal nightly-scrape lag)
    when not given, so scrape_age_days is always populated even for callers that
    don't pass it explicitly.
    """
    allocation_set = _first_allocation_set(api_response, "workflow")
    if allocation_set is None:
        return []

    records: list[CostAllocation] = []

    date_str = report_date.isoformat()
    scrape_age_days = ((scrape_date or (report_date + timedelta(days=1))) - report_date).days

    for workflow_name, alloc in allocation_set.items():
        if not workflow_name or workflow_name.startswith("__"):
            continue
        if not cost_is_recorded(workflow_name):
            continue

        subject = alloc.get("properties", {}).get("labels", {}).get("subjectid", "")
        adjustment = sum(alloc.get(f, 0) or 0 for f in ADJUSTMENT_FIELDS)

        records.append(
            CostAllocation(
                date=date_str,
                workflow_name=workflow_name,
                subject=subject,
                total_cost_usd=round(alloc.get("totalCost", 0.0), 6),
                cpu_cost_usd=round(alloc.get("cpuCost", 0.0), 6),
                memory_cost_usd=round(alloc.get("ramCost", 0.0), 6),
                gpu_cost_usd=round(alloc.get("gpuCost", 0.0), 6),
                total_adjustment_usd=round(adjustment, 6),
                scrape_age_days=scrape_age_days,
                pipeline=pipeline,
            )
        )

    return records


def _label(alloc: dict[str, Any], key: str) -> str:
    """Read one sanitized label off an allocation, "" when absent."""
    return alloc.get("properties", {}).get("labels", {}).get(key, "") or ""


def fetch_node_assets(
    window: str = "yesterday",
    base_url: str = KUBECOST_BASE,
    timeout: int = 60,
    verify_ssl: bool = True,
) -> dict[str, Any]:
    """Call the Kubecost Assets API for Node assets and return the raw dict."""
    url = f"{base_url}{ASSETS_PATH}"
    params = {"window": window, "accumulate": "true", "filterTypes": "Node"}
    resp = requests.get(url, params=params, timeout=timeout, verify=verify_ssl)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(f"Kubecost assets API error: {data.get('message', data)}")
    return data


@dataclass(frozen=True)
class NodeRate:
    """What one node cost per hour, and what it would have cost on demand."""

    capacity_type: str  # "spot" | "on-demand" | ""
    effective_usd_per_hour: float | None
    ondemand_usd_per_hour: float | None


@dataclass(frozen=True)
class NodeAsset:
    """One EC2 instance from the Assets API, and the window it was up in.

    A hostname is NOT an instance. EC2 hostnames are private IPs, the cluster
    reuses them, and one daily window routinely holds several instances under
    one name (#1060: up to 133 hostnames a day in 2026-10, about a quarter of
    workflow pods on a reused name). So the node table keeps every instance a
    hostname had, and a pod is matched to one by time (resolve_node).
    """

    provider_id: str  # EC2 instance ID, from properties.providerID
    start: datetime | None
    end: datetime | None
    instance_type: str  # the asset's nodeType
    labelled_instance_type: str  # the asset's own node.kubernetes.io/instance-type label
    rate: NodeRate


# hostname -> every instance that held it in the window
NodeIndex = dict[str, list[NodeAsset]]

# Why a pod did or did not get its node's rates. Logged as a tally per scrape.
MATCHED = "matched"
NO_ASSET = "no_asset"
AMBIGUOUS = "ambiguous"
INCONSISTENT_ASSET = "inconsistent_asset"
TYPE_MISMATCH = "type_mismatch"
# Not proven by this read, but by the stored day+1 record of the same pod.
CARRIED = "carried"


@dataclass(frozen=True)
class CarriedNode:
    """What a stored (day+1) PodCost record proved about its pod's instance.

    Only records that name an instance (node_instance_id, schema 1.2+) carry
    anything: a 1.1 record's rates came from the hostname table #1060 fixed,
    and are exactly what must not be carried forward.
    """

    instance_id: str
    instance_type: str
    capacity_type: str
    ondemand_usd_per_hour: float | None


# (workflow_name, pod) -> what the stored record of that pod proved
CarriedIndex = dict[tuple[str, str], CarriedNode]


def carried_from_records(records: list[dict[str, Any]]) -> CarriedIndex:
    """Index stored PodCost dicts by (workflow_name, pod), keeping only proven ones."""
    index: CarriedIndex = {}
    for rec in records:
        instance_id = rec.get("node_instance_id") or ""
        if not instance_id:
            continue
        index[(rec.get("workflow_name", ""), rec.get("pod", ""))] = CarriedNode(
            instance_id=instance_id,
            instance_type=rec.get("node_instance_type") or "",
            capacity_type=rec.get("node_capacity_type") or "",
            ondemand_usd_per_hour=rec.get("node_ondemand_usd_per_hour"),
        )
    return index


def _parse_ts(value: Any) -> datetime | None:
    """Kubecost's RFC 3339 `start`/`end` ("2026-11-05T21:46:00Z") -> aware datetime."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _capacity_type(asset: dict[str, Any]) -> str:
    """Classify a node asset as spot or on-demand.

    Prefers Karpenter's own label. Falls back to Kubecost's `preemptible`
    field (1.0 = spot) for nodes Karpenter did not provision — the EKS managed
    nodegroups carry no capacity-type label at all, and calling those "" would
    lose the one genuinely on-demand thing in the cluster.
    """
    labelled = (asset.get("labels") or {}).get(LABEL_CAPACITY_TYPE, "") or ""
    if labelled:
        return labelled

    preemptible = asset.get("preemptible")
    if preemptible is None:
        return ""
    return "spot" if float(preemptible) > 0 else "on-demand"


def parse_node_assets(
    assets_response: dict[str, Any],
    region: str | None = None,
) -> NodeIndex:
    """Build {node_hostname: [NodeAsset, ...]} from an Assets API response.

    The effective rate is the node's reconciled cost divided by the node-hours
    it was actually up for, which is the rate Kubecost priced this window's pod
    shares from. It is NOT the spot price at launch: it moves with
    reconciliation, so a node read at day+1 and again at day+3 gives two
    different effective rates, and the settled one is the one to trust.

    Nodes with zero recorded minutes keep a NULL effective rate rather than a
    division by zero.

    Grouped on `properties.name` (the hostname), because that is all the pod
    side has (its kubernetes.io/hostname label), but every instance under a
    hostname is kept: the response key is a compound
    `AWS/<account>/Compute/<cluster>/Node/Kubernetes/<instance-id>/<hostname>`
    path and `properties.providerID` is the instance ID, so two instances that
    reused one private IP are two assets here. Until #1060 this was a plain
    {hostname: NodeRate}, and the later instance silently overwrote the
    earlier one's rates for every pod on either.
    """
    sets = assets_response.get("data", [])
    asset_set = sets[0] if isinstance(sets, list) else sets
    if not asset_set:
        log.warning("Kubecost returned no node assets for window")
        return {}

    index: NodeIndex = {}
    for key, asset in asset_set.items():
        props = asset.get("properties") or {}
        name = props.get("name", "")
        if not name:
            continue

        minutes = float(asset.get("minutes", 0.0) or 0.0)
        total_cost = float(asset.get("totalCost", 0.0) or 0.0)
        effective = round(total_cost / (minutes / 60.0), 6) if minutes > 0 else None

        instance_type = asset.get("nodeType") or ""
        index.setdefault(name, []).append(
            NodeAsset(
                provider_id=props.get("providerID") or key,
                start=_parse_ts(asset.get("start")),
                end=_parse_ts(asset.get("end")),
                instance_type=instance_type,
                labelled_instance_type=(asset.get("labels") or {}).get(
                    "node_kubernetes_io_instance_type", ""
                )
                or "",
                rate=NodeRate(
                    capacity_type=_capacity_type(asset),
                    effective_usd_per_hour=effective,
                    ondemand_usd_per_hour=ondemand_hourly_usd(instance_type, region=region),
                ),
            )
        )

    reused = sum(1 for assets in index.values() if len(assets) > 1)
    if reused:
        log.info(
            "%d hostname(s) were held by more than one instance in this window; "
            "their pods are matched to an instance by time",
            reused,
        )
    return index


def _windows_overlap(candidates: list[NodeAsset]) -> bool:
    """True when any two instances under one hostname claim overlapping time.

    A window that is missing counts as overlapping: it cannot be ruled out.
    """
    windows = sorted((c.start, c.end) for c in candidates if c.start and c.end)
    if len(windows) < len(candidates):
        return True
    return any(later[0] < earlier[1] for earlier, later in pairwise(windows))


def resolve_node(
    candidates: list[NodeAsset],
    pod_start: datetime | None,
    pod_end: datetime | None,
    pod_instance_type: str,
) -> tuple[NodeAsset | None, str]:
    """Pick the instance a pod ran on from its hostname's instances, or none.

    Returns (instance, reason). An instance comes back only when it is proven;
    otherwise None, so the row is unpriced (NULL) rather than priced against an
    instance it never ran on — unless the stored day+1 record proved it (see
    _node_columns).

    - One instance under the hostname: that one.
    - Several: the one whose up-window contains the pod's window. Two
      instances cannot hold one private IP at once, so on the day+1 read,
      which Kubecost serves from hourly data, exactly one does (2026-11-06:
      91/91 pods). Zero or several is AMBIGUOUS, and so is every pod on a
      hostname whose instances' windows overlap: that is physically
      impossible, so those windows — and the minutes, and so the effective
      rates, computed over them — are not real.
    - Either way, the instance is refused if its nodeType disagrees with its
      own instance-type label (INCONSISTENT_ASSET), or with the pod's
      (TYPE_MISMATCH).

    The last two exist for the settled re-scrape. Kubecost keeps hourly data
    for 49 h (retention1h, chart default) and answers older windows from daily
    rollups, and the rollups merge the instances that shared a hostname: their
    up-windows overlap, often from the same minute, and one instance's
    nodeType is stamped on both (2026-10-29: 123 of 202 instances on a reused
    hostname disagree with their own label; about half the pods on reused
    hostnames sit inside two windows). A day+3 read therefore cannot tell
    those instances apart by itself.
    """
    if not candidates:
        return None, NO_ASSET

    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        if pod_start is None or pod_end is None or _windows_overlap(candidates):
            return None, AMBIGUOUS
        containing = [
            c
            for c in candidates
            if c.start is not None
            and c.end is not None
            and c.start <= pod_start
            and pod_end <= c.end
        ]
        if len(containing) != 1:
            return None, AMBIGUOUS
        chosen = containing[0]

    if chosen.labelled_instance_type and chosen.labelled_instance_type != chosen.instance_type:
        return None, INCONSISTENT_ASSET
    if pod_instance_type and chosen.instance_type and pod_instance_type != chosen.instance_type:
        return None, TYPE_MISMATCH
    return chosen, MATCHED


@dataclass(frozen=True)
class _NodeColumns:
    instance_id: str
    instance_type: str
    capacity_type: str
    effective_usd_per_hour: float | None
    ondemand_usd_per_hour: float | None


def _node_columns(
    asset: NodeAsset | None,
    reason: str,
    prior: CarriedNode | None,
    pod_instance_type: str,
) -> tuple[_NodeColumns, str]:
    """The node columns for one pod, from this read and the stored record.

    The day+1 read is the authority on WHICH instance (hourly data keeps them
    apart); the read being written is the authority on the effective rate,
    which only settles at day+3. So:

    - proven here, and no stored instance or the same one: this read's columns.
    - stored instance, and this read cannot prove one, or proves another: the
      stored instance, type, capacity type and on-demand rate, all drift-free,
      with the effective rate NULL. Pairing an unreconciled day+1 rate with
      settled dollars would break the cancellation spot_savings() relies on.
    - neither: everything NULL / "".
    """
    if asset is not None and (prior is None or prior.instance_id == asset.provider_id):
        return (
            _NodeColumns(
                instance_id=asset.provider_id,
                instance_type=pod_instance_type or asset.instance_type,
                capacity_type=asset.rate.capacity_type,
                effective_usd_per_hour=asset.rate.effective_usd_per_hour,
                ondemand_usd_per_hour=asset.rate.ondemand_usd_per_hour,
            ),
            reason,
        )
    if prior is not None:
        return (
            _NodeColumns(
                instance_id=prior.instance_id,
                instance_type=prior.instance_type,
                capacity_type=prior.capacity_type,
                effective_usd_per_hour=None,
                ondemand_usd_per_hour=prior.ondemand_usd_per_hour,
            ),
            CARRIED,
        )
    return _NodeColumns("", pod_instance_type, "", None, None), reason


def parse_pod_allocations(
    api_response: dict[str, Any],
    report_date: date,
    pipeline: str = "cloudpipe_minproc",
    scrape_date: date | None = None,
    node_rates: NodeIndex | None = None,
    carried: CarriedIndex | None = None,
) -> list[PodCost]:
    """Convert an `aggregate=pod` allocation response to PodCost records.

    Same window and response shape as parse_allocations, one grain finer: each
    key is a pod name rather than a workflow name. Pods with no workflow label
    are dropped — that excludes kubecost's own pods, the Argo controller, and
    anything else sharing the cluster, matching what the workflow-grain pass
    already ignores.

    Kubecost reports ram as byte-hours and gpu/cpu as hours; ram is converted
    to GB-hours here so the column is directly comparable to a pod's memory
    request without a 2**30 in every query.

    node_rates, when given, is the parse_node_assets() index that fills the
    node columns, matched per pod by resolve_node(). Omitting it leaves those
    NULL rather than failing — the counterfactual is an addition to this
    table, not a precondition for it.

    carried, when given, is what the stored records for this date proved
    (load_carried_nodes): on a re-scrape it supplies the instance for pods this
    read cannot prove. See _node_columns for which read wins which column.

    node_instance_type is Kubecost's own label, which Kubecost joins onto the
    pod by hostname too, so on a reused hostname it can name the other
    instance. Trust it where node_instance_id is set: a pod is only matched
    when the instance's type equals it, and a carried pod takes the type its
    day+1 record verified.
    """
    allocation_set = _first_allocation_set(api_response, "pod")
    if allocation_set is None:
        return []

    records: list[PodCost] = []
    match_reasons: Counter[str] = Counter()

    date_str = report_date.isoformat()
    scrape_age_days = ((scrape_date or (report_date + timedelta(days=1))) - report_date).days

    for pod_name, alloc in allocation_set.items():
        if not pod_name or pod_name.startswith("__"):
            continue

        workflow_name = _label(alloc, LABEL_WORKFLOW)
        if not workflow_name or not cost_is_recorded(workflow_name):
            continue

        props = alloc.get("properties", {})
        adjustment = sum(alloc.get(f, 0) or 0 for f in ADJUSTMENT_FIELDS)
        ram_byte_hours = alloc.get("ramByteHours", 0.0) or 0.0

        pod = props.get("pod") or pod_name
        node_name = _label(alloc, "kubernetes_io_hostname")
        node_instance_type = _label(alloc, "node_kubernetes_io_instance_type")
        asset: NodeAsset | None = None
        reason = NO_ASSET
        if node_rates is not None:
            asset, reason = resolve_node(
                node_rates.get(node_name, []),
                _parse_ts(alloc.get("start")),
                _parse_ts(alloc.get("end")),
                node_instance_type,
            )
        cols, reason = _node_columns(
            asset, reason, (carried or {}).get((workflow_name, pod)), node_instance_type
        )
        if node_rates is not None or reason == CARRIED:
            match_reasons[reason] += 1

        records.append(
            PodCost(
                date=date_str,
                workflow_name=workflow_name,
                pod=pod,
                step=_label(alloc, LABEL_STEP),
                phase=_label(alloc, LABEL_PHASE),
                subject=_label(alloc, LABEL_SUBJECT),
                session=_label(alloc, LABEL_SESSION),
                total_cost_usd=round(alloc.get("totalCost", 0.0) or 0.0, 6),
                cpu_cost_usd=round(alloc.get("cpuCost", 0.0) or 0.0, 6),
                memory_cost_usd=round(alloc.get("ramCost", 0.0) or 0.0, 6),
                gpu_cost_usd=round(alloc.get("gpuCost", 0.0) or 0.0, 6),
                pv_cost_usd=round(alloc.get("pvCost", 0.0) or 0.0, 6),
                network_cost_usd=round(alloc.get("networkCost", 0.0) or 0.0, 6),
                total_adjustment_usd=round(adjustment, 6),
                runtime_minutes=round(alloc.get("minutes", 0.0) or 0.0, 3),
                cpu_core_hours=round(alloc.get("cpuCoreHours", 0.0) or 0.0, 6),
                ram_gb_hours=round(ram_byte_hours / _BYTES_PER_GB, 6),
                gpu_hours=round(alloc.get("gpuHours", 0.0) or 0.0, 6),
                cpu_efficiency=round(alloc.get("cpuEfficiency", 0.0) or 0.0, 6),
                ram_efficiency=round(alloc.get("ramEfficiency", 0.0) or 0.0, 6),
                # Under aggregate=pod, Kubecost's `properties` carries only `pod` and
                # `labels` — there is no `node` key, so the previous props.get("node")
                # read was silently "" on every record (verified 2026-07-31: 0/354 rows
                # populated). Aggregating by "pod,node" does NOT fix it — the node
                # dimension comes back as `__unallocated__` and merely inflates the row
                # count. The node NAME does arrive, as the standard kubernetes.io/hostname
                # node label that Kubecost propagates onto the allocation (same mechanism
                # node_instance_type below already relies on).
                node=node_name,
                node_instance_type=cols.instance_type,
                # Node-grain facts, denormalized onto the pod row from the
                # Assets pass so the no-spot counterfactual is a single-table
                # query. They repeat across every pod on a node, which Parquet
                # compresses to nothing — cheaper than making every consumer
                # join a node table.
                node_capacity_type=cols.capacity_type,
                node_effective_usd_per_hour=cols.effective_usd_per_hour,
                node_ondemand_usd_per_hour=cols.ondemand_usd_per_hour,
                node_instance_id=cols.instance_id,
                scrape_age_days=scrape_age_days,
                pipeline=pipeline,
            )
        )

    if match_reasons[CARRIED]:
        log.info(
            "%d pod(s) took their instance from the stored day+1 record "
            "(effective rate left NULL); see _node_columns",
            match_reasons[CARRIED],
        )
    unproven = {r: n for r, n in match_reasons.items() if r not in (MATCHED, NO_ASSET, CARRIED)}
    if unproven:
        log.warning(
            "%d pod(s) left unpriced because their node could not be proven (%s); "
            "see resolve_node — expected on a settled read of reused hostnames whose "
            "day+1 record predates schema 1.2",
            sum(unproven.values()),
            ", ".join(f"{r}={n}" for r, n in sorted(unproven.items())),
        )
    return records


def _group_by_workflow(records: list[PodCost]) -> dict[str, list[PodCost]]:
    """Bucket pod records by workflow — one S3 key holds one workflow's pods."""
    grouped: dict[str, list[PodCost]] = {}
    for rec in records:
        grouped.setdefault(rec.workflow_name, []).append(rec)
    return grouped


def _resolve_window(report_date: date | None) -> tuple[date, date, str]:
    """Return (scrape_date, report_date, window) for a scrape pass.

    Shared by both grains so the pod pass and the workflow pass always query
    the identical window — if they drifted, pod costs would no longer sum to
    their workflow's CostAllocation and the two tables would silently
    disagree.
    """
    scrape_date = date.today()
    if report_date is None:
        report_date = scrape_date - timedelta(days=1)
        window = "yesterday"
    else:
        window = f"{report_date.isoformat()}T00:00:00Z,{report_date.isoformat()}T23:59:59Z"
    return scrape_date, report_date, window


def count_stored_workflows(
    bucket: str,
    report_date: date,
    region: str | None = None,
    prefix: str = "metrics/costs/",
) -> int:
    """Count objects already stored under {prefix}dt={report_date}/.

    Both grains write exactly one S3 key per workflow for a date (PodCost packs
    a workflow's pods into one JSONL key), so an object count is a workflow
    count for either prefix — which is what makes one guard serve both passes.
    """
    import boto3
    import deployment_env

    s3 = boto3.client("s3", region_name=region or deployment_env.region())
    paginator = s3.get_paginator("list_objects_v2")
    full_prefix = f"{prefix}dt={report_date.isoformat()}/"
    return sum(
        len(page.get("Contents", []))
        for page in paginator.paginate(Bucket=bucket, Prefix=full_prefix)
    )


def load_carried_nodes(
    bucket: str,
    report_date: date,
    region: str | None = None,
) -> CarriedIndex:
    """What the pod-cost records already stored for report_date proved.

    Read before a re-scrape overwrites them, so the settled pass can keep the
    instance its day+1 read proved (see _node_columns). On the day+1 pass
    nothing is stored yet and this is one empty listing. Best-effort, like the
    whole counterfactual: a failed read means nothing is carried, and the
    affected pods are left NULL as they would have been anyway.
    """
    import boto3
    import deployment_env
    from compactor import (  # type: ignore[import-not-found]
        list_raw_keys,
        read_raw_records_counting_skips,
    )

    try:
        s3 = boto3.client("s3", region_name=region or deployment_env.region())
        keys = list_raw_keys(s3, bucket, "metrics/pod-costs/", report_date.isoformat())
        if not keys:
            return {}
        records, skipped = read_raw_records_counting_skips(s3, bucket, keys)
    except Exception as exc:  # noqa: BLE001 — enrichment must not fail the scrape
        log.warning(
            "Could not read stored pod costs for %s (%s); carrying nothing", report_date, exc
        )
        return {}
    if skipped:
        log.warning(
            "%d stored pod-cost object(s) for %s were unreadable; their pods carry nothing",
            skipped,
            report_date,
        )
    carried = carried_from_records(records)
    log.info(
        "Stored records for %s prove the instance of %d of %d pod(s)",
        report_date,
        len(carried),
        len(records),
    )
    return carried


def _guard_partial_read(
    bucket: str,
    region: str | None,
    report_date: date,
    prefix: str,
    n_fetched: int,
    fraction: float,
) -> None:
    """Raise PartialReadError rather than overwrite a date with a partial read.

    A no-op on the normal day+1 scrape, where nothing is stored for the date
    yet. It only bites on a re-scrape (settled pass, or a retried scrape whose
    first attempt already wrote), which is exactly where an overwrite can
    destroy data — the bucket keeps no versions of these keys.
    """
    if fraction <= 0:
        return
    stored = count_stored_workflows(bucket, report_date, region=region, prefix=prefix)
    if stored and n_fetched < fraction * stored:
        raise PartialReadError(
            f"Kubecost returned {n_fetched} workflow(s) for {report_date} under {prefix}, "
            f"below {fraction:.0%} of the {stored} already stored — refusing to overwrite. "
            "This is the signature of a partial Kubecost response, not a settled read."
        )


def route_workflows(
    workflow_names: set[str], bucket: str, pairs: dict[str, dict[str, str]] | None
) -> dict[str, set[str]]:
    """{metrics bucket: workflows} — production to `bucket`, audit runs to their pair's.

    An audit workflow whose pair is not configured is DROPPED with an error,
    never written to production (openspec add-reprocess-audit, audit-isolation):
    QC and cost keys carry no batch identity, so an audit row in the production
    store could not be told apart afterwards.
    """
    routed: dict[str, set[str]] = {}
    dropped: list[str] = []
    for wf in workflow_names:
        target = audit_pairs.metrics_bucket_for(wf, bucket, pairs or {})
        if target is None:
            dropped.append(wf)
        else:
            routed.setdefault(target, set()).add(wf)
    if dropped:
        log.error(
            "%d audit workflow(s) have no configured bucket pair (Prefect Variable %s) and were "
            "NOT recorded anywhere: %s",
            len(dropped),
            audit_pairs.PAIRS_VARIABLE,
            ", ".join(sorted(dropped)[:10]),
        )
    for target, names in sorted(routed.items()):
        if target != bucket:
            log.info("Routing %d audit workflow(s) to %s", len(names), target)
    return routed


def scrape_and_upload(
    bucket: str,
    region: str | None = None,
    base_url: str = KUBECOST_BASE,
    pipeline: str = "cloudpipe_minproc",
    report_date: date | None = None,
    verify_ssl: bool = True,
    partial_read_fraction: float = PARTIAL_READ_FRACTION,
    pairs: dict[str, dict[str, str]] | None = None,
) -> int:
    """Fetch allocations for report_date (default: yesterday) and write to S3.

    Returns the number of records written. Raises PartialReadError, having
    written nothing, when the response is too small to be a credible rewrite of
    what is already stored for the date (see _guard_partial_read; pass
    partial_read_fraction=0 to disable).

    `pairs` is the reprocess audit's parsed bucket pairs (audit_pairs.parse_pairs):
    each workflow is written to the metrics bucket route_workflows gives it, and
    the partial-read guard runs per bucket, all of them before any write.
    """
    scrape_date, report_date, window = _resolve_window(report_date)

    log.info("Fetching Kubecost allocations for %s (window=%s)", report_date, window)
    api_resp = fetch_allocations(base_url=base_url, window=window, verify_ssl=verify_ssl)
    records = parse_allocations(
        api_resp, report_date=report_date, pipeline=pipeline, scrape_date=scrape_date
    )
    log.info("Found %d subject allocation(s)", len(records))

    routed = route_workflows({r.workflow_name for r in records}, bucket, pairs)
    # Production is always guarded, even with none of its workflows in the
    # response: an empty read is exactly the partial-read signature.
    for target in {bucket} | set(routed):
        _guard_partial_read(
            target,
            region,
            report_date,
            "metrics/costs/",
            len(routed.get(target, ())),
            partial_read_fraction,
        )

    target_of = {wf: target for target, names in routed.items() for wf in names}
    written = 0
    for rec in records:
        dest = target_of.get(rec.workflow_name)
        if dest is None:  # an unconfigured audit workflow, dropped above
            continue
        key = CostAllocation.s3_key(rec.date, rec.workflow_name)
        emit_to_s3(rec.to_dict(), bucket=dest, key=key, region=region)
        written += 1
        log.debug("Wrote s3://%s/%s", dest, key)

    return written


def scrape_pod_costs_and_upload(
    bucket: str,
    region: str | None = None,
    base_url: str = KUBECOST_BASE,
    pipeline: str = "cloudpipe_minproc",
    report_date: date | None = None,
    verify_ssl: bool = True,
    partial_read_fraction: float = PARTIAL_READ_FRACTION,
    pairs: dict[str, dict[str, str]] | None = None,
) -> int:
    """Fetch pod-grain allocations for report_date and write them to S3.

    Deliberately a second API call rather than deriving the workflow totals by
    summing this finer pass: Kubecost's own workflow-grain aggregation is the
    authority for `costs`, and re-deriving it here would make an existing,
    validated table depend on the correctness of new label-parsing code.

    Returns the number of pod records written (not the number of S3 keys —
    each key holds one workflow's pods as JSONL).

    `pairs` routes audit workflows as in scrape_and_upload. The carry-forward
    source is read from every bucket written to, so an audit pod's day+1
    identity is found where its day+1 record was written.
    """
    scrape_date, report_date, window = _resolve_window(report_date)

    log.info("Fetching Kubecost pod allocations for %s (window=%s)", report_date, window)
    api_resp = fetch_allocations(
        base_url=base_url, window=window, aggregate="pod", verify_ssl=verify_ssl
    )

    # Best-effort, for the same reason the whole pod pass is best-effort to the
    # flow: the counterfactual columns are an enrichment. A failing Assets call
    # or a missing pricing:GetProducts grant must cost us three NULL columns,
    # not the pod-cost table for that night.
    node_rates: NodeIndex = {}
    try:
        node_rates = parse_node_assets(
            fetch_node_assets(base_url=base_url, window=window, verify_ssl=verify_ssl),
            region=region,
        )
        log.info(
            "Resolved rates for %d instance(s) under %d hostname(s)",
            sum(len(v) for v in node_rates.values()),
            len(node_rates),
        )
    except Exception as exc:  # noqa: BLE001 — enrichment must not fail the scrape
        log.warning("Node asset pass failed (%s); rate columns will be NULL", exc)

    carried: CarriedIndex = {}
    for source in [bucket] + sorted(audit_pairs.audit_metrics_buckets(pairs or {})):
        carried.update(load_carried_nodes(source, report_date, region=region))

    records = parse_pod_allocations(
        api_resp,
        report_date=report_date,
        pipeline=pipeline,
        scrape_date=scrape_date,
        node_rates=node_rates,
        carried=carried,
    )

    all_grouped = _group_by_workflow(records)
    routed = route_workflows(set(all_grouped), bucket, pairs)
    log.info("Found %d pod allocation(s) across %d workflow(s)", len(records), len(all_grouped))

    # Guarded on workflow count, not pod count, so the threshold means the same
    # thing here as in the workflow pass. Every bucket before any write, and
    # production always (an empty read is the partial-read signature).
    for target in {bucket} | set(routed):
        _guard_partial_read(
            target,
            region,
            report_date,
            "metrics/pod-costs/",
            len(routed.get(target, ())),
            partial_read_fraction,
        )
    grouped = {wf: all_grouped[wf] for wf in routed.get(bucket, ())}

    # Cross-grain check. Both passes query the identical window (_resolve_window),
    # so they must see the same workflows; a large disagreement means one of the
    # two got a partial Kubecost response. This is the only detector that works
    # on a day+1 scrape, where the stored-count guard above has nothing to
    # compare against — and it is not hypothetical: on 2026-08-05 a single run
    # stored 1 workflow under metrics/costs/ and 110 under metrics/pod-costs/ for
    # dt=2026-08-04, and nothing flagged it. Warn rather than raise, because the
    # pod records themselves are fine and the deficient pass is the other one.
    if partial_read_fraction > 0:
        n_workflow_grain = count_stored_workflows(bucket, report_date, region=region)
        if n_workflow_grain and len(grouped) > n_workflow_grain / partial_read_fraction:
            log.warning(
                "Grain disagreement for %s: %d workflow(s) at pod grain vs %d stored under "
                "metrics/costs/ — the workflow-grain scrape likely got a partial Kubecost "
                "response. Re-run `kubecost_scraper.py --date %s` once Kubecost is healthy.",
                report_date,
                len(grouped),
                n_workflow_grain,
                report_date,
            )

    unlabeled = sum(1 for r in records if not r.step)
    if unlabeled:
        # Not fatal, but it means some template is missing its
        # `cloudpipe.io/step` pod label and its cost will land in a "" bucket
        # that no per-component query will attribute.
        log.warning("%d pod(s) carry a workflow label but no cloudpipe.io/step", unlabeled)

    # Coverage of the counterfactual columns. Partial coverage is the failure
    # mode that hides: a no-spot total summed over rows where only some carry
    # rates is an understatement that looks like a normal number, so the gap is
    # logged at scrape time rather than left for a query to notice.
    unpriced = sum(
        1 for r in records if not (r.node_effective_usd_per_hour and r.node_ondemand_usd_per_hour)
    )
    if unpriced:
        log.warning(
            "%d/%d pod(s) have no on-demand counterfactual rate — they will be excluded "
            "from spot_savings() and lower its coverage_frac",
            unpriced,
            len(records),
        )

    written = 0
    for target, names in routed.items():
        for workflow_name in sorted(names):
            pods = all_grouped[workflow_name]
            key = PodCost.s3_key(report_date.isoformat(), workflow_name)
            emit_jsonl_to_s3([p.to_dict() for p in pods], bucket=target, key=key, region=region)
            written += len(pods)
            log.debug("Wrote s3://%s/%s (%d pods)", target, key, len(pods))

    return written


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    p = argparse.ArgumentParser(description="Scrape Kubecost and write cost metrics to S3")
    p.add_argument("--bucket", required=True, help="the metrics bucket")
    p.add_argument("--region", default=None, help="default: this deployment's region")
    p.add_argument("--base-url", default=KUBECOST_BASE)
    p.add_argument("--pipeline", default="cloudpipe_minproc")
    p.add_argument("--date", default=None, help="Report date ISO 8601 (default: yesterday)")
    p.add_argument(
        "--settled",
        action="store_true",
        help=f"Re-scrape the report-date {SETTLED_AGE_DAYS} days ago, whose Kubecost "
        "reconciliation has converged, overwriting its day+1 records",
    )
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL verification (for external Kubecost URL)",
    )
    p.add_argument(
        "--skip-pod-costs",
        action="store_true",
        help="Skip the per-pod pass (metrics/pod-costs/), scraping workflow totals only",
    )
    args = p.parse_args()

    if args.settled and args.date:
        p.error("--settled and --date are mutually exclusive")
    if args.settled:
        parsed_date = date.today() - timedelta(days=SETTLED_AGE_DAYS)
    else:
        parsed_date = date.fromisoformat(args.date) if args.date else None
    common = dict(
        bucket=args.bucket,
        region=args.region,
        base_url=args.base_url,
        pipeline=args.pipeline,
        report_date=parsed_date,
        verify_ssl=not args.insecure,
    )
    n = scrape_and_upload(**common)  # type: ignore[arg-type]
    print(f"Wrote {n} cost allocation record(s) to s3://{args.bucket}/metrics/costs/")

    if not args.skip_pod_costs:
        n_pods = scrape_pod_costs_and_upload(**common)  # type: ignore[arg-type]
        print(f"Wrote {n_pods} pod cost record(s) to s3://{args.bucket}/metrics/pod-costs/")


if __name__ == "__main__":
    main()
