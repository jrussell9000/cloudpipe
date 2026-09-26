"""
Validate the shared-query-source wiring on the Grafana QC dashboards.

Most panels on the three QC dashboards do not query Athena. A few "source"
panels run the query; the rest read that panel's raw result frame through the
built-in `-- Dashboard --` data source and pick their own column out of it.
That wiring fails *silently*: a consumer whose field selector matches no column
in the source query renders "No data", not an error, and `gitops/**` sits
outside the `pull_request.paths` filter in `.github/workflows/ci.yaml`, so no
CI job would catch it either.

Checks, per dashboard:
  1. every consumer's `panelId` resolves to a real panel
  2. that panel actually queries Athena (a consumer pointed at a consumer
     yields an empty frame)
  3. every field name the consumer selects exists as a column alias in the
     source query -- case-sensitively, since Athena preserves alias case and
     the selectors are regexes
  4. no source panel sits inside a collapsed row, where Grafana never runs it

Usage:
  python scripts/validate_dashboard_queries.py
  python scripts/validate_dashboard_queries.py gitops/apps/grafana/dashboards/functional-qc.json
"""

import json
import re
import sys
from pathlib import Path

DASHBOARD_DIR = Path("gitops/apps/grafana/dashboards")


def walk(doc):
    """Yield (panel, parent_row) for every panel, including inside rows."""
    for panel in doc.get("panels", []):
        yield panel, None
        for nested in panel.get("panels", []) or []:
            yield nested, panel


def raw_sql(panel):
    for target in panel.get("targets") or []:
        if target.get("rawSQL"):
            return target["rawSQL"]
    return None


def split_top_level(select_list: str) -> list[str]:
    """Split a SELECT list on commas that are not inside parens or quotes."""
    items, depth, current, quote = [], 0, [], None
    for char in select_list:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(char)
    items.append("".join(current))
    return [item.strip() for item in items if item.strip()]


def column_aliases(sql: str) -> list[str]:
    """Column names the final SELECT of `sql` produces."""
    body = sql.partition(") SELECT")[2] if ") SELECT" in sql else sql.partition("SELECT")[2]
    froms = list(re.finditer(r"\bFROM\b", body))
    if froms:
        body = body[: froms[-1].start()]
    names = []
    for item in split_top_level(body):
        aliased = re.search(r"\bAS\s+([A-Za-z_]\w*)\s*$", item, re.IGNORECASE)
        if aliased:
            names.append(aliased.group(1))
        elif re.fullmatch(r'[A-Za-z_]\w*(\.[\w"]+)?', item):
            names.append(item.split(".")[-1].strip('"'))
    return names


def selected_fields(panel) -> list[str]:
    """Field names a consumer panel picks out of the shared frame."""
    for transform in panel.get("transformations") or []:
        if transform.get("id") == "filterFieldsByName":
            return list(transform.get("options", {}).get("include", {}).get("names", []))
    selector = panel.get("options", {}).get("reduceOptions", {}).get("fields")
    if not selector:
        return []
    if selector.startswith("/^") and selector.endswith("$/"):
        inner = selector[2:-2]
        if inner.startswith("(") and inner.endswith(")"):
            inner = inner[1:-1]
        return [re.sub(r"\\(.)", r"\1", name) for name in inner.split("|")]
    return [selector]


def check(path: Path) -> list[str]:
    doc = json.loads(path.read_text())
    panels = {p.get("id"): (p, parent) for p, parent in walk(doc)}
    errors, sources, consumers = [], set(), 0
    collapsed_reported = set()

    for pid, (panel, _) in sorted(panels.items(), key=lambda kv: (kv[0] is None, kv[0])):
        targets = panel.get("targets") or []
        if not targets or not targets[0].get("panelId"):
            continue
        consumers += 1
        source_id = targets[0]["panelId"]
        if source_id not in panels:
            errors.append(f"panel {pid} points at panel {source_id}, which does not exist")
            continue
        source, source_row = panels[source_id]
        sql = raw_sql(source)
        if not sql:
            errors.append(f"panel {pid} points at panel {source_id}, which has no rawSQL")
            continue
        sources.add(source_id)
        if source_row is not None and source_row.get("collapsed"):
            # One report per source, not one per consumer hanging off it.
            if source_id not in collapsed_reported:
                collapsed_reported.add(source_id)
                errors.append(
                    f"source panel {source_id} is inside collapsed row "
                    f"{source_row.get('title')!r} -- its query never runs"
                )
        aliases = column_aliases(sql)
        lowered = {a.lower(): a for a in aliases}
        for name in selected_fields(panel):
            if name in aliases:
                continue
            hint = ""
            if name.lower() in lowered:
                hint = f" (case differs: source produces {lowered[name.lower()]!r})"
            errors.append(
                f"panel {pid} ({panel.get('title')!r}) selects {name!r}, "
                f"which panel {source_id} does not produce{hint}"
            )

    queries = sum(1 for p, _ in walk(doc) for t in (p.get("targets") or []) if t.get("rawSQL"))
    print(
        f"{path.name:<26} {queries:>3} athena queries, "
        f"{len(sources):>2} source panels, {consumers:>2} consumers"
    )
    for error in errors:
        print(f"   FAIL {error}")
    return errors


def main() -> int:
    args = sys.argv[1:]
    paths = [Path(a) for a in args] if args else sorted(DASHBOARD_DIR.glob("*.json"))
    if not paths:
        print(f"no dashboards found under {DASHBOARD_DIR}", file=sys.stderr)
        return 2
    failures = sum(len(check(path)) for path in paths)
    print("\n" + ("OK -- no wiring errors" if not failures else f"{failures} problem(s)"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
