#!/usr/bin/env python
"""plot_distributions.py — histogram each QC metric with warn/fail thresholds.

Answers Phase 1: are failures borderline (piled near a threshold) or catastrophic
(in the far tail)?
"""

from __future__ import annotations

import argparse
import csv
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# (warn, fail) verbatim from registration_qc.py.
_THRESHOLDS = {
    "dice": (0.90, 0.82),
    "jac_det_frac_negative": (0.001, 0.01),
    "centroid_displacement_mm": (5.0, 15.0),
}


def plot_metric(values: list[float], metric: str, out_path: str) -> None:
    if metric not in _THRESHOLDS:
        raise ValueError(f"unknown metric: {metric}")
    warn, fail = _THRESHOLDS[metric]
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.hist([v for v in values if v is not None], bins=30)
    ax.axvline(warn, color="orange", linestyle="--", label=f"warn={warn}")
    ax.axvline(fail, color="red", linestyle="--", label=f"fail={fail}")
    ax.set_title(metric)
    ax.set_xlabel(metric)
    ax.set_ylabel("sessions")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True)
    p.add_argument("--out-dir", required=True)
    args = p.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    cols = {m: [] for m in _THRESHOLDS}
    with open(args.csv) as fh:
        for row in csv.DictReader(fh):
            for m in _THRESHOLDS:
                try:
                    cols[m].append(float(row[m]))
                except (KeyError, ValueError):
                    pass
    for m, vals in cols.items():
        plot_metric(vals, m, os.path.join(args.out_dir, f"{m}.png"))


if __name__ == "__main__":
    main()
