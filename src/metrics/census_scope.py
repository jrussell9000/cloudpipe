"""Restrict an export to what the completeness census says we KEPT.

`export_batch_metrics.py` was written for test batches, where every workflow was current
by construction, so its scoping rule is "the workflow finished" — it keeps a cost row if
`workflow_runs` reports that name, and it keeps every metric record written for a subject.
On the full cohort that is too loose. 1,695 of the 13,529 workflows are repeat attempts,
so the export admits cost for workflows whose output was later replaced, and metric rows
describing objects that no longer exist in `derivatives/`.

The census applies the stricter rule. `data/census/analyze.py` walks S3 gated on
`_complete.json` (ADR 017), attributes each STORED object to the workflow that wrote it,
and counts cost only for workflows that produced stored output. This module carries that
verdict over to the export, so both answer the same question: what did the data we are
keeping cost, and what describes it?

Two sidecars, both written by `analyze.py`:

  units.parquet       one row per stored unit: subject, kind, key, metric status
  cost_scope.parquet  one row per cost row the census counts, with the day-weighted
                      `share` for rows two subjects split via a reused workflow name

A unit is accepted on `metric in ACCEPTED`. `later_workflow_same_object` belongs there:
a later workflow re-measuring the same retained object still describes that object
truthfully. `stale_or_unattributed` does not — it describes something since replaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Unit-key layouts, from analyze.py's `metric(kind, idx, key, lm)` calls. The census stores
# the key as "|".join(key[1:]) — the tuple minus the leading subject.
TABLE_KINDS: dict[str, tuple[str, list[str]]] = {
    "anat_qc": ("anat", ["session"]),
    "fsqc_qc": ("fsqc", ["session"]),
    "func_qc": ("func", ["session", "task", "run"]),
    "surface_qc": ("surf", ["session", "task", "run"]),
}

# registration_qc carries BOTH grains in one table, split by registration_type: t1w_to_mni
# is per session and leaves task/run empty, bold_to_t1w is per BOLD run.
REGISTRATION_KINDS: dict[str, tuple[str, list[str]]] = {
    "t1w_to_mni": ("reg_t1w", ["session"]),
    "bold_to_t1w": ("reg_b2t", ["session", "task", "run"]),
}

ACCEPTED = frozenset({"ok", "later_workflow_same_object"})


@dataclass
class CensusScope:
    """What the census kept: accepted units, cost rows with their split shares, and the
    workflows that produced stored output."""

    units: set[tuple[str, str, str]] = field(default_factory=set)
    cost_share: dict[tuple[str, str, str], float] = field(default_factory=dict)
    # The census's own producing set, NOT derived from cost_share: a workflow that wrote
    # stored output but has no cost row yet (its day not scraped) is still producing.
    workflows: set[str] = field(default_factory=set)

    @property
    def empty(self) -> bool:
        return not self.units and not self.cost_share

    @property
    def claims_by_wf_date(self) -> dict[tuple[str, str], list[tuple[str, float]]]:
        """(workflow_name, date) -> [(subject, share), ...] — who the census bills for
        each stored cost object.

        Keyed WITHOUT the subject, because a stored cost object's own `subject` cannot be
        trusted for attribution: CostAllocation.s3_key is (date, workflow_name), so a
        workflow name two subjects reused on one day produces ONE object, stored with
        subject="" because no single subject owns it. The census splits it by each run's
        day-weighted pod resources; this is how the export finds that split.
        """
        out: dict[tuple[str, str], list[tuple[str, float]]] = {}
        for (subj, wf, d), share in self.cost_share.items():
            out.setdefault((wf, d), []).append((subj, share))
        return out


def load_scope(census_dir: str | Path) -> CensusScope:
    """Read the census sidecars. Raises if any is absent — a silently empty scope
    would drop every row and read as 'the cohort produced nothing'."""
    import pandas as pd

    d = Path(census_dir)
    units_p = d / "units.parquet"
    cost_p = d / "cost_scope.parquet"
    prod_p = d / "producing_workflows.parquet"
    for p in (units_p, cost_p, prod_p):
        if not p.exists():
            raise FileNotFoundError(
                f"{p} not found — run data/census/analyze.py before exporting with --census-scope"
            )

    u = pd.read_parquet(units_p)
    units = {(r.subject, r.kind, r.key) for r in u[u.metric.isin(ACCEPTED)].itertuples()}

    c = pd.read_parquet(cost_p)
    cost_share = {(r.subject, r.workflow_name, str(r.date)): float(r.share) for r in c.itertuples()}

    # Its own sidecar because neither of the others can stand in for it: units.parquet
    # omits a workflow whose only output was a long-template or subregion tree (produced,
    # but not a QC unit), and cost_scope.parquet omits one that produced output but has
    # no cost row yet.
    workflows = set(pd.read_parquet(prod_p).workflow_name)
    return CensusScope(units=units, cost_share=cost_share, workflows=workflows)


def _unit_keys(df, cols: list[str]):
    """Series of "a|b|c" keys, matching the census's "|".join(key[1:])."""
    out = df[cols[0]].astype(str)
    for c in cols[1:]:
        out = out + "|" + df[c].astype(str)
    return out


def _latest_per_unit(df, key_series):
    """Keep one row per unit — the newest `completed_at`.

    A reprocess writes its record under a NEW dt partition while the old partition keeps
    the old one, and the raw read returns both. The store's S3-key overwrite cannot
    collapse them because the keys differ by dt.

    `key_series` must already identify the unit ACROSS subjects. The census stores its key
    without the subject (it lives in a neighbouring column), so grouping on that key alone
    would put every subject's ses-00A in one group and keep a single row for the whole
    cohort — 33,430 rows collapsed to 4 the first time this ran.
    """
    if "completed_at" not in df.columns:
        return df
    order = df["completed_at"].astype(str)
    idx = (
        df.assign(_k=key_series.values, _o=order.values)
        .sort_values("_o")
        .groupby("_k", sort=False)
        .tail(1)
        .index
    )
    return df.loc[sorted(idx)]


def scope_metric_frame(df, table: str, scope: CensusScope) -> tuple[Any, int]:
    """Filter one QC frame to census-accepted units, newest record per unit.

    Returns (frame, rows_dropped). A table the census does not cover is passed through.
    """
    if df is None or df.empty or scope.empty:
        return df, 0
    before = len(df)

    if table == "registration_qc":
        if "registration_type" not in df.columns:
            return df, 0
        parts = []
        for rtype, (kind, cols) in REGISTRATION_KINDS.items():
            sub = df[df["registration_type"] == rtype]
            if sub.empty or not set(cols) <= set(sub.columns):
                continue
            keys = _unit_keys(sub, cols)
            keep = [(s, kind, k) in scope.units for s, k in zip(sub["subject"], keys, strict=True)]
            sub = sub[keep]
            parts.append(_latest_per_unit(sub, _unit_keys(sub, ["subject", *cols])))
        if not parts:
            return df, 0
        import pandas as pd

        out = pd.concat(parts).sort_index()
        return out, before - len(out)

    if table not in TABLE_KINDS:
        return df, 0
    kind, cols = TABLE_KINDS[table]
    if not set(cols) <= set(df.columns) or "subject" not in df.columns:
        return df, 0
    keys = _unit_keys(df, cols)
    keep = [(s, kind, k) in scope.units for s, k in zip(df["subject"], keys, strict=True)]
    out = df[keep]
    out = _latest_per_unit(out, _unit_keys(out, ["subject", *cols]))
    return out, before - len(out)


_MONEY_COLUMNS = (
    "total_cost_usd",
    "cpu_cost_usd",
    "memory_cost_usd",
    "gpu_cost_usd",
    "total_adjustment_usd",
)


def scope_cost_frame(df, scope: CensusScope) -> tuple[Any, int, float]:
    """Keep only cost rows the census counts, attributed the way the census attributes them.

    Each stored row is matched on (workflow_name, date) and becomes one output row per
    subject the census bills for it, carrying that subject and `share` of every dollar
    column. For an ordinary row that is one subject at share 1.0 — unchanged. For a
    workflow name two subjects reused on one day it is two rows that sum to the original.

    The stored `subject` is deliberately ignored. Those shared rows are stored with
    subject="" (no single subject owns them), so matching on subject — as the first version
    did — could never reach the one case the share exists for: it silently dropped $7.56
    across four rows of the 2026-09-18 cohort export.

    Returns (frame, source_rows_dropped, usd_dropped). The frame has a fresh index,
    because expanding a row duplicates its label and downstream code masks on the index.
    """
    if df is None or df.empty or scope.empty:
        return df, 0, 0.0
    if not {"workflow_name", "date"} <= set(df.columns):
        return df, 0, 0.0

    claims = scope.claims_by_wf_date
    picks: list[tuple[Any, str, float]] = []  # (source index label, subject, share)
    dropped, dropped_usd = 0, 0.0
    usd = df["total_cost_usd"] if "total_cost_usd" in df.columns else None
    for label, wf, d in zip(df.index, df["workflow_name"], df["date"], strict=True):
        billed = claims.get((wf, str(d)))
        if not billed:
            dropped += 1
            if usd is not None:
                dropped_usd += float(usd.loc[label] or 0.0)
            continue
        picks.extend((label, subj, share) for subj, share in billed)

    out = df.loc[[label for label, _, _ in picks]].copy().reset_index(drop=True)
    if picks:
        out["subject"] = [subj for _, subj, _ in picks]
        shares = [share for _, _, share in picks]
        for col in _MONEY_COLUMNS:
            if col in out.columns:
                out[col] = out[col].astype(float).values * shares
    return out, dropped, dropped_usd


def scope_workflow_runs(df, scope: CensusScope) -> tuple[Any, int]:
    """Keep only workflows that produced stored output — drop superseded attempts."""
    if df is None or df.empty or scope.empty or "workflow_name" not in df.columns:
        return df, 0
    keep = df["workflow_name"].isin(scope.workflows)
    return df[keep], int((~keep).sum())
