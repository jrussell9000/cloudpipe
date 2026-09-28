"""Operator tooling for the CloudPipe Globus ingress.

One command surface (`pixi run globus <command>`) for every routine Globus task,
so no runbook asks an operator to type a raw `globus-connect-server`, `aws ssm`,
or `kubectl` command. See the `simplify-globus-ingress` OpenSpec change for the
requirements this implements, and `docs/globus-contract.md` for the outputs a
setup wizard may depend on.

Two rules hold across every module here:

1. **AWS credentials come from the environment only** — an AWS CLI v2 SSO
   profile. Nothing in this package accepts an access key, writes AWS config, or
   runs a login on the operator's behalf.
2. **Every command must be drivable by another program**: no prompt under
   `--non-interactive`, machine-readable output on stdout under `--json`, human
   text on stderr, and the exit codes in `exits.py`.
"""

CONTRACT_VERSION = "1.0"
"""Version of the integration contract (JSON outputs, exit codes, gate list).

Additive changes within a major version only: fields are never removed or
renamed, and identifiers stay stable, because a wizard reads them.
"""
