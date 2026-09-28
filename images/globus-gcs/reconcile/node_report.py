"""What node records this endpoint holds, written down where a stopped host can be read.

`globus-connect-server node list` is answered by the GCS Manager running on the
node itself, and the node is stopped between batches — so the host is the only
thing that can ask the question, and the only time it can ask is while it is up.
This produces the answer at registration time; `cloudpipe-gcs-boot` stores it in
SSM, and `globus doctor` reads it from a workstation hours later.

The question worth asking is what happens after an AMI bump. `ami` is ForceNew, so
a new pin replaces the host — and the obvious prediction, that the previous node's
record is stranded, is **wrong**. It reasons from EC2's identity when the identity
that counts is Globus's: `cloudpipe-gcs-boot` runs `node setup -d <deployment key
from SSM>`, and that key is what makes a node *this* node. A replacement boots with
the same key, so the endpoint sees one node coming back with a new address, not a
second one. Measured across two swaps on 2026-09-23: exactly one record each time.

That does not make this report pointless — it makes it the thing that established
the above, and a record CAN still strand (a node built by hand, or one registered
with a different key). It does mean a stale record here is a surprise worth
reading carefully rather than the routine cost of an image bump.

Facts only, deliberately. Which record is *stale* is a rule, and the rule lives in
`globus_admin.doctor` — correcting it there is a code change, correcting it here
would be an AMI rebuild.

Two shapes, and a caller must tell them apart:

    {"nodes": [ … ], "unavailable": null}   the records, as the endpoint reported them
    {"nodes": null, "unavailable": "…"}     nobody asked, or the ask failed, and why

`nodes: null` is never "there are none". An endpoint whose records could not be
read must not be reported as an endpoint with nothing stale on it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

from . import gcs

SCHEMA_VERSION = "1.0"

#: This deployment runs one node. The cap is not about that — it is about a
#: report that cannot be stored being no report at all: an SSM standard parameter
#: holds 4 KB.
MAX_NODES = 20
MAX_BYTES = 4096


def _now() -> str:
    # `timezone.utc`, not `datetime.UTC`: the same package has to import on the
    # 3.10 runtimes elsewhere in this repository.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def project(node: dict[str, Any]) -> dict[str, Any]:
    """One node record, reduced to the three fields the staleness rule needs.

    An absent `ip_addresses` becomes `None` rather than `[]`, because the two mean
    different things downstream: a record with no addresses reads as stale, while
    "this version of GCS did not tell us" must not — that would have `doctor`
    naming the live node as the one to delete.
    """
    addresses = node.get("ip_addresses")
    return {
        "id": str(node.get("id") or ""),
        "ip_addresses": [str(address) for address in addresses]
        if isinstance(addresses, list)
        else None,
        "status": str(node.get("status") or "unknown"),
    }


def build(
    *,
    instance_id: str,
    instance_ip: str,
    unavailable: str | None = None,
    runner=subprocess.run,
    now=_now,
) -> dict[str, Any]:
    """The report. Always a report — a failed listing is a recorded fact, not an error.

    `instance_id` is what makes the report falsifiable: a reader can tell whether
    the host that wrote it is still the deployment's instance, and treat a report
    from a replaced host as unobserved rather than as evidence about this one.
    """
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now(),
        "instance_id": instance_id,
        "instance_ip": instance_ip,
        "nodes": None,
        "unavailable": unavailable,
        "truncated": False,
    }
    if unavailable:
        return report

    try:
        records = gcs.unwrap(gcs.run(["node", "list"], runner=runner))
    except (gcs.GcsError, OSError) as exc:
        # OSError covers a missing `globus-connect-server` binary. Both are the
        # same thing here: the endpoint's records were not read, and the report
        # has to say so rather than be absent.
        report["unavailable"] = str(exc)
        return report

    report["nodes"] = [project(record) for record in records[:MAX_NODES]]
    report["truncated"] = len(records) > MAX_NODES
    while len(json.dumps(report)) > MAX_BYTES and report["nodes"]:
        report["nodes"] = report["nodes"][:-1]
        report["truncated"] = True
    return report


def main(argv: list[str] | None = None, *, runner=subprocess.run, out=None) -> int:
    parser = argparse.ArgumentParser(prog="reconcile.node_report", description=__doc__)
    parser.add_argument("--instance-id", required=True, help="this host's EC2 instance id")
    parser.add_argument(
        "--instance-ip", required=True, help="the public address this host registered with"
    )
    parser.add_argument(
        "--unavailable",
        help="record that the node records were not read, and why, instead of asking for them",
    )
    args = parser.parse_args(argv)

    report = build(
        instance_id=args.instance_id,
        instance_ip=args.instance_ip,
        unavailable=args.unavailable,
        runner=runner,
    )
    # Compact, because the whole thing has to fit in one SSM parameter.
    (out or sys.stdout).write(json.dumps(report, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
