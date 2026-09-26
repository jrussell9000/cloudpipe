"""Split the long-format shards into one wide table per original stats file.

`aggregate.py` writes LONG format — one row per (subject, session, source,
structure, measure) — because the region set is ragged across sessions and a
single wide table over every file would be ~6,200 columns. This module builds
the shape an analysis actually reads: for EACH source file (`aseg.stats`,
`lh.aparc.DKTatlas.mapped.stats`, `thalamus/ThalamicNuclei.long.volumes.txt`,
...), one table with one row per subject x session and one column per
`{structure}__{measure}`.

Splitting by source is what makes a blank cell mean one thing. Two different
absences exist in this data:

    a structure the file did not report   segstats writes "Only reporting
                                          non-empty segmentations", so a small
                                          structure can be absent in one session
                                          and present in the next
    the whole file missing                e.g. sub-EBFULY5Y ses-04A has a HypVINN
                                          segmentation but no stats file

In a per-source table the second case is a MISSING ROW, so a NULL cell only
ever means the first. Nothing is zero-filled: for `Volume_mm3` an unreported
structure is effectively 0, but for `normMean` it is undefined, and that call
belongs to the analysis, not to the reshape.

Long-templates are not sessions — a template is the subject's within-subject
average, used as the longitudinal reference — so they get their own tables,
keyed by subject alone, rather than rows that a session-level analysis could
mistake for a scan.

ONE SHARD AT A TIME. The first version pivoted each source over the whole
cohort in a single DuckDB query (a window function for row positions plus one
filtered aggregate per column) and was OOM-killed on the first table on a 31 GB
machine. Pivoting each ~97-subject shard separately bounds the working set to
one shard; what accumulates is only the pivoted pieces, which are the output
itself (at most ~33,000 rows x ~800 columns per table).

Units cannot live in a wide table, so every column is described in
`columns.csv`: its table, structure, measure, unit, and how many rows carry it.
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .discovery import KIND_SUBREGION, LONG_TEMPLATE

log = logging.getLogger(__name__)

SHARD_GLOB = "shard-*.parquet"
KEY_SEPARATOR = "__"
TEMPLATE_DIR = "long-template"
DICTIONARY_NAME = "columns.csv"

# A FreeSurfer label number, identical in every session for a given structure:
# the structure name already carries it, and a column of constants per
# structure would add ~500 columns of nothing.
DROPPED_MEASURES = frozenset({"SegId"})

SCOPE_SESSION = "session"
SCOPE_TEMPLATE = "long-template"

_KEY = ["structure", "measure"]


class DuplicateKeyError(RuntimeError):
    """A (structure, measure) appears twice for one unit — a pivot would have to pick one."""


@dataclass(frozen=True)
class Column:
    """One wide column and the long-format key it came from."""

    name: str
    structure: str
    measure: str
    unit: str
    n_rows: int


@dataclass
class _Accumulator:
    """Everything one (source, scope) collects across shards."""

    kind: str
    pieces: list = field(default_factory=list)
    stats: list = field(default_factory=list)


def table_name(source: str) -> str:
    """Output filename for a source. `thalamus/X.txt` -> `thalamus__X.txt.parquet`."""
    return source.replace("/", KEY_SEPARATOR) + ".parquet"


def index_columns(scope: str) -> list[str]:
    return ["subject"] if scope == SCOPE_TEMPLATE else ["subject", "session"]


def check_unique(part, scope: str, source: str) -> None:
    """Raise if any unit carries the same (structure, measure) twice.

    Nothing in this pipeline's outputs does this today, so a hit means a parser
    or format change, and it should stop the build rather than let a pivot keep
    one of the two values.
    """
    duplicated = part.duplicated(index_columns(scope) + _KEY, keep=False)
    if duplicated.any():
        sample = part.loc[duplicated, index_columns(scope) + _KEY].head(5).to_dict("records")
        raise DuplicateKeyError(f"{source}: duplicate (structure, measure) within a unit: {sample}")


def _shard_frame(path: Path):
    """One shard as a DataFrame, with each row's position within its file.

    `pos` is the row's offset within its (subject, session, source) unit, i.e.
    its order in the original stats file: the aggregator appends a unit's rows
    contiguously and in parse order. Averaged per column across the cohort it
    gives a column order that follows the file (Left-/Right- pairs together,
    table order preserved), which alphabetical order would scatter.
    """
    import pyarrow.parquet as pq

    frame = pq.read_table(path).to_pandas()
    frame["pos"] = frame.groupby(["subject", "session", "source"], sort=False).cumcount()
    frame["scope"] = SCOPE_SESSION
    frame.loc[frame["session"] == LONG_TEMPLATE, "scope"] = SCOPE_TEMPLATE
    return frame


def _collect(part, scope: str, source: str, acc: _Accumulator) -> None:
    """Pivot one shard's rows for one (source, scope) and keep its per-column stats."""
    check_unique(part, scope, source)
    keys = index_columns(scope)
    wide = part.pivot(index=keys, columns=_KEY, values="value")
    fs_version = part.groupby(keys, sort=False)["fs_version"].first()
    acc.pieces.append((wide, fs_version))
    acc.stats.append(
        part.groupby(_KEY, sort=False).agg(
            pos_sum=("pos", "sum"),
            n_rows=("pos", "size"),
            n_nonnull=("value", "count"),
            unit=("unit", "first"),
        )
    )


def column_plan(stats, bare_names: bool) -> list[Column]:
    """The wide columns for one source, in file order.

    `stats` holds one row per (structure, measure) with pos_sum / n_rows /
    n_nonnull / unit summed across shards. Dropped: `SegId` (see
    DROPPED_MEASURES) and any column NULL in every row (the categorical
    curvature fields, e.g. `Curvature Calculation Type`).

    Named `{structure}__{measure}`. With `bare_names` — the subregion tables,
    where the parser emits exactly one measure (`Volume_mm3`) per structure BY
    CONSTRUCTION — the structure alone is used. That is decided by the kind of
    file, never inferred from how many measures survive the filtering above:
    inferring it would let a curvature table whose other columns happened to be
    all-null lose its measure names and be labelled by surface alone.
    """
    table = stats.reset_index()
    table = table[~table["measure"].isin(DROPPED_MEASURES) & (table["n_nonnull"] > 0)]
    table = table.assign(order=table["pos_sum"] / table["n_rows"])
    table = table.sort_values(["order", "structure", "measure"], kind="stable")
    return [
        Column(
            row.structure if bare_names else f"{row.structure}{KEY_SEPARATOR}{row.measure}",
            row.structure,
            row.measure,
            row.unit,
            int(row.n_rows),
        )
        for row in table.itertuples(index=False)
    ]


def _merge_stats(frames):
    import pandas as pd

    return (
        pd.concat(frames)
        .groupby(level=[0, 1], sort=False)
        .agg(
            pos_sum=("pos_sum", "sum"),
            n_rows=("n_rows", "sum"),
            n_nonnull=("n_nonnull", "sum"),
            unit=("unit", "first"),
        )
    )


def assemble(acc: _Accumulator, scope: str):
    """Stitch one source's per-shard pieces into its final table. Returns (frame, columns)."""
    import pandas as pd

    columns = column_plan(_merge_stats(acc.stats), bare_names=acc.kind == KIND_SUBREGION)
    wide = pd.concat([piece for piece, _ in acc.pieces])
    wide = wide.reindex(
        columns=pd.MultiIndex.from_tuples([(c.structure, c.measure) for c in columns])
    )
    wide.columns = [c.name for c in columns]
    fs_version = pd.concat([fs for _, fs in acc.pieces]).replace("", None)
    wide.insert(0, "fs_version", fs_version.reindex(wide.index).astype("string"))
    return wide.sort_index().reset_index(), columns


def write_table(frame, path: Path) -> None:
    """Write via a temporary sibling and rename, so `path` is never partial."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    tmp = path.with_name(f".{path.name}.tmp")
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), tmp, compression="zstd")
    tmp.replace(path)


def write_dictionary(path: Path, entries: list[dict]) -> None:
    fields = ["table", "scope", "column", "source", "structure", "measure", "unit", "n_rows"]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(entries)


def collect_shards(shard_paths: list[Path]) -> dict[tuple[str, str], _Accumulator]:
    """One pass over every shard, pivoting each (source, scope) as it goes."""
    accumulators: dict[tuple[str, str], _Accumulator] = {}
    for index, path in enumerate(shard_paths, 1):
        frame = _shard_frame(path)
        for (source, scope, kind), part in frame.groupby(["source", "scope", "kind"], sort=False):
            acc = accumulators.setdefault((source, scope), _Accumulator(kind=kind))
            _collect(part, scope, source, acc)
        if index % 16 == 0 or index == len(shard_paths):
            log.info("pivoted %d/%d shards", index, len(shard_paths))
    return accumulators


def build_wide(shards_dir: Path, out_dir: Path) -> list[dict]:
    """Build every per-source wide table from the shards in `shards_dir`.

    Session tables land in `out_dir/`, template tables in
    `out_dir/long-template/`, and `out_dir/columns.csv` describes every column.
    Returns one summary dict per table.
    """
    shard_paths = sorted(shards_dir.glob(SHARD_GLOB))
    (out_dir / TEMPLATE_DIR).mkdir(parents=True, exist_ok=True)
    accumulators = collect_shards(shard_paths)

    summaries: list[dict] = []
    dictionary: list[dict] = []
    # Session tables first, then templates; alphabetical within each.
    for (source, scope), acc in sorted(
        accumulators.items(), key=lambda i: (i[0][1] != SCOPE_SESSION, i[0][0])
    ):
        started = time.monotonic()
        frame, columns = assemble(acc, scope)
        folder = out_dir / TEMPLATE_DIR if scope == SCOPE_TEMPLATE else out_dir
        path = folder / table_name(source)
        write_table(frame, path)
        relative = str(path.relative_to(out_dir))
        summaries.append(
            {"table": relative, "scope": scope, "rows": len(frame), "columns": len(columns)}
        )
        dictionary.extend(
            {
                "table": relative,
                "scope": scope,
                "column": c.name,
                "source": source,
                "structure": c.structure,
                "measure": c.measure,
                "unit": c.unit,
                "n_rows": c.n_rows,
            }
            for c in columns
        )
        log.info(
            "%s: %d rows x %d columns, %.1fs",
            relative,
            len(frame),
            len(columns),
            time.monotonic() - started,
        )

    write_dictionary(out_dir / DICTIONARY_NAME, dictionary)
    log.info("Wrote %d tables and %s", len(summaries), out_dir / DICTIONARY_NAME)
    return summaries
