"""
rewrite_func_costs.py — remove the func pods of non-producing workflows from
pod_costs, and subtract them from costs, so both tables describe only what
produced the stored func derivatives (plan 017, Step 2).

A func pod is a `bold-preprocessing` or `surface-resample` pod. Its workflow is
the subject's func PRODUCER when that workflow's [started_at - 2 min,
finished_at + 2 min] window contains the LastModified of a stored tarball (the
same rule as reprocess_func.py reconcile). Every other func pod goes: the
original pre-fix runs, failed reprocess attempts, and the pods of a subject that
now has no func derivative at all.

The totals then no longer reconcile to the AWS bill. That is accepted.

Safety:
  - Only report-dates at least SAFE_AGE_DAYS old are touched. The cost scraper
    rewrites today-1 and today-3 every night and would put the pods back.
  - Nothing is written if any costs row would go below -$0.001, if a costs row
    is missing or names another subject, or (with --expect-usd) if the total is
    more than 5% off.
  - A subject whose stored tarballs no workflow window covers keeps its pods.
  - Each (date, workflow) is a two-object rewrite; a ledger in the audit dir
    makes a crash between the two resumable instead of double-subtracting.
  - A costs row whose latest version is a delete marker was removed on purpose
    (a test-batch flush deletes costs but keeps pod-costs unless --flush-qc).
    Its func pods still go; there is nothing to subtract from. A costs row that
    is simply absent stops the run.
  - Compacted costs and pod_costs are patched in place, never rebuilt from raw:
    a rebuild shrinks any partition whose raw is a subset of compacted.

Reconstructed costs rows (source = "reconstructed", the 2026-09-17 gap-fill) are
a model, not a sum of these pods, so scraped pod costs are not subtracted from
them. Instead reconstruct_costs.py's own model is re-run for each one: the row
loses the fraction of its modelled cost that the non-producer's func pods carry,
stored_total x (model_func / model_total), in every cost column. A row the model
no longer reproduces to within RECON_TOLERANCE_USD stops the run. The model's
outputs are frozen in the ledger the first time they are computed, because the
model calibrates on pod_costs, which this tool then edits. The pass is driven by
the costs rows themselves, so it also reaches rows that have no pod-costs object.

Usage:
  pixi run -e ops python scripts/rewrite_func_costs.py \\
      --data-bucket "$CLOUDPIPE_BUCKET" --metrics-bucket "$CLOUDPIPE_METRICS_BUCKET" \\
      [--expect-usd N] [--write]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import boto3

# src/metrics/ modules import each other flatly, so the package directory itself
# has to be on the path (as in prep_test_batch.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "metrics"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compactor import UnattributableRows, drop_rows, write_parquet_to_s3  # noqa: E402
from prep_test_batch import list_keys, workflow_name_from_key  # noqa: E402
from reprocess_func import (  # noqa: E402
    DEFAULT_AUDIT_DIR,
    Audit,
    compacted_files,
    find_producers,
    list_func_units,
    load_runs,
    now_utc,
)

# PodCost.step for the two func pods. Pod-cost steps are the template names, not
# the step_outcomes vocabulary: func-preproc's pod is `bold-preprocessing`.
FUNC_POD_STEPS = ("bold-preprocessing", "surface-resample")
# The CostAllocation fields a pod's cost contributes to. scraped_total_cost_usd is
# deliberately absent: it is the scrape-time audit value and stays as scraped.
COST_FIELDS = (
    "total_cost_usd",
    "cpu_cost_usd",
    "memory_cost_usd",
    "gpu_cost_usd",
    "total_adjustment_usd",
)
# The scraper rewrites today-1 and today-SETTLED_AGE_DAYS (3); one day past that
# is the newest date it will not touch again.
SAFE_AGE_DAYS = 4
NEGATIVE_FLOOR_USD = -0.001
EXPECT_TOLERANCE = 0.05
POD_COST_PREFIX = "metrics/pod-costs/"
POD_COST_SUFFIX = "_pod_costs.json"
DT_RE = re.compile(r"/dt=(\d{4}-\d{2}-\d{2})/")
LEDGER_NAME = "func_cost_rewrite_ledger.jsonl"
COST_PREFIX = "metrics/costs/"
# The Argo templates of the two func pods, as the argo-nodes snapshots name them.
FUNC_TEMPLATES = ("functional-preprocessing-session-template", "surface-resample-session-template")
# Re-running the model reproduced 1,649 of 1,696 rows exactly on 2026-10-12, and the
# rest within $0.03; a larger miss means the model no longer describes the row.
RECON_TOLERANCE_USD = 0.05


def cost_key(day: str, workflow: str) -> str:
    return f"metrics/costs/dt={day}/{day}_{workflow}_cost_allocation.json"


def parse_pods(body: bytes) -> list[dict]:
    """A pod-costs body: JSONL since the writer began, a JSON array in older notes."""
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        return [json.loads(line) for line in body.splitlines() if line.strip()]
    return doc if isinstance(doc, list) else [doc]


def serialize_pods(pods: list[dict]) -> bytes:
    """The same layout writer.emit_jsonl_to_s3 writes."""
    return ("\n".join(json.dumps(p) for p in pods) + "\n").encode()


def producer_windows(runs, producers) -> dict[str, list]:
    """subject -> the Run records of its func producers."""
    out: dict[str, list] = defaultdict(list)
    for r in runs:
        if (r.name, r.uid) in producers.get(r.subject, set()):
            out[r.subject].append(r)
    return out


def pod_goes(pod: dict, day: date, windows, unattributed: set) -> bool:
    """True when a pod is a func pod that did not come from its subject's producer.

    PodCost carries no workflow_uid, and Argo reuses names, so a name match also
    has to fall on a date the producer was running.
    """
    if pod.get("step") not in FUNC_POD_STEPS:
        return False
    subject = pod.get("subject")
    if not subject or subject in unattributed:
        return False
    for r in windows.get(subject, []):
        if (
            r.name == pod.get("workflow_name")
            and r.started_at.date() <= day <= r.finished_at.date()
        ):
            return False
    return True


@dataclass
class Edit:
    day: str
    workflow: str
    pod_key: str
    pod_etag: str
    keep: list[dict]
    removed: list[dict]
    cost_key: str = ""
    cost_etag: str = ""
    cost_before: dict = field(default_factory=dict)
    cost_after: dict = field(default_factory=dict)
    # The costs row is already gone (a test-batch flush deletes costs but keeps
    # pod-costs unless --flush-qc), so there is nothing to subtract from.
    orphan: bool = False
    # Its costs row is reconstructed: the costs side is the reconstruction pass's.
    recon: bool = False

    @property
    def reconstructed(self) -> bool:
        """A gap-fill row from reconstruct_costs.py: its total was not summed from these pods."""
        return self.cost_before.get("source") == "reconstructed"

    def removed_sums(self) -> dict[str, float]:
        return {f: round(sum(p.get(f) or 0.0 for p in self.removed), 6) for f in COST_FIELDS}


def read_obj(s3, bucket: str, key: str) -> tuple[bytes, str] | None:
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except Exception:
        return None
    return obj["Body"].read(), obj["ETag"]


def subtract(cost: dict, sums: dict[str, float]) -> dict:
    out = dict(cost)
    for f in COST_FIELDS:
        out[f] = round((cost.get(f) or 0.0) - sums[f], 6)
    return out


@dataclass
class Context:
    windows: dict
    unattributed: set
    newest_safe: date


def plan(s3, args, today: date) -> tuple[list[Edit], list[Edit], list[str], Context]:
    """(edits, edits held inside the scraper window, problems that block writing, context)."""
    mb = args.metrics_bucket
    _, _, tarball_times = list_func_units(s3, args.data_bucket)
    runs = load_runs(s3, mb, args.workers)
    producers, unattributed = find_producers(runs, tarball_times)
    windows = producer_windows(runs, producers)
    if unattributed:
        print(f"  [hold] {len(unattributed)} subject(s) unattributed; their pods are kept")

    keys = [k for k in list_keys(s3, mb, POD_COST_PREFIX) if k.endswith(POD_COST_SUFFIX)]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        bodies = list(pool.map(lambda k: read_obj(s3, mb, k), keys))

    newest_safe = today - timedelta(days=SAFE_AGE_DAYS)
    edits, held, problems = [], [], []
    for key, got in zip(keys, bodies, strict=True):
        m = DT_RE.search(key)
        workflow = workflow_name_from_key(key, POD_COST_PREFIX, POD_COST_SUFFIX)
        if got is None or not m or not workflow:
            problems.append(f"unreadable or malformed pod-costs object {key}")
            continue
        day = date.fromisoformat(m.group(1))
        pods = parse_pods(got[0])
        removed = [p for p in pods if pod_goes(p, day, windows, unattributed)]
        if not removed:
            continue
        keep = [p for p in pods if not pod_goes(p, day, windows, unattributed)]
        edit = Edit(day.isoformat(), workflow, key, got[1], keep, removed)
        (edits if day <= newest_safe else held).append(edit)

    for e in edits:
        problems += attach_cost(s3, mb, e)
    return edits, held, problems, Context(windows, unattributed, newest_safe)


def attach_cost(s3, mb: str, e: Edit) -> list[str]:
    """Fill the edit's costs row and its subtraction; return what blocks it."""
    e.cost_key = cost_key(e.day, e.workflow)
    got = read_obj(s3, mb, e.cost_key)
    if got is None:
        if costs_row_was_deleted(s3, mb, e.cost_key):
            e.orphan = True
            return []
        return [f"no costs row, and no delete marker to explain it, for {e.pod_key}"]
    e.cost_before, e.cost_etag = json.loads(got[0]), got[1]
    if e.reconstructed:
        e.recon = True
        return []
    subjects = {p.get("subject") for p in e.removed}
    if subjects != {e.cost_before.get("subject")}:
        return [f"{e.cost_key} names {e.cost_before.get('subject')}, its pods {sorted(subjects)}"]
    e.cost_after = subtract(e.cost_before, e.removed_sums())
    if e.cost_after["total_cost_usd"] < NEGATIVE_FLOOR_USD:
        return [f"{e.cost_key} would go to ${e.cost_after['total_cost_usd']:.5f}"]
    return []


def costs_row_was_deleted(s3, mb: str, key: str) -> bool:
    """The key's latest version is a delete marker: someone removed the row on purpose.

    Any other absence (never written, wrong key) is unexplained and stops the run.
    """
    resp = s3.list_object_versions(Bucket=mb, Prefix=key)
    return any(m["Key"] == key and m.get("IsLatest") for m in resp.get("DeleteMarkers", []))


def put_json(s3, bucket, key, doc: dict, etag: str) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(doc).encode(),
        ContentType="application/json",
        IfMatch=etag,
    )


def write_pods(s3, bucket, e: Edit) -> None:
    if e.keep:
        s3.put_object(
            Bucket=bucket,
            Key=e.pod_key,
            Body=serialize_pods(e.keep),
            ContentType="application/json",
            IfMatch=e.pod_etag,
        )
    else:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": e.pod_key}], "Quiet": True})


def pending_from_ledger(ledger: Audit) -> list[dict]:
    """Edits a previous run started and did not finish."""
    started: dict[str, dict] = {}
    for ev in ledger.events():
        if ev["event"] == "pending":
            started[ev["pod_key"]] = ev
        elif ev["event"] == "done":
            started.pop(ev["pod_key"], None)
    return list(started.values())


def resume(s3, mb: str, pending: list[dict], ledger: Audit) -> list[str]:
    """Finish half-done edits from their recorded before/after. Returns problems."""
    problems = []
    for ev in pending:
        if not (ev.get("orphan") or ev.get("recon")):
            problem = resume_cost(s3, mb, ev)
            if problem:
                problems.append(problem)
                continue
        removed = {p.get("pod") for p in ev["removed"]}
        pods_got = read_obj(s3, mb, ev["pod_key"])
        if pods_got is not None:
            pods = parse_pods(pods_got[0])
            if any(p.get("pod") in removed for p in pods):
                keep = [p for p in pods if p.get("pod") not in removed]
                write_pods(
                    s3, mb, Edit(ev["day"], ev["workflow"], ev["pod_key"], pods_got[1], keep, [])
                )
        ledger.write("done", pod_key=ev["pod_key"], resumed=True)
    return problems


def resume_cost(s3, mb: str, ev: dict) -> str | None:
    """Bring a pending edit's costs row to its recorded after-value, or say why not."""
    got = read_obj(s3, mb, ev["cost_key"])
    if got is None:
        return f"pending {ev['cost_key']} is gone"
    totals = json.loads(got[0]).get("total_cost_usd")
    if totals == ev["cost_before"]["total_cost_usd"]:
        put_json(s3, mb, ev["cost_key"], ev["cost_after"], got[1])
    elif totals != ev["cost_after"]["total_cost_usd"]:
        return f"pending {ev['cost_key']} changed since; check it by hand"
    return None


def apply(s3, mb: str, edits: list[Edit], ledger: Audit) -> None:
    # costs first, pods second; a crash between them is what resume() repairs.
    for e in edits:
        ledger.write(
            "pending",
            day=e.day,
            workflow=e.workflow,
            pod_key=e.pod_key,
            cost_key=e.cost_key,
            removed=e.removed,
            cost_before=e.cost_before,
            cost_after=e.cost_after,
            orphan=e.orphan,
            recon=e.recon,
        )
        if not (e.orphan or e.recon):
            put_json(s3, mb, e.cost_key, e.cost_after, e.cost_etag)
        write_pods(s3, mb, e)
        ledger.write("done", pod_key=e.pod_key, emptied=not e.keep)


def done_edits(ledger: Audit) -> list[dict]:
    """Every finished edit, oldest first: the ledger's pending records with a done."""
    done = {ev["pod_key"] for ev in ledger.events() if ev["event"] == "done"}
    return [ev for ev in ledger.events() if ev["event"] == "pending" and ev["pod_key"] in done]


def patch_costs_file(s3, mb: str, key: str, after_by_wf: dict[str, dict]) -> int:
    """Set each matching compacted costs row to its raw after-value. Returns rows changed.

    Sets rather than subtracts, so a re-run is a no-op.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    obj = s3.get_object(Bucket=mb, Key=key)
    table = pq.read_table(pa.BufferReader(obj["Body"].read()))
    if "workflow_name" not in table.column_names:
        raise UnattributableRows(f"{key} has no workflow_name column")
    rows = table.to_pylist()
    changed = 0
    for r in rows:
        after = after_by_wf.get(r.get("workflow_name"))
        if not after or r.get("subject") != after.get("subject"):
            continue
        before = dict(r)
        for f in COST_FIELDS:
            if f in r:
                integer = pa.types.is_integer(table.schema.field(f).type)
                r[f] = int(after[f]) if integer and float(after[f]).is_integer() else after[f]
        changed += r != before
    if changed:
        write_parquet_to_s3(
            s3, pa.Table.from_pylist(rows, schema=table.schema), mb, key, if_match=obj["ETag"]
        )
    return changed


def sync_compacted(s3, mb: str, ledger: Audit) -> list[str]:
    """Make compacted costs and pod_costs agree with every finished edit. Returns failures.

    In place, never a rebuild from raw: compact_prefix_dt replaces a partition
    with whatever raw holds, and on a dt whose raw is a subset of compacted that
    silently shrinks it (memory cost-reconstruction-2026-09-17). Idempotent, so it
    runs over the whole ledger every time and a crash before it costs nothing.
    """
    pods_by_day: dict[str, set] = defaultdict(set)
    after_by_day: dict[str, dict[str, dict]] = defaultdict(dict)
    for ev in done_edits(ledger):
        pods_by_day[ev["day"]].update((ev["workflow"], p.get("pod")) for p in ev["removed"])
        if not (ev.get("orphan") or ev.get("recon")):
            after_by_day[ev["day"]][ev["workflow"]] = ev["cost_after"]
    for ev in recon_done_edits(ledger):
        after_by_day[ev["day"]][ev["workflow"]] = ev["cost_after"]
    failed = []
    jobs = [("pod_costs", pods_by_day), ("costs", after_by_day)]
    for table, by_day in jobs:
        for key in compacted_files(s3, mb, table):
            m = DT_RE.search(key)
            values = by_day.get(m.group(1)) if m else None
            if not values:
                continue
            try:
                if table == "pod_costs":
                    drop_rows(s3, mb, key, ("workflow_name", "pod"), values, write=True)
                else:
                    patch_costs_file(s3, mb, key, values)
            except UnattributableRows as exc:
                print(f"    [note] {exc}")
            except Exception as exc:  # noqa: BLE001 — reported, and the run exits non-zero
                print(f"    [FAILED] {key}: {exc}")
                failed.append(key)
    return failed


# ---------------------------------------------------------------------------
# reconstructed costs rows
# ---------------------------------------------------------------------------


def price_reconstruction(s3, mb: str, region, days: list[str]) -> dict[str, dict[str, list]]:
    """reconstruct_costs' model for each day: {day: {workflow: [model_total, model_func]}}.

    The same calibration, snapshots and pricing reconstruct_costs.py wrote the rows
    with; the func figure prices only the func templates' pods.
    """
    from athena import CloudpipeMetrics
    from reconstruct_costs import (
        DEFAULT_CALIBRATION,
        build_rows,
        calibrate_from_days,
        load_snapshot_pods,
    )

    m = CloudpipeMetrics(bucket=mb, region=region)
    rates, node_type = calibrate_from_days(s3, m, mb, list(DEFAULT_CALIBRATION))
    runs_by_name: dict[str, list[dict]] = defaultdict(list)
    for r in m._run_sql("""
        SELECT workflow_name, subject, started_at, finished_at FROM cloudpipe_metrics.workflow_runs
        UNION SELECT workflow_name, subject, started_at, finished_at
        FROM cloudpipe_metrics.workflow_runs_compacted""").to_dict("records"):
        runs_by_name[r["workflow_name"]].append(r)

    out: dict[str, dict[str, list]] = {}
    for day_s in days:
        day = date.fromisoformat(day_s)
        pods = load_snapshot_pods(s3, mb, day)
        types = dict(node_type)
        for r in m._run_sql(
            "SELECT DISTINCT node, node_instance_type FROM cloudpipe_metrics.pod_costs "
            f"WHERE date = '{day_s}'"
        ).to_dict("records"):
            if r.get("node") and r.get("node_instance_type"):
                types[r["node"]] = r["node_instance_type"]
        for pod in pods:
            pod["instance_type"] = types.get(pod.get("host_node"))
        model: dict[str, list] = defaultdict(lambda: [0.0, 0.0])
        for row in build_rows(pods, runs_by_name, rates, day, {}, "cloudpipe_minproc")[0]:
            model[row.workflow_name][0] += row.total_cost_usd
        func = [p for p in pods if p.get("template") in FUNC_TEMPLATES]
        for row in build_rows(func, runs_by_name, rates, day, {}, "cloudpipe_minproc")[0]:
            model[row.workflow_name][1] += row.total_cost_usd
        out[day_s] = dict(model)
    return out


def frozen_models(s3, mb: str, region, days: list[str], ledger: Audit) -> dict[str, dict]:
    """The model for each day, computed once and then read back from the ledger.

    Frozen because the model calibrates on pod_costs, and this tool edits pod_costs:
    a re-run after a partial write would otherwise price the same rows differently.
    """
    have = {ev["day"]: ev["models"] for ev in ledger.events() if ev["event"] == "recon_model"}
    missing = sorted(set(days) - set(have))
    if missing:
        for day, models in price_reconstruction(s3, mb, region, missing).items():
            ledger.write("recon_model", day=day, models=models)
            have[day] = models
    return {d: have[d] for d in days}


@dataclass
class ReconEdit:
    day: str
    workflow: str
    cost_key: str
    cost_etag: str
    cost_before: dict
    fraction: float  # of the row's cost carried by the non-producer's func pods
    cost_after: dict = field(default_factory=dict)

    def removed_usd(self) -> float:
        return (self.cost_before.get("total_cost_usd") or 0.0) - self.cost_after["total_cost_usd"]


def scale(cost: dict, keep: float) -> dict:
    out = dict(cost)
    for f in COST_FIELDS:
        out[f] = round((cost.get(f) or 0.0) * keep, 6)
    return out


def recon_done_edits(ledger: Audit) -> list[dict]:
    done = {ev["cost_key"] for ev in ledger.events() if ev["event"] == "recon_done"}
    return [
        ev for ev in ledger.events() if ev["event"] == "recon_pending" and ev["cost_key"] in done
    ]


def recon_pending(ledger: Audit) -> list[dict]:
    """Reconstruction edits started and not finished."""
    started: dict[str, dict] = {}
    for ev in ledger.events():
        if ev["event"] == "recon_pending":
            started[ev["cost_key"]] = ev
        elif ev["event"] == "recon_done":
            started.pop(ev["cost_key"], None)
    return list(started.values())


def plan_recon(s3, args, ctx: Context, ledger: Audit) -> tuple[list[ReconEdit], list[str]]:
    """Every reconstructed costs row whose workflow is not its subject's func producer."""
    mb = args.metrics_bucket
    done = {ev["cost_key"] for ev in ledger.events() if ev["event"] == "recon_done"}
    keys = [
        k
        for k in list_keys(s3, mb, COST_PREFIX)
        if k.endswith("_cost_allocation.json") and k not in done
    ]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        bodies = list(pool.map(lambda k: read_obj(s3, mb, k), keys))
    candidates = []
    for key, got in zip(keys, bodies, strict=True):
        m = DT_RE.search(key)
        if got is None or not m:
            continue
        doc = json.loads(got[0])
        if doc.get("source") != "reconstructed":
            continue
        day = date.fromisoformat(m.group(1))
        wf = doc.get("workflow_name")
        probe = {"step": FUNC_POD_STEPS[0], "subject": doc.get("subject"), "workflow_name": wf}
        if day <= ctx.newest_safe and pod_goes(probe, day, ctx.windows, ctx.unattributed):
            candidates.append((key, got[1], doc, m.group(1), wf))
    if not candidates:
        return [], []

    models = frozen_models(s3, mb, args.region, sorted({c[3] for c in candidates}), ledger)
    edits, problems = [], []
    for key, etag, doc, day, wf in candidates:
        model_total, model_func = models[day].get(wf, [0.0, 0.0])
        stored = doc.get("total_cost_usd") or 0.0
        if abs(model_total - stored) > RECON_TOLERANCE_USD:
            problems.append(
                f"{key}: the model gives ${model_total:.4f}, the row holds ${stored:.4f}"
            )
            continue
        if model_func <= 0 or model_total <= 0:
            continue
        fraction = min(model_func / model_total, 1.0)
        e = ReconEdit(day, wf, key, etag, doc, fraction)
        e.cost_after = scale(doc, 1.0 - fraction)
        edits.append(e)
    return edits, problems


def apply_recon(s3, mb: str, edits: list[ReconEdit], ledger: Audit) -> None:
    for e in edits:
        ledger.write(
            "recon_pending",
            day=e.day,
            workflow=e.workflow,
            cost_key=e.cost_key,
            fraction=e.fraction,
            cost_before=e.cost_before,
            cost_after=e.cost_after,
        )
        put_json(s3, mb, e.cost_key, e.cost_after, e.cost_etag)
        ledger.write("recon_done", cost_key=e.cost_key)


def resume_recon(s3, mb: str, pending: list[dict], ledger: Audit) -> list[str]:
    problems = []
    for ev in pending:
        problem = resume_cost(s3, mb, ev)
        if problem:
            problems.append(problem)
        else:
            ledger.write("recon_done", cost_key=ev["cost_key"], resumed=True)
    return problems


def report(edits: list[Edit], held: list[Edit], recon: list[ReconEdit]) -> float:
    total = sum(e.removed_sums()["total_cost_usd"] for e in edits)
    pods = sum(len(e.removed) for e in edits)
    by_step: dict[str, float] = defaultdict(float)
    for e in edits:
        for p in e.removed:
            by_step[p.get("step")] += p.get("total_cost_usd") or 0.0
    print(f"  remove {pods} func pods from {len(edits)} workflow-days: ${total:.2f}")
    for step, usd in sorted(by_step.items()):
        print(f"    {step}: ${usd:.2f}")
    print(f"  emptied pod-costs objects: {sum(1 for e in edits if not e.keep)}")
    orphans = [e for e in edits if e.orphan]
    print(
        f"  costs row already deleted (pods removed, nothing to subtract): {len(orphans)} "
        f"workflow-days, ${sum(e.removed_sums()['total_cost_usd'] for e in orphans):.2f}"
    )
    print(
        f"  of which the costs row is a reconstructed gap-fill (adjusted by the model "
        f"instead): {sum(1 for e in edits if e.recon)} workflow-days"
    )
    print(
        f"  reconstructed costs rows re-priced: {len(recon)}, "
        f"${sum(e.removed_usd() for e in recon):.2f} removed from costs"
    )
    if held:
        newest = max(e.day for e in held)
        print(
            f"  [held] {len(held)} workflow-day(s) inside the scraper window "
            f"(newest {newest}); re-run after it settles"
        )
    return total


def finish_previous(s3, mb: str, ledger: Audit, write: bool) -> int | None:
    """Finish a previous run's half-done edits; an exit code if this run must stop."""
    pending, rpending = pending_from_ledger(ledger), recon_pending(ledger)
    if not (pending or rpending):
        return None
    print(f"{len(pending) + len(rpending)} edit(s) from a previous run are unfinished.")
    if not write:
        print("Re-run with --write to finish them before anything else.")
        return 1
    problems = resume(s3, mb, pending, ledger) + resume_recon(s3, mb, rpending, ledger)
    for msg in problems:
        print(f"STOP: {msg}", file=sys.stderr)
    return 2 if problems else None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    # Buckets are required, never defaulted — the same stance as prep_test_batch.py.
    p.add_argument("--data-bucket", required=True)
    p.add_argument("--metrics-bucket", required=True)
    p.add_argument("--region")
    p.add_argument("--workers", type=int, default=32)
    p.add_argument("--audit-dir", default=str(DEFAULT_AUDIT_DIR))
    p.add_argument("--expect-usd", type=float, help="Refuse to write if 5%% or more off this.")
    p.add_argument("--write", action="store_true", help="Apply (default: dry run).")
    return p


def main(argv: list[str] | None = None, s3=None, today: date | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.data_bucket.strip() or not args.metrics_bucket.strip():
        # "$CLOUDPIPE_BUCKET" outside `pixi shell -e ops` expands to nothing.
        raise SystemExit(
            "--data-bucket and --metrics-bucket must not be empty (run in pixi shell -e ops)"
        )
    s3 = s3 or boto3.client("s3", region_name=args.region)
    today = today or now_utc().date()
    audit_dir = Path(args.audit_dir)
    ledger = Audit(audit_dir / LEDGER_NAME)
    mb = args.metrics_bucket

    unfinished = finish_previous(s3, mb, ledger, args.write)
    if unfinished is not None:
        return unfinished

    print(f"{'Applying' if args.write else 'Planning (dry run)'}: func pod costs, today {today}")
    edits, held, problems, ctx = plan(s3, args, today)
    recon, recon_problems = plan_recon(s3, args, ctx, ledger)
    problems += recon_problems
    total = report(edits, held, recon)
    if args.expect_usd and abs(total - args.expect_usd) > EXPECT_TOLERANCE * args.expect_usd:
        problems.append(f"removing ${total:.2f}, expected ${args.expect_usd:.2f} (±5%)")
    if problems:
        for msg in problems:
            print(f"STOP: {msg}", file=sys.stderr)
        return 2
    audit = Audit(audit_dir / f"func_cost_rewrite_{now_utc():%Y%m%dT%H%M%SZ}.jsonl")
    if not args.write:
        for e in edits:
            audit.write("planned", pod_key=e.pod_key, removed=e.removed, cost_after=e.cost_after)
        for e in recon:
            audit.write(
                "planned_recon", cost_key=e.cost_key, fraction=e.fraction, cost_after=e.cost_after
            )
        print(f"Dry run. Audit: {audit.path}")
        return 0

    apply(s3, mb, edits, ledger)
    apply_recon(s3, mb, recon, ledger)
    failed = sync_compacted(s3, mb, ledger)
    print(f"Ledger: {ledger.path}")
    if failed:
        print(f"{len(failed)} compacted file(s) failed to sync; re-run.", file=sys.stderr)
        return 1
    return 1 if held else 0


if __name__ == "__main__":
    sys.exit(main())
