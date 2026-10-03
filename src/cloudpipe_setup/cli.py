"""`pixi run cloudpipe <command>` — the deployer's entry point.

Global options exist on every command rather than per command, so one caller — a
person, a CI job, another program — drives all of them the same way:

    --json              one JSON envelope on stdout; human text stays on stderr
    --non-interactive   never prompt; a needed answer exits 2 instead
    --answers PATH      the answers document (default: ./cloudpipe-answers.yaml)
    --root DIR          the Terraform root to write into (default: the cwd)

Two commands. `setup` collects the inputs and renders the Terraform files;
`preflight` checks the collected answers against the world and is read-only — it
creates, modifies and deletes nothing, which is what makes it safe to re-run and
safe to run against a deployment you did not build.

This tool creates no cloud resource and takes no credential. AWS credentials come
from an AWS CLI v2 SSO profile in the environment; the Cloudflare token is read
from `CLOUDFLARE_API_TOKEN` and never prompted for, stored or printed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass

from .exits import CliError, ExitCode
from .output import Emitter

COMMANDS: dict[str, Callable[[Context], ExitCode]] = {}


@dataclass
class Context:
    """Everything a command needs, resolved once by `main`."""

    args: argparse.Namespace
    emitter: Emitter

    @property
    def non_interactive(self) -> bool:
        return bool(self.args.non_interactive)


def _add_global_options(parser: argparse.ArgumentParser, *, with_defaults: bool) -> None:
    """The flags that apply to every command, whichever side of it they are typed.

    Added twice: once on the top-level parser and once, through a parent, on every
    subparser. argparse binds an option to the parser that declares it, and a
    subcommand's own options come after the subcommand name — so a global declared
    only at the top level makes `cloudpipe setup --json` a usage error.

    `with_defaults=False` is for the per-subcommand copies, and it matters: a real
    default there would overwrite a value given BEFORE the subcommand, so
    `cloudpipe --non-interactive setup` would quietly start prompting. SUPPRESS
    leaves the attribute alone unless the flag is actually given. Given on both
    sides, the one after the subcommand wins, which is argparse's last-wins rule.
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
        help="Never prompt. A missing answer exits 2 instead, naming every field.",
    )
    parser.add_argument(
        "--answers",
        metavar="PATH",
        default=_default(None),
        help="Answers document (default: ./cloudpipe-answers.yaml).",
    )
    parser.add_argument(
        "--root",
        metavar="DIR",
        default=_default(None),
        help="Your copy of terraform/modules/stack/example/ (default: the current directory).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cloudpipe",
        description="Collect the inputs a CloudPipe deployment needs and render its Terraform "
        "files. Creates nothing in any cloud account.",
        epilog=(
            "Credentials come from your environment: AWS CLI v2 with an SSO profile "
            "(`aws sso login`), and CLOUDFLARE_API_TOKEN. This tool never takes a key as an "
            "argument and never writes one to a file."
        ),
    )
    _add_global_options(parser, with_defaults=True)

    # Every subcommand inherits the globals from here. `add_help=False` so the
    # parent does not fight each subparser for `-h`; the side effect is that a
    # subcommand's `--help` lists them, which is where someone looks first.
    common = argparse.ArgumentParser(add_help=False)
    _add_global_options(common, with_defaults=False)

    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser(
        "setup",
        help="Ask for the deployment's inputs and render terraform.tfvars and backend.tf.",
        parents=[common],
    )
    setup.add_argument(
        "--force",
        action="store_true",
        help="Overwrite rendered files that already exist. Without it, an existing file is an "
        "error: this tool does not merge into a file it did not write.",
    )
    setup.add_argument(
        "--no-backend",
        action="store_true",
        help="Do not render backend.tf. Use this if you keep state somewhere else, or if you "
        "have uncommented the example root's own backend block.",
    )
    setup.set_defaults(handler="setup")

    preflight = subparsers.add_parser(
        "preflight",
        help="Check the collected answers against the world. Read-only; creates nothing.",
        parents=[common],
        description="Verify the facts the collected inputs assert about the world: the resolved "
        "AWS identity, the hosted zone, the managed prefix lists, the OIDC issuer's discovery "
        "document, the Cloudflare token and its Zero Trust scope, the hand-created Access OIDC "
        "secret, and whether `globus init` has rendered its inputs. Every call is a read.",
    )
    preflight.add_argument(
        "--account",
        metavar="ID",
        default=None,
        help="The AWS account id you intend to deploy into. Without it, the account check reports "
        "skipped rather than passed: there is nothing to compare the resolved account against.",
    )
    preflight.set_defaults(handler="preflight")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    emitter = Emitter(args.handler, json_mode=args.json)

    handler = COMMANDS.get(args.handler)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        emitter.note(f"unknown command {args.handler!r}")
        return int(ExitCode.ERROR)

    try:
        return int(handler(Context(args=args, emitter=emitter)))
    except CliError as err:
        return int(emitter.failure(err))
    except KeyboardInterrupt:  # pragma: no cover
        # An interrupted run has already written the answers document up to the
        # last field answered, so re-running resumes rather than restarting.
        emitter.note("interrupted; re-run to continue from the last answer")
        return int(ExitCode.ERROR)


def register(name: str) -> Callable[[Callable[[Context], ExitCode]], Callable[[Context], ExitCode]]:
    def decorator(fn: Callable[[Context], ExitCode]) -> Callable[[Context], ExitCode]:
        COMMANDS[name] = fn
        return fn

    return decorator


# Imported for their side effect of registering handlers. Placed at the bottom to
# avoid a circular import: the command modules need `Context` and `register`.
from .commands import preflight as _preflight  # noqa: E402,F401
from .commands import setup as _setup  # noqa: E402,F401


def run() -> None:
    sys.exit(main())
