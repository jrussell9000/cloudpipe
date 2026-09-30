"""Read every deployment in prefect.yaml back from the Prefect server and compare.

prefect/deploy.sh runs this straight after `prefect deploy --all`, because the
deploy command's own success says nothing about what was stored. Prefect fills a
`{{ $VAR }}` placeholder once, at deploy time, and an unset one becomes "" with
only a WARNING — so a deploy that stored an empty bucket or an image of
`/cloudpipe/...` still reports success. The server's record is the only state
that matters; `git diff` and the CLI's output are evidence of intent, not of it.

For each deployment it renders the file's `parameters` and `job_variables` with
the same environment the deploy saw, and requires the server to hold exactly
that, key by key. Keys the server adds on its own are ignored.

It reads through Prefect's API client, not the CLI: the CLI's variable listing
is known to misreport, and the client honours the same PREFECT_API_URL and
auth settings `prefect deploy` just used.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import DeploymentFilter, DeploymentFilterName

PREFECT_YAML = Path(__file__).resolve().parent / "prefect.yaml"
PLACEHOLDER = re.compile(r"\{\{\s*\$(\w+)\s*\}\}")


def render(value: Any) -> Any:
    """Substitute `{{ $VAR }}` from the environment, refusing an unset or empty one."""
    if isinstance(value, dict):
        return {k: render(v) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v) for v in value]
    if not isinstance(value, str):
        return value

    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        found = os.environ.get(name, "")
        if not found:
            raise SystemExit(f"{name} is unset or empty; run this through prefect/deploy.sh")
        return found

    return PLACEHOLDER.sub(sub, value)


def mismatches(expected: Any, stored: Any, path: str) -> list[str]:
    """Every place `stored` differs from `expected`, ignoring keys only `stored` has."""
    if isinstance(expected, dict):
        if not isinstance(stored, dict):
            return [f"{path}: expected a mapping, server holds {stored!r}"]
        out: list[str] = []
        for key, want in expected.items():
            out += mismatches(want, stored.get(key), f"{path}.{key}")
        return out
    if expected != stored:
        return [f"{path}: expected {expected!r}, server holds {stored!r}"]
    return []


def main() -> int:
    specs = yaml.safe_load(PREFECT_YAML.read_text())["deployments"]
    names = [s["name"] for s in specs]

    with get_client(sync_client=True) as client:
        found = client.read_deployments(
            deployment_filter=DeploymentFilter(name=DeploymentFilterName(any_=names))
        )

    by_name: dict[str, list] = {}
    for d in found:
        by_name.setdefault(d.name, []).append(d)

    failures: list[str] = []
    for spec in specs:
        name = spec["name"]
        stored = by_name.get(name, [])
        if len(stored) != 1:
            failures.append(f"{name}: expected one deployment on the server, found {len(stored)}")
            continue
        d = stored[0]
        want = {
            "parameters": render(spec.get("parameters") or {}),
            "job_variables": render(spec["work_pool"].get("job_variables") or {}),
        }
        have = {"parameters": d.parameters, "job_variables": d.job_variables}
        problems = mismatches(want, have, name)
        failures += problems
        if not problems:
            print(f"OK  {name}: server holds what prefect.yaml renders to")

    for line in failures:
        print(f"FAIL {line}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
