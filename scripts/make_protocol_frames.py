#!/usr/bin/env python3
"""Build `tools/fasttrack/protocol_frames.csv`: the measured frame count of each stratum.

The FastTrack conversion (OpenSpec `add-fasttrack-minproc`, task 1.5) checks every BOLD
run against its stratum's reference frame count: fewer frames means a truncated run,
more means a longer acquisition or a wrong non-steady-state (NSS) count. The reference
is not the published protocol. It is the most frequent frame count this deployment has
actually processed in that stratum, with its support, because mrQA found ABCD's acquired
parameters often depart from the published ones.

Strata are vendor x GE software major version x task. The minimally processed BOLD
sidecars carry no model or full software version, so finer strata wait for the
FastTrack DAIC `.json` files.

Inputs:
- every run's latest FuncQC record (`cloudpipe_metrics.func_preproc_compacted`, newest
  `completed_at` per subject x session x task x run), read through Athena, or a CSV
  export of the same columns (`--runs-csv`);
- `tools/nss_volumes.csv`, for each session's vendor, GE version and NSS count.

Output rows carry counts only and never name a session. The script also reports, as
counts, the GE sessions whose runs imply different NSS counts (see `GE_NSS_BY_LENGTH`).

    pixi run -e ops python scripts/make_protocol_frames.py --out tools/fasttrack/protocol_frames.csv
"""

from __future__ import annotations

import argparse
import collections
import csv
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HEADER = [
    "vendor",
    "software",
    "task",
    "runs",
    "mode_frames",
    "mode_runs",
    "mode_share",
    "nss_frames",
    "steady_state_frames",
    "distribution",
]
# GE run lengths that identify the NSS count on their own (rest, n-back). Each pair
# leaves the same 375 / 362 steady-state frames as Siemens and Philips.
GE_NSS_BY_LENGTH = {
    "task-rest": {380: 5, 391: 16},
    "task-nback": {367: 5, 378: 16},
}
QUERY = """
SELECT subject, session, task, run, n_frames
FROM (
  SELECT subject, session, task, run, n_frames,
         row_number() OVER (PARTITION BY subject, session, task, run
                            ORDER BY completed_at DESC) AS rn
  FROM cloudpipe_metrics.func_preproc_compacted
)
WHERE rn = 1
"""


def load_sessions(path: Path) -> dict[tuple[str, str], tuple[str, str, int]]:
    """(subject, session) -> (vendor, software, nss_frames) from nss_volumes.csv."""
    out = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            version = row["GEmajDVver"].strip()
            software = f"DV{int(float(version))}" if version else ""
            out[(row["subject_id"], row["session"])] = (
                row["scanner_make"],
                software,
                int(row["nss_frames"]),
            )
    return out


def read_runs_csv(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def query_runs(workgroup: str, region: str) -> list[dict]:
    import boto3

    athena = boto3.client("athena", region_name=region)
    qid = athena.start_query_execution(QueryString=QUERY, WorkGroup=workgroup)["QueryExecutionId"]
    while True:
        status = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if status["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)
    if status["State"] != "SUCCEEDED":
        sys.exit(f"Athena query {status['State']}: {status.get('StateChangeReason', '')}")
    rows, token = [], None
    columns = None
    while True:
        kwargs = {"QueryExecutionId": qid, "MaxResults": 1000}
        if token:
            kwargs["NextToken"] = token
        page = athena.get_query_results(**kwargs)
        data = page["ResultSet"]["Rows"]
        if columns is None:
            columns = [c["VarCharValue"] for c in data[0]["Data"]]
            data = data[1:]
        rows += [
            dict(zip(columns, (c.get("VarCharValue", "") for c in r["Data"]), strict=True))
            for r in data
        ]
        token = page.get("NextToken")
        if not token:
            return rows


def aggregate(runs: list[dict], sessions: dict) -> tuple[list[dict], dict]:
    """Per-stratum rows, plus counts-only diagnostics."""
    frames: dict[tuple, collections.Counter] = collections.defaultdict(collections.Counter)
    table_nss: dict[tuple, collections.Counter] = collections.defaultdict(collections.Counter)
    implied: dict[tuple, set] = collections.defaultdict(set)
    unmatched = 0
    for run in runs:
        key = (run["subject"], run["session"])
        if key not in sessions:
            unmatched += 1
            continue
        vendor, software, nss = sessions[key]
        n = int(run["n_frames"])
        stratum = (vendor, software, run["task"])
        frames[stratum][n] += 1
        table_nss[stratum][nss] += 1
        by_length = GE_NSS_BY_LENGTH.get(run["task"], {}) if vendor == "GE" else {}
        if n in by_length:
            implied[key].add(by_length[n])

    rows = []
    for stratum in sorted(frames):
        counts = frames[stratum]
        total = sum(counts.values())
        mode, mode_runs = counts.most_common(1)[0]
        nss = table_nss[stratum].most_common(1)[0][0]
        rows.append(
            {
                "vendor": stratum[0],
                "software": stratum[1],
                "task": stratum[2],
                "runs": total,
                "mode_frames": mode,
                "mode_runs": mode_runs,
                "mode_share": f"{mode_runs / total:.4f}",
                "nss_frames": nss,
                "steady_state_frames": mode - nss,
                "distribution": ";".join(f"{k}:{v}" for k, v in counts.most_common()),
            }
        )
    diagnostics = {
        "runs": len(runs),
        "unmatched_runs": unmatched,
        "ge_sessions_with_mixed_lengths": sum(1 for v in implied.values() if len(v) > 1),
    }
    return rows, diagnostics


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--nss-volumes", type=Path, default=REPO / "tools" / "nss_volumes.csv")
    ap.add_argument("--runs-csv", type=Path, help="CSV export instead of querying Athena")
    ap.add_argument("--workgroup", default="cloudpipe_metrics_workgroup")
    ap.add_argument(
        "--region", default=None, help="AWS region (default: AWS_REGION / your AWS config)"
    )
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    sessions = load_sessions(args.nss_volumes)
    runs = (
        read_runs_csv(args.runs_csv) if args.runs_csv else query_runs(args.workgroup, args.region)
    )
    rows, diagnostics = aggregate(runs, sessions)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADER, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    for k, v in diagnostics.items():
        print(f"{k}: {v}")
    print(f"wrote {len(rows)} strata to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
