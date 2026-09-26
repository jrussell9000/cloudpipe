"""`pixi run globus <command>` — the one entry point operators use.

Global options exist on every command, not per-command, so a caller (a wizard, a
CI job, an agent) can drive any of them the same way:

    --env {production,staging}   which gateway/collection/secret to act on
    --json                       machine-readable envelope on stdout
    --non-interactive            never prompt; a needed confirmation exits 2
    --yes                        supply that confirmation up front
    --answers PATH               the answers document

Commands implemented so far: `doctor`, `login`, `status`, `tasks`, `configure`,
`init`, `setup-status`, `register-service-client`, `grant-project-admin`,
`delete-service-client`, `bootstrap-endpoint`, `cleanup-endpoint`.
`rotate-s3-key` lands in a later phase
of the change; it is deliberately absent rather than stubbed, so `--help` never
advertises something that does nothing.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import config as config_module
from . import environments, prereqs
from .aws import AwsGateway
from .exits import CliError, ExitCode
from .output import Emitter
from .shell import Runner, subprocess_runner

COMMANDS: dict[str, Callable[[Context], ExitCode]] = {}


@dataclass
class Context:
    """Everything a command needs, resolved once by `main`."""

    args: argparse.Namespace
    config: config_module.DeploymentConfig
    env: environments.Environment
    emitter: Emitter
    runner: Runner
    session_factory: Callable[[], Any]
    _identity: prereqs.AwsIdentity | None = None
    _aws: AwsGateway | None = None

    @property
    def non_interactive(self) -> bool:
        return bool(self.args.non_interactive)

    @property
    def assume_yes(self) -> bool:
        return bool(self.args.yes)

    def require_prereqs(self, *, mutating: bool = False) -> prereqs.AwsIdentity:
        if self._identity is None:
            self._identity = prereqs.require(
                self.config,
                mutating=mutating,
                runner=self.runner,
                session_factory=self.session_factory,
            )
        elif mutating:
            self.config.require_account_id()
        return self._identity

    @property
    def aws(self) -> AwsGateway:
        if self._aws is None:
            self._aws = AwsGateway(self.session_factory())
        return self._aws


def _add_global_options(parser: argparse.ArgumentParser, *, with_defaults: bool) -> None:
    """The flags that apply to every command, whichever side of it they are typed.

    Added twice: once on the top-level parser and once, through a parent, on every
    subparser. argparse binds an option to the parser that declares it, and a
    subcommand's own options come after the subcommand name — so a global
    declared only at the top level makes `globus login --env staging` a usage
    error ("unrecognized arguments: --env staging"). That is the order this repo
    writes everywhere: the published contract specifies `globus doctor --json`,
    `doctor` hands out `pixi run globus configure --env staging` as a remedy, and
    it is the order people type. The parser was the thing out of line.

    `with_defaults=False` is for the per-subcommand copies, and it matters: a real
    default there would overwrite a value given BEFORE the subcommand, so
    `globus --env staging login` would quietly act on production. SUPPRESS leaves
    the attribute alone unless the flag is actually given. Given on both sides,
    the one after the subcommand wins, which is the usual argparse last-wins rule.
    """

    def _default(value: object) -> object:
        return value if with_defaults else argparse.SUPPRESS

    parser.add_argument(
        "--env",
        default=_default(environments.PRODUCTION),
        choices=list(environments.ENV_NAMES),
        help="Which Globus environment to act on (default: production).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=_default(False),
        help="Emit a JSON envelope on stdout.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        default=_default(False),
        help="Never prompt. A required confirmation exits 2 instead.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        default=_default(False),
        help="Answer yes to confirmations (required with --non-interactive for mutations).",
    )
    parser.add_argument(
        "--answers",
        metavar="PATH",
        default=_default(None),
        help="Answers document (default: $GLOBUS_ANSWERS or ./globus-answers.yaml).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="globus",
        description="Operate the CloudPipe Globus ingress.",
        epilog=(
            "Credentials come from your environment: AWS CLI v2 with an SSO profile "
            "(`aws sso login`). This tool never takes AWS keys."
        ),
    )
    _add_global_options(parser, with_defaults=True)

    # Every subcommand inherits the globals from here. `add_help=False` so the
    # parent does not fight each subparser for `-h`; the side effect is that a
    # subcommand's `--help` lists them, which is where someone looks first.
    common = argparse.ArgumentParser(add_help=False)
    _add_global_options(common, with_defaults=False)

    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor",
        help="Check every dependency in order and say what to do about each problem.",
        parents=[common],
    )
    doctor.add_argument(
        "--start-instance",
        action="store_true",
        help="Start the GCS instance if it is stopped, so the checks that need it can run.",
    )
    doctor.set_defaults(handler="doctor")

    login = subparsers.add_parser(
        "login",
        help="Establish a Globus session and store the credential the pipeline uses.",
        parents=[common],
    )
    login.add_argument(
        "--client-id",
        metavar="UUID",
        help="Globus native app client id (default: the answers document, then the stored one).",
    )
    login.add_argument(
        "--no-browser",
        action="store_true",
        help="Print a URL to open elsewhere instead of starting a local browser flow.",
    )
    login.set_defaults(handler="login")

    status = subparsers.add_parser(
        "status",
        help="Where things stand: instance, session expiry, collections, active transfers.",
        parents=[common],
    )
    status.set_defaults(handler="status")

    setup_status = subparsers.add_parser(
        "setup-status",
        help="The ordered setup steps: what is done, what is next, and what is waiting on whom.",
        parents=[common],
    )
    setup_status.set_defaults(handler="setup-status")

    init = subparsers.add_parser(
        "init",
        help="Render the Terraform inputs and the configuration document from the answers.",
        parents=[common],
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite rendered files that already exist.",
    )
    init.add_argument(
        "--output-dir",
        metavar="PATH",
        help="Where to write the rendered files (default: ./terraform).",
    )
    init.set_defaults(handler="init")

    configure = subparsers.add_parser(
        "configure",
        help="Reconcile the Globus endpoint with the declared configuration (plan, then apply).",
        parents=[common],
    )
    configure.add_argument(
        "--plan-only",
        action="store_true",
        help="Show what would change and stop. Never applies anything.",
    )
    configure.add_argument(
        "--keep-running",
        action="store_true",
        help="Do not offer to stop the instance afterwards, even if this command started it.",
    )
    configure.set_defaults(handler="configure")

    # Once per deployment, and its own opposite. Neither touches the instance:
    # both are Globus Auth API calls plus two SSM writes, so there is nothing to
    # start and no `--keep-running`. They run *before* `bootstrap-endpoint`, which
    # authenticates as what they register.
    register_client = subparsers.add_parser(
        "register-service-client",
        help="Register the confidential client that will own this deployment's endpoint.",
        parents=[common],
    )
    register_client.add_argument(
        "--name",
        metavar="NAME",
        help="Display name in the Globus console (default: <deployment name>-gcs).",
    )
    register_client.add_argument(
        "--project-id",
        metavar="UUID",
        help="Globus Auth project to create the client in. Required when you administer more "
        "than one, because a client cannot be moved between projects afterwards.",
    )
    register_client.add_argument(
        "--create-project",
        metavar="NAME",
        help="Create a Globus Auth project with this display name and put the client in it.",
    )
    register_client.add_argument(
        "--no-browser",
        action="store_true",
        help="Print a URL to open elsewhere instead of starting a local browser flow.",
    )
    register_client.set_defaults(handler="register-service-client")

    # The repair half of `register-service-client`'s last step. Separate because the
    # occupancy guard makes registration un-rerunnable by design, so a grant that
    # failed after the credentials were stored has nothing to retry it.
    grant_admin = subparsers.add_parser(
        "grant-project-admin",
        help="Make this deployment's service client an administrator of its Auth project.",
        parents=[common],
    )
    grant_admin.add_argument(
        "--project-id",
        metavar="UUID",
        required=True,
        # Required, and not derived: the project is not recorded anywhere in the
        # deployment, and the operator must administer it for the write to be
        # possible at all.
        help="The Globus Auth project the client belongs to. You must administer it.",
    )
    grant_admin.add_argument(
        "--no-browser",
        action="store_true",
        help="Print a URL to open elsewhere instead of starting a local browser flow.",
    )
    grant_admin.set_defaults(handler="grant-project-admin")

    delete_client = subparsers.add_parser(
        "delete-service-client",
        help="Delete this deployment's service client. Permanent; needs the client id.",
        parents=[common],
    )
    delete_client.add_argument(
        "--client-id",
        metavar="UUID",
        required=True,
        # Required, and the one guard that does not read its answer out of the
        # answers document — the same reasoning as `cleanup-endpoint --endpoint-id`.
        # Note this names the *service client*, not the native app `login` uses.
        help="The service client to delete. Must match what this deployment records.",
    )
    delete_client.add_argument(
        "--no-browser",
        action="store_true",
        help="Print a URL to open elsewhere instead of starting a local browser flow.",
    )
    delete_client.set_defaults(handler="delete-service-client")

    # Once per deployment, and its own opposite. Both take `--keep-running` for the
    # same reason `configure` does: an operator with more to do on the instance
    # should not have to wait for it to boot twice.
    bootstrap = subparsers.add_parser(
        "bootstrap-endpoint",
        help="Create this deployment's Globus endpoint (once, under the service client).",
        parents=[common],
    )
    bootstrap.add_argument(
        "--project-id",
        metavar="UUID",
        help=(
            "Globus Auth project to create the endpoint in. Omit unless the service client "
            "belongs to more than one."
        ),
    )
    bootstrap.add_argument(
        "--keep-running",
        action="store_true",
        help="Do not offer to stop the instance afterwards, even if this command started it.",
    )
    bootstrap.set_defaults(handler="bootstrap-endpoint")

    cleanup = subparsers.add_parser(
        "cleanup-endpoint",
        help="Delete this deployment's Globus endpoint. Permanent; needs the endpoint id.",
        parents=[common],
    )
    cleanup.add_argument(
        "--endpoint-id",
        metavar="UUID",
        required=True,
        # Required, and the only guard that does not read its answer out of the
        # answers document — which is the file most likely to be wrong when this
        # command is run by mistake. See `commands/cleanup_endpoint.py`.
        help="The endpoint to delete. Must match what this deployment records, or nothing runs.",
    )
    cleanup.add_argument(
        "--keep-running",
        action="store_true",
        help="Do not offer to stop the instance afterwards, even if this command started it.",
    )
    cleanup.set_defaults(handler="cleanup-endpoint")

    tasks = subparsers.add_parser(
        "tasks",
        help="List — or cancel — this pipeline's Globus transfer tasks.",
        parents=[common],
    )
    tasks.add_argument(
        "--cancel",
        action="store_true",
        help="Cancel the listed tasks (asks first; needs --yes when non-interactive).",
    )
    tasks.add_argument(
        "--subject",
        metavar="SUBJECT_ID",
        help="Only tasks whose label names this subject.",
    )
    tasks.set_defaults(handler="tasks")

    return parser


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner = subprocess_runner,
    session_factory: Callable[[], Any] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    emitter = Emitter(args.handler, env=args.env, json_mode=args.json)
    try:
        cfg = config_module.load(args.answers)
        env = environments.resolve(
            args.env,
            deployment_name=cfg.deployment_name,
            collection_name=cfg.collection_name,
            gateway_name=cfg.gateway_name,
        )
    except CliError as err:
        return int(emitter.failure(err))

    # Say which environment is being acted on before doing anything, in both
    # modes: a staging run that looks like a production run is how someone
    # mistakes one for the other.
    emitter.note(f"environment: {env.name} ({env.gateway_name})")

    ctx = Context(
        args=args,
        config=cfg,
        env=env,
        emitter=emitter,
        runner=runner,
        session_factory=session_factory or _default_session_factory,
    )

    handler = COMMANDS.get(args.handler)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        emitter.note(f"unknown command {args.handler!r}")
        return int(ExitCode.ERROR)

    try:
        return int(handler(ctx))
    except CliError as err:
        return int(emitter.failure(err))
    except KeyboardInterrupt:  # pragma: no cover
        emitter.note("interrupted")
        return int(ExitCode.ERROR)


def register(name: str) -> Callable[[Callable[[Context], ExitCode]], Callable[[Context], ExitCode]]:
    def decorator(fn: Callable[[Context], ExitCode]) -> Callable[[Context], ExitCode]:
        COMMANDS[name] = fn
        return fn

    return decorator


def _default_session_factory() -> Any:
    import boto3

    return boto3.Session()


# Imported for their side effect of registering handlers. Placed at the bottom
# to avoid a circular import: the command modules need `Context` and `register`.
from .commands import bootstrap_endpoint as _bootstrap_endpoint  # noqa: E402,F401
from .commands import cleanup_endpoint as _cleanup_endpoint  # noqa: E402,F401
from .commands import configure as _configure  # noqa: E402,F401
from .commands import doctor as _doctor  # noqa: E402,F401
from .commands import init as _init  # noqa: E402,F401
from .commands import login as _login  # noqa: E402,F401
from .commands import service_client as _service_client  # noqa: E402,F401
from .commands import setup_status as _setup_status  # noqa: E402,F401
from .commands import status as _status  # noqa: E402,F401
from .commands import tasks as _tasks  # noqa: E402,F401


def run() -> None:
    sys.exit(main())
