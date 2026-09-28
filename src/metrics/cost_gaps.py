"""Detect report-dates whose cost data is missing or under-captured, while it is fixable.

The nightly scraper's own `PARTIAL_READ_FRACTION` guard compares a Kubecost response
against what is already stored for that date, so at day+1 — when nothing is stored yet —
it cannot fire. That is exactly how 2026-09-04 was lost: the first write WAS the partial
one, and by the time the hole was found on 2026-09-17, Kubecost's ~2-week Allocation
retention had dropped 2026-08-18/19/20 entirely.

This checks the one thing the scraper cannot: the cluster's own record of what ran.
`snapshots/argo-nodes/` lists every pod with its template, start and finish, so a
workflow-day with pod activity and no cost row is a gap, and a day whose billed
resource-hours fall far below the pods' own `resourcesDuration` is an under-capture.

Both signals are counted in **cpu-core-hours, never dollars**. That is deliberate. Dollar
comparisons across days are unreliable here because the cluster's packing varies: September
days ran ~43 pod-core-hours per node against August's ~24, so real EC2 spend per pod-core-hour
moved between $0.0347 and $0.0450 over six measured days. Core-hours are what the pods
actually consumed and are immune to that; this module measures *capture*, not price.

This lives in `src/metrics/` rather than beside the CLI in `scripts/` because the
prefect-flow-runner image ships `src/metrics/` and `prefect/flows/` only — a nightly flow
cannot import from `scripts/`.

It reads S3 directly rather than going through Athena, for three reasons: the flow-runner
image has no pandas, which `athena._run_sql` needs; raw objects are the authoritative record
of what was written, while a Glue projection whose `schema_version` enum has not been
extended returns zero rows with NO error, which would make this report a whole day missing
when it is merely invisible; and the MISSING signal needs no object reads at all, because
`CostAllocation.s3_key` puts the workflow name in the key.
"""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

SNAPSHOT_PREFIX = "snapshots/argo-nodes/"
GPU_KEY = "nvidia.com/gpu"

# Clean days measure ~0.95-1.0; 2026-09-04, the day that prompted this, measured 0.5.
DEFAULT_MIN_CAPTURE = 0.75
# Day+1 through the settled re-scrape, so a gap is seen at every age the scraper writes at.
DEFAULT_SINCE_DAYS = 4
# An unbilled workflow is only worth waking someone for if it consumed something.
# Measured 2026-09-17 over 09-12/15/16, the two populations are three orders of magnitude
# apart and nothing sits between them:
#   argo-nodes-snapshot-* cron   0.0000-0.0003 core-h   (runs EVERY day, never billed)
#   probe-* one-offs             0.0000 core-h          (36 min of wall clock, no cpu, no gpu)
#   real unbilled subject work   0.78-22.73 core-h
# The threshold is on resources consumed, not wall-clock minutes: a GPU probe idling for
# 36 minutes costs nothing, while the thing this exists to catch burns core-hours. A
# wall-clock rule flagged the cron nightly, which with fail_on_gap would have failed the
# run every night over a fraction of a cent and taught everyone to ignore it.
DEFAULT_MIN_CORE_HOURS = 0.05


def ts(value):
    """Parse an ISO timestamp, tolerating the several ways 'absent' is spelled here.

    Athena hands back NaN, pandas hands back <NA>, and the snapshots hand back None for a
    pod that had not finished when the hour was captured.
    """
    if value is None or str(value) in ("", "nan", "None", "<NA>"):
        return None
    t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def pod_day_share(started_at, finished_at, day: date, now=None) -> tuple[float, float]:
    """(fraction of the pod's life that fell on `day`, minutes on `day`)."""
    d0 = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    d1 = d0 + timedelta(days=1)
    a, b = ts(started_at), ts(finished_at) or now or d1
    if a is None or a >= d1 or b <= d0:
        return 0.0, 0.0
    on = (min(b, d1) - max(a, d0)).total_seconds()
    whole = (b - a).total_seconds()
    return (on / whole if whole > 0 else 1.0), on / 60


def load_snapshot_pods(s3, bucket: str, day: date, workers: int = 24) -> list[dict]:
    """Every distinct pod seen on `day` or the morning after (pods crossing midnight)."""
    keys = []
    for dt in (day.isoformat(), (day + timedelta(days=1)).isoformat()):
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=f"{SNAPSHOT_PREFIX}dt={dt}/"
        ):
            keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".jsonl.gz")]

    def scan(key):
        out = []
        for line in (
            gzip.decompress(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
            .decode()
            .splitlines()
        ):
            rec = json.loads(line)
            for p in rec.get("pods") or []:
                rd = p.get("resources_duration") or {}
                out.append(
                    {
                        "workflow": rec.get("workflow"),
                        # Carried for reconstruct_costs: a workflow that died before its exit
                        # handler ran has no workflow_runs record, so the runs join cannot name
                        # its subject. The snapshot recorded the subject while it was alive.
                        "subject": rec.get("subject"),
                        "pod": p.get("node_id"),
                        "template": p.get("template"),
                        "started_at": p.get("started_at"),
                        "finished_at": p.get("finished_at"),
                        "host_node": p.get("host_node"),
                        "rd_cpu": rd.get("cpu") or 0,
                        "rd_mem": rd.get("memory") or 0,
                        "rd_gpu": rd.get(GPU_KEY) or 0,
                    }
                )
        return out

    pods: dict[tuple[str, str], dict] = {}
    with ThreadPoolExecutor(workers) as ex:
        for out in ex.map(scan, sorted(set(keys))):
            for p in out:
                # Keyed by (node_id, started_at), NOT node_id alone. An Argo node id is
                # deterministic from (workflow name, template), so when a workflow name is
                # REUSED across subjects both runs emit the SAME ids — 32 of 37 ids for
                # cloudpipe-k6s5v appear twice, once per run. Deduping on the id alone
                # silently discarded one whole run, which also defeated
                # reconstruct_costs.assign_run(): it splits reused names by pod start time,
                # but never saw the pods this had already dropped.
                # started_at is fixed once a pod starts, so repeated hourly sightings of one
                # pod still collapse, while two runs stay distinct.
                key = (p["pod"], str(p["started_at"]))
                prev = pods.get(key)
                # Hourly snapshots repeat every pod; keep the sighting that has its end time.
                if prev is None or (prev["finished_at"] is None and p["finished_at"] is not None):
                    pods[key] = p
    return list(pods.values())


def billed_workflows(s3, bucket: str, day: date) -> set[str]:
    """Workflow names that have a cost row for `day`, from raw key names alone.

    No object reads: `CostAllocation.s3_key` is
    `metrics/costs/dt={date}/{date}_{workflow}_cost_allocation.json`.
    """
    d = day.isoformat()
    prefix, suffix = f"metrics/costs/dt={d}/", "_cost_allocation.json"
    out = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            base = o["Key"].rsplit("/", 1)[-1]
            if base.startswith(f"{d}_") and base.endswith(suffix):
                out.add(base[len(d) + 1 : -len(suffix)])
    return out


def billed_cpu_core_hours(s3, bucket: str, day: date, workers: int = 24) -> float:
    """Sum `cpu_core_hours` over every pod_costs record for `day`.

    The workflow-grain CostAllocation carries dollars only, so the under-capture signal has
    to come from the pod grain. Each object is newline-delimited PodCost JSON.
    """
    d = day.isoformat()
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"metrics/pod-costs/dt={d}/"
    ):
        keys += [o["Key"] for o in page.get("Contents", [])]

    def total(key) -> float:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
        return sum(
            float(json.loads(line).get("cpu_core_hours") or 0)
            for line in body.splitlines()
            if line.strip()
        )

    if not keys:
        return 0.0
    with ThreadPoolExecutor(workers) as ex:
        return sum(ex.map(total, keys))


def gaps_for_day(
    pods: list[dict],
    billed: set[str],
    billed_cpu: float,
    day: date,
    min_capture: float = DEFAULT_MIN_CAPTURE,
    min_core_hours: float = DEFAULT_MIN_CORE_HOURS,
) -> dict:
    """Compare what ran against what was billed, for one report-date."""
    active: dict[str, float] = defaultdict(float)
    rd_cpu: dict[str, float] = defaultdict(float)
    rd_gpu: dict[str, float] = defaultdict(float)
    for p in pods:
        frac, minutes = pod_day_share(p["started_at"], p["finished_at"], day)
        if frac <= 0:
            continue
        active[p["workflow"]] += minutes
        rd_cpu[p["workflow"]] += float(p["rd_cpu"]) / 3600 * frac
        rd_gpu[p["workflow"]] += float(p["rd_gpu"]) / 3600 * frac
    # Unbilled AND cost-bearing. GPU counts as well as CPU: a GPU step that barely touches
    # the cpu is still expensive, and missing its row still loses real money.
    missing = sorted(
        wf for wf in active if wf not in billed and (rd_cpu[wf] + rd_gpu[wf]) > min_core_hours
    )
    snap_cpu = sum(rd_cpu.values())
    capture = billed_cpu / snap_cpu if snap_cpu else 1.0
    return {
        "date": day.isoformat(),
        "workflows_active": len(active),
        "workflows_billed": len(billed),
        "missing": missing,
        "snapshot_cpu_core_hours": round(snap_cpu, 1),
        "billed_cpu_core_hours": round(billed_cpu, 1),
        "capture": round(capture, 3),
        "shortfall": capture < min_capture,
    }


def scan_recent_days(
    s3,
    bucket: str,
    since_days: int = DEFAULT_SINCE_DAYS,
    min_capture: float = DEFAULT_MIN_CAPTURE,
    today: date | None = None,
    min_core_hours: float = DEFAULT_MIN_CORE_HOURS,
) -> list[dict]:
    """Run `gaps_for_day` over the last `since_days` report-dates.

    Days with no pod activity at all are skipped rather than reported clean: an idle day
    has nothing to bill, and flagging it would train the reader to ignore the signal.
    """
    today = today or datetime.now(timezone.utc).date()
    results = []
    for back in range(1, since_days + 1):
        day = today - timedelta(days=back)
        pods = load_snapshot_pods(s3, bucket, day)
        if not pods:
            continue
        results.append(
            gaps_for_day(
                pods,
                billed_workflows(s3, bucket, day),
                billed_cpu_core_hours(s3, bucket, day),
                day,
                min_capture,
                min_core_hours,
            )
        )
    return results


def format_day(g: dict) -> str:
    """One line per report-date, for a CLI or a flow log."""
    flag = "GAP" if (g["missing"] or g["shortfall"]) else "ok "
    return (
        f"{flag} {g['date']}: {g['workflows_active']:>5} workflows ran, "
        f"{g['workflows_billed']:>5} billed, {len(g['missing']):>4} with no cost row | "
        f"capture {g['capture']:.0%} "
        f"({g['billed_cpu_core_hours']:,} of {g['snapshot_cpu_core_hours']:,} cpu-core-h)"
    )


def has_gap(results: list[dict]) -> bool:
    return any(g["missing"] or g["shortfall"] for g in results)
