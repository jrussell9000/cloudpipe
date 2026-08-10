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
    python kubecost_scraper.py --bucket <YOUR_S3_BUCKET> --region <YOUR_AWS_REGION>
    python kubecost_scraper.py --bucket <YOUR_S3_BUCKET> --settled   # re-scrape day-3

This module is also imported by prefect/flows/cost_scraper.py.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from typing import Any

import requests
from schemas import CostAllocation, PodCost  # type: ignore[import-not-found]
from writer import emit_jsonl_to_s3, emit_to_s3  # type: ignore[import-not-found]

log = logging.getLogger(__name__)

KUBECOST_BASE = "http://kubecost-frontend.kubecost.svc.cluster.local:9090"
ALLOCATION_PATH = "/model/allocation"

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
    sets: list[dict] = api_response.get("data", [])
    if not sets:
        log.warning("Kubecost returned no allocation sets for window")
        return []

    allocation_set: dict[str, dict] = sets[0]
    records: list[CostAllocation] = []

    date_str = report_date.isoformat()
    scrape_age_days = ((scrape_date or (report_date + timedelta(days=1))) - report_date).days

    for workflow_name, alloc in allocation_set.items():
        if not workflow_name or workflow_name.startswith("__"):
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


def parse_pod_allocations(
    api_response: dict[str, Any],
    report_date: date,
    pipeline: str = "cloudpipe_minproc",
    scrape_date: date | None = None,
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
    """
    sets: list[dict] = api_response.get("data", [])
    if not sets:
        log.warning("Kubecost returned no pod allocation sets for window")
        return []

    allocation_set: dict[str, dict] = sets[0]
    records: list[PodCost] = []

    date_str = report_date.isoformat()
    scrape_age_days = ((scrape_date or (report_date + timedelta(days=1))) - report_date).days

    for pod_name, alloc in allocation_set.items():
        if not pod_name or pod_name.startswith("__"):
            continue

        workflow_name = _label(alloc, LABEL_WORKFLOW)
        if not workflow_name:
            continue

        props = alloc.get("properties", {})
        adjustment = sum(alloc.get(f, 0) or 0 for f in ADJUSTMENT_FIELDS)
        ram_byte_hours = alloc.get("ramByteHours", 0.0) or 0.0

        records.append(
            PodCost(
                date=date_str,
                workflow_name=workflow_name,
                pod=props.get("pod") or pod_name,
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
                node=_label(alloc, "kubernetes_io_hostname"),
                node_instance_type=_label(alloc, "node_kubernetes_io_instance_type"),
                scrape_age_days=scrape_age_days,
                pipeline=pipeline,
            )
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
    region: str = "<YOUR_AWS_REGION>",
    prefix: str = "metrics/costs/",
) -> int:
    """Count objects already stored under {prefix}dt={report_date}/.

    Both grains write exactly one S3 key per workflow for a date (PodCost packs
    a workflow's pods into one JSONL key), so an object count is a workflow
    count for either prefix — which is what makes one guard serve both passes.
    """
    import boto3

    s3 = boto3.client("s3", region_name=region)
    paginator = s3.get_paginator("list_objects_v2")
    full_prefix = f"{prefix}dt={report_date.isoformat()}/"
    return sum(
        len(page.get("Contents", []))
        for page in paginator.paginate(Bucket=bucket, Prefix=full_prefix)
    )


def _guard_partial_read(
    bucket: str,
    region: str,
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


def scrape_and_upload(
    bucket: str,
    region: str = "<YOUR_AWS_REGION>",
    base_url: str = KUBECOST_BASE,
    pipeline: str = "cloudpipe_minproc",
    report_date: date | None = None,
    verify_ssl: bool = True,
    partial_read_fraction: float = PARTIAL_READ_FRACTION,
) -> int:
    """Fetch allocations for report_date (default: yesterday) and write to S3.

    Returns the number of records written. Raises PartialReadError, having
    written nothing, when the response is too small to be a credible rewrite of
    what is already stored for the date (see _guard_partial_read; pass
    partial_read_fraction=0 to disable).
    """
    scrape_date, report_date, window = _resolve_window(report_date)

    log.info("Fetching Kubecost allocations for %s (window=%s)", report_date, window)
    api_resp = fetch_allocations(base_url=base_url, window=window, verify_ssl=verify_ssl)
    records = parse_allocations(
        api_resp, report_date=report_date, pipeline=pipeline, scrape_date=scrape_date
    )
    log.info("Found %d subject allocation(s)", len(records))

    _guard_partial_read(
        bucket, region, report_date, "metrics/costs/", len(records), partial_read_fraction
    )

    for rec in records:
        key = CostAllocation.s3_key(rec.date, rec.workflow_name)
        emit_to_s3(rec.to_dict(), bucket=bucket, key=key, region=region)
        log.debug("Wrote %s", key)

    return len(records)


def scrape_pod_costs_and_upload(
    bucket: str,
    region: str = "<YOUR_AWS_REGION>",
    base_url: str = KUBECOST_BASE,
    pipeline: str = "cloudpipe_minproc",
    report_date: date | None = None,
    verify_ssl: bool = True,
    partial_read_fraction: float = PARTIAL_READ_FRACTION,
) -> int:
    """Fetch pod-grain allocations for report_date and write them to S3.

    Deliberately a second API call rather than deriving the workflow totals by
    summing this finer pass: Kubecost's own workflow-grain aggregation is the
    authority for `costs`, and re-deriving it here would make an existing,
    validated table depend on the correctness of new label-parsing code.

    Returns the number of pod records written (not the number of S3 keys —
    each key holds one workflow's pods as JSONL).
    """
    scrape_date, report_date, window = _resolve_window(report_date)

    log.info("Fetching Kubecost pod allocations for %s (window=%s)", report_date, window)
    api_resp = fetch_allocations(
        base_url=base_url, window=window, aggregate="pod", verify_ssl=verify_ssl
    )
    records = parse_pod_allocations(
        api_resp, report_date=report_date, pipeline=pipeline, scrape_date=scrape_date
    )

    grouped = _group_by_workflow(records)
    log.info("Found %d pod allocation(s) across %d workflow(s)", len(records), len(grouped))

    # Guarded on workflow count, not pod count, so the threshold means the same
    # thing here as in the workflow pass.
    _guard_partial_read(
        bucket, region, report_date, "metrics/pod-costs/", len(grouped), partial_read_fraction
    )

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

    for workflow_name, pods in grouped.items():
        key = PodCost.s3_key(report_date.isoformat(), workflow_name)
        emit_jsonl_to_s3([p.to_dict() for p in pods], bucket=bucket, key=key, region=region)
        log.debug("Wrote %s (%d pods)", key, len(pods))

    return len(records)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    p = argparse.ArgumentParser(description="Scrape Kubecost and write cost metrics to S3")
    p.add_argument("--bucket", required=True, help="S3 bucket name")
    p.add_argument("--region", default="<YOUR_AWS_REGION>")
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
