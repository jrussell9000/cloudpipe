#!/usr/bin/env python3
"""Export all metrics for a batch of subjects to CSV files.

Pulls func-preproc QC, grayordinate (surface) QC, anatomical QC (both halves —
FastSurfer's anat_qc and fsqc's fsqc_qc), registration QC, workflow-run
summaries, and cost data for a
given subject list and date window, and writes each to its own CSV. One CSV
per table, not one merged file — the tables sit at different grains (func_qc
and surface_qc are per-BOLD-run, anat_qc and fsqc_qc are per-session,
workflow_runs is per-workflow, costs is per-workflow-per-scrape-date), and
join_subject()'s own docstring explains why naively joining grains inflates
SUMs. Keeping them separate leaves aggregation to the analysis step, where
the grain is a deliberate choice rather than an accident of the export.

Usage:
    # QC scoped to a write-date window (matches the batch's actual run dates)
    pixi run python scripts/export_batch_metrics.py \\
        --subjects tools/cloudpipe_test_sample_10.csv \\
        --dt-from 2026-07-30 --dt-to 2026-07-30 \\
        --window-start 2026-07-30T01:45:00Z --window-end 2026-07-30T05:00:00Z \\
        --out-dir metrics_exports/2026-07-30_10subj

    # No date scoping: every record ever written for these subjects (careful —
    # per-scan QC accumulates duplicate dt= copies across re-runs; see
    # docs/operations.md's "Running a test batch" section)
    pixi run python scripts/export_batch_metrics.py \\
        --subjects tools/cloudpipe_test_sample_10.csv --out-dir metrics_exports/all

--dt-from/--dt-to must span the batch's start day through its finish day, not
just one of them: QC grains are partitioned by workflow start and
workflow_runs by workflow finish, so a batch crossing UTC midnight straddles
two dt= partitions. The export warns if it detects that split.

--window-start/--window-end follow the same convention as
validate_test_batch.py --cost: RFC3339 UTC bounding the batch's run time.
The cost scrape date window is derived from them (widened one day past
--window-end, since the scraper writes costs on the day *after* a workflow
runs) rather than reusing --dt-from/--dt-to directly.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def read_subjects(path: str) -> list[str]:
    with open(path, newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # skip header
        return [row[0].strip().strip('"') for row in reader if row and row[0].strip()]


def cost_date_window(
    window_start: str | None, window_end: str | None
) -> tuple[str | None, str | None]:
    """Derive the cost scrape-date range from a batch's RFC3339 run window.

    Mirrors validate_test_batch.py's _cost_date_window: the scraper writes a
    workflow's cost on the UTC day *after* it ran, so date_to is
    window_end's date plus one day. date_from is left unwidened — a scrape
    never lands before the day a workflow starts.
    """
    if not window_start or not window_end:
        return None, None
    date_from = window_start[:10]
    date_to = (date.fromisoformat(window_end[:10]) + timedelta(days=1)).isoformat()
    return date_from, date_to


def scope_to_subjects(df, subjects: list[str]):
    if df.empty or "subject" not in df.columns:
        return df
    return df[df["subject"].isin(subjects)]


# Grains scoped by --dt-from/--dt-to. They do not all derive dt from the same
# event, which is what grain_asymmetry_warning() exists to catch.
#
# surface_qc is dt-scoped too but deliberately NOT listed. It takes dt from the
# same {{workflow.creationTimestamp}} as func_qc, so it adds no detection power
# to the midnight-split check — while adding a false-positive mode this list has
# no way to express: a batch whose surface-resample step failed broadly is a
# real coverage gap, not a partition split, and the warning only knows how to
# report the latter.
DT_SCOPED_GRAINS = ("func_qc", "anat_qc", "fsqc_qc", "registration_qc", "workflow_runs")


# A grain covering fewer than this fraction of the best-covered grain's
# subjects is reported as a partition split rather than as real absence.
#
# 0.9 tolerates the legitimate reasons one grain covers fewer subjects than
# another — a subject with no BOLD in the requested scan types is missing from
# func_qc, one whose FastSurfer was reused is missing from anat_qc — while still
# catching a split. It is deliberately far from the observed failures: the
# 2026-08-10 200-subject batch put 15 of 200 workflows (7.5%) on the earlier dt,
# and the 2026-08-05 pilot put 0 of 100 (0%) there.
PARTIAL_SPLIT_COVERAGE_RATIO = 0.9


def grain_asymmetry_warning(
    counts: dict[str, int], subject_counts: dict[str, int] | None = None
) -> str | None:
    """Warn when dt-scoped grains disagree about which subjects the batch had.

    The QC grains take dt from {{workflow.creationTimestamp}}, templated into
    the artifact key in the WorkflowTemplate YAML — workflow *start*.
    workflow_runs takes it from _today_utc() at write time — workflow
    *finish*. A batch crossing UTC midnight therefore lands its QC and its
    run summaries in different dt= partitions, and a single-day window
    returns one or the other with no error. That asymmetry is always this
    bug; every-grain-empty is a different problem (wrong subjects or window),
    so it is deliberately not flagged here.

    Row counts alone catch this only when the split is total. They missed the
    2026-08-10 200-subject batch entirely: 15 of its 200 workflows finished
    before UTC midnight, so a single-day window returned workflow_runs at 15
    rows — not 0, and therefore not "empty" — losing 92.5% of the grain in
    silence. Subject coverage is what makes a partial split visible, since
    every dt-scoped grain should see very nearly the same subject list.
    """
    present = {name: counts[name] for name in DT_SCOPED_GRAINS if name in counts}
    if not present or not any(present.values()):
        return None
    coverage = {name: n for name, n in (subject_counts or {}).items() if name in present}
    best = max(coverage.values(), default=0)

    empty = sorted(name for name, n in present.items() if n == 0)
    partial = sorted(
        name
        for name, n in present.items()
        if n and best and coverage.get(name, best) < best * PARTIAL_SPLIT_COVERAGE_RATIO
    )
    if not empty and not partial:
        return None
    short = set(empty) | set(partial)
    populated = ", ".join(
        f"{name} {n}"
        for name, n in sorted(present.items(), key=lambda kv: -kv[1])
        if n and name not in short
    )
    if not populated:  # nothing left to contrast against; not a diagnosable split
        return None

    clauses = []
    if empty:
        clauses.append(f"{', '.join(empty)} returned 0 rows")
    if partial:
        clauses.append(
            ", ".join(
                f"{name} covered only {coverage[name]} of {best} subjects" for name in partial
            )
        )
    return (
        f"WARNING: dt partition asymmetry — {' and '.join(clauses)} while "
        f"{populated} returned data.\n"
        "  QC grains partition dt on workflow START; workflow_runs partitions on "
        "workflow FINISH.\n"
        "  A batch crossing UTC midnight splits across two dt= partitions. Widen "
        "--dt-from/--dt-to\n"
        "  to span the batch's start day through its finish day.\n"
        "  costs_raw and costs_by_subject are scoped to the workflows workflow_runs "
        "returned, so\n"
        "  a short workflow_runs silently truncates them too — do not read the cost "
        "totals below."
    )


def anatomical_pairing_warning(counts: dict[str, int]) -> str | None:
    """Explain an anat_qc/fsqc_qc split that is NOT the midnight-partition bug.

    The two T1w-derived grains are exported as a pair, but only one of them is
    written unconditionally. `fsqc-metrics` runs on every workflow (its spec
    forbids a skip-on-existing-output gate), while `anat_qc` is written only
    when FastSurfer itself runs — a batch reprocessing subjects whose
    derivatives already exist skips FastSurfer entirely and writes no anat_qc
    row at all. That is the expected outcome of a reused-derivative batch, not
    a defect, and grain_asymmetry_warning() would otherwise attribute it to a
    UTC-midnight partition split and send the operator chasing the wrong thing.
    """
    anat, fsqc = counts.get("anat_qc"), counts.get("fsqc_qc")
    if not fsqc or anat is None or anat > 0:
        return None
    return (
        f"NOTE: fsqc_qc returned {fsqc} rows while anat_qc returned 0.\n"
        "  This is expected when FastSurfer derivatives already existed: fsqc-metrics runs "
        "on every\n"
        "  workflow, FastSurfer (and so anat_qc) only when it actually reprocesses. The "
        "anat_qc rows\n"
        "  for these subjects are under the dt= of their ORIGINAL run — widen --dt-from to "
        "reach them,\n"
        "  or query them unscoped."
    )


def cost_coverage_warning(
    cost_dates: set[str], window_start: str | None, window_end: str | None
) -> str | None:
    """Warn when costs_raw is missing UTC days the batch actually spanned.

    A partition that has not been scraped yet is indistinguishable from one
    with no cost: Athena reports absence as zero rows, not as an error. So a
    batch whose later (and typically more expensive) hours have not been
    scraped exports a cost total that is silently low.
    """
    if not window_start or not window_end:
        return None
    first, last = date.fromisoformat(window_start[:10]), date.fromisoformat(window_end[:10])
    spanned = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    missing = [d.isoformat() for d in spanned if d.isoformat() not in cost_dates]
    if not missing:
        return None
    return (
        f"WARNING: cost coverage gap — costs_raw has no rows for {', '.join(missing)} "
        f"({len(missing)} of {len(spanned)} UTC days spanned by the batch window).\n"
        "  Cost totals below cover only part of the batch and UNDERSTATE it. The scraper\n"
        "  writes a day's costs at 02:00Z the following day; re-run the export after that."
    )


# Kubecost reconciliation is unstable for the first ~2 days and freezes at
# age 3 (measured 2026-08-05, issue #171). Anything younger is provisional.
SETTLED_SCRAPE_AGE_DAYS = 3


def stale_cost_warning(scrape_ages: list[int]) -> str | None:
    """Warn when any exported cost record predates reconciliation freeze.

    A day+1 read is not merely imprecise, it is biased: it overstates the
    settled total by a median ~51% (8-240% across 10 measured dates, #171)
    and is *rewritten in place* by the age-3 re-scrape. So a total computed
    now does not converge on the settled figure, it gets replaced by one
    that can be less than half of it. The spread is too wide to correct
    with a constant factor, which is why this warns rather than adjusts.
    """
    unsettled = [a for a in scrape_ages if a < SETTLED_SCRAPE_AGE_DAYS]
    if not unsettled:
        return None
    return (
        f"WARNING: unsettled cost records — {len(unsettled)} of {len(scrape_ages)} rows have "
        f"scrape_age_days < {SETTLED_SCRAPE_AGE_DAYS} (youngest {min(unsettled)}).\n"
        "  Kubecost reconciliation freezes at age 3; younger reads overstate settled cost by\n"
        "  a median ~51% (8-240%) and are rewritten by the age-3 re-scrape. Re-export then."
    )


def cross_batch_warning(foreign: set[str], foreign_rows: int, foreign_usd: float) -> str | None:
    """Warn that cost rows from other batches shared this batch's subjects.

    The cost prefix is partitioned by usage day, not by batch, so a previous
    batch whose tail ran into the same UTC day lands in the same partition.
    When both batches use the same subject list — the norm for repeated test
    batches — subject scoping cannot separate them; only workflow_name can.
    """
    if not foreign:
        return None
    sample = ", ".join(sorted(foreign)[:3])
    more = f", +{len(foreign) - 3} more" if len(foreign) > 3 else ""
    return (
        f"NOTE: dropped {foreign_rows} cost row(s) from {len(foreign)} workflow(s) outside this "
        f"batch (${foreign_usd:.2f}).\n"
        f"  These share the batch's subjects but not its workflows ({sample}{more}) — an "
        "adjacent batch\n"
        "  whose run bled into the same UTC day. costs_raw and costs_by_subject exclude them."
    )


def scope_costs_to_workflows(df, batch_workflows: set[str]):
    """Restrict cost rows to the batch's own workflows.

    Returns (scoped_df, foreign_workflow_names). A no-op when the batch's
    workflow set is unknown (workflow_runs empty or failed) — dropping every
    row because the *other* grain came back empty would turn one silent
    wrong answer into a louder one.
    """
    if not batch_workflows or df.empty or "workflow_name" not in df.columns:
        return df, set()
    in_batch = df["workflow_name"].isin(batch_workflows)
    return df[in_batch], set(df.loc[~in_batch, "workflow_name"].unique())


class CostScope(NamedTuple):
    """A batch-scoped costs frame plus everything the cost warnings need."""

    frame: Any
    dates: set[str]
    scrape_ages: list[int]
    foreign: set[str]
    foreign_rows: int
    foreign_usd: float


def analyze_costs(df, batch_workflows: set[str]) -> CostScope:
    """Scope a costs frame to the batch and extract its coverage signals."""
    scoped, foreign = scope_costs_to_workflows(df, batch_workflows)
    dropped = df[~df.index.isin(scoped.index)]
    return CostScope(
        frame=scoped,
        dates=(
            {str(v)[:10] for v in scoped["date"].dropna().unique()}
            if "date" in scoped.columns
            else set()
        ),
        scrape_ages=(
            [int(a) for a in scoped["scrape_age_days"].dropna()]
            if "scrape_age_days" in scoped.columns
            else []
        ),
        foreign=foreign,
        foreign_rows=len(dropped),
        foreign_usd=float(dropped["total_cost_usd"].sum()) if len(dropped) else 0.0,
    )


def summarize_subject_costs(df):
    """Per-subject cost totals from an already-scoped costs frame.

    Mirrors CloudpipeMetrics.subject_costs() column for column. Computed here
    rather than by that method because it re-queries S3 with subject and date
    filters only — it has no workflow filter, so it cannot honour the
    workflow scoping applied above, and would otherwise contradict
    costs_raw.csv sitting beside it.
    """
    grouped = df.groupby("subject", as_index=False).agg(
        n_records=("workflow_name", "size"),
        n_workflows=("workflow_name", "nunique"),
        total_cost_usd=("total_cost_usd", "sum"),
        cpu_cost_usd=("cpu_cost_usd", "sum"),
        memory_cost_usd=("memory_cost_usd", "sum"),
        gpu_cost_usd=("gpu_cost_usd", "sum"),
        total_adjustment_usd=("total_adjustment_usd", "sum"),
        max_scrape_age_days=("scrape_age_days", "max"),
        first_date=("date", "min"),
        last_date=("date", "max"),
    )
    return grouped.sort_values("total_cost_usd", ascending=False)


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--subjects", required=True, help="Path to batch subjects CSV (subject_id column)."
    )
    p.add_argument(
        "--out-dir", required=True, help="Directory to write CSVs into (created if absent)."
    )
    p.add_argument("--bucket", default="cloudpipe-metrics", help="Metrics bucket.")
    p.add_argument("--region", default="<YOUR_AWS_REGION>")
    p.add_argument(
        "--engine",
        choices=["duckdb", "athena"],
        default="duckdb",
        help="Query backend. duckdb reads S3 JSON directly (no billing, scans every "
        "matched file). athena queries the Glue-cataloged tables (partition-pruned, "
        "billed per query) — prefer it for wide date ranges over the full cohort.",
    )
    p.add_argument("--dt-from", help="Inclusive YYYY-MM-DD lower bound on QC/workflow write date.")
    p.add_argument("--dt-to", help="Inclusive YYYY-MM-DD upper bound on QC/workflow write date.")
    p.add_argument(
        "--window-start", help="RFC3339 UTC batch start, for cost scoping (see docstring)."
    )
    p.add_argument("--window-end", help="RFC3339 UTC batch end, for cost scoping (see docstring).")
    args = p.parse_args()

    if args.engine == "duckdb":
        from metrics.duckdb_query import CloudpipeMetrics
    else:
        from metrics.athena import CloudpipeMetrics

    subjects = read_subjects(args.subjects)
    if not subjects:
        print(f"Error: no subjects found in {args.subjects}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== cloudpipe metrics export ({args.engine}) ===")
    print(f"Subjects   : {len(subjects)} from {args.subjects}")
    print(f"QC window  : dt {args.dt_from or '(none)'} .. {args.dt_to or '(none)'}")
    date_from, date_to = cost_date_window(args.window_start, args.window_end)
    print(f"Cost window: date {date_from or '(none)'} .. {date_to or '(none)'}")
    print(f"Out dir    : {out_dir}\n")

    m = CloudpipeMetrics(bucket=args.bucket, region=args.region)

    # Order matters: costs_raw is scoped to the workflow names workflow_runs
    # returns, so workflow_runs must be fetched first.
    tables = {
        "func_qc": lambda: m.func_qc(dt_from=args.dt_from, dt_to=args.dt_to),
        # Grayordinate QC, same per-BOLD-run grain as func_qc and overlapping it
        # in field names — exported separately because it is the only copy for
        # runs processed by preproc.py's `--emit grayordinate` short path, which
        # leaves func_qc untouched. Read `emit` to tell those rows apart. Expect
        # FEWER rows than func_qc: only runs that produced surfaces appear.
        "surface_qc": lambda: m.surface_qc(dt_from=args.dt_from, dt_to=args.dt_to),
        # anat_qc and fsqc_qc are the two halves of the T1w-derived QC, at the
        # same subject×session grain. Exported as two CSVs rather than one
        # joined file for the reason in the module docstring — but unlike the
        # other pairs here they CAN be joined safely, on subject+session; see
        # CloudpipeMetrics.anatomical_qc() for the view that does it.
        "anat_qc": lambda: m.anat_qc(dt_from=args.dt_from, dt_to=args.dt_to),
        "fsqc_qc": lambda: m.fsqc_qc(dt_from=args.dt_from, dt_to=args.dt_to),
        "registration_qc": lambda: m.registration_qc(dt_from=args.dt_from, dt_to=args.dt_to),
        "workflow_runs": lambda: m.workflow_runs(dt_from=args.dt_from, dt_to=args.dt_to),
        "costs_raw": lambda: m.costs(dt_from=date_from, dt_to=date_to),
    }

    counts: dict[str, int] = {}
    subject_counts: dict[str, int] = {}
    batch_workflows: set[str] = set()
    cost = CostScope(None, set(), [], set(), 0, 0.0)

    for name, fetch in tables.items():
        try:
            df = fetch()
        except Exception as exc:  # noqa: BLE001 - report and continue with other tables
            print(f"  [SKIP] {name}: {exc}")
            continue
        df = scope_to_subjects(df, subjects)
        if name == "workflow_runs" and "workflow_name" in df.columns:
            batch_workflows = set(df["workflow_name"].dropna().unique())
        if name == "costs_raw":
            cost = analyze_costs(df, batch_workflows)
            df = cost.frame
        out_path = out_dir / f"{name}.csv"
        df.to_csv(out_path, index=False)
        covered = df["subject"].nunique() if "subject" in df.columns else "n/a"
        print(f"  {name:<18} {len(df):>5} rows, {covered} subjects -> {out_path}")
        counts[name] = len(df)
        if isinstance(covered, int):
            subject_counts[name] = covered

    # Per-subject totals, derived from the scoped frame above so the two cost
    # CSVs cannot disagree; subject_costs() is the fallback when costs_raw
    # itself failed to load.
    try:
        if cost.frame is not None and not cost.frame.empty:
            subj_costs = summarize_subject_costs(cost.frame)
        else:
            subj_costs = m.subject_costs(subjects=subjects, date_from=date_from, date_to=date_to)
        out_path = out_dir / "costs_by_subject.csv"
        subj_costs.to_csv(out_path, index=False)
        print(f"  {'costs_by_subject':<18} {len(subj_costs):>5} rows -> {out_path}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [SKIP] costs_by_subject: {exc}")

    # Printed last, not inline with the tables, so they are the final thing on
    # screen — every defect here is one an operator can otherwise scroll past.
    warnings = [
        w
        for w in (
            grain_asymmetry_warning(counts, subject_counts),
            anatomical_pairing_warning(counts),
            cost_coverage_warning(cost.dates, args.window_start, args.window_end),
            stale_cost_warning(cost.scrape_ages),
            cross_batch_warning(cost.foreign, cost.foreign_rows, cost.foreign_usd),
        )
        if w
    ]
    for warning in warnings:
        print(f"\n{warning}", file=sys.stderr)

    print(f"\n=== Export complete{f' ({len(warnings)} warning(s) above)' if warnings else ''}. ===")


if __name__ == "__main__":
    main()
