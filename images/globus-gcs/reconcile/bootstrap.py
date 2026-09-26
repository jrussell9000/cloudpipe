"""Create this deployment's Globus endpoint, from the host that will serve it.

Run once per deployment (and again only for disaster recovery), by
`globus bootstrap-endpoint` through SSM. It exists on the instance rather than on
a workstation for the same reason the reconcile does: `endpoint setup` is a GCS
Manager operation, and the confidential-client credentials that authorize it live
in SSM where only this host may read them. Nothing here ever needs a browser.

Three things make this different from every other module in this package:

* **It is the only one that creates state Terraform cannot recreate.** The
  endpoint UUID and the deployment key are what make an AMI bump survivable, so
  both are written to SSM *before* anything else is attempted, and the endpoint
  id parameter is re-checked immediately before `endpoint setup` runs. A key that
  exists only in a temp directory on a host that is about to be replaced is the
  failure this ordering exists to prevent.
* **It refuses rather than converges.** The reconcile is idempotent by design; a
  second `endpoint setup` is not — it would create a *second* endpoint, leave the
  first orphaned and subscribed, and overwrite the only copy of the first's
  deployment key. So a parameter that already holds anything but the placeholder
  stops this cold, including a value that is not UUID-shaped: an unreadable value
  is not evidence that nothing is there.
* **It handles a secret.** The deployment key is written with
  `--cli-input-json file://…` from a file created under `umask 077`, never as an
  `aws` argument: every local process can read another's argv through `/proc`, and
  an operator connected with Session Manager is a local process.

Node setup is deliberately *not* done here, and neither is installing the key at
`/etc/globus-connect-server/deployment-key.json`. `cloudpipe-gcs-boot` already
owns both — it fetches the key from SSM under `umask 077`, runs `node setup`,
starts GridFTP and records the node report — so this stores the key in SSM and
lets the caller restart that unit. Writing the file here would be a second
implementation of a step that has already been got right once: 10.1 found the old
boot script creating that file under `umask 022` and chmod'ing it afterwards,
leaving the deployment key world-readable in between.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .gcs import GCS

SCHEMA_VERSION = "1.0"

#: What Terraform seeds the endpoint-id and deployment-key parameters with. Any
#: other value means someone or something has already been here.
PLACEHOLDER = "REPLACE_AFTER_GCS_SETUP"

#: What `endpoint setup` names the file it writes into the working directory.
KEY_FILE = "deployment-key.json"

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

#: Fields of `deployment-key.json` that have carried the endpoint UUID across GCS
#: versions, most specific first. Tried in order, then the setup output is
#: scraped — see `endpoint_id_from`.
#:
#: On GCS **5.4.98** the key holds exactly `client_id`, `node_key` and `secret`:
#: there is no `endpoint_id`, so the second field is the only one that ever
#: matches, and it really is the endpoint UUID — production's `client_id` is
#: byte-equal to what `/cloudpipe/globus/endpoint-id` records (task 10.6a).
#: `endpoint_id` stays first because a later version reintroducing it should win,
#: but do not read this tuple as "the tested path, then a fallback".
_KEY_ID_FIELDS = ("endpoint_id", "client_id")


class BootstrapError(RuntimeError):
    """Something went wrong that must stop the bootstrap, with a reason to print."""


def _now() -> str:
    # `timezone.utc` rather than `datetime.UTC`: this package also imports on the
    # 3.10 runtimes elsewhere in this repository.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aws(args: list[str], *, region: str, runner) -> subprocess.CompletedProcess:
    return runner(["aws", *args, "--region", region], capture_output=True, text=True)


def occupant(param: str, *, region: str, runner=subprocess.run) -> str | None:
    """What the endpoint-id parameter holds, or `None` if it is free.

    Free means exactly two things: the parameter does not exist, or it still holds
    the placeholder. Everything else — a UUID, a truncated UUID, a stray note
    someone pasted — is returned as an occupant, because this function's answer
    decides whether a second endpoint gets created. "I could not interpret it" has
    to read as "something is there".
    """
    result = _aws(
        ["ssm", "get-parameter", "--name", param, "--query", "Parameter.Value", "--output", "text"],
        region=region,
        runner=runner,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "ParameterNotFound" in stderr:
            return None
        # Not folded into "free": an AccessDenied here would otherwise read as an
        # empty parameter and let this create a duplicate endpoint.
        raise BootstrapError(f"could not read {param}: {stderr}")

    value = (result.stdout or "").strip()
    if not value or value == PLACEHOLDER:
        return None
    return value


def run_setup(
    *,
    display_name: str,
    owner: str,
    organization: str,
    contact_email: str,
    project_id: str = "",
    work_dir: Path,
    runner=subprocess.run,
) -> str:
    """`globus-connect-server endpoint setup`, run where its key file can be found.

    `endpoint setup` writes `deployment-key.json` into the current directory, so
    the caller's choice of directory is the only thing that decides where the
    secret lands. Not `-F json`: the setup subcommand is interactive-shaped and
    its machine-readable output is not something this can rely on across
    versions, which is why the key file is the primary source for the UUID.

    `--dont-set-advertised-owner` keeps the service client off the endpoint's
    public listing — the advertised owner should be a person an institution can
    contact, and that is set later, by hand, once there is a subscription.

    `--project-id` is passed only when one is given. That was an open question and
    is now settled the way this shape assumed: 5.4.98's own help says it is "only
    required if you are an admin on multiple Auth projects", so GCS infers it in
    the single-project case and an operator with several supplies one, neither
    needing a code change (task 10.6a).
    """
    argv = [
        GCS,
        "endpoint",
        "setup",
        display_name,
        "--owner",
        owner,
        "--organization",
        organization,
        "--contact-email",
        contact_email,
        "--agree-to-letsencrypt-tos",
        "--dont-set-advertised-owner",
    ]
    if project_id:
        argv += ["--project-id", project_id]
    result = runner(argv, capture_output=True, text=True, cwd=str(work_dir))
    if result.returncode != 0:
        raise BootstrapError(
            f"endpoint setup exited {result.returncode}: {(result.stderr or '').strip()}"
        )
    return result.stdout or ""


def endpoint_id_from(key: dict[str, Any], setup_output: str) -> tuple[str, str]:
    """The new endpoint's UUID, and which source it came from.

    The key file first, its own output second. Both are here because neither is a
    contract: the field GCS stores the endpoint UUID under has moved between
    versions, and the sentence `endpoint setup` prints is prose. Task 10.6 is what
    turns one of these into the known answer; until then, reporting *which* source
    answered is what makes that observation possible from a transcript.
    """
    for field in _KEY_ID_FIELDS:
        value = key.get(field)
        if isinstance(value, str) and _UUID.fullmatch(value.strip()):
            return value.strip(), f"deployment_key.{field}"

    match = _UUID.search(setup_output)
    if match:
        return match.group(0), "setup_output"

    raise BootstrapError(
        "endpoint setup reported no endpoint UUID: it is in neither "
        f"{' nor '.join(_KEY_ID_FIELDS)} of the deployment key nor its output"
    )


def read_key_text(path: Path) -> str:
    """The key file exactly as GCS wrote it.

    Text rather than a re-serialized dict, because this is what gets stored: a
    round trip through `json.dumps` would be a second opinion about a credential's
    bytes, and `cloudpipe-gcs-boot` hands whatever comes back straight to
    `node setup -d`.
    """
    try:
        return path.read_text()
    except FileNotFoundError as exc:
        raise BootstrapError(
            f"endpoint setup succeeded but wrote no {path.name}; without it this node "
            "can never register against the endpoint it just created"
        ) from exc
    except OSError as exc:
        raise BootstrapError(f"could not read {path}: {exc}") from exc


def parse_key(path: Path, text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"{path} is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BootstrapError(f"{path} is not a JSON object")
    return payload


def put_parameter(
    param: str,
    value: str,
    *,
    secure: bool,
    region: str,
    work_dir: Path,
    runner=subprocess.run,
) -> None:
    """Store one parameter, without the value ever appearing in an argument list.

    `--cli-input-json` from a file rather than `--value`, because `/proc/<pid>/
    cmdline` is world-readable and an operator on this host through Session
    Manager is a local process. Built in Python rather than in the calling shell
    for the ordinary reason: a deployment key is JSON, and quoting JSON inside
    JSON inside `sh` is where escaping bugs live.
    """
    document = {
        "Name": param,
        "Value": value,
        "Type": "SecureString" if secure else "String",
        # Terraform created both parameters as placeholders, so every write here is
        # an overwrite. Without this the first one fails as ParameterAlreadyExists.
        "Overwrite": True,
    }
    fd, name = tempfile.mkstemp(dir=str(work_dir), suffix=".json")
    path = Path(name)
    try:
        # mkstemp already creates the file 0600; written through the descriptor it
        # returned so there is no moment where the path exists with other modes.
        with os.fdopen(fd, "w") as handle:
            json.dump(document, handle)
        result = _aws(
            ["ssm", "put-parameter", "--cli-input-json", f"file://{path}"],
            region=region,
            runner=runner,
        )
    finally:
        path.unlink(missing_ok=True)

    if result.returncode != 0:
        raise BootstrapError(f"could not write {param}: {(result.stderr or '').strip()}")


def bootstrap(
    *,
    display_name: str,
    owner: str,
    organization: str,
    contact_email: str,
    endpoint_id_param: str,
    deployment_key_param: str,
    region: str,
    project_id: str = "",
    work_dir: Path,
    runner=subprocess.run,
    now=_now,
) -> dict[str, Any]:
    """Create the endpoint and record it. Returns the report; raises on refusal.

    The ordering is the design. The occupancy check is re-read here, on the
    instance, immediately before `endpoint setup` — the caller checks too, but the
    caller's check happened before an instance was started and a confirmation was
    answered, which is long enough for another operator to have run this. And both
    parameters are written before this returns, because the key exists only in
    `work_dir` until then: a failure between `endpoint setup` and the SSM write
    leaves an endpoint that can never register a node, which is the one state with
    no recovery short of deleting it.
    """
    occupied = occupant(endpoint_id_param, region=region, runner=runner)
    if occupied is not None:
        raise BootstrapError(
            f"{endpoint_id_param} already holds {occupied!r}, so this deployment already "
            "has an endpoint. Creating another would orphan it and overwrite the only "
            "copy of its deployment key."
        )

    setup_output = run_setup(
        display_name=display_name,
        owner=owner,
        organization=organization,
        contact_email=contact_email,
        project_id=project_id,
        work_dir=work_dir,
        runner=runner,
    )
    written_key = work_dir / KEY_FILE
    key_text = read_key_text(written_key)
    endpoint_id, id_source = endpoint_id_from(parse_key(written_key, key_text), setup_output)

    # Secret first. If the endpoint id lands and the key does not, the deployment
    # reads as bootstrapped to every other tool here while holding no way to
    # register a node; the reverse — a stored key and a placeholder id — is a
    # refusal on the next run, which is recoverable by hand.
    put_parameter(
        deployment_key_param,
        key_text,
        secure=True,
        region=region,
        work_dir=work_dir,
        runner=runner,
    )
    put_parameter(
        endpoint_id_param,
        endpoint_id,
        secure=False,
        region=region,
        work_dir=work_dir,
        runner=runner,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now(),
        "endpoint_id": endpoint_id,
        "endpoint_id_source": id_source,
        "display_name": display_name,
        "owner": owner,
        "endpoint_id_param": endpoint_id_param,
        "deployment_key_param": deployment_key_param,
        # Never the key itself. Its length is enough to tell "something was
        # stored" from "the placeholder is still there" in a transcript.
        "deployment_key_bytes": len(key_text),
    }


def main(argv: list[str] | None = None, *, runner=subprocess.run, out=None, err=None) -> int:
    parser = argparse.ArgumentParser(prog="reconcile.bootstrap", description=__doc__)
    parser.add_argument("--display-name", required=True, help="the endpoint's display name")
    parser.add_argument(
        "--owner", required=True, help="<service-client-id>@clients.auth.globus.org"
    )
    parser.add_argument("--organization", required=True)
    parser.add_argument("--contact-email", required=True)
    parser.add_argument("--endpoint-id-param", required=True)
    parser.add_argument("--deployment-key-param", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument(
        "--project-id",
        default="",
        help="Globus Auth project to create the endpoint in; omitted when empty",
    )
    args = parser.parse_args(argv)

    stdout = out or sys.stdout
    stderr = err or sys.stderr
    # A private directory, so the key is never written anywhere group- or
    # world-readable even for the moment before it is stored.
    with tempfile.TemporaryDirectory(prefix="cloudpipe-bootstrap-") as tmp:
        work_dir = Path(tmp)
        work_dir.chmod(0o700)
        try:
            report = bootstrap(
                display_name=args.display_name,
                owner=args.owner,
                organization=args.organization,
                contact_email=args.contact_email,
                endpoint_id_param=args.endpoint_id_param,
                deployment_key_param=args.deployment_key_param,
                region=args.region,
                project_id=args.project_id,
                work_dir=work_dir,
                runner=runner,
            )
        except BootstrapError as exc:
            # Reason on stderr, which the operator CLI always streams; stdout stays
            # reserved for the report so a caller parsing it never has to tell a
            # report from an apology.
            stderr.write(f"{exc}\n")
            # CHECK_FAILED, not ERROR: every path here is "this was asked and the
            # answer is no", which is what that code means in `globus_admin.exits`.
            return 3

    stdout.write(json.dumps(report, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
