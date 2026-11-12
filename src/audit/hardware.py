"""The hardware each audit subject's anatomy ran on, from its pod-cost records (add-reprocess-audit 5.3).

classify() (audit.compare) takes a hardware key per side, and the noise floor
reports its distributions by hardware pair. The key is the FastSurfer device and
the instance family of the subject's `template-build` pod: "cuda:g5", "cpu:c7i".

Why the template build: on the first fresh-vs-production subject (2026-11-09),
the two trees first differ in the template build's conformed input
(`long-inputs/<ses>/long_conform.nii.gz`: 78% of voxels, by at most 0.098, from a
byte-identical raw input, on the same FastSurfer build and device). That is float
rounding, and rounding can follow the CPU, which varies with the instance family
even within one GPU pool. Every later anatomy difference, and through the
registration every func one, grows from there. If the noise floor shows the
family does not matter, the key degrades harmlessly to the device.

Read-only. The pod-cost rows are the PodCost 1.2 records the cost scraper writes
to an audit metrics bucket. #1060 made node_instance_type trustworthy per pod
(matched by EC2 instance, not hostname).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ANATOMY_STEP = "template-build"
UNKNOWN = "unknown"
MIXED = "mixed"


def instance_family(instance_type: str | None) -> str:
    """ "g5.2xlarge" -> "g5"; "c8i-flex.4xlarge" -> "c8i-flex"; empty -> "unknown"."""
    t = (instance_type or "").strip()
    return t.split(".", 1)[0] if t else UNKNOWN


def hardware_key(device: str, family: str) -> str:
    return f"{device or UNKNOWN}:{family or UNKNOWN}"


def anatomy_hardware(rows: list[dict[str, Any]], prefix: str, exclude: str = "") -> dict[str, str]:
    """{subject: key} over one pair's workflows (name prefix, minus `exclude`).

    Every template-build pod the subject had in that pair counts: a retry or a
    resubmission that reran the template on other hardware gives "mixed", since
    which attempt's output was kept is not in the cost records. A subject whose
    template build has no instance type gives "unknown" for the family.
    """
    seen: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for r in rows:
        wf = str(r.get("workflow_name", ""))
        if not wf.startswith(prefix) or (exclude and wf.startswith(exclude)):
            continue
        if r.get("step") != ANATOMY_STEP or not r.get("subject"):
            continue
        device = "cuda" if float(r.get("gpu_hours") or 0) > 0 else "cpu"
        # Only a PROVEN instance's type (#1060): without node_instance_id the type
        # is the hostname-derived label, which a reused hostname makes wrong. On the
        # noise floor (2026-11-10), 21 GPU-hour rows carried c6i/t8i types that way.
        proven = bool(str(r.get("node_instance_id") or "").strip())
        family = instance_family(r.get("node_instance_type")) if proven else UNKNOWN
        seen[str(r["subject"])].add((device, family))
    out = {}
    for subject, pods in seen.items():
        if len(pods) == 1:
            out[subject] = hardware_key(*next(iter(pods)))
        else:
            devices = {d for d, _ in pods}
            out[subject] = hardware_key(devices.pop() if len(devices) == 1 else MIXED, MIXED)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--metrics-bucket", required=True)
    p.add_argument(
        "--prefix", required=True, help="e.g. cloudpipe-audit- or cloudpipe-audit-repeat-"
    )
    p.add_argument("--exclude-prefix", default="")
    p.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    p.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    p.add_argument("--region")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)

    import boto3

    from audit.report import load

    s3 = boto3.client("s3", region_name=args.region)
    pods, _ = load(s3, args.metrics_bucket, args.start, args.end)
    keys = anatomy_hardware(pods.to_dict("records"), args.prefix, args.exclude_prefix)
    with args.out.open("w") as fh:
        for subject in sorted(keys):
            fh.write(json.dumps({"subject": subject, "hardware": keys[subject]}) + "\n")
    counts: dict[str, int] = defaultdict(int)
    for k in keys.values():
        counts[k] += 1
    print(json.dumps({"subjects": len(keys), "by_hardware": dict(sorted(counts.items()))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
