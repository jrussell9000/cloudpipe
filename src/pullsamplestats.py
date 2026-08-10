"""Descriptive statistics for the cloudpipe test batch.

Run with:  pixi run python src/pullsamplestats.py [--window-start YYYY-MM-DD]
                                                  [--window-end   YYYY-MM-DD]

Pulls each metric table via the DuckDB query helper (no Athena billing), scopes
every table to the batch subject list, and prints .describe() per metric group.

Only fields that live emitters actually write are reported. RegistrationQC in
src/metrics/schemas.py carries several fields that are retained purely so older
records still deserialize, and describing them yields columns of defaults:
  - t1w_to_mni (schema 2.0) emits mask_dice / lncc; `dice` is NOT written
  - bold_to_t1w emits `nmi` as its ONLY real quality metric (schema 2.1
    normalized MI; superseded raw `mi`). `dice` is hardcoded 0.0, and bbr_cost /
    bbr_converged / bbr_init_used went dead when BBR was dropped in 61ccff7
    (ABCD 2.4 mm EPI lacks the contrast bbregister needs). No `ncc` field exists.

Costs are read via subject_costs(), which sums the per-(workflow, scrape-date)
grain into one row per subject. Averaging the raw cost rows instead skews the
mean toward subjects that were retried or spanned multiple scrape days. Pass the
batch's scrape-date window — metrics/costs/ accumulates across batches, and a
bare call mixes them.

Caveat on cost magnitudes: a day+1 Kubecost scrape substantially overstates
settled billed cost. Treat these as relative comparisons within a batch, not as
billed figures.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import pandas as pd

from metrics.duckdb_query import CloudpipeMetrics

BUCKET = "<YOUR_S3_BUCKET>"
SUBJECTS_CSV = Path(__file__).resolve().parents[1] / "tools" / "cloudpipe_test_sample.csv"

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 20)


def load_batch_subjects(csv_path: Path) -> set[str]:
    """Return the set of `sub-XXXX` IDs in the batch CSV (header: subject_id)."""
    with csv_path.open(newline="") as fh:
        return {row["subject_id"].strip() for row in csv.DictReader(fh) if row.get("subject_id")}


def scope(df: pd.DataFrame, subjects: set[str]) -> pd.DataFrame:
    """Restrict a metrics DataFrame to the batch subjects.

    These prefixes are flushed per subject by prep_test_batch, so this is
    normally a no-op; it guards against records left behind by a partial flush.
    Costs are the exception and are handled by subject_costs() instead — their
    keys carry no subject id, so they need the workflow-name index rather than
    a dataframe filter.
    """
    if df.empty or "subject" not in df.columns:
        return df
    return df[df["subject"].isin(subjects)]


def describe(df: pd.DataFrame, cols: list[str], title: str) -> None:
    print(f"\n=== {title}  (n={len(df)}) ===")
    present = [c for c in cols if c in df.columns]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        print(f"  (columns not present in records: {', '.join(missing)})")
    if df.empty or not present:
        print("  no data")
        return
    numeric = df[present].apply(pd.to_numeric, errors="coerce")
    print(numeric.describe())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--window-start", help="Inclusive YYYY-MM-DD lower bound on the cost scrape date"
    )
    p.add_argument("--window-end", help="Inclusive YYYY-MM-DD upper bound on the cost scrape date")
    args = p.parse_args()

    subjects = load_batch_subjects(SUBJECTS_CSV)
    print(f"Batch: {len(subjects)} subjects from {SUBJECTS_CSV.name}")
    m = CloudpipeMetrics(bucket=BUCKET)

    # Functional QC — per BOLD run
    func = scope(m.func_qc(), subjects)
    describe(
        func,
        [
            "mean_fd",
            "median_fd",
            "max_fd",
            "pct_fd_above_0p5",
            "mean_dvars",
            "tsnr_median",
            "total_runtime_s",
            "peak_memory_gb",
        ],
        "Functional QC (per BOLD run)",
    )

    # Anatomical QC — per subject x session
    anat = scope(m.anat_qc(), subjects)
    describe(
        anat,
        [
            "etiv_mm3",
            "total_brain_vol_mm3",
            "wm_vol_mm3",
            "lh_mean_thickness_mm",
            "rh_mean_thickness_mm",
        ],
        "Anatomical QC (per subject x session)",
    )

    # Registration QC — T1w->MNI (FireANTs, schema 2.0: mask_dice / lncc)
    t1w = scope(m.registration_qc(registration_type="t1w_to_mni"), subjects)
    describe(
        t1w,
        [
            "mask_dice",
            "lncc",
            "jac_det_frac_negative",
            "centroid_displacement_mm",
        ],
        "Registration QC — t1w_to_mni",
    )

    # Registration QC — BOLD->T1w (SynthMorph). `nmi` is the primary live metric
    # (schema 2.1, superseded raw `mi`): dice is hardcoded 0.0 and the bbr_*
    # fields died with BBR in 61ccff7. Schema 2.2 adds the recorded-only
    # transform-magnitude trio and mhd_mm — describing them here is exactly the
    # batch characterisation their thresholds must be calibrated against (mhd_mm
    # carries a -1.0 could-not-compute sentinel, so read its floor with care).
    bold = scope(m.registration_qc(registration_type="bold_to_t1w"), subjects)
    describe(
        bold,
        ["nmi", "rigid_disp_mean_mm", "rigid_disp_max_mm", "rigid_rot_deg", "mhd_mm"],
        "Registration QC — bold_to_t1w",
    )

    # Workflow runs — durations and queue wait
    runs = scope(m.workflow_runs(), subjects)
    describe(runs, ["total_duration_s", "pending_duration_s"], "Workflow runs")

    # Cost — one row per subject, summed over the per-(workflow, scrape-date)
    # grain. Without a scrape-date window this mixes in earlier batches, since
    # metrics/costs/ accumulates.
    if not (args.window_start or args.window_end):
        print(
            "\nWARNING: no --window-start/--window-end given; cost rows from "
            "earlier batches may be included."
        )
    costs = m.subject_costs(
        subjects=sorted(subjects),
        date_from=args.window_start,
        date_to=args.window_end,
    )
    describe(
        costs,
        [
            "total_cost_usd",
            "cpu_cost_usd",
            "memory_cost_usd",
            "gpu_cost_usd",
        ],
        "Cost (per subject, batch window)",
    )


if __name__ == "__main__":
    main()
