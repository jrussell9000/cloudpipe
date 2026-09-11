#!/usr/bin/env python
"""Snapshot a batch's DERIVED numbers where no flush can reach them (#238).

`metrics/` is a scratchpad by design: `scripts/prep_test_batch.py` clears every
`metrics/` prefix before each test batch, raw and compacted alike, and `--flush-qc`
additionally takes the per-scan QC. Most of what a batch produces is redoable at the cost of
re-running it — but some of it is only readable in a window that closes, and that
has already bitten once: the 2026-08-05 pilot's settled cost was due 2026-08-09,
the raw records were flushed ahead of the 08-10 batch, and the number cannot be
re-derived because re-running produces a *different* batch's cost.

So this writes the aggregates, not the records, to
`s3://{metrics-bucket}/snapshots/batches/`, alongside the per-attempt telemetry
the CronWorkflow from #237 already lands under `snapshots/argo-nodes/`. Neither
prefix is under `metrics/`, which is the whole point.

Two-phase by necessity. Cost reconciliation FREEZES at scrape age 3, and a day+1
read overstates settled by a median ~51%, so a batch snapshot taken the morning
after is honest about outcomes and dishonest about money:

    # right after the batch (outcomes, durations, attempt causes)
    snapshot_batch_numbers.py --batch-id 2026-08-12-300 \
        --since 2026-08-12T03:28:04Z --subjects tools/cloudpipe_test_sample_300.csv

    # >= 3 days later, once the scrape has settled
    snapshot_batch_numbers.py --batch-id 2026-08-12-300 --amend-cost \
        --window-start 2026-08-12T03:28:04Z --window-end 2026-08-12T16:00:52Z

`--amend-cost` refuses to overwrite a good cost block with a worse one: it checks
`scrape_age_days >= 3` and that the record count did not shrink, because a blind
re-scrape once landed $0.222 where the real figure was ~$37.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import boto3

SNAPSHOT_PREFIX = "snapshots/batches"
ARGO_NODES_PREFIX = "snapshots/argo-nodes"

# Reconciliation freezes at age 3; below it a read is an overstatement, not a number.
SETTLED_AGE_DAYS = 3


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _classify(pod: dict) -> str:
    """Why this attempt was non-zero.

    An OOM kill and a spot reclaim are both SIGKILL/137 and only the pod message
    separates them; they need opposite fixes, so they must never be merged into a
    single "137" bucket.
    """
    message = pod.get("message") or ""
    exit_code = pod.get("exit_code")
    if "imminent node shutdown" in message:
        return "spot: imminent node shutdown"
    if "pod deleted" in message:
        return "spot: pod deleted"
    if "OOM" in message:
        return "OOMKilled"
    if exit_code == "65":
        return "QC gate (exit 65)"
    if exit_code == "75":
        return "EX_TEMPFAIL (exit 75)"
    return f"other (exit {exit_code})"


def _latest_argo_snapshot(s3, bucket: str) -> tuple[str, list[dict]]:
    """Newest per-attempt snapshot written by the #237 CronWorkflow."""
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=ARGO_NODES_PREFIX + "/dt="):
        keys.extend(o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".jsonl.gz"))
    if not keys:
        raise SystemExit(
            f"no snapshots under s3://{bucket}/{ARGO_NODES_PREFIX}/ — is the "
            "argo-nodes-snapshot CronWorkflow running? (#237)"
        )
    key = max(keys)
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return key, [json.loads(line) for line in gzip.decompress(body).decode().splitlines()]


def summarize(rows: list[dict], since: datetime, until: datetime | None = None) -> dict:
    """Outcome counts, per-step attempt causes, and duration distributions.

    `until` matters more than it looks: resubmitting a failed subject after the
    batch is routine, and those reruns are created after `since`, so a lower bound
    alone silently folds them into the batch they were meant to repair.
    """
    batch = [
        r
        for r in rows
        if (created := _parse_ts(r.get("created_at")) or since) >= since
        and (until is None or created <= until)
    ]

    phases: Counter[str] = Counter()
    losses: list[dict] = []
    wall: list[float] = []
    per_step: dict[str, dict] = defaultdict(
        lambda: {"attempts": 0, "nonzero": 0, "causes": Counter(), "durations_min": []}
    )

    for wf in batch:
        phases[wf.get("phase") or "(none)"] += 1
        if wf.get("phase") not in (None, "Succeeded"):
            losses.append(
                {
                    "workflow": wf.get("workflow"),
                    "subject": wf.get("subject"),
                    "phase": wf.get("phase"),
                    "message": wf.get("workflow_message"),
                }
            )
        started, finished = _parse_ts(wf.get("started_at")), _parse_ts(wf.get("finished_at"))
        if started and finished:
            wall.append((finished - started).total_seconds() / 60)

        for pod in wf.get("pods") or []:
            step = pod.get("template") or "(unknown)"
            entry = per_step[step]
            entry["attempts"] += 1
            phase = pod.get("phase")
            if phase == "Succeeded":
                pod_start, pod_end = (
                    _parse_ts(pod.get("started_at")),
                    _parse_ts(pod.get("finished_at")),
                )
                if pod_start and pod_end:
                    entry["durations_min"].append((pod_end - pod_start).total_seconds() / 60)
            elif phase not in ("Omitted", "Skipped"):
                entry["nonzero"] += 1
                entry["causes"][_classify(pod)] += 1

    steps = {}
    for name, entry in sorted(per_step.items()):
        durations = entry["durations_min"]
        steps[name] = {
            "attempts": entry["attempts"],
            "nonzero_attempts": entry["nonzero"],
            "nonzero_rate": round(entry["nonzero"] / entry["attempts"], 4)
            if entry["attempts"]
            else None,
            "causes": dict(entry["causes"]),
            # Medians are over SUCCESSFUL pods only — a step whose reclaims land
            # early looks faster than it is. Stated here so the number is not read
            # as a per-attempt cost.
            "median_duration_min": round(statistics.median(durations), 2) if durations else None,
            "p95_duration_min": (
                round(sorted(durations)[int(len(durations) * 0.95)], 2)
                if len(durations) >= 20
                else None
            ),
            "n_successful": len(durations),
        }

    return {
        "workflows": len(batch),
        "phases": dict(phases),
        "non_succeeded": losses,
        "wall_clock_min": {
            "median": round(statistics.median(wall), 2) if wall else None,
            "mean": round(statistics.fmean(wall), 2) if wall else None,
            "max": round(max(wall), 2) if wall else None,
        },
        "steps": steps,
    }


def cost_block(
    metrics_bucket: str, subjects: list[str], window_start: str, window_end: str
) -> dict:
    """Per-subject settled cost, with the scrape age that makes it trustworthy or not."""
    from metrics.athena import CloudpipeMetrics

    start, end = _parse_ts(window_start), _parse_ts(window_end)
    if start is None or end is None:
        raise SystemExit("--window-start and --window-end are required for a cost block")

    metrics = CloudpipeMetrics(bucket=metrics_bucket)
    # The scraper runs at 02:00 UTC and writes the PREVIOUS day, so a workflow that
    # finished late on day D is attributed D+1. Widen by a day or lose the tail.
    date_from = start.date().isoformat()
    date_to = (end.date()).isoformat()
    frame = metrics.subject_costs(subjects=subjects, date_from=date_from, date_to=date_to)

    if frame is None or len(frame) == 0:
        return {
            "available": False,
            "reason": "no cost rows in window",
            "window": [date_from, date_to],
        }

    totals = [float(v) for v in frame["total_cost_usd"]]
    age_days = (datetime.now(timezone.utc).date() - end.date()).days
    return {
        "available": True,
        "settled": age_days >= SETTLED_AGE_DAYS,
        "scrape_age_days": age_days,
        "window": [date_from, date_to],
        "n_subjects_with_cost": len(totals),
        "total_usd": round(sum(totals), 4),
        "mean_usd": round(statistics.fmean(totals), 4),
        "median_usd": round(statistics.median(totals), 4),
        "min_usd": round(min(totals), 4),
        "max_usd": round(max(totals), 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--batch-id", required=True, help="Stable name, e.g. 2026-08-12-300")
    parser.add_argument("--metrics-bucket", default="cloudpipe-metrics")
    parser.add_argument(
        "--subjects", type=Path, help="Batch subject CSV (recorded, and used for cost scoping)"
    )
    parser.add_argument("--since", help="RFC3339 UTC submit time — scopes which workflows count")
    parser.add_argument(
        "--until",
        help=(
            "RFC3339 UTC upper bound on workflow creation. Pass it whenever any subject "
            "was resubmitted after the batch, or those reruns are counted as batch members."
        ),
    )
    parser.add_argument("--window-start", help="Cost window start (RFC3339 UTC)")
    parser.add_argument("--window-end", help="Cost window end (RFC3339 UTC)")
    parser.add_argument("--notes", default="", help="Free text: what this batch was testing")
    parser.add_argument(
        "--amend-cost",
        action="store_true",
        help="Add/replace only the cost block of an existing snapshot, once the scrape has settled.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Allow an unsettled or shrinking cost amendment."
    )
    args = parser.parse_args()

    s3 = boto3.client("s3")
    key = f"{SNAPSHOT_PREFIX}/{args.batch_id}.json"

    subjects: list[str] = []
    if args.subjects:
        with args.subjects.open() as handle:
            subjects = [row["subject_id"] for row in csv.DictReader(handle)]

    if args.amend_cost:
        try:
            existing = json.loads(s3.get_object(Bucket=args.metrics_bucket, Key=key)["Body"].read())
        except s3.exceptions.NoSuchKey as exc:
            raise SystemExit(
                f"no snapshot at s3://{args.metrics_bucket}/{key} — run without --amend-cost first"
            ) from exc

        if not subjects:
            subjects = existing.get("subjects", [])
        block = cost_block(args.metrics_bucket, subjects, args.window_start, args.window_end)
        previous = existing.get("cost") or {}

        if not args.force:
            if not block.get("settled"):
                raise SystemExit(
                    f"cost is not settled (scrape_age_days={block.get('scrape_age_days')}, "
                    f"need >= {SETTLED_AGE_DAYS}). Reconciliation freezes at age 3 and a day+1 "
                    "read overstates by a median ~51%. Wait, or pass --force."
                )
            older = previous.get("n_subjects_with_cost")
            if older and block.get("n_subjects_with_cost", 0) < older:
                raise SystemExit(
                    f"refusing to overwrite {older} subjects of cost data with "
                    f"{block.get('n_subjects_with_cost')} — a partial re-scrape once landed "
                    "$0.222 where the real figure was ~$37. Pass --force if this is intended."
                )

        existing["cost"] = block
        existing["amended_at"] = datetime.now(timezone.utc).isoformat()
        s3.put_object(
            Bucket=args.metrics_bucket,
            Key=key,
            Body=json.dumps(existing, indent=2).encode(),
            ContentType="application/json",
        )
        print(f"amended cost in s3://{args.metrics_bucket}/{key}: {json.dumps(block, indent=2)}")
        return 0

    if not args.since:
        raise SystemExit("--since is required (the batch submit time in RFC3339 UTC)")

    source_key, rows = _latest_argo_snapshot(s3, args.metrics_bucket)
    summary = summarize(rows, _parse_ts(args.since), _parse_ts(args.until))

    snapshot = {
        "batch_id": args.batch_id,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "submitted_at": args.since,
        "created_until": args.until,
        "window": [args.window_start, args.window_end],
        "notes": args.notes,
        "subjects_file": str(args.subjects) if args.subjects else None,
        "n_subjects": len(subjects),
        "subjects": subjects,
        "source_snapshot": f"s3://{args.metrics_bucket}/{source_key}",
        "outcomes": summary,
        # Filled in later by --amend-cost, once the scrape reaches age 3.
        "cost": None,
    }

    if args.window_start and args.window_end and subjects:
        try:
            snapshot["cost"] = cost_block(
                args.metrics_bucket, subjects, args.window_start, args.window_end
            )
        except Exception as exc:  # noqa: BLE001 — a missing cost read must not lose the outcomes
            snapshot["cost"] = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    s3.put_object(
        Bucket=args.metrics_bucket,
        Key=key,
        Body=json.dumps(snapshot, indent=2).encode(),
        ContentType="application/json",
    )
    print(f"wrote s3://{args.metrics_bucket}/{key}")
    print(
        f"  {summary['workflows']} workflows {summary['phases']}, "
        f"median wall {summary['wall_clock_min']['median']} min"
    )
    cost = snapshot.get("cost") or {}
    if cost.get("available"):
        state = (
            "SETTLED" if cost.get("settled") else f"UNSETTLED (age {cost.get('scrape_age_days')}d)"
        )
        print(f"  cost {state}: total ${cost['total_usd']} mean ${cost['mean_usd']}")
    else:
        print(
            f"  cost not recorded ({cost.get('reason', 'not requested')}) — re-run with --amend-cost at age >= 3"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
