"""On-instance reconcile for a Globus Connect Server endpoint.

Reads the declared configuration (rendered by Terraform into
`/<prefix>/globus/config`), reads the endpoint's live state through the
`globus-connect-server` CLI, and reports or applies the difference.

Stdlib only. It is baked into the GCS AMI, where adding a dependency means
rebuilding the image, so nothing here imports outside the standard library —
including `globus_admin`, which it uses for validation only when it happens to be
importable (in the repository and in CI, not on the instance).

Layout:

* `planner` — pure diffing. Never deletes, never touches undeclared objects,
  refuses rather than recreating. Testable without an endpoint.
* `gcs` — the CLI wrapper and the response-shape normalisation.
* `node_report`, `plan_report` — facts recorded in SSM for `globus doctor` to read
  back from a workstation while this host is stopped. Neither applies a rule; the
  rules live in `globus_admin.doctor`, so correcting one is a code change rather
  than an AMI rebuild.
* `__main__` — the `plan` and `apply` entry points.
"""

from . import gcs, planner
from .planner import Action, Note, Plan, Problem, plan

__all__ = ["Action", "Note", "Plan", "Problem", "gcs", "plan", "planner"]
