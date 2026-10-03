"""Deployment setup wizard: a fresh clone to a validated `terraform.tfvars`.

One command surface (`pixi run cloudpipe <command>`) for the inputs a new
deployment must supply, so nobody reads `variables.tf` and three documentation
pages to find out what to fill in. The requirements this implements are the
`deployment-setup-wizard` OpenSpec spec; the "design D<n>" citations throughout
this package refer to the decisions in the archived
`2026-10-02-add-deployment-setup-wizard` change. See `src/globus_admin/` for the
pattern it mirrors — that package solved the same problem for the Globus
subsystem and its spec names "a future setup wizard" as the consumer of its JSON
contract.

Four rules hold across every module here:

1. **Nothing is created or modified outside the target root.** Version 1 writes
   `terraform.tfvars`, `backend.tf` and its own answers document, and no AWS,
   Cloudflare, Kubernetes or Globus resource at all.
2. **AWS credentials come from the environment only** — an AWS CLI v2 SSO
   profile. Nothing here accepts an access key, writes AWS config, or runs a
   login on the deployer's behalf.
3. **No question is hard-coded.** Prompts, help text and validation rules are
   read from `schemas/inputs.schema.json`, so a later browser or full-screen
   front end renders the same form from the same file.
4. **Every command must be drivable by another program**: no prompt under
   `--non-interactive`, machine-readable output on stdout under `--json`, human
   text on stderr, and the exit codes in `exits.py`.

No deployment-specific value — a domain, an account id, a bucket name, an
institution name, a personal identity — may appear in this package or its schema.
The publish gate scans `src/`, and it fails closed.
"""

CONTRACT_VERSION = "1.0"
"""Version of the integration contract (the input schema, JSON outputs, exit codes).

Additive changes within a major version only: fields are never removed or
renamed, and identifiers stay stable, because another front end reads them. This
is the same promise `globus_admin.CONTRACT_VERSION` makes, and the two are
versioned independently — they are separate contracts with separate consumers.
"""
