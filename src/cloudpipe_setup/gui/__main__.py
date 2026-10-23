"""`python -m cloudpipe_setup.gui` — serve the setup form on localhost.

The root and the answers document are resolved and checked here, before the
server starts, with the same functions and the same errors as `cloudpipe setup`.
A page that opened on a directory that is not a stack root would only fail
later, at the step that writes into it.
"""

from __future__ import annotations

import argparse

from .. import config, roots
from ..exits import CliError
from ..output import Emitter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cloudpipe-gui",
        description="Serve the CloudPipe setup form on http://127.0.0.1. Creates nothing in "
        "any cloud account unless started with --allow-install and a run is confirmed.",
    )
    parser.add_argument(
        "--root",
        metavar="DIR",
        default=None,
        help="Your copy of terraform/modules/stack/example/ (default: the current directory).",
    )
    parser.add_argument(
        "--answers",
        metavar="PATH",
        default=None,
        help="Answers document (default: ./cloudpipe-answers.yaml).",
    )
    parser.add_argument("--port", type=int, default=8765, help="Local port (default: 8765).")
    parser.add_argument(
        "--no-browser", action="store_true", help="Print the address without opening a browser."
    )
    parser.add_argument(
        "--allow-install",
        action="store_true",
        help="Let the page run scripts/stack/install.sh phases itself. Off by default: without "
        "it the page only shows each phase's command. Every run is confirmed first.",
    )
    args = parser.parse_args(argv)

    try:
        root = roots.resolve(args.root)
        answers_path = config.resolve_path(args.answers)
        answers, _ = config.load(answers_path)
        config.reject_credentials(answers)
    except CliError as err:
        return int(Emitter("gui").failure(err))

    # Imported after the checks, so a wrong root is reported without paying for
    # NiceGUI's import.
    from . import app

    try:
        app.serve(
            root,
            answers_path,
            port=args.port,
            show=not args.no_browser,
            allow_install=args.allow_install,
        )
    except KeyboardInterrupt:
        # Ctrl-C is how the page is meant to be stopped. Uvicorn finishes its
        # shutdown, including a run's interrupt-and-wait, and then re-raises the
        # signal; a traceback here would read as a crash.
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
