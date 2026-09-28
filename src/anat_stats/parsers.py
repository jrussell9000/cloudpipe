"""Parsers for the three text formats anatomical stats arrive in.

Every file we aggregate is plain text. Nothing here shells out to FreeSurfer:
`asegstats2table`/`aparcstats2table` need a populated local `$SUBJECTS_DIR`,
which would mean staging ~250 MB of a FastSurfer tree to read ~120 KB of text
out of it, 33,000 times over. The formats are simple enough to read directly.

Three shapes, and they are genuinely different:

1. FreeSurfer `.stats` (`parse_freesurfer_stats`) — the FastSurfer outputs.
   `# Measure` header lines carry whole-brain scalars; an optional
   `# ColHeaders` line declares a whitespace-delimited per-structure table.
   Column ORDER VARIES BY FILE: `StructName` is field 5 in `aseg.stats` and
   field 1 in `lh.aparc.DKTatlas.mapped.stats`, so positions are never assumed.
   `brainvol.stats` has measures and no table at all. Per-column units come from
   the header's `# TableCol N Units` lines, so `Volume_mm3` carries `mm^3`
   because the file says so, not because of what its name looks like.

2. Curvature stats (`parse_curvature_stats`) — `[lr]h.curv.stats` only, and NOT
   a FreeSurfer table despite the extension. It is `mris_curvature_stats`
   human-readable output: blocks keyed by (curvature type, surface), each with
   ~20 labelled scalars. Nine types (Raw K H k1 k2 S C BE FI) over three
   surfaces.

3. Bare `name value` (`parse_volume_table`) — the subregion outputs
   (`ThalamicNuclei.long.volumes.txt` and friends). No header, no units, and no
   subject or session inside the file; identity comes only from the S3 key.

NaN is preserved as None, never coerced to 0.0. `[lr]h.w-g.pct.stats` really
does emit `-nan` in its SNR column for empty structures, and the same reasoning
that made every `FsqcQC` field nullable applies here: 0.0 is a legitimate value
for curvature means and integrals, so a real 0 must stay distinguishable from a
missing measurement. Filter on IS NOT NULL, never on `> 0`.
"""

from __future__ import annotations

import math
import re
from typing import NamedTuple


class StatRow(NamedTuple):
    """One measurement. Subject/session/source are attached by the caller."""

    structure: str
    measure: str
    value: float | None
    unit: str


# `# Measure <structure>, <measure>, <description>, <value>, <unit>`. The
# description may itself contain commas, so the fields are taken from both ends
# inward rather than by splitting into a fixed count.
_MEASURE_RE = re.compile(r"^#\s*Measure\s+(.*)$")
_COLHEADERS_RE = re.compile(r"^#\s*ColHeaders\s+(.*)$")
_FS_VERSION_RE = re.compile(r"^#\s*(?:FastSurfer_version|fs_version|cmdline_version)\s+(\S+)")
# `# TableCol  4 ColHeader Volume_mm3` / `# TableCol  4 Units     mm^3` — the
# header declares each table column's unit, one line per (column, attribute).
_TABLECOL_RE = re.compile(
    r"^#\s*TableCol\s+(?P<column>\d+)\s+(?P<attr>ColHeader|Units)\s+(?P<value>.*)$"
)

# `K <mean> +- <std> (using 'lh.smoothwm'):   0.04827 +- 5.8775 mm^-2`
_CURV_BLOCK_RE = re.compile(
    r"^(?P<type>\S+) <mean> \+- <std> \(using '(?P<surface>[^']+)'\):\s+"
    r"(?P<mean>\S+) \+- (?P<std>\S+)\s*(?P<unit>\S*)\s*$"
)
# `         K Mean Positive Surface Integral:    0.02669 across 65236 (46.80%) vertices`
_CURV_METRIC_RE = re.compile(r"^\s+(?P<type>\S+) (?P<label>.+?):\s+(?P<value>\S+)(?P<tail>.*)$")
_CURV_TAIL_STD_RE = re.compile(r"^\s*\+-\s*(?P<std>\S+)\s*(?P<unit>\S*)\s*$")
_CURV_TAIL_VERTEX_RE = re.compile(r"^\s*at vertex\s+(?P<vertex>\d+)\s*$")
_CURV_TAIL_ACROSS_RE = re.compile(
    r"^\s*across\s+(?P<across>\S+)\s+\((?P<pct>[\d.]+)%\)\s*(?P<unit>\S*)\s*$"
)

# Row ordinal within a stats table — not a measurement, and meaningless once
# rows are keyed by structure. SegId is kept: it joins to FreeSurfer's LUTs.
_DROPPED_COLUMNS = frozenset({"Index"})

_STRUCT_COLUMN = "StructName"


def to_float(token: str) -> float | None:
    """Parse a stats token, mapping NaN/inf to None.

    FreeSurfer writes `-nan` (and occasionally `inf`) for a metric whose inputs
    were empty. `float("-nan")` succeeds and yields a NaN that silently
    poisons any downstream mean, so it is normalised to None here instead.
    Returns None for anything non-numeric, which is how categorical values like
    `discrete` (the Curvature Calculation Type) get skipped.
    """
    try:
        value = float(token)
    except ValueError:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def parse_measure_line(body: str) -> StatRow | None:
    """Parse the payload of a `# Measure` line into a row, or None if malformed."""
    fields = [f.strip() for f in body.split(",")]
    if len(fields) < 4:
        return None
    structure, measure, unit, value = fields[0], fields[1], fields[-1], fields[-2]
    if not structure or not measure:
        return None
    return StatRow(structure, measure, to_float(value), unit)


def table_units(text: str) -> dict[str, str]:
    """Map each table column name to the unit its `# TableCol` lines declare.

    Read in a separate pass rather than inline, so the result does not depend on
    `# TableCol` preceding `# ColHeaders` — it does in every file this pipeline
    produces, but the files are ~120 KB and a second pass costs nothing to be
    order-independent about it.

    `NA` becomes "" (the column is an identifier, not a measurement). `unitless`
    is kept verbatim: a declared count is different information from an
    undeclared unit, and flattening both to "" would lose that.
    """
    attributes: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        if not line.startswith("#"):
            continue
        if match := _TABLECOL_RE.match(line):
            column = attributes.setdefault(match.group("column"), {})
            column[match.group("attr")] = match.group("value").strip()
    return {
        column["ColHeader"]: "" if column.get("Units") == "NA" else column.get("Units", "")
        for column in attributes.values()
        if "ColHeader" in column
    }


def _parse_table_row(
    line: str, headers: list[str], struct_index: int, units: dict[str, str]
) -> list[StatRow]:
    fields = line.split()
    if len(fields) != len(headers):
        return []
    structure = fields[struct_index]
    return [
        StatRow(structure, header, to_float(field), units.get(header, ""))
        for index, (header, field) in enumerate(zip(headers, fields, strict=True))
        if index != struct_index and header not in _DROPPED_COLUMNS
    ]


def parse_freesurfer_stats(text: str) -> tuple[list[StatRow], str]:
    """Parse a FreeSurfer-format `.stats` file.

    Returns (rows, fs_version). `fs_version` is "" when the header carries no
    version line; it is worth recording per file because region definitions are
    version-dependent — the same reason `FsqcQC` records `fsqc_version` per row.

    A file with `# Measure` lines and no `# ColHeaders` (brainvol.stats) is
    valid and yields only the measure rows. A `# ColHeaders` line WITHOUT a
    `StructName` column is not something this pipeline produces, and is raised
    rather than skipped: silently returning no rows is how the previous
    aggregation script hid the fact that it was reading nothing.
    """
    rows: list[StatRow] = []
    fs_version = ""
    headers: list[str] = []
    struct_index = -1
    units = table_units(text)

    for line in text.splitlines():
        if line.startswith("#"):
            if version_match := _FS_VERSION_RE.match(line):
                fs_version = fs_version or version_match.group(1)
            elif measure_match := _MEASURE_RE.match(line):
                if row := parse_measure_line(measure_match.group(1)):
                    rows.append(row)
            elif colheaders_match := _COLHEADERS_RE.match(line):
                headers = colheaders_match.group(1).split()
                if _STRUCT_COLUMN not in headers:
                    raise ValueError(f"ColHeaders has no {_STRUCT_COLUMN}: {headers}")
                struct_index = headers.index(_STRUCT_COLUMN)
            continue
        if headers and line.strip():
            rows.extend(_parse_table_row(line, headers, struct_index, units))

    return rows, fs_version


def _curv_tail(surface: str, measure: str, tail: str) -> tuple[str, list[StatRow]]:
    """Split a curvature metric line's trailing clause into (unit, extra rows).

    The unit returned belongs to the line's PRIMARY value, which is not always
    the unit the clause names. `... 0.02669 across 65236 (100.00%) vertices`
    counts vertices, but the integral itself is not measured in vertices — so
    `vertices` is attached to the `|across` row and the primary value is left
    unitless rather than mislabelled.
    """
    if std_match := _CURV_TAIL_STD_RE.match(tail):
        unit = std_match.group("unit")
        return unit, [StatRow(surface, f"{measure}|std", to_float(std_match.group("std")), unit)]
    if vertex_match := _CURV_TAIL_VERTEX_RE.match(tail):
        return "", [
            StatRow(surface, f"{measure}|vertex", to_float(vertex_match.group("vertex")), "")
        ]
    if across_match := _CURV_TAIL_ACROSS_RE.match(tail):
        across_unit = across_match.group("unit")
        return "", [
            StatRow(
                surface, f"{measure}|across", to_float(across_match.group("across")), across_unit
            ),
            StatRow(surface, f"{measure}|across_pct", to_float(across_match.group("pct")), "%"),
        ]
    # A bare trailing token is the primary value's own unit (`... 86946.75 mm^2`).
    return tail.strip(), []


def parse_curvature_stats(text: str) -> list[StatRow]:
    """Parse `mris_curvature_stats` output (`[lr]h.curv.stats`).

    `structure` is the surface the block was computed over (`lh.smoothwm`), and
    `measure` is `{type}|{label}` so the nine curvature types stay separable —
    `K|Min` and `H|Min` are different quantities over the same surface.

    Lines whose value is categorical rather than numeric (`Curvature
    Calculation Type: discrete`) yield a row with a None value, which keeps the
    file's structure visible without inventing a number for it.
    """
    rows: list[StatRow] = []
    # An indented metric line names only its curvature type, never its surface;
    # the enclosing block header is what binds the two. Metric lines before any
    # block header have no surface to attribute to and are dropped.
    surface = ""
    for line in text.splitlines():
        if not line.strip():
            continue
        if block_match := _CURV_BLOCK_RE.match(line):
            curv_type = block_match.group("type")
            surface = block_match.group("surface")
            unit = block_match.group("unit")
            rows.append(
                StatRow(surface, f"{curv_type}|mean", to_float(block_match.group("mean")), unit)
            )
            rows.append(
                StatRow(surface, f"{curv_type}|std", to_float(block_match.group("std")), unit)
            )
            continue
        if (metric_match := _CURV_METRIC_RE.match(line)) and surface:
            measure = f"{metric_match.group('type')}|{metric_match.group('label').strip()}"
            unit, extra = _curv_tail(surface, measure, metric_match.group("tail"))
            rows.append(StatRow(surface, measure, to_float(metric_match.group("value")), unit))
            rows.extend(extra)
    return rows


def parse_volume_table(text: str, measure: str = "Volume_mm3", unit: str = "mm^3") -> list[StatRow]:
    """Parse a bare `name value` table — the subregion segmentation outputs.

    Two whitespace-separated fields per line, no header. Lines with a different
    shape are skipped rather than raised: these files are written by FreeSurfer
    GEMS binaries whose output we do not control.
    """
    rows: list[StatRow] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        rows.append(StatRow(fields[0], measure, to_float(fields[1]), unit))
    return rows
