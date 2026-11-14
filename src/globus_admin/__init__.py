"""Operator tooling for the CloudPipe HTTPS ingress's Globus credentials.

Two commands (`pixi run globus login`, `pixi run globus doctor`), so no runbook
asks an operator to type a raw `aws ssm` or `kubectl` command. The Globus Connect
Server host and every command that addressed it were retired (openspec
retire-globus-host); see `docs/globus-contract.md` for the outputs a setup wizard
may depend on.

Two rules hold across every module here:

1. **AWS credentials come from the environment only** — an AWS CLI v2 SSO
   profile. Nothing in this package accepts an access key, writes AWS config, or
   runs a login on the operator's behalf.
2. **Every command must be drivable by another program**: no prompt under
   `--non-interactive`, machine-readable output on stdout under `--json`, human
   text on stderr, and the exit codes in `exits.py`.
"""

CONTRACT_VERSION = "2.0"
"""Version of the integration contract (JSON outputs, exit codes, the answers schema).

Additive changes within a major version only: fields are never removed or
renamed, and identifiers stay stable, because a wizard reads them. 2.0 removed the
endpoint checks and every command but `login` and `doctor`, keeping each surviving
check's 1.x identifier.
"""
