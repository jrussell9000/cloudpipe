"""Cost and performance of the current pipeline, from the audit run (openspec add-reprocess-audit, task 3.5).

Spec `audit-comparison`, "The audit reports cost and performance for the current
pipeline". Read-only: reads the audit metrics bucket's pod-cost and workflow-run
records and the audit data bucket's func markers; writes only --out.

Pricing is the fMRIPrep comparison's (methods 8.1), reused rather than re-derived:
equivalence.efficiency.aggregate_pod_costs prices each pod from its own usage
and its instance's list price (node_h x node_ondemand_usd_per_hour + pv +
network) and marks it settled at scrape_age_days >= 3. So an audit pod whose
settled row carries its instance but no effective rate (#1060's carry-forward)
is still priced at list price; only the spot-savings figure needs the effective
rate, and it states its coverage.

Per session, a pod with a session label is that session's; a subject-level pod
(fsqc, inventory, the template steps, bookkeeping) is charged two ways, as the
methods do for the longitudinal template: in FULL to each of the subject's k
sessions, and as 1/k. Each per-session cost is also divided by the session's
BOLD run count, read from its func `_complete.json`.

Ingress (https-transfer, verify-staged-input, delete-globus-input) is reported
apart from compute: it is bound by the source's HTTPS server, not the pipeline.
Noise-floor repeat runs (cloudpipe-audit-repeat-*) are excluded, so a subject
is counted once.

A report requested before every row has settled refuses cost and reports
performance only (spec: "Unsettled costs").

    pixi run -e ops python -m audit.report --metrics-bucket <audit metrics> --data-bucket <audit data> \\
        --sample data/reprocess/audit-sample-2026-11-08/sample.jsonl --from 2026-12-01 --to 2026-12-10 --out report.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

AUDIT_PREFIX = "cloudpipe-audit-"
REPEAT_PREFIX = "cloudpipe-audit-repeat-"
INGRESS_STEPS = frozenset(
    {"https-transfer", "verify-staged-input", "delete-globus-input", "globus-transfer"}
)

# The instance families each CPU step's nodepool allows, for the whole pipeline
# (the efficiency reader's own map covers only what its benchmark prices). From
# each WorkflowTemplate's nodeSelector and the Karpenter nodepools' instance
# categories: cpu-light-nodepool is t; cpu-heavy- and ingress-nodepool are c/m
# (the default). A pod on another family is not the pod's node (#1060), so it is
# unpriced. tests/audit/test_audit_report.py holds this against the templates.
CPU_LIGHT_STEPS = (
    "delete-globus-input", "dvars-backfill", "fsqc-metrics", "newmsm-image-guard",
    "published-sessions", "record-step-outcome", "subject-data-inventory", "surface-resample",
    "verify-staged-input", "workflow-run-metrics", "workflow-start-marker", "workflow-verdict",
)  # fmt: skip
STEP_FAMILIES: dict[str, tuple[str, ...]] = {s: ("t",) for s in CPU_LIGHT_STEPS}
POD_COLUMNS = (
    "date", "workflow_name", "pod", "step", "subject", "session", "total_cost_usd", "cpu_cost_usd",
    "memory_cost_usd", "gpu_cost_usd", "pv_cost_usd", "network_cost_usd", "runtime_minutes",
    "cpu_core_hours", "ram_gb_hours", "gpu_hours", "cpu_efficiency", "ram_efficiency",
    "node_instance_type", "node_capacity_type", "node_effective_usd_per_hour",
    "node_ondemand_usd_per_hour", "node_instance_id", "scrape_age_days",
)  # fmt: skip


def audit_rows(
    rows: pd.DataFrame, prefix: str = AUDIT_PREFIX, exclude: str = REPEAT_PREFIX
) -> pd.DataFrame:
    """The main audit run's pod rows only: no repeat runs, nothing else in the bucket."""
    wf = rows["workflow_name"].astype(str)
    keep = wf.str.startswith(prefix)
    if exclude:
        keep &= ~wf.str.startswith(exclude)
    out = rows[keep].copy()
    for c in POD_COLUMNS:
        if c not in out:
            out[c] = None if c.startswith("node_") or c in ("session", "step") else 0.0
    out["step"] = out["step"].fillna("").astype(str)
    out["session"] = out["session"].fillna("").astype(str)
    return out


def _pct(values: pd.Series, q: float) -> float:
    v = values.dropna().astype(float)
    return float(np.percentile(v, q)) if len(v) else math.nan


def _spot_savings(rows: pd.DataFrame) -> dict[str, float]:
    """CloudpipeMetrics.spot_savings()'s formula, over the rows that carry both rates."""
    both = rows["node_effective_usd_per_hour"].notna() & rows["node_ondemand_usd_per_hour"].notna()
    both &= rows["node_effective_usd_per_hour"].astype(float) > 0
    r = rows[both]
    mult = r["node_ondemand_usd_per_hour"].astype(float) / r["node_effective_usd_per_hour"].astype(
        float
    )
    compute = r["cpu_cost_usd"] + r["memory_cost_usd"] + r["gpu_cost_usd"]
    on_demand = float((compute * mult + r["pv_cost_usd"] + r["network_cost_usd"]).sum())
    operated = float(r["total_cost_usd"].sum())
    total = float(rows["total_cost_usd"].sum())
    return {
        "operated_usd_covered": operated,
        "on_demand_usd_covered": on_demand,
        "multiple": on_demand / operated if operated else math.nan,
        "coverage_frac": operated / total if total else math.nan,
    }


def build_report(
    rows: pd.DataFrame,
    sessions: dict[str, list[str]],
    runs: dict[tuple[str, str], int],
    workflow_runs: list[dict[str, Any]] | None = None,
    prefix: str = AUDIT_PREFIX,
    exclude: str = REPEAT_PREFIX,
) -> dict[str, Any]:
    """The report from the audit's pod rows, each subject's sessions, and per-session run counts.

    `prefix`/`exclude` select the workflows: the main audit run by default. Another
    prefix (e.g. "cloudpipe-" with exclude "cloudpipe-audit-") runs the same report
    over production days, which is how the report was checked before audit costs existed.
    """
    from equivalence.efficiency import aggregate_pod_costs

    rows = audit_rows(rows, prefix, exclude)
    settled = bool(len(rows)) and bool((rows["scrape_age_days"].astype(float) >= 3).all())
    report: dict[str, Any] = {"n_workflows": int(rows["workflow_name"].nunique()), "n_pod_rows": len(rows),
                              "settled": settled}  # fmt: skip

    meta = rows.groupby(["workflow_name", "pod"], sort=False).agg(
        step=("step", "first"), subject=("subject", "first"), session=("session", "first"),
        gpu_pod=("gpu_hours", lambda s: bool((s.astype(float) > 0).any())),
        has_instance=("node_instance_id", lambda s: bool(s.fillna("").astype(str).ne("").all())),
        has_effective=("node_effective_usd_per_hour", lambda s: bool(s.notna().all())),
    ).reset_index()  # fmt: skip
    priced_pods = aggregate_pod_costs(rows, STEP_FAMILIES) if len(rows) else None
    pods = meta.merge(priced_pods, on=["workflow_name", "pod"]) if priced_pods is not None else meta

    # Performance: always reported.
    perf: dict[str, Any] = {}
    for step, g in pods.groupby("step"):
        perf[step or "<unlabelled>"] = {
            "n_pods": int(len(g)),
            "gpu": bool(g["gpu_pod"].any()),
            "ingress": step in INGRESS_STEPS,
            "runtime_min_p50": _pct(g["runtime_minutes"], 50),
            "runtime_min_p95": _pct(g["runtime_minutes"], 95),
            "runtime_min_total": float(g["runtime_minutes"].sum()),
        }
    report["per_step"] = perf
    if workflow_runs:
        dur = pd.Series([w.get("total_duration_s") for w in workflow_runs
                         if str(w.get("workflow_name", "")).startswith(prefix)
                         and not (exclude and str(w.get("workflow_name", "")).startswith(exclude))], dtype=float)  # fmt: skip
        report["workflow_wall_clock_h"] = {"n": int(dur.notna().sum()), "p50": _pct(dur, 50) / 3600,
                                           "p95": _pct(dur, 95) / 3600, "max": float(dur.max()) / 3600}  # fmt: skip

    if not settled:
        report["cost"] = None
        report["cost_refused"] = (
            "not every pod row has reached scrape_age_days >= 3; re-run after it settles"
        )
        return report

    # Cost.
    priced = pods["ondemand_usd"].notna()
    report["coverage"] = {
        "pods": int(len(pods)),
        "priced_at_list": int(priced.sum()),
        "unpriced": int((~priced).sum()),
        "unpriced_reasons": pods.loc[~priced, "problem"].fillna("unknown").value_counts().to_dict(),
        "carried_instance_no_effective_rate": int(
            (pods["has_instance"] & ~pods["has_effective"]).sum()
        ),
    }
    report["spot_savings"] = _spot_savings(rows)
    for step, g in pods.groupby("step"):
        perf[step or "<unlabelled>"] |= {
            "operated_usd": float(g["operated_usd"].sum()),
            "on_demand_usd": float(g["ondemand_usd"].sum())
            if g["ondemand_usd"].notna().all()
            else math.nan,
        }
    ingress = pods["step"].isin(INGRESS_STEPS)
    report["ingress"] = {"operated_usd": float(pods.loc[ingress, "operated_usd"].sum()),
                         "runtime_min_total": float(pods.loc[ingress, "runtime_minutes"].sum())}  # fmt: skip

    compute = pods[~ingress]
    per_session = []
    for subject, g in compute.groupby("subject"):
        ses = sessions.get(subject) or sorted(set(g["session"]) - {""})
        k = len(ses)
        if not k:
            continue
        shared = g[g["session"] == ""]
        for s in ses:
            own = g[g["session"] == s]
            n_runs = runs.get((subject, s), 0)
            row = {"subject": subject, "session": s, "k": k, "runs": n_runs}
            for col, name in (("operated_usd", "operated"), ("ondemand_usd", "on_demand")):
                o, sh = own[col].astype(float), shared[col].astype(float)
                complete = o.notna().all() and sh.notna().all()
                full = float(o.sum() + sh.sum()) if complete else math.nan
                share = float(o.sum() + sh.sum() / k) if complete else math.nan
                row[f"{name}_full"], row[f"{name}_1k"] = full, share
                row[f"{name}_1k_per_run"] = share / n_runs if n_runs and complete else math.nan
            per_session.append(row)
    ps = pd.DataFrame(per_session)
    summary: dict[str, Any] = {"n_sessions": int(len(ps))}
    for c in ("operated_full", "operated_1k", "operated_1k_per_run", "on_demand_full", "on_demand_1k",
              "on_demand_1k_per_run"):  # fmt: skip
        if c in ps:
            summary[c] = {"median": _pct(ps[c], 50), "p5": _pct(ps[c], 5), "p95": _pct(ps[c], 95),
                          "n": int(ps[c].notna().sum())}  # fmt: skip
    report["per_session"] = summary
    report["total"] = {"operated_usd": float(pods["operated_usd"].sum()),
                       "on_demand_usd_priced": float(pods.loc[priced, "ondemand_usd"].sum())}  # fmt: skip
    return report


# ---------------------------------------------------------------------------
# Loaders (read-only S3)
# ---------------------------------------------------------------------------


def _jsonl_objects(s3, bucket: str, prefix: str) -> list[dict]:
    out: list[dict] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            body = s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read().decode()
            try:
                out.append(json.loads(body))
            except json.JSONDecodeError:
                out += [json.loads(line) for line in body.splitlines() if line.strip()]
    return out


def load(s3, metrics_bucket: str, start: date, end: date) -> tuple[pd.DataFrame, list[dict]]:
    pods: list[dict] = []
    wfs: list[dict] = []
    d = start
    while d <= end:
        pods += _jsonl_objects(s3, metrics_bucket, f"metrics/pod-costs/dt={d.isoformat()}/")
        wfs += _jsonl_objects(s3, metrics_bucket, f"metrics/workflow-runs/dt={d.isoformat()}/")
        d += timedelta(days=1)
    return pd.DataFrame(pods), wfs


def load_runs(s3, data_bucket: str, sessions: dict[str, list[str]]) -> dict[tuple[str, str], int]:
    runs: dict[tuple[str, str], int] = defaultdict(int)
    for subject, ses_list in sessions.items():
        for s in ses_list:
            try:
                body = s3.get_object(
                    Bucket=data_bucket, Key=f"derivatives/func/{subject}/{s}/_complete.json"
                )
            except s3.exceptions.NoSuchKey:
                continue
            doc = json.loads(body["Body"].read())
            if doc.get("kind") == "func-session":
                runs[(subject, s)] = len(doc.get("runs") or {})
    return dict(runs)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--metrics-bucket", required=True)
    p.add_argument("--data-bucket", required=True)
    p.add_argument("--sample", type=Path, required=True)
    p.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    p.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    p.add_argument("--prefix", default=AUDIT_PREFIX)
    p.add_argument("--exclude-prefix", default=REPEAT_PREFIX)
    p.add_argument("--region")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)

    import boto3

    s3 = boto3.client("s3", region_name=args.region)
    sample = [json.loads(line) for line in args.sample.read_text().splitlines() if line.strip()]
    sessions = {r["subject"]: r["production_sessions"] for r in sample}
    rows, wfs = load(s3, args.metrics_bucket, args.start, args.end)
    report = build_report(rows, sessions, load_runs(s3, args.data_bucket, sessions), wfs,
                          args.prefix, args.exclude_prefix)  # fmt: skip
    args.out.write_text(json.dumps(report, indent=1, default=float) + "\n")
    print(
        json.dumps(
            {k: report.get(k) for k in ("n_workflows", "settled", "coverage", "per_session")},
            default=float,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
