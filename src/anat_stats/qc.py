"""Pull the per-session QC that belongs alongside the morphometry.

Nothing here computes a QC metric. `CloudpipeMetrics.anatomical_qc()` already
returns `anat_qc` ⋈ `fsqc_qc` as one per-subject×session view, with the window
semantics that took measurement to get right (filtering BOTH sides by the `dt=`
window paired 0 of 23 rows on the 2026-08-09 pilot, versus 23 of 23 with the
split the builder now uses — see `athena.anatomical_join_sql`). Re-deriving that
join here would mean re-deriving that bug.

What this module adds is one thing the shared builder does not cover: the
t1w→MNI registration record. Note what it does and does not tell you. FastSurfer
segments in native space, so a poor t1w→MNI warp does NOT corrupt any volume or
thickness in the stats tables — it is included because these volumes are
normally read next to MNI-space functional output, and a session whose
registration failed is one whose two halves do not describe the same space.

`metrics/` is APPEND-ONLY, which is the asymmetry to keep in mind when reading
this next to `aggregate.py`: S3 `derivatives/` is last-write-wins, so a
reprocessed session simply overwrites its stats and the aggregation needs no
dedupe at all. A reprocessed session's metrics, by contrast, accumulate — the
live corpus holds 962 `anat_qc` rows for 271 subject×session, up to 9 for one
scan. `anatomical_qc()` handles its own two tables; the registration rows are
deduped here, latest `completed_at` wins.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

METRICS_BUCKET = "cloudpipe-metrics"
REGION = "<YOUR_AWS_REGION>"

# Registration columns worth carrying. mask_dice and lncc are the overlap and
# local-correlation scores; verdict is the gate's own answer; the jacobian and
# inverse-consistency fields say whether the warp is well-behaved rather than
# merely well-scoring.
REGISTRATION_COLUMNS = (
    "mask_dice",
    "lncc",
    "verdict",
    "jac_det_frac_negative",
    "log_jac_std",
    "ice_p95_mm",
)


def _metrics_client(engine: str, bucket: str, region: str):
    """Build a CloudpipeMetrics for the chosen engine.

    Athena is the default for the same reason the metrics export defaults to it
    (#428): over the full corpus DuckDB reads every raw JSON object itself,
    which measured ~4 hours against Athena's ~14 minutes. Athena's catalog does
    not see partitions earlier than dt=2026-08-18, so a query that must reach
    further back needs engine="duckdb" and the wait.
    """
    if engine == "athena":
        from metrics.athena import CloudpipeMetrics
    elif engine == "duckdb":
        from metrics.duckdb_query import CloudpipeMetrics
    else:
        raise ValueError(f"unknown engine {engine!r} (expected 'athena' or 'duckdb')")
    return CloudpipeMetrics(bucket, region=region)


def latest_per_unit(frame, order_column: str = "completed_at"):
    """Collapse append-only records to one row per subject×session.

    The SQL equivalent is `ROW_NUMBER() OVER (PARTITION BY subject, session
    ORDER BY completed_at DESC)`. Done in pandas because the shared query
    methods return frames, and duplicating the window into a hand-written SQL
    string would put a second copy of the dedupe rule in the tree.
    """
    if frame.empty or order_column not in frame.columns:
        return frame
    ordered = frame.sort_values(order_column, na_position="first")
    return ordered.drop_duplicates(subset=["subject", "session"], keep="last")


def anatomical_qc(
    engine: str = "athena",
    bucket: str = METRICS_BUCKET,
    region: str = REGION,
    dt_from: str | None = None,
    dt_to: str | None = None,
):
    """One row per subject×session: anat_qc ⋈ fsqc_qc, plus t1w→MNI registration.

    Every metric is nullable, and 0.0 is a legitimate value for several of them
    (`rot_tal_*`, `n_outlier_*`, curvature means). Filter on IS NOT NULL, never
    on `> 0` — the same rule the `FsqcQC` docstring spells out. On this pipeline
    `holes_*`, `defects_*`, `topo_*` and `n_outlier_sample_*` are always null.
    """
    client = _metrics_client(engine, bucket, region)
    anatomical = client.anatomical_qc(dt_from=dt_from, dt_to=dt_to)
    log.info("anatomical_qc: %d rows", len(anatomical))

    registration = latest_per_unit(
        client.registration_qc(dt_from=dt_from, dt_to=dt_to, registration_type="t1w_to_mni")
    )
    log.info("t1w_to_mni registration_qc: %d rows after dedupe", len(registration))
    if registration.empty:
        return anatomical

    keep = ["subject", "session", *(c for c in REGISTRATION_COLUMNS if c in registration.columns)]
    renamed = registration[keep].rename(columns={c: f"t1w_mni_{c}" for c in REGISTRATION_COLUMNS})
    # LEFT, not inner: a session with morphometry and no registration record is a
    # real state (registration is skipped when its output already exists), and an
    # inner join would silently drop it from a cohort table.
    return anatomical.merge(renamed, on=["subject", "session"], how="left")
