#!/usr/bin/env python3
"""
Query Kubecost for cloudpipe_minproc pipeline costs and write to CSV.

Aggregates by cloudpipe.io/step by default, filtered to pods labelled with
cloudpipe.io/phase (i.e. only cloudpipe pipeline pods — excludes
fmri-first-level-proc and other co-tenants in argo-workflows namespace).

Usage
-----
    # Per-step costs for the last 24 hours:
    python cloudpipe_minproc_costs.py --window 24h

    # Per-phase breakdown:
    python cloudpipe_minproc_costs.py --window 360h --by phase

    # Per-subject totals (billing view):
    python cloudpipe_minproc_costs.py --window 360h --by subject

    # Per-subject × per-step cross-product:
    python cloudpipe_minproc_costs.py --window 360h --by subject+step

    # Write to S3:
    python cloudpipe_minproc_costs.py --window 24h -o s3://bucket/cloudpipe-costs.csv
"""

import argparse
import csv
import io
import sys

import boto3
import requests

KUBECOST_URL = "https://kubecost.<YOUR_DOMAIN>"
DEFAULT_OUTPUT = "cloudpipe_minproc_costs.csv"

# Restrict to the three cloudpipe_minproc phases.  Kubecost v1 treats comma as OR
# within a single label condition, so this excludes all non-cloudpipe pods
# regardless of which label we aggregate on.
CLOUDPIPE_FILTER = (
    'namespace:"argo-workflows"+label[cloudpipe.io/phase]:"anatomical","registration","functional"'
)

BY_LABEL = {
    "step": "cloudpipe.io/step",
    "phase": "cloudpipe.io/phase",
    "subject": "subjectid",
    "subject+step": "subjectid,cloudpipe.io/step",
}

SCALAR_FIELDS = [
    "start",
    "end",
    "minutes",
    "cpuCores",
    "cpuCoreHours",
    "cpuCoreRequestAverage",
    "cpuCoreLimitAverage",
    "cpuCoreUsageAverage",
    "cpuCost",
    "cpuCostIdle",
    "cpuCostAdjustment",
    "cpuEfficiency",
    "ramBytes",
    "ramByteHours",
    "ramByteRequestAverage",
    "ramByteLimitAverage",
    "ramByteUsageAverage",
    "ramCost",
    "ramCostIdle",
    "ramCostAdjustment",
    "ramEfficiency",
    "gpuCount",
    "gpuHours",
    "gpuAllocation",
    "gpuCost",
    "gpuCostIdle",
    "gpuCostAdjustment",
    "gpuEfficiency",
    "networkCost",
    "networkCostAdjustment",
    "networkCrossZoneCost",
    "networkCrossRegionCost",
    "networkInternetCost",
    "networkReceiveBytes",
    "networkTransferBytes",
    "pvBytes",
    "pvByteHours",
    "pvCost",
    "pvCostAdjustment",
    "loadBalancerCost",
    "loadBalancerCostAdjustment",
    "sharedCost",
    "externalCost",
    "totalCost",
    "totalEfficiency",
]


def query_kubecost(
    window: str,
    label: str,
    kubecost_url: str = KUBECOST_URL,
    share_idle: bool = True,
    share_tenancy_costs: bool = True,
) -> dict:
    params = {
        "window": window,
        "aggregate": f"label:{label}",
        "accumulate": "true",
        "shareIdle": "true" if share_idle else "false",
        "shareTenancyCosts": "true" if share_tenancy_costs else "false",
        "filter": CLOUDPIPE_FILTER,
    }
    resp = requests.get(f"{kubecost_url}/model/allocation", params=params, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    allocations = ((payload.get("data") or [{}])[0]) or {}
    return {k: v for k, v in allocations.items() if k not in ("__idle__", "__unallocated__")}


def write_csv(allocations: dict, label: str, output: str, s3) -> None:
    fieldnames = [label] + SCALAR_FIELDS
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for key, values in sorted(allocations.items()):
        row = {label: key, **{f: values.get(f) for f in SCALAR_FIELDS}}
        writer.writerow(row)
    data = buf.getvalue().encode()

    if output.startswith("s3://"):
        _, _, rest = output.partition("s3://")
        bucket, _, key = rest.partition("/")
        s3.put_object(Bucket=bucket, Key=key, Body=data)
        print(f"Wrote {len(allocations)} rows → s3://{bucket}/{key}")
    else:
        with open(output, "wb") as f:
            f.write(data)
        print(f"Wrote {len(allocations)} rows → {output}")


def _pct(v) -> str:
    return f"{v * 100:5.1f}%" if v is not None else "   n/a"


def _gb(v) -> str:
    return f"{v / 1e9:6.1f}" if v is not None else "   n/a"


def print_summary(allocations: dict, label: str) -> None:
    """Print a per-key table with cost + efficiency breakdown."""
    if not allocations:
        return

    # Column widths
    max_key = max((len(k) for k in allocations), default=4)
    col = max(max_key, len(label))

    header = (
        f"{'KEY':{col}}  {'TOTAL$':>8}  {'CPU$':>7}  "
        f"{'cpuEff':>6}  {'RAM_req_GB':>10}  {'ramEff':>6}  "
        f"{'GPU$':>7}  {'gpuEff':>6}"
    )
    print(header, file=sys.stderr)
    print("-" * len(header), file=sys.stderr)

    total = 0.0
    for key in sorted(allocations):
        v = allocations[key]
        tc = v.get("totalCost") or 0.0
        cc = v.get("cpuCost") or 0.0
        ce = v.get("cpuEfficiency")
        rreq = v.get("ramByteRequestAverage")
        re = v.get("ramEfficiency")
        gc = v.get("gpuCost") or 0.0
        ge = v.get("gpuEfficiency")
        total += tc
        print(
            f"{key:{col}}  ${tc:>7.2f}  ${cc:>6.2f}  "
            f"{_pct(ce)}  {_gb(rreq):>10}  {_pct(re)}  "
            f"${gc:>6.2f}  {_pct(ge)}",
            file=sys.stderr,
        )

    print("-" * len(header), file=sys.stderr)
    print(f"{'TOTAL':{col}}  ${total:>7.2f}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("start", nargs="?", help="Window start (ISO8601 UTC)")
    parser.add_argument("end", nargs="?", help="Window end (ISO8601 UTC)")
    parser.add_argument(
        "--window", help="Relative window (e.g. 24h, 7d, today, yesterday); overrides start/end"
    )
    parser.add_argument(
        "--by",
        default="step",
        choices=list(BY_LABEL),
        help="Aggregation dimension (default: step)",
    )
    parser.add_argument("--url", default=KUBECOST_URL, help="Kubecost base URL")
    parser.add_argument(
        "--output", "-o", default=DEFAULT_OUTPUT, help="Output path (local or s3://bucket/key)"
    )
    parser.add_argument("--no-share-idle", action="store_true", help="Disable idle cost sharing")
    parser.add_argument(
        "--no-share-tenancy", action="store_true", help="Disable tenancy cost sharing"
    )
    args = parser.parse_args()

    if args.window:
        window = args.window
    elif args.start and args.end:
        window = f"{args.start},{args.end}"
    else:
        parser.error("Provide either --window or both start and end positional arguments.")

    label = BY_LABEL[args.by]
    print(f"Querying Kubecost: window={window}  by={args.by} ({label})", file=sys.stderr)

    allocations = query_kubecost(
        window=window,
        label=label,
        kubecost_url=args.url,
        share_idle=not args.no_share_idle,
        share_tenancy_costs=not args.no_share_tenancy,
    )

    if not allocations:
        print("No cloudpipe allocation data returned for the specified window.", file=sys.stderr)
        sys.exit(1)

    print_summary(allocations, label)

    s3 = boto3.client("s3") if args.output.startswith("s3://") else None
    write_csv(allocations, label, args.output, s3)


if __name__ == "__main__":
    main()
