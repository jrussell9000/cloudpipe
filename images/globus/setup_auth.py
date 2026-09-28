"""Retired. The Globus login lives in the operator CLI now.

    pixi run globus login                # production
    pixi run globus login --env staging  # the staging gateway

Why it moved, rather than being kept as a second way to do the same thing:

* the old flow pasted an auth code between a browser and a terminal, where the
  new one completes in the browser and hands the token back directly;
* it could not record *when* the session was established, so nothing could warn
  that a batch was about to be submitted against a session that had lapsed —
  which is what happened to 300 subjects on 2026-08-17;
* it wrote the token to Secrets Manager and then ran `kubectl delete secret`,
  which leaves every pod mounting that Secret without a credential until it is
  re-created. The CLI annotates the ExternalSecret and waits for the sync.

This file stays as a signpost: anyone following an older runbook lands here
instead of on a missing file.
"""

import sys

MESSAGE = __doc__ or ""


def main() -> None:
    print(MESSAGE.strip(), file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
