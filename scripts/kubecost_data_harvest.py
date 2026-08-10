#!/usr/bin/env python3
"""
Query the Kubecost allocation API and write per-subject cost data to CSV.

Usage
-----
    # Explicit window:
    python kubecost_data_harvest.py 2026-05-08T18:00:00Z 2026-05-08T20:00:00Z

    # Relative window:
    python kubecost_data_harvest.py --window today

    # Write to S3:
    python kubecost_data_harvest.py 2026-05-08T18:00:00Z 2026-05-08T20:00:00Z \
        --output s3://<YOUR_S3_BUCKET>/kubecost-first-level-costs.csv

    # Aggregate by workflow name instead of subjectid:
    python kubecost_data_harvest.py --window today --label workflows.argoproj.io/workflow
"""

import argparse
import csv
import io
import sys

import boto3
import requests

KUBECOST_URL = "https://kubecost.<YOUR_DOMAIN>"
DEFAULT_OUTPUT = "kubecost_costs.csv"

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
    kubecost_url: str = KUBECOST_URL,
    namespace: str = "argo-workflows",
    label: str = "subjectid",
    share_idle: bool = True,
    share_tenancy_costs: bool = True,
) -> dict:
    params = {
        "window": window,
        "aggregate": f"label:{label}",
        "accumulate": "true",
        "shareIdle": "true" if share_idle else "false",
        "shareTenancyCosts": "true" if share_tenancy_costs else "false",
        "filter": f'namespace:"{namespace}"',
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


def print_summary(allocations: dict) -> None:
    costs = sorted(v.get("totalCost", 0) or 0 for v in allocations.values())
    n = len(costs)
    if n == 0:
        return
    mean = sum(costs) / n
    print(
        f"  runs={n}  min=${costs[0]:.5f}  max=${costs[-1]:.5f}  "
        f"mean=${mean:.5f}  total=${sum(costs):.5f}",
        file=sys.stderr,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("start", nargs="?", help="Window start (ISO8601 UTC)")
    parser.add_argument("end", nargs="?", help="Window end (ISO8601 UTC)")
    parser.add_argument(
        "--window", help="Relative window (e.g. today, yesterday, 1h, 2d); overrides start/end"
    )
    parser.add_argument("--url", default=KUBECOST_URL, help="Kubecost base URL")
    parser.add_argument("--namespace", default="argo-workflows", help="Namespace filter")
    parser.add_argument("--label", default="subjectid", help="Pod label to aggregate by")
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

    print(
        f"Querying Kubecost: window={window} label={args.label} namespace={args.namespace}",
        file=sys.stderr,
    )

    allocations = query_kubecost(
        window=window,
        kubecost_url=args.url,
        namespace=args.namespace,
        label=args.label,
        share_idle=not args.no_share_idle,
        share_tenancy_costs=not args.no_share_tenancy,
    )

    if not allocations:
        print("No allocation data returned for the specified window.", file=sys.stderr)
        sys.exit(1)

    print_summary(allocations)

    s3 = boto3.client("s3") if args.output.startswith("s3://") else None
    write_csv(allocations, args.label, args.output, s3)


if __name__ == "__main__":
    main()
