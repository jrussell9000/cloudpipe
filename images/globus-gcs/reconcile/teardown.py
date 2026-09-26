"""Delete this deployment's Globus endpoint, from the host that serves it.

The inverse of `bootstrap.py`, and the only other module here that destroys
something Terraform cannot put back. It exists for task 10.6's throwaway
endpoint: a test deployment creates an endpoint, proves what it was built to
prove, and has to leave nothing behind — no node record on a subscription, no
endpoint still counting against one.

Three properties, and each is a refusal or an ordering rather than a feature:

* **The caller has to name the endpoint it believes it is deleting.**
  `--expect-endpoint-id` is compared against what the parameter holds, here, on
  the instance, immediately before anything is deleted. The workstation checks
  too, but its check happened before a confirmation was answered and an instance
  was booted. An endpoint deletion cannot be walked back, so the two reads have
  to agree.
* **Nodes go before the endpoint, and the parameters go last.** Globus requires
  the first (`endpoint cleanup` expects the nodes already gone). The second is
  this module's choice: resetting the parameters before the deletion succeeded
  would leave a live endpoint that nothing records — an orphan nobody can find
  by looking at this deployment — whereas the other order leaves parameters
  naming an endpoint that may be gone, which is visible and fixed by re-running.
* **A node that will not clean up does not stop the endpoint deletion.** The
  opposite of `bootstrap.py`, deliberately: teardown's goal is that nothing is
  left, so pushing on is the safe direction, and GCS provides
  `--lost-deployment-node-key` for exactly this. The outcome of each step is in
  the report instead, which is what 10.6 reads.

**The service client is consumed.** GCS states that the client id used to create
an endpoint may not be used to create another one. So this is not `terraform
destroy`, which can be followed by `terraform apply` — after it, a re-bootstrap
of the same deployment needs a *new* service client registered in the Globus
Developers Portal. That is why 10.6 registers a throwaway client of its own, and
why the workstation command refuses anything that looks like production.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bootstrap import PLACEHOLDER, BootstrapError, put_parameter
from .gcs import GCS

SCHEMA_VERSION = "1.0"

#: Where `cloudpipe-gcs-boot` installs the deployment key (its `KEY_PATH`).
KEY_PATH = Path("/etc/globus-connect-server/deployment-key.json")

#: What GCS calls the flag that lets `endpoint cleanup` proceed without a usable
#: node key. Some endpoint resources may survive it, which is why its use is
#: recorded in the report rather than being silent.
LOST_NODE_KEY = "--lost-deployment-node-key"


class TeardownError(RuntimeError):
    """Something went wrong that must stop the teardown, with a reason to print."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aws(args: list[str], *, region: str, runner) -> subprocess.CompletedProcess:
    return runner(["aws", *args, "--region", region], capture_output=True, text=True)


def recorded_endpoint(param: str, *, region: str, runner=subprocess.run) -> str:
    """What the endpoint-id parameter holds. Anything unusable is a refusal.

    Unlike `bootstrap.occupant`, which answers "is this free?", this answers
    "which endpoint am I about to delete?" — so the placeholder, an absent
    parameter and an unreadable one are all failures here, not one of them a
    green light.
    """
    result = _aws(
        ["ssm", "get-parameter", "--name", param, "--query", "Parameter.Value", "--output", "text"],
        region=region,
        runner=runner,
    )
    if result.returncode != 0:
        raise TeardownError(f"could not read {param}: {(result.stderr or '').strip()}")
    value = (result.stdout or "").strip()
    if not value or value == PLACEHOLDER:
        raise TeardownError(
            f"{param} holds no endpoint id, so there is nothing recorded here to delete"
        )
    return value


def _gcs(args: list[str], *, runner) -> tuple[int, str]:
    result = runner([GCS, *args], capture_output=True, text=True)
    detail = ((result.stderr or "") + (result.stdout or "")).strip()
    return result.returncode, detail


def clean_node(*, runner=subprocess.run) -> dict[str, Any]:
    """Remove this host from the endpoint. Reports failure instead of raising.

    A node that was never registered, or whose key is gone, still has to be
    followed by the endpoint deletion — stopping here would leak the endpoint,
    which is the thing this command exists to prevent.
    """
    code, detail = _gcs(["node", "cleanup"], runner=runner)
    return {"ok": code == 0, "exit_status": code, "detail": detail[:2000]}


def clean_endpoint(*, key_path: Path = KEY_PATH, runner=subprocess.run) -> dict[str, Any]:
    """Delete the endpoint configuration from Globus. Raises when it does not.

    `--agree-to-delete-endpoint` because there is no terminal behind an SSM
    invocation: without it the command prompts and hangs until the document's
    timeout, which looks like a failure of the deletion rather than of the
    prompt. The deployment key is passed explicitly when it is on disk — GCS
    would find it there anyway, but naming it makes "the key is missing" a
    condition this module decided about rather than one GCS discovered.

    Whether the key is present is read here rather than passed in, so the one
    argument callers give (`key_path`) decides both the check and the flag. A
    boolean parameter alongside the path would let the two disagree.
    """
    lost_node_key = not key_path.is_file()
    argv = ["endpoint", "cleanup", "--agree-to-delete-endpoint"]
    if lost_node_key:
        argv.append(LOST_NODE_KEY)
    else:
        argv += ["--deployment-key", str(key_path)]

    code, detail = _gcs(argv, runner=runner)
    if code != 0:
        raise TeardownError(f"endpoint cleanup exited {code}: {detail}")
    return {"ok": True, "lost_node_key": lost_node_key, "detail": detail[:2000]}


def teardown(
    *,
    expect_endpoint_id: str,
    endpoint_id_param: str,
    deployment_key_param: str,
    region: str,
    work_dir: Path,
    key_path: Path = KEY_PATH,
    runner=subprocess.run,
    now=_now,
) -> dict[str, Any]:
    """Delete the endpoint and clear what recorded it. Returns the report."""
    recorded = recorded_endpoint(endpoint_id_param, region=region, runner=runner)
    if recorded != expect_endpoint_id:
        raise TeardownError(
            f"{endpoint_id_param} holds {recorded!r}, not the {expect_endpoint_id!r} this run "
            "was told to delete. Nothing was deleted: the two disagree about which endpoint "
            "this deployment owns, and an endpoint deletion cannot be undone."
        )

    node = clean_node(runner=runner)
    endpoint = clean_endpoint(key_path=key_path, runner=runner)

    # Last, and only after the deletion succeeded. Reset to the placeholder rather
    # than deleted: Terraform owns these parameters and would recreate them on the
    # next apply, and the placeholder is the value every other tool here already
    # reads as "no endpoint yet".
    #
    # The endpoint id goes first, which is `bootstrap` written backwards rather
    # than copied. The id is the marker — it is what every other tool reads as
    # "this deployment has an endpoint" — so it is the last thing set when
    # creating and the first thing cleared when destroying. That leaves the only
    # reachable half-done state as a placeholder id beside a stale key, which the
    # next bootstrap overwrites unaided. The other order leaves an id naming a
    # deleted endpoint beside a placeholder key: reads as bootstrapped, cannot
    # register a node, refuses a re-bootstrap, and needs a human to unpick.
    for param, secure in ((endpoint_id_param, False), (deployment_key_param, True)):
        try:
            put_parameter(
                param,
                PLACEHOLDER,
                secure=secure,
                region=region,
                work_dir=work_dir,
                runner=runner,
            )
        except BootstrapError as exc:
            # The endpoint is already gone, so this cannot be retried into a
            # different outcome by refusing — say which parameter still names a
            # deleted endpoint and let the operator clear it.
            raise TeardownError(
                f"the endpoint was deleted but {param} could not be cleared: {exc}"
            ) from exc

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now(),
        "endpoint_id": recorded,
        "endpoint_id_param": endpoint_id_param,
        "deployment_key_param": deployment_key_param,
        "node_cleanup": node,
        "endpoint_cleanup": endpoint,
        # In the order they were cleared, which is the order that matters if only
        # some of them were.
        "parameters_reset": [endpoint_id_param, deployment_key_param],
    }


def main(argv: list[str] | None = None, *, runner=subprocess.run, out=None, err=None) -> int:
    parser = argparse.ArgumentParser(prog="reconcile.teardown", description=__doc__)
    parser.add_argument(
        "--expect-endpoint-id",
        required=True,
        help="the endpoint the caller believes this deployment owns; a mismatch refuses",
    )
    parser.add_argument("--endpoint-id-param", required=True)
    parser.add_argument("--deployment-key-param", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args(argv)

    stdout = out or sys.stdout
    stderr = err or sys.stderr
    # A private directory only because `put_parameter` writes its `--cli-input-json`
    # file there. Nothing secret passes through it — the value written is the
    # placeholder — but the same function is used, so it gets the same directory.
    with tempfile.TemporaryDirectory(prefix="cloudpipe-teardown-") as tmp:
        work_dir = Path(tmp)
        work_dir.chmod(0o700)
        try:
            report = teardown(
                expect_endpoint_id=args.expect_endpoint_id,
                endpoint_id_param=args.endpoint_id_param,
                deployment_key_param=args.deployment_key_param,
                region=args.region,
                work_dir=work_dir,
                runner=runner,
            )
        except (TeardownError, BootstrapError) as exc:
            stderr.write(f"{exc}\n")
            # CHECK_FAILED, as in `bootstrap.py`: every path here is "this was
            # asked and the answer is no", and the operator CLI passes the remote
            # status straight through.
            return 3

    stdout.write(json.dumps(report, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
