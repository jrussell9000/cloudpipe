#!/usr/bin/env python3
"""Rebuild cost rows Kubecost lost or under-captured, from archived pod durations.

Why this exists
---------------
The nightly scraper is the only source of `metrics/costs/`, and when Kubecost returns a
partial response there is no second chance: its Allocation API keeps ~2 weeks, so by the
time a gap is noticed the day is gone. On 2026-09-17, 187 workflow-days across
2026-08-18/19/20 and 2026-09-04/05 had real pod activity and NO cost row at all.

What survives is enough to rebuild them. `snapshots/argo-nodes/` holds every pod's
template, host node, start/finish and Argo `resourcesDuration` (cpu core-seconds, memory
100Mi-seconds, nvidia.com/gpu device-seconds), and `pod_costs` from a clean day gives the
$/core-hour, $/GB-hour and $/GPU-hour of each instance type.

USE THIS AS GAP-FILL ONLY. Never overwrite a settled scraped row with a rebuilt one.
--------------------------------------------------------------------------------------
An earlier version of this docstring claimed ±3% accuracy, citing 2026-09-05 (+3.0%) and
2026-09-06 (+1.4%). Those are the days the rates are CALIBRATED on, so that figure was an
in-sample fit residual, not a validation, and it overstated the method badly.

The first genuine held-out test was 2026-08-20 — settled (`scrape_age_days=3`) and covering
1429 of 1430 workflow-days — and the rebuild came in 24% BELOW the measurement.

The reason is structural and not fixable by tuning: pricing pod resource-hours at fixed
per-instance-type rates cannot see node idle or packing, which varies enormously here.
September ran ~43 pod-core-hours per node, August ~24, so real EC2 spend per pod-core-hour
moved from $0.0347 (08-19) to $0.0450 (08-20) — the same pod-hour genuinely cost more in
August. Rates calibrated on efficient September days therefore UNDERPRICE August.

So: where a settled scraped row exists, it wins. Pass `--missing-only` unless you have a
specific reason not to. A full-day rebuild run on 2026-09-17 was reverted for exactly this
reason (see `data/restore_paired_costs.py`).

A rebuilt row is a MODEL, not an invoice, and says so: `source="reconstructed"`, with the
figure it replaced kept in `scraped_total_cost_usd`. Sum dollars as billed truth only over
`source='kubecost'`, or state the basis.

Calibrate on clean days only. Calibrating on a deficient day bakes that day's
under-capture into the ratios and reconstructs the hole it is meant to fill.

Usage
-----
    pixi run python scripts/reconstruct_costs.py --metrics-bucket cloudpipe-metrics \\
        --day 2026-09-04 [--missing-only] [--write]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "metrics"))

from athena import CloudpipeMetrics  # noqa: E402
from compactor import compact_prefix_dt  # noqa: E402

# Re-exported: these moved to src/metrics/cost_gaps.py so the nightly flow could import them
# (the flow-runner image ships src/metrics/, not scripts/). Imported here rather than
# duplicated, and kept in this namespace because callers and tests already reference them.
from cost_gaps import (  # noqa: E402, F401
    GPU_KEY,
    SNAPSHOT_PREFIX,
    load_snapshot_pods,
    pod_day_share,
    ts,
)
from schemas import CostAllocation  # noqa: E402

# Days Kubecost captured cleanly. Both are ordinary production days with the full step mix.
# Rates measured here do NOT transfer to a day with different node packing — see the
# accuracy note in the module docstring.
DEFAULT_CALIBRATION = ("2026-09-05", "2026-09-06")


@dataclass(frozen=True)
class Rates:
    """Unit conversions and per-instance-type prices, from clean days."""

    cpu_ratio: float  # cpu_core_hours per (resourcesDuration.cpu / 3600)
    mem_ratio: float  # ram_gb_hours  per (resourcesDuration.memory / 3600)
    gpu_ratio: float  # gpu_hours     per (resourcesDuration[GPU_KEY] / 3600)
    per_type: dict[str, tuple[float, float, float]]  # itype -> ($/cpu-h, $/GB-h, $/GPU-h)
    fallback: tuple[float, float, float]

    def price(
        self, itype: str | None, cpu_h: float, ram_h: float, gpu_h: float
    ) -> tuple[float, float, float]:
        cpu_rate, ram_rate, gpu_rate = self.per_type.get(itype or "", self.fallback)
        return cpu_h * cpu_rate, ram_h * ram_rate, gpu_h * gpu_rate


def median(values: list[float]) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def calibrate(workflow_quantities: dict, pod_cost_rows: list[dict], key=None) -> Rates:
    """Derive unit ratios and per-instance-type prices from clean-day data.

    `workflow_quantities` maps a key -> (rd_cpu, rd_mem, rd_gpu) from the snapshots;
    `pod_cost_rows` is the matching pod_costs. Ratios come from the workflow grain because
    the two sources key pods differently (Kubecost's pod name carries the template, Argo's
    node id does not) — and the workflow-day IS the grain a cost row is written at.

    `key` maps a pod_costs row to the same key the quantities use. Calibrating over more
    than one day must key by (date, workflow): a workflow that ran on both days otherwise
    has two days of snapshot quantities compared against two days of billing as if it were
    one, and the ratios come out halved.
    """
    key = key or (lambda r: r["workflow_name"])
    billed: dict[object, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    per_type_num: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    for r in pod_cost_rows:
        wf = key(r)
        cpu_h, ram_h, gpu_h = (
            float(r["cpu_core_hours"]),
            float(r["ram_gb_hours"]),
            float(r["gpu_hours"]),
        )
        billed[wf][0] += cpu_h
        billed[wf][1] += ram_h
        billed[wf][2] += gpu_h
        t = per_type_num[r.get("node_instance_type") or ""]
        t[0] += float(r["cpu_cost_usd"])
        t[1] += cpu_h
        t[2] += float(r["memory_cost_usd"])
        t[3] += ram_h
        t[4] += float(r["gpu_cost_usd"])
        t[5] += gpu_h

    cpu_r, mem_r, gpu_r = [], [], []
    for wf, (rd_cpu, rd_mem, rd_gpu) in workflow_quantities.items():
        b = billed.get(wf)
        if not b:
            continue
        # Only workflows with substantial usage: a 3-second recorder pod's ratio is noise.
        if rd_cpu > 3600 and b[0] > 1:
            cpu_r.append(b[0] / (rd_cpu / 3600))
        if rd_mem > 3600 and b[1] > 1:
            mem_r.append(b[1] / (rd_mem / 3600))
        if rd_gpu > 60 and b[2] > 0.1:
            gpu_r.append(b[2] / (rd_gpu / 3600))

    per_type = {}
    for itype, t in per_type_num.items():
        per_type[itype] = (
            t[0] / t[1] if t[1] else 0.0,
            t[2] / t[3] if t[3] else 0.0,
            t[4] / t[5] if t[5] else 0.0,
        )
    tot = [sum(t[i] for t in per_type_num.values()) for i in range(6)]
    fallback = (
        tot[0] / tot[1] if tot[1] else 0.0,
        tot[2] / tot[3] if tot[3] else 0.0,
        tot[4] / tot[5] if tot[5] else 0.0,
    )
    return Rates(
        median(cpu_r) or 1.0, median(mem_r) or 0.0958, median(gpu_r) or 1.0, per_type, fallback
    )


def assign_run(pod_start, candidates: list[dict]) -> dict | None:
    """Pick which run of a REUSED workflow name a pod belongs to, by time window.

    `cloudpipe-*` names repeat once the earlier Workflow object is TTL'd, and two runs of
    one name can even share a day — which is why their Kubecost rows arrive with an empty
    subject. A pod's start time separates them.
    """
    a = ts(pod_start)
    if a is None:
        return candidates[0] if len(candidates) == 1 else None
    hits = [
        c
        for c in candidates
        if ts(c["started_at"]) - timedelta(minutes=2)
        <= a
        <= (ts(c["finished_at"]) or a) + timedelta(minutes=2)
    ]
    if len(hits) == 1:
        return hits[0]
    if not hits:  # outside every window: nearest run that had started
        started = [c for c in candidates if ts(c["started_at"]) <= a]
        return max(started, key=lambda c: ts(c["started_at"])) if started else None
    return None


def build_rows(
    pods: list[dict],
    runs_by_name: dict[str, list[dict]],
    rates: Rates,
    day: date,
    existing: dict[tuple[str, str], float],
    pipeline: str,
) -> tuple[list[CostAllocation], list[dict]]:
    """Aggregate priced pods into one CostAllocation per (workflow, subject)."""
    agg: dict[tuple[str, str], dict] = {}
    audit = []
    for p in pods:
        frac, minutes = pod_day_share(p["started_at"], p["finished_at"], day)
        if frac <= 0:
            continue
        run = assign_run(p["started_at"], runs_by_name.get(p["workflow"], []))
        # workflow_runs is written by the exit handler, so it only exists for a workflow that
        # reached the end. A reconstruction exists BECAUSE workflows failed, so the join misses
        # exactly the rows it is needed for: 1,365 `cloudpipe-*` workflows worth $178 were
        # written with subject="" and then silently dropped by export_batch_metrics, which
        # filters on subject. The snapshot that already supplied this pod also recorded the
        # subject; prefer the run (it is post-hoc and authoritative) and fall back to that.
        subject = (run or {}).get("subject") or p.get("subject") or ""
        cpu_h = p["rd_cpu"] / 3600 * rates.cpu_ratio * frac
        ram_h = p["rd_mem"] / 3600 * rates.mem_ratio * frac
        gpu_h = p["rd_gpu"] / 3600 * rates.gpu_ratio * frac
        cpu_usd, ram_usd, gpu_usd = rates.price(p.get("instance_type"), cpu_h, ram_h, gpu_h)
        key = (p["workflow"], subject)
        a = agg.setdefault(
            key,
            {
                "cpu_h": 0.0,
                "ram_h": 0.0,
                "gpu_h": 0.0,
                "cpu": 0.0,
                "ram": 0.0,
                "gpu": 0.0,
                "pods": 0,
                "minutes": 0.0,
                "unpriced": 0,
            },
        )
        a["cpu_h"] += cpu_h
        a["ram_h"] += ram_h
        a["gpu_h"] += gpu_h
        a["cpu"] += cpu_usd
        a["ram"] += ram_usd
        a["gpu"] += gpu_usd
        a["pods"] += 1
        a["minutes"] += minutes
        a["unpriced"] += int(p.get("instance_type") is None)

    rows = []
    for (wf, subject), a in sorted(agg.items()):
        total = a["cpu"] + a["ram"] + a["gpu"]
        rows.append(
            CostAllocation(
                date=day.isoformat(),
                workflow_name=wf,
                subject=subject,
                total_cost_usd=round(total, 6),
                cpu_cost_usd=round(a["cpu"], 6),
                memory_cost_usd=round(a["ram"], 6),
                gpu_cost_usd=round(a["gpu"], 6),
                total_adjustment_usd=0.0,
                scrape_age_days=(date.today() - day).days,
                source="reconstructed",
                scraped_total_cost_usd=existing.get(wf),
                pipeline=pipeline,
            )
        )
        audit.append(
            {
                "date": day.isoformat(),
                "workflow_name": wf,
                "subject": subject,
                "pods": a["pods"],
                "pod_minutes": round(a["minutes"], 1),
                "cpu_core_hours": round(a["cpu_h"], 4),
                "ram_gb_hours": round(a["ram_h"], 4),
                "gpu_hours": round(a["gpu_h"], 4),
                "pods_without_instance_type": a["unpriced"],
                "total_cost_usd": round(total, 6),
                "scraped_total_cost_usd": existing.get(wf),
            }
        )
    return rows, audit


# ----------------------------------------------------------------- AWS-facing plumbing


def calibrate_from_days(s3, m, bucket: str, cal_days: list[str]) -> tuple[Rates, dict[str, str]]:
    """Load the clean days and return (rates, node -> instance type seen there)."""
    # Key everything by (date, workflow). load_snapshot_pods reads the day AND the morning
    # after (for pods crossing midnight), so two calibration days share snapshot files: a
    # pod-level concat would count those pods twice and halve every ratio. Quantities are
    # also apportioned to the day, to match what that day's pod_costs billed.
    cal_rows, quantities = [], defaultdict(lambda: [0.0, 0.0, 0.0])
    for d in cal_days:
        day = date.fromisoformat(d)
        for p in load_snapshot_pods(s3, bucket, day):
            frac, _ = pod_day_share(p["started_at"], p["finished_at"], day)
            if frac <= 0:
                continue
            q = quantities[(d, p["workflow"])]
            q[0] += float(p["rd_cpu"]) * frac
            q[1] += float(p["rd_mem"]) * frac
            q[2] += float(p["rd_gpu"]) * frac
        cal_rows += m._run_sql(f"""
            SELECT date, workflow_name, node, node_instance_type, cpu_core_hours, ram_gb_hours,
                   gpu_hours, cpu_cost_usd, memory_cost_usd, gpu_cost_usd
            FROM cloudpipe_metrics.pod_costs WHERE date = '{d}'""").to_dict("records")
    rates = calibrate(
        {k: tuple(v) for k, v in quantities.items()},
        cal_rows,
        key=lambda r: (r["date"], r["workflow_name"]),
    )
    node_type = {
        r["node"]: r["node_instance_type"]
        for r in cal_rows
        if r.get("node") and r.get("node_instance_type")
    }
    return rates, node_type


def reconstruct_day(
    s3,
    m,
    args,
    day: date,
    rates: Rates,
    node_type: dict[str, str],
    runs_by_name: dict[str, list[dict]],
) -> tuple[list[CostAllocation], list[dict]]:
    """Price one report-date's pods and return its rows (plus audit records)."""
    day_s = day.isoformat()
    pods = load_snapshot_pods(s3, args.metrics_bucket, day)
    types = dict(node_type)
    for (
        r
    ) in m._run_sql(f"""SELECT DISTINCT node, node_instance_type FROM cloudpipe_metrics.pod_costs
                            WHERE date = '{day_s}'""").to_dict("records"):
        if r.get("node") and r.get("node_instance_type"):
            types[r["node"]] = r["node_instance_type"]
    for p in pods:
        p["instance_type"] = types.get(p.get("host_node"))

    stored = m._run_sql(f"""SELECT workflow_name, subject, total_cost_usd
                            FROM cloudpipe_metrics.costs WHERE date = '{day_s}'""").to_dict(
        "records"
    )
    # Keyed by workflow_name ALONE, because that is how the write is keyed:
    # CostAllocation.s3_key is (date, workflow_name) with no subject in it, so one object
    # per date+workflow and at most one stored row per name per day.
    #
    # It used to be keyed (workflow_name, subject). Argo REUSES workflow names across
    # subjects, so a name billed under subject A but attributed by the snapshots to
    # subject B missed the lookup, was called "missing", and then overwrote the very row
    # the lookup failed to find — with scraped_total_cost_usd=None, erasing the evidence.
    # That silently replaced a settled $3.12 row on 2026-09-05 with a reconstructed $0.22.
    existing = {r["workflow_name"]: float(r["total_cost_usd"]) for r in stored}
    rows, audit = build_rows(pods, runs_by_name, rates, day, existing, args.pipeline)
    if args.missing_only:
        rows = [r for r in rows if r.workflow_name not in existing]
        audit = [a for a in audit if a["workflow_name"] not in existing]

    new = sum(r.total_cost_usd for r in rows)
    replaced = sum(a["scraped_total_cost_usd"] or 0 for a in audit)
    print(
        f"{day_s}: {len(pods):,} pods -> {len(rows):,} rows, ${new:,.2f} reconstructed "
        f"(replacing ${replaced:,.2f} scraped); "
        f"{sum(a['pods_without_instance_type'] for a in audit)} pods unpriced"
    )
    return rows, audit


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--metrics-bucket", required=True)
    ap.add_argument(
        "--day", action="append", required=True, help="Report date to rebuild (repeatable)."
    )
    ap.add_argument(
        "--calibration-day",
        action="append",
        default=None,
        help=f"Clean days to calibrate on (default: {', '.join(DEFAULT_CALIBRATION)}).",
    )
    ap.add_argument(
        "--missing-only",
        action="store_true",
        help="Only write rows for workflow-days that have no cost row at all.",
    )
    ap.add_argument("--pipeline", default="cloudpipe_minproc")
    ap.add_argument("--region", default="<YOUR_AWS_REGION>")
    ap.add_argument("--write", action="store_true", help="Apply (default: dry run).")
    args = ap.parse_args()

    s3 = boto3.client("s3", region_name=args.region)
    m = CloudpipeMetrics(bucket=args.metrics_bucket, region=args.region)
    cal_days = args.calibration_day or list(DEFAULT_CALIBRATION)

    rates, node_type = calibrate_from_days(s3, m, args.metrics_bucket, cal_days)
    print(
        f"calibrated on {', '.join(cal_days)}: cpu x{rates.cpu_ratio:.4f} mem x{rates.mem_ratio:.4f} "
        f"gpu x{rates.gpu_ratio:.4f}, {len(rates.per_type)} instance types"
    )

    runs = m._run_sql("""
        SELECT workflow_name, subject, started_at, finished_at FROM cloudpipe_metrics.workflow_runs
        UNION SELECT workflow_name, subject, started_at, finished_at FROM cloudpipe_metrics.workflow_runs_compacted
    """).to_dict("records")
    runs_by_name: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        runs_by_name[r["workflow_name"]].append(r)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_path = Path(f"data/cost-reconstruction-{stamp}.jsonl")
    total_written = 0
    for day_s in args.day:
        day = date.fromisoformat(day_s)
        rows, audit = reconstruct_day(s3, m, args, day, rates, node_type, runs_by_name)
        if not args.write:
            continue
        with audit_path.open("a") as fh:
            for rec, a in zip(rows, audit, strict=True):
                key = CostAllocation.s3_key(rec.date, rec.workflow_name)
                s3.put_object(
                    Bucket=args.metrics_bucket,
                    Key=key,
                    Body=rec.to_json().encode(),
                    ContentType="application/json",
                )
                fh.write(json.dumps({**a, "key": key}) + "\n")
        total_written += len(rows)
        print(f"  wrote {len(rows):,} objects; recompacting dt={day_s}")
        print("   ", compact_prefix_dt(s3, args.metrics_bucket, args.region, "costs", day_s))

    if args.write:
        print(f"\nwrote {total_written:,} reconstructed rows; audit {audit_path}")
    else:
        print("\n(dry run — nothing written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
