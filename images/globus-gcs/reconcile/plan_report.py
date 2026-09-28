"""The reconcile's plan, written down where a stopped host can still be read.

`globus doctor` runs on a workstation and cannot answer "does the endpoint still
match the configuration it was given". Answering that needs
`globus-connect-server` listings, and those are served by the GCS Manager running
on the node — which is stopped between batches. So the same split task 10.5 used
for node records applies here: the host records facts, `doctor.check_drift`
applies the rule off-host, where correcting the rule is a code change rather than
an AMI rebuild.

Before this, the plan existed only as SSM command output: addressed by command id,
aged out by SSM's retention, and present only for runs that someone happened to
make. That is why check 7 reported `skipped` rather than measuring anything.

Three fields carry the weight, and a reader must not skip any of them.

    {"actions": [...], "problems": [...], "unavailable": null}   a plan was built
    {"actions": null,  "problems": null,  "unavailable": "..."}  nobody could ask

`actions: null` is never "no drift". A comparison that could not be made must not
read as an endpoint that matches its configuration.

`gateway` is the scope, and it is the trap peculiar to this report. `globus
configure` always runs with `--only <that environment's gateway>`, so most
published plans examined ONE gateway and say nothing whatever about the other.
Only the Terraform association runs unscoped, and it records `gateway: null`.

`counts` is measured before trimming, which is the other difference from
`node_report`. There, *which* record is stale is the finding, so a record dropped
to fit is a record not examined. Here the finding is how much differs, so the
counts are computed first and never change: trimming costs a reader the identity
of some objects and never the fact that they exist.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1.0"

#: An SSM standard parameter holds 4 KB, and a report that cannot be stored is no
#: report at all. Measured against the NON-compact dump, as `node_report` does, so
#: the estimate always exceeds what is actually written.
MAX_BYTES = 4096

#: A per-list cap applied before the byte budget, so one enormous list cannot
#: crowd the other two out entirely.
MAX_ITEMS = 20

#: What to shed first, least load-bearing first. A note is something observed and
#: deliberately not acted on; an action is recoverable by reading the next plan; a
#: problem is the one kind that needs a person, so it is dropped last.
SHED_ORDER = ("notes", "actions", "problems")

IMDS = "http://169.254.169.254/latest"
IMDS_TIMEOUT = 2.0


def _now() -> str:
    # `timezone.utc`, not `datetime.UTC`: the same package has to import on the
    # 3.10 runtimes elsewhere in this repository.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def host_instance_id(*, opener=urllib.request.urlopen, timeout: float = IMDS_TIMEOUT) -> str:
    """This host's EC2 instance id, or `""` when the metadata service did not answer.

    IMDSv2, the same two-step token exchange `cloudpipe-gcs-boot`'s `metadata`
    helper makes. Read here rather than passed in by the SSM document because the
    document is a POSIX-sh string rendered by Terraform, where a token exchange is
    three more untestable lines; here it has a seam and a test.

    `""` is a usable answer rather than an error, and deliberately so: a report
    naming no writer fails `doctor`'s freshness guard and is ignored, which is the
    right outcome and strictly better than leaving an older, believable-looking
    plan in place. `urllib.error.URLError` and `HTTPError` are both `OSError`.
    """
    try:
        token_request = urllib.request.Request(
            f"{IMDS}/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        with opener(token_request, timeout=timeout) as response:
            token = response.read().decode().strip()
        request = urllib.request.Request(
            f"{IMDS}/meta-data/instance-id",
            headers={"X-aws-ec2-metadata-token": token},
        )
        with opener(request, timeout=timeout) as response:
            return response.read().decode().strip()
    except (OSError, ValueError):
        return ""


def build(
    plan: Any | None,
    *,
    mode: str,
    gateway: str | None,
    instance_id: str,
    unavailable: str | None = None,
    now=_now,
) -> dict[str, Any]:
    """The report. Always a report — a comparison that failed is a recorded fact.

    `instance_id` is what makes it falsifiable: a reader can tell whether the host
    that produced it is still the deployment's instance. That matters more here
    than for node records, because an instance is replaced by pinning a new AMI and
    the AMI is where the planner's own rules live — so a plan from a replaced host
    compared an older configuration using older rules.
    """
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now(),
        "instance_id": instance_id,
        "mode": mode,
        "gateway": gateway,
        "counts": None,
        "actions": None,
        "problems": None,
        "notes": None,
        "unavailable": unavailable,
        "truncated": False,
    }
    if unavailable or plan is None:
        report["unavailable"] = unavailable or "no plan was produced"
        return report

    body = plan.as_dict()
    report["counts"] = {key: len(body[key]) for key in SHED_ORDER}
    for key in SHED_ORDER:
        report[key] = body[key]
    _trim(report)
    return report


def _trim(report: dict[str, Any]) -> None:
    """Shrink the lists in place until the report fits one SSM parameter.

    Safe only because `counts` was measured first: the severity of the drift rides
    on the numbers, which never move, so a reader of a truncated report still
    learns how much differs and loses only which objects.
    """
    for key in SHED_ORDER:
        if len(report[key]) > MAX_ITEMS:
            report[key] = report[key][:MAX_ITEMS]
            report["truncated"] = True
    for key in SHED_ORDER:
        if len(json.dumps(report)) <= MAX_BYTES:
            return
        while len(json.dumps(report)) > MAX_BYTES and report[key]:
            report[key] = report[key][:-1]
            report["truncated"] = True


def publish(
    report: dict[str, Any], *, param: str, region: str, runner=subprocess.run
) -> str | None:
    """Store the report in SSM. Returns `None` on success, or why it failed.

    Never raises and never becomes an exit code: publishing is a diagnostic, and a
    diagnostic that could not be stored must not make a good plan or a successful
    apply look like a failure. It does not print either — in `--json` mode stdout is
    the plan, and `globus configure` parses the whole of stdout as JSON, so one
    stray line there costs the operator the plan they asked for.

    `aws`, not boto3: nothing else in this package has an AWS client, and the CLI
    is already how the boot unit and the reconcile's own SSM document reach SSM.
    """
    argv = [
        "aws",
        "ssm",
        "put-parameter",
        "--name",
        param,
        "--type",
        "String",
        "--overwrite",
        "--value",
        json.dumps(report, separators=(",", ":")),
        "--region",
        region,
    ]
    try:
        result = runner(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        return str(exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return detail[:200] or f"aws exited {result.returncode}"
    return None
