"""Whether each gateway's signing listener is serving, written where a stopped host can be read.

The listener binds `127.0.0.1` and nothing else (`retire-globus-s3-access-keys`
D4), which is what makes it safe to run unauthenticated: only a process on the
host can reach it. The same property means `globus doctor` cannot reach it either.
`doctor` runs on a workstation, its `tcp_probe` opens a socket from there, and
there is no address it could open one to. So the host answers the question and
records the answer; `doctor.check_s3_listener` applies the rule off-host, where
correcting the rule is a code change rather than an AMI rebuild.

WHY THIS MATTERS AS A SEPARATE CHECK. A listener that is not running does not fail
loudly. Globus signs the request with a dummy key, the listener is not there to
discard and re-sign it, and S3 answers an ordinary 403 — the same 403 an
unassumable role produces (measured in 3.1) and the same one a wrong prefix
produces. All three read as "the credential is broken". This report is what makes
the first of them say so in its own words.

WHO WRITES IT. The SSM document in `terraform/modules/globus/s3_listeners.tf`,
last, after it has enabled, swapped and started every declared listener. Not
`cloudpipe-gcs-boot`: that unit registers the Globus node and returns early when
GridFTP is already up, so it runs at times unrelated to whether a listener came
back. The document runs exactly when liveness becomes knowable, and it is where
the declared gateways and their ports are already known.

Two shapes, and a caller must tell them apart:

    {"listeners": [ … ], "unavailable": null}   what the host observed, per gateway
    {"listeners": null, "unavailable": "…"}     nobody asked, and why

`listeners: null` is never "there are none declared". A host that could not look
must not be reported as a host with no listeners to run.

WHAT THE RULE IS NOT TOLD. `serving` is a socket that accepted a connection, at
the moment the installer ran. It is not a promise about now: the host is stopped
between batches, and a listener that died afterwards leaves this report unchanged.
`restarts` is the field that carries what a single snapshot cannot — a unit systemd
has already restarted is a unit that has been failing — and `doctor` reads it as a
warning for exactly that reason.

Facts only, deliberately, and the same one the check needs is the one that is hard
to get right: **`LoadState` is not an absence test for a templated unit.** The unit
is `cloudpipe-s3-listener@<gateway>.service`, instantiated from a template baked
into the AMI, so `LoadState=loaded` the moment the template exists — for a gateway
nothing ever enabled as much as for one that is running. `UnitFileState` and
`config_present` are what separate them, which is why both are recorded. And
`systemctl is-active` is worse than either: a unit that does not exist reports
`inactive`/`dead`, indistinguishable from one somebody stopped (task 5.6).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

from .plan_report import host_instance_id, publish

SCHEMA_VERSION = "1.0"

#: This deployment declares two gateways. The cap is not about that — it is about a
#: report that cannot be stored being no report at all: an SSM standard parameter
#: holds 4 KB.
MAX_LISTENERS = 10
MAX_BYTES = 4096

#: The systemd template every listener is an instance of.
UNIT_TEMPLATE = "cloudpipe-s3-listener@{gateway}.service"

#: Where the SSM document writes each gateway's rendered config, and what the
#: unit's `ConditionPathExists` names. A missing file here is not a nuance: it is
#: the state every instance this ever provisioned was left in on 2026-09-23, when
#: the candidate was validated under a `.new` suffix and the swap never ran.
CONFIG_PATH = "/etc/cloudpipe/envoy/{gateway}.yaml"

#: Only what the rule needs, and `NRestarts` is the one worth naming twice: it is
#: the sole field here that says anything about time before this instant.
PROPERTIES = ("LoadState", "UnitFileState", "ActiveState", "SubState", "NRestarts")

#: Loopback, because that is the only address the listener binds. Short, because
#: the listener is a local process that either accepted or is not there.
PROBE_HOST = "127.0.0.1"
PROBE_TIMEOUT = 2.0


def _now() -> str:
    # `timezone.utc`, not `datetime.UTC`: the same package has to import on the
    # 3.10 runtimes elsewhere in this repository.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def probe(port: int, *, host: str = PROBE_HOST, timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    """Did a TCP connection to the listener's port succeed? Returns (serving, detail).

    The same shape as `globus_admin.doctor.tcp_probe` and deliberately a separate
    copy: this package is what the AMI carries and it imports nothing from
    `globus_admin`, which lives only in the repository checkout.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "connected"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def unit_state(gateway: str, *, runner=subprocess.run) -> tuple[dict[str, str], str | None]:
    """What systemd says about this gateway's unit, and why it could not be asked.

    `systemctl show` rather than `is-active` or `status`: it prints one
    `Property=value` line per property and exits 0 even for a unit that does not
    exist, so a parse failure means the tool was unavailable rather than the unit.
    """
    argv = ["systemctl", "show", UNIT_TEMPLATE.format(gateway=gateway)]
    argv += [f"--property={name}" for name in PROPERTIES]
    try:
        result = runner(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        return {}, str(exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return {}, detail[:200] or f"systemctl exited {result.returncode}"

    values: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        name, separator, value = line.partition("=")
        if separator and name in PROPERTIES:
            values[name] = value.strip()
    return values, None


def project(
    gateway: str,
    port: int,
    *,
    runner=subprocess.run,
    connect=probe,
    exists=os.path.exists,
) -> dict[str, Any]:
    """One listener, reduced to the fields the liveness rule needs.

    An unreadable field is `None`, never a default that reads as an observation: a
    `restarts` that silently became `0` because `systemctl` was missing would have
    `doctor` reporting a healthy unit it never looked at.
    """
    serving, detail = connect(port)
    values, error = unit_state(gateway, runner=runner)

    restarts = values.get("NRestarts")
    return {
        "gateway": gateway,
        "port": port,
        "serving": serving,
        "probe": detail,
        "config_present": bool(exists(CONFIG_PATH.format(gateway=gateway))),
        "load_state": values.get("LoadState") or None,
        "unit_file_state": values.get("UnitFileState") or None,
        "active_state": values.get("ActiveState") or None,
        "sub_state": values.get("SubState") or None,
        "restarts": int(restarts) if restarts and restarts.isdigit() else None,
        "error": error,
    }


def build(
    *,
    instance_id: str,
    gateways: list[tuple[str, int]],
    unavailable: str | None = None,
    runner=subprocess.run,
    connect=probe,
    exists=os.path.exists,
    now=_now,
) -> dict[str, Any]:
    """The report. Always a report — a listener that is down is a recorded fact.

    `instance_id` is what makes the report falsifiable: a reader can tell whether
    the host that wrote it is still the deployment's instance, and treat a report
    from a replaced host as unobserved rather than as evidence about this one.
    """
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now(),
        "instance_id": instance_id,
        "listeners": None,
        "unavailable": unavailable,
        "truncated": False,
    }
    if unavailable:
        return report

    report["listeners"] = [
        project(gateway, port, runner=runner, connect=connect, exists=exists)
        for gateway, port in gateways[:MAX_LISTENERS]
    ]
    report["truncated"] = len(gateways) > MAX_LISTENERS
    while len(json.dumps(report)) > MAX_BYTES and report["listeners"]:
        report["listeners"] = report["listeners"][:-1]
        report["truncated"] = True
    return report


def parse_gateway(spec: str) -> tuple[str, int]:
    """`<gateway>=<port>`, as the SSM document renders one per declared gateway.

    Rejected rather than defaulted, because a gateway whose port could not be read
    would be probed on the wrong one and reported as down.
    """
    name, separator, port = spec.rpartition("=")
    if not separator or not name.strip() or not port.strip().isdigit():
        raise argparse.ArgumentTypeError(f"expected `<gateway>=<port>`, got {spec!r}")
    return name.strip(), int(port)


def main(
    argv: list[str] | None = None,
    *,
    runner=subprocess.run,
    out=None,
    host_id=host_instance_id,
) -> int:
    parser = argparse.ArgumentParser(prog="reconcile.listener_report", description=__doc__)
    parser.add_argument(
        "--gateway",
        action="append",
        default=[],
        type=parse_gateway,
        metavar="NAME=PORT",
        help="a declared gateway and the loopback port its listener binds; repeatable",
    )
    parser.add_argument(
        "--unavailable",
        help="record that the listeners were not examined, and why, instead of examining them",
    )
    parser.add_argument(
        "--publish-to",
        metavar="SSM_PARAMETER",
        help="also store the report in this SSM parameter, for `globus doctor` to read",
    )
    parser.add_argument(
        "--region",
        help="AWS region for the --publish-to write (no default: the CLI's own would be a guess)",
    )
    args = parser.parse_args(argv)

    report = build(
        instance_id=host_id(),
        gateways=args.gateway,
        unavailable=args.unavailable,
        runner=runner,
    )
    stream = out or sys.stdout
    # Compact, because the whole thing has to fit in one SSM parameter.
    stream.write(json.dumps(report, separators=(",", ":")) + "\n")

    if not args.publish_to:
        return 0
    if not args.region:
        print("--publish-to needs --region; the report was not recorded", file=sys.stderr)
        return 2
    # A diagnostic that could not be stored must not look like a listener that is
    # down, so the failure is reported and the exit code stays 0. `doctor` then says
    # no report has been written, which is true and is its own check.
    failure = publish(report, param=args.publish_to, region=args.region, runner=runner)
    if failure:
        print(
            f"the listener report was not recorded in {args.publish_to}: {failure}", file=sys.stderr
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
