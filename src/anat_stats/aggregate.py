"""Fetch, parse, and shard the cohort's anatomical stats into Parquet.

Shape of the job, measured rather than guessed (see `data/census/s3_scan.jsonl`,
2026-09-17): 11,834 subjects, 33,434 complete FastSurfer sessions, 19 `.stats`
per session plus 5 per long-template plus 6 subregion tables per session. That
is ~650k GET requests for ~4 GB of text, which parses to roughly 210 million
rows. The request charge is about $0.26, and the transfer is egress-free only
when this runs INSIDE <YOUR_AWS_REGION> — from a laptop it is ~4 GB of internet egress,
and every one of those small GETs pays a cross-country round trip. Measured from
a laptop: 96 s per ~97-subject shard, ~3.4 hours for the cohort. Run it in-region.

Either way the cost of a re-run is negligible, so the thing worth engineering is
not having to re-run the whole cohort after a crash.

Hence sharding. A subject's shard is `crc32(subject) % shards`, NOT its index in
a sorted subject list: the cohort grows, and index-based sharding would renumber
every shard the moment subject 11,835 appears, invalidating a completed run's
output. A hash keeps every existing subject in the shard it was already written
to.

Resume is "the shard's Parquet file exists". For that test to be safe the file
must never exist in a partial state, so it is written to a temporary name and
`os.replace`d into place — and the errors sidecar is made durable BEFORE that
rename. The rule is the same one ADR 017 applies in S3: the object whose
presence means "this unit is done" is written last, after everything it vouches
for.

LONG format, not wide: one row per (subject, session, source, structure,
measure). Wide is the more natural shape for an analyst, but the per-session
region set is ragged — segstats omits empty segmentations, and `--excludeid`
drops others — so a wide table would either grow a column union with holes it
cannot distinguish from real absences, or silently drop structures. Pivot to
wide at query time, where the column set is scoped to the question being asked.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3

from .discovery import (
    FMT_CURVATURE,
    FMT_FREESURFER,
    StatsFile,
    discover_subject,
)
from .parsers import (
    StatRow,
    parse_curvature_stats,
    parse_freesurfer_stats,
    parse_volume_table,
)

log = logging.getLogger(__name__)

REGION = "<YOUR_AWS_REGION>"
DEFAULT_SHARDS = 128
DEFAULT_WORKERS = 32

COLUMNS = (
    "subject",
    "session",
    "kind",
    "source",
    "structure",
    "measure",
    "value",
    "unit",
    "fs_version",
)

_LOCAL = threading.local()


def _client(s3=None):
    """This thread's S3 client, or the caller's if one was injected.

    Per-thread by the same reasoning as `images/shared/fs_derivatives.py::_client`:
    boto3 documents clients as thread-safe for API calls, but a `publish` in the
    2026-08-14 batch segfaulted inside the window where several threads open
    their first TLS connection through one shared client (issue #268). A client
    costs a few hundred microseconds to build and owns its own connection pool,
    so there is no reason to keep relying on the guarantee.
    """
    if s3 is not None:
        return s3
    client = getattr(_LOCAL, "s3", None)
    if client is None:
        client = boto3.client("s3", region_name=REGION)
        _LOCAL.s3 = client
    return client


def shard_of(subject: str, shards: int) -> int:
    """Stable shard assignment, independent of the cohort's size or ordering."""
    return zlib.crc32(subject.encode()) % shards


def plan_shards(subjects: list[str], shards: int) -> dict[int, list[str]]:
    """Group subjects by shard, dropping shards no subject hashes into."""
    plan: dict[int, list[str]] = defaultdict(list)
    for subject in subjects:
        plan[shard_of(subject, shards)].append(subject)
    return {index: sorted(members) for index, members in sorted(plan.items())}


def parse_text(stats_file: StatsFile, text: str) -> tuple[list[StatRow], str]:
    """Dispatch to the right parser, returning (rows, fs_version)."""
    if stats_file.fmt == FMT_CURVATURE:
        return parse_curvature_stats(text), ""
    if stats_file.fmt == FMT_FREESURFER:
        return parse_freesurfer_stats(text)
    return parse_volume_table(text), ""


class RowBuffer:
    """Column-oriented accumulator for one shard.

    Columnar rather than a list of dicts because a shard holds a few million
    rows and only nine columns: per-row dicts would spend more memory on keys
    than on values. The string columns are interned on the way in — `structure`
    and `measure` draw from a few thousand distinct values repeated across
    33,000 sessions, so interning collapses them to one object each instead of
    one per row.
    """

    def __init__(self) -> None:
        self.columns: dict[str, list] = {name: [] for name in COLUMNS}

    def __len__(self) -> int:
        return len(self.columns["value"])

    def add(
        self,
        subject: str,
        session: str,
        kind: str,
        source: str,
        fs_version: str,
        rows: list[StatRow],
    ) -> None:
        columns = self.columns
        subject = sys.intern(subject)
        session = sys.intern(session)
        kind = sys.intern(kind)
        source = sys.intern(source)
        fs_version = sys.intern(fs_version)
        for row in rows:
            columns["subject"].append(subject)
            columns["session"].append(session)
            columns["kind"].append(kind)
            columns["source"].append(source)
            columns["structure"].append(sys.intern(row.structure))
            columns["measure"].append(sys.intern(row.measure))
            columns["value"].append(row.value)
            columns["unit"].append(sys.intern(row.unit))
            columns["fs_version"].append(fs_version)


def _arrow_schema():
    import pyarrow as pa

    return pa.schema([(name, pa.float64() if name == "value" else pa.string()) for name in COLUMNS])


def read_subject(subject: str, bucket: str, s3=None) -> tuple[list, list[dict]]:
    """Fetch and parse every stats object for one subject.

    Returns (parsed units, errors). A unit is
    `(session, kind, source, fs_version, rows)` with `fs_version` already
    propagated: aparc, BA_exvivo, w-g.pct and curv files carry no version header
    at all (they come from `mris_anatomical_stats`, not FastSurfer's
    `segstats.py`), so the version is taken from whichever of the session's files
    does declare one. Recording it per row is not redundant with the pipeline's
    own provenance — region definitions are version-dependent, which is the same
    reason `FsqcQC` carries `fsqc_version` on every row.

    A file that fails to download or parse is recorded as an error and skipped.
    It is never turned into zero rows: a silently empty result is exactly how
    the previous aggregation script reported success while reading nothing.
    """
    client = _client(s3)
    errors: list[dict] = []
    try:
        stats_files = discover_subject(client, bucket, subject)
    except Exception as exc:  # noqa: BLE001 — one bad subject must not end the run
        return [], [{"subject": subject, "key": "", "error": repr(exc)}]

    parsed: list[tuple[StatsFile, list[StatRow], str]] = []
    for stats_file in stats_files:
        try:
            body = client.get_object(Bucket=bucket, Key=stats_file.key)["Body"].read()
            rows, fs_version = parse_text(stats_file, body.decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            errors.append({"subject": subject, "key": stats_file.key, "error": repr(exc)})
            continue
        parsed.append((stats_file, rows, fs_version))

    versions: dict[tuple[str, str], str] = {}
    for stats_file, _, fs_version in parsed:
        if fs_version:
            versions.setdefault((stats_file.kind, stats_file.session), fs_version)

    units = [
        (
            stats_file.session,
            stats_file.kind,
            stats_file.source,
            fs_version or versions.get((stats_file.kind, stats_file.session), ""),
            rows,
        )
        for stats_file, rows, fs_version in parsed
    ]
    return units, errors


def _write_atomic(path: Path, write) -> None:
    """Write via a temporary sibling and rename, so `path` is never partial."""
    tmp = path.with_name(f".{path.name}.tmp")
    write(tmp)
    os.replace(tmp, path)


def write_shard(buffer: RowBuffer, errors: list[dict], path: Path) -> None:
    """Persist one shard: errors sidecar first, then the Parquet that gates resume."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    errors_path = path.with_name(f"{path.stem}-errors.jsonl")
    if errors:
        _write_atomic(
            errors_path,
            lambda tmp: tmp.write_text("".join(json.dumps(e) + "\n" for e in errors)),
        )
    elif errors_path.exists():
        errors_path.unlink()

    table = pa.Table.from_pydict(buffer.columns, schema=_arrow_schema())
    _write_atomic(path, lambda tmp: pq.write_table(table, tmp, compression="zstd"))


def aggregate_shard(
    subjects: list[str], bucket: str, path: Path, workers: int, s3=None
) -> tuple[int, int]:
    """Read every subject in one shard and write it. Returns (rows, errors)."""
    buffer = RowBuffer()
    errors: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for subject, (units, subject_errors) in zip(
            subjects,
            pool.map(lambda s: read_subject(s, bucket, s3), subjects),
            strict=True,
        ):
            for session, kind, source, fs_version, rows in units:
                buffer.add(subject, session, kind, source, fs_version, rows)
            errors.extend(subject_errors)
    write_shard(buffer, errors, path)
    return len(buffer), len(errors)


def aggregate(
    subjects: list[str],
    bucket: str,
    out: Path,
    shards: int = DEFAULT_SHARDS,
    workers: int = DEFAULT_WORKERS,
    resume: bool = True,
    s3=None,
) -> dict[str, int]:
    """Aggregate the cohort into `out`, one Parquet file per shard.

    Shards are processed one at a time so peak memory is one shard's rows, and
    so an interrupted run leaves every completed shard usable. `resume=True`
    skips shards already on disk; pass False to force a rewrite after a parser
    change (the shard files are the only thing that would otherwise still hold
    the old interpretation).
    """
    out.mkdir(parents=True, exist_ok=True)
    plan = plan_shards(subjects, shards)
    totals = {"shards": 0, "skipped": 0, "subjects": 0, "rows": 0, "errors": 0}

    for index, members in plan.items():
        path = out / f"shard-{index:04d}.parquet"
        if resume and path.exists():
            totals["skipped"] += 1
            continue
        started = time.monotonic()
        rows, errors = aggregate_shard(members, bucket, path, workers, s3)
        totals["shards"] += 1
        totals["subjects"] += len(members)
        totals["rows"] += rows
        totals["errors"] += errors
        log.info(
            "shard %04d: %d subjects, %d rows, %d errors, %.1fs",
            index,
            len(members),
            rows,
            errors,
            time.monotonic() - started,
        )

    log.info(
        "done: %d shards written, %d skipped, %d subjects, %d rows, %d errors",
        totals["shards"],
        totals["skipped"],
        totals["subjects"],
        totals["rows"],
        totals["errors"],
    )
    return totals
