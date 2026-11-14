"""`pixi run globus <command>` — the one entry point operators use.

Global options exist on every command, not per-command, so a caller (a wizard, a
CI job, an agent) can drive either the same way:

    --json                       machine-readable envelope on stdout
    --non-interactive            never prompt; a needed human step exits 2
    --yes                        accepted for a 1.x caller; nothing asks for it now
    --answers PATH               the answers document

The commands are `login` and `doctor`, and no other (contract 2.0). Every command
that addressed the Globus Connect Server host, its endpoint, a storage gateway, a
service client or a Transfer task was retired with the host (openspec
retire-globus-host), and so was `--env`: one deployment has one source, one token
secret and one Kubernetes Secret. A retired command is a usage error, before any
AWS or Globus request.
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
    declared only at the top level makes `globus doctor --json` a usage error.
    The published contract writes it in that order, and it is the order people type.

    `with_defaults=False` is for the per-subcommand copies, and it matters: a real
    default there would overwrite a value given BEFORE the subcommand. SUPPRESS
    leaves the attribute alone unless the flag is actually given.
    """

    def _default(value: object) -> object:
        return value if with_defaults else argparse.SUPPRESS

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
        help="Never prompt. A step that needs a person exits 2 instead.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        default=_default(False),
        help="Accepted for compatibility; no command asks for confirmation.",
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
        description="The Globus credentials behind CloudPipe's HTTPS ingress.",
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
        help="Before a batch, or when ingress fails: check every dependency in order "
        "and say what to do about each problem.",
        parents=[common],
    )
    doctor.set_defaults(handler="doctor")

    login = subparsers.add_parser(
        "login",
        help="When doctor says the session is old or a token is missing: log in to "
        "Globus and store the credentials the pipeline uses.",
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

    return parser


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner = subprocess_runner,
    session_factory: Callable[[], Any] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    emitter = Emitter(args.handler, env=environments.PRODUCTION, json_mode=args.json)
    try:
        cfg = config_module.load(args.answers)
        env = environments.resolve(deployment_name=cfg.deployment_name)
    except CliError as err:
        return int(emitter.failure(err))

    # Say which deployment is being acted on before doing anything, in both modes.
    emitter.note(f"deployment: {env.deployment_name} ({env.ssm_prefix})")

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
from .commands import doctor as _doctor  # noqa: E402,F401
from .commands import login as _login  # noqa: E402,F401


def run() -> None:
    sys.exit(main())
