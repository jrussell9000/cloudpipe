"""
compactor.py — compacts one day's raw JSON metrics into per-schema_version
Parquet, for the nightly `metrics-compactor` Prefect flow.

Schema-agnostic by design: records are read as plain dicts and never round-
tripped through the schemas.py dataclasses. Some historical records don't
satisfy the current dataclass shape (e.g. schema-1.0 `costs` records lack
`workflow_name`, a required field on CostAllocation with no default), so
`CostAllocation.from_dict()` would raise on them. Grouping by the raw
`schema_version` string and building a pyarrow.Table straight from each
group's dicts sidesteps that entirely — pyarrow infers each Parquet file's
columns from whatever fields that schema_version's records actually have.

Idempotent by construction: the output S3 key is fully determined by
(table_name, dt, schema_version), and there is exactly one file per group
(part-0000.parquet, no sharding), so every re-run overwrites the same key
set. This is what lets compaction run with no delete IAM grant — see
docs/observability.md's compaction section. Known limitation: if any one
group's daily record count later makes a single Parquet file unwieldy,
sharding would need an explicit list-and-overwrite-full-shard-set step to
preserve this idempotency property; not needed at current corpus scale.

Per-key idempotency is not per-*partition-set* idempotency, though. A `dt`
whose raw records get re-emitted under a different schema_version leaves the
old schema_version= partition behind with nobody to overwrite it, and the
Glue table unions every schema_version under a `dt=`, so that date
double-counts (GitHub #180: `costs` dt=2026-07-29 reported $7.83 against a
raw truth of $2.755 after a 1.1 -> 1.2 bump). `repair_orphan_partitions`
closes that: raw JSON is authoritative, so a schema_version absent from raw
for a `dt` genuinely has zero rows there, and its compacted partition is
overwritten in place with a zero-row Parquet of its own schema. That is a
PutObject, not a DeleteObject — the orphan key survives as a harmless empty
file, so the "nothing may delete from the run-of-record bucket" stance in
terraform/metrics_bucket.tf is preserved and no new IAM grant is needed.

Repair is deliberately skipped whenever the evidence for it is incomplete
(any unreadable raw key, or a `dt` with no raw keys at all) — leaving a
stale partition in place is visible and fixable, whereas emptying a live one
on a transient S3 error silently destroys a day.
"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

# table_name (Glue-table form) -> raw S3 prefix. Compacted output for each
# table is written under metrics/compacted/{table_name}/ rather than the
# hyphenated raw prefix, so the compacted tree's directory names already
# match their eventual Glue table names one-for-one.
RAW_PREFIXES = {
    "func_preproc": "metrics/func-preproc/",
    "surface_sample": "metrics/surface-sample/",
    "anat_qc": "metrics/anat-qc/",
    "fsqc_qc": "metrics/fsqc-qc/",
    "workflow_runs": "metrics/workflow-runs/",
    "step_outcomes": "metrics/step-outcomes/",
    "subject_manifests": "metrics/subject-manifests/",
    "registration": "metrics/registration/",
    "costs": "metrics/costs/",
    "pod_costs": "metrics/pod-costs/",
}


def list_raw_keys(s3_client, bucket: str, prefix: str, dt: str) -> list[str]:
    """List every object key under {prefix}dt={dt}/."""
    paginator = s3_client.get_paginator("list_objects_v2")
    full_prefix = f"{prefix}dt={dt}/"
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=full_prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def read_raw_records(s3_client, bucket: str, keys: list[str]) -> list[dict]:
    """get_object + json.loads for each key, one or many records per key.

    Most schemas.py s3_key()s address exactly one JSON object, but PodCost
    writes all of a workflow's pods to a single key as newline-delimited JSON
    (see writer.emit_jsonl_to_s3). A whole-body parse is tried first so the
    one-object case is unchanged; only if that fails do we fall back to
    parsing line by line.

    A single malformed or unreadable key is logged and skipped, not raised —
    one bad object must not abort compaction of the whole day. Within a JSONL
    key the same applies per line: one bad line does not discard the other
    pods in that workflow.
    """
    records, _ = read_raw_records_counting_skips(s3_client, bucket, keys)
    return records


def read_raw_records_counting_skips(
    s3_client, bucket: str, keys: list[str]
) -> tuple[list[dict], int]:
    """read_raw_records, plus how many keys were skipped as unreadable.

    The skip count exists for `repair_orphan_partitions`' benefit: skipping a
    key makes the parsed record set an *under*-estimate of what raw actually
    holds, and repair infers "this schema_version has no rows for this dt"
    from exactly that set. If a transient GetObject failure dropped every key
    of one version, a repair pass would empty a partition that is in fact
    live. Callers that mutate compacted state on the basis of absence must
    check this and stand down when it is non-zero.
    """
    records = []
    skipped = 0
    for key in keys:
        try:
            body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as exc:
            log.warning("Skipping unreadable record %s: %s", key, exc)
            skipped += 1
            continue

        try:
            records.append(json.loads(body))
        except json.JSONDecodeError:
            parsed = _parse_jsonl(body, key)
            if not parsed:
                skipped += 1
            records.extend(parsed)
    return records, skipped


def _parse_jsonl(body: bytes, key: str) -> list[dict]:
    """Parse a newline-delimited JSON body, skipping unparseable lines."""
    parsed = []
    for lineno, line in enumerate(body.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError as exc:
            log.warning("Skipping malformed line %d in %s: %s", lineno, key, exc)
    if not parsed:
        log.warning("Skipping unparseable record %s: neither JSON nor JSONL", key)
    return parsed


def group_by_schema_version(records: list[dict]) -> dict[str, list[dict]]:
    """Group raw dicts by their 'schema_version' field.

    Records missing the field entirely go under the literal "unknown" bucket
    rather than being dropped, so compaction never silently loses
    non-conforming records.
    """
    groups: dict[str, list[dict]] = {}
    for rec in records:
        version = rec.get("schema_version") or "unknown"
        groups.setdefault(version, []).append(rec)
    return groups


def build_table(records: list[dict]):
    """Build a pyarrow.Table from one schema_version's records.

    No explicit pyarrow.schema(...) is constructed by hand — pyarrow's own
    type inference from the dict list is trusted, since callers only ever
    pass one schema_version's records (from group_by_schema_version), so the
    inferred column set IS that version's column set.
    """
    import pyarrow as pa

    return pa.Table.from_pylist(records)


def write_parquet_to_s3(
    s3_client, table, bucket: str, key: str, if_match: str | None = None
) -> None:
    """Serialize `table` to Parquet in memory and put_object it to S3.

    Deliberately reuses the plain put_object idiom from writer.py::emit_to_s3
    (no pyarrow.fs.S3FileSystem) so there's one S3-write path in src/metrics/.
    `if_match` makes the write conditional on the object still carrying that
    ETag, for a read-modify-write that must not clobber a concurrent rewrite.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    body = sink.getvalue().to_pybytes()
    conditional = {"IfMatch": if_match} if if_match else {}
    s3_client.put_object(
        Bucket=bucket, Key=key, Body=body, ContentType="application/octet-stream", **conditional
    )
    log.info("Wrote compacted parquet: s3://%s/%s (%d rows)", bucket, key, table.num_rows)


def compacted_key(table_name: str, dt: str, schema_version: str) -> str:
    return (
        f"metrics/compacted/{table_name}/dt={dt}/schema_version={schema_version}/part-0000.parquet"
    )


_SCHEMA_VERSION_RE = re.compile(r"/schema_version=([^/]+)/")


def list_compacted_schema_versions(s3_client, bucket: str, table_name: str, dt: str) -> set[str]:
    """The schema_version= partitions that already exist for one (table, dt).

    Parsed out of the listed keys rather than fetched with Delimiter=
    CommonPrefixes, so a partition directory that somehow holds a file other
    than part-0000.parquet still registers as present. The Prefix already
    pins table and dt, so the only schema_version= segment in a listed key is
    the one being read.
    """
    paginator = s3_client.get_paginator("list_objects_v2")
    prefix = f"metrics/compacted/{table_name}/dt={dt}/"
    versions = set()
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            match = _SCHEMA_VERSION_RE.search(obj["Key"])
            if match:
                versions.add(match.group(1))
    return versions


def _read_parquet_schema(s3_client, bucket: str, key: str):
    """Read just the Parquet footer schema of an existing compacted file."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    return pq.read_schema(pa.BufferReader(body))


def repair_orphan_partitions(
    s3_client, bucket: str, table_name: str, dt: str, written_versions: set[str]
) -> list[str]:
    """Neutralize compacted schema_version= partitions raw no longer backs.

    Raw JSON is the authority for what a `dt` contains. A schema_version
    present under `metrics/compacted/{table}/dt={dt}/` but absent from
    `written_versions` (the versions this compaction pass just derived from
    raw) therefore has zero rows for that date, and the Glue table would
    otherwise keep unioning its stale rows in — see GitHub #180.

    Each orphan is overwritten with a zero-row Parquet carrying its own
    original schema, read back from its footer. Preserving the schema matters:
    the `{table}_compacted` Glue table declares the superset of columns across
    versions and matches by name, so an empty file with the right columns
    reads as "no rows" while one with no columns at all risks a schema-mismatch
    read error. Returns the keys neutralized.
    """
    existing = list_compacted_schema_versions(s3_client, bucket, table_name, dt)
    orphans = sorted(existing - written_versions)
    neutralized = []
    for version in orphans:
        key = compacted_key(table_name, dt, version)
        try:
            schema = _read_parquet_schema(s3_client, bucket, key)
        except Exception as exc:
            # Can't establish the column set, so can't write a compatible
            # empty file. Log loudly and leave it: a visible double-count
            # beats an unreadable partition that breaks the whole table.
            log.error(
                "Orphan compacted partition %s for %s dt=%s is unreadable (%s) — "
                "left in place, this dt may double-count; see GitHub #180.",
                key,
                table_name,
                dt,
                exc,
            )
            continue

        write_parquet_to_s3(s3_client, schema.empty_table(), bucket, key)
        neutralized.append(key)
        log.warning(
            "Neutralized orphan compacted partition %s: schema_version=%s no longer "
            "present in raw for %s dt=%s (emptied, not deleted).",
            key,
            version,
            table_name,
            dt,
        )
    return neutralized


class UnattributableRows(Exception):
    """A compacted file without the column to match on, so no row can be matched.

    Its own class, not ValueError: pyarrow's ArrowInvalid (a corrupt file) IS
    a ValueError, and a caller has to tell "nothing to match on" from "broken".
    """


_KEY_SEP = "\x1f"  # ASCII unit separator: cannot occur in an ID, a timestamp, or a task name


def composite_key(parts) -> str:
    """The string drop_rows compares a multi-column row identity by."""
    return _KEY_SEP.join(parts)


def drop_rows(
    s3_client,
    bucket: str,
    key: str,
    column: str | tuple[str, ...],
    values,
    write: bool = True,
    *,
    complement: bool = False,
) -> int:
    """Remove every row whose `column` is in `values` — or, with `complement`,
    NOT in `values` — from one compacted Parquet object, in place. Returns the
    rows removed (with `write=False`, the rows that would be). A null in
    `column` is never removed: it matches nothing, either way round.

    `column` may be a tuple of columns, for tables whose rows carry no single
    identifying column (the per-scan QC tables: a record is a scan plus its
    `completed_at`). `values` are then tuples in the same order, and a row
    matches only when every part matches. A null in ANY part makes the row's
    identity null, so it is never removed — the same rule as the single column.

    This is how a raw flush reaches compacted (GitHub #382). The compactor
    alone never gets there: it rebuilds only the last few `dt`s from raw, so a
    record flushed from an older day stays in `*_compacted` while raw — the
    authority, see the module docstring — no longer has it, and the two tables
    disagree about that history indefinitely. prep_test_batch drops the flushed
    subjects; reconcile_compacted_with_raw.py keeps only the workflows raw
    still holds (`complement`).

    Same stance as repair_orphan_partitions: a PutObject rewrite, never a
    delete, and the file keeps its own schema even when every row goes, so an
    emptied partition reads as "no rows". The rewrite is conditional on the
    ETag read, so a compactor run that rewrote the object in between fails
    this write instead of being clobbered by a filtered copy of the old file.

    Raises UnattributableRows for a file without `column`: its rows cannot be
    attributed, and guessing would be deleting.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    obj = s3_client.get_object(Bucket=bucket, Key=key)
    table = pq.read_table(pa.BufferReader(obj["Body"].read()))
    columns = (column,) if isinstance(column, str) else tuple(column)
    missing = [c for c in columns if c not in table.column_names]
    if missing:
        raise UnattributableRows(
            f"{key} has no {', '.join(missing)} column, so its rows cannot be attributed"
        )

    # Cast first: a column that is null on every row infers the `null` type,
    # which is_in cannot compare against strings. is_in answers False (not
    # null) for a null input, so inverting it would sweep every null row into
    # `complement`'s drop set — the explicit is_valid is what keeps them.
    # binary_join_element_wise emits null when any part is null, so a composite
    # identity inherits that rule unchanged.
    parts = [table[c].cast(pa.string()) for c in columns]
    if len(parts) == 1:
        col, wanted = parts[0], sorted(values)
    else:
        col = pc.binary_join_element_wise(*parts, _KEY_SEP)
        wanted = sorted(composite_key(v) for v in values)
    hit = pc.is_in(col, value_set=pa.array(wanted, type=pa.string()))
    if complement:
        hit = pc.invert(hit)
    hit = pc.and_(hit, pc.is_valid(col))
    dropped = pc.sum(hit).as_py() or 0
    if dropped and write:
        write_parquet_to_s3(
            s3_client, table.filter(pc.invert(hit)), bucket, key, if_match=obj["ETag"]
        )
    return dropped


def compact_prefix_dt(
    s3_client, bucket: str, region: str, table_name: str, dt: str
) -> dict[str, int]:
    """Compact one (table_name, dt): list -> read -> group -> write one
    Parquet file per schema_version present, then neutralize any compacted
    schema_version= partition raw no longer backs. Returns
    {schema_version: count}.
    """
    prefix = RAW_PREFIXES[table_name]
    keys = list_raw_keys(s3_client, bucket, prefix, dt)
    if not keys:
        # No repair pass here on purpose. "Raw is empty" is indistinguishable
        # from "raw hasn't been written/has been flushed", and the metrics
        # corpus IS periodically flushed per test batch, so treating an empty
        # listing as authoritative absence would empty every compacted
        # partition for the day.
        log.info("No raw records for %s dt=%s, nothing to compact.", table_name, dt)
        return {}

    records, skipped = read_raw_records_counting_skips(s3_client, bucket, keys)
    groups = group_by_schema_version(records)

    counts = {}
    for version, group_records in groups.items():
        table = build_table(group_records)
        key = compacted_key(table_name, dt, version)
        write_parquet_to_s3(s3_client, table, bucket, key)
        counts[version] = len(group_records)

    if skipped:
        log.warning(
            "Skipping orphan-partition repair for %s dt=%s: %d of %d raw keys were "
            "unreadable, so absence of a schema_version can't be trusted.",
            table_name,
            dt,
            skipped,
            len(keys),
        )
    else:
        repair_orphan_partitions(s3_client, bucket, table_name, dt, set(groups))
    return counts


def compact_date(
    s3_client, bucket: str, region: str, dt: str, tables: list[str] | None = None
) -> dict[str, dict[str, int]]:
    """Compact every table in `tables` (default: all RAW_PREFIXES) for one
    dt. Flow-facing entry point."""
    table_names = tables if tables is not None else list(RAW_PREFIXES)
    return {name: compact_prefix_dt(s3_client, bucket, region, name, dt) for name in table_names}
