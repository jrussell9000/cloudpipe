"""The difference-against-mean figure for the four log-ratio endpoints (spec, "Ratio-scale endpoint").

One small multiple per endpoint, each a Bland–Altman plot on the ratio scale: a
dot per session at (the pair's geometric mean, cloudpipe / fMRIPrep), with the
line of no difference, the mean ratio, and the endpoint's margin bounds. Both
axes are logarithmic, so a proportional difference is a flat band and a constant
absolute one would bend. The numbers behind every line are in the results tables;
the figure shows the spread they summarise.

A manuscript figure, so static and light-surface only: one series per panel, so
no legend, each panel titled by its endpoint and every reference line labelled
directly.

    python -m equivalence.figures --values values.csv --out ratio_agreement.png
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter  # noqa: E402

from .analyze import Row, read_table  # noqa: E402
from .margins import DEFAULT_MARGINS, LOG_RATIO, TOST, Margins, load_margins  # noqa: E402

# The validated reference palette (dataviz skill, references/palette.md), light mode.
SURFACE = "#fcfcfb"
SERIES = "#2a78d6"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"

#: Panel order and titles.
ENDPOINTS = {
    "tsnr_median": "Median tSNR",
    "gcor": "gcor",
    "aor": "aor (AFNI outlier ratio)",
    "aqi": "aqi (AFNI quality index)",
}


def _ratio_rows(margins: Margins, table: dict[str, list[Row]]) -> dict[str, list[Row]]:
    out = {}
    for name in ENDPOINTS:
        m = margins[name]
        if m.test != TOST or m.scale != LOG_RATIO:
            raise ValueError(f"{name} is not a log-ratio TOST endpoint")
        out[name] = table[name]
    return out


def _panel(ax, title: str, rows: list[Row], upper: float, offset: float) -> None:
    a = np.array([r.cloudpipe for r in rows], dtype=float)
    b = np.array([r.reference for r in rows], dtype=float)
    ratio = np.exp([r.value for r in rows])
    pair = np.exp((np.log(a + offset) + np.log(b + offset)) / 2) - offset
    mean_ratio = float(np.exp(np.mean([r.value for r in rows])))

    ax.set_facecolor(SURFACE)
    lo, hi = 1 / upper, upper
    # Reference lines stop short of the right edge; their labels sit in the gap.
    line_end = 0.80
    for y, style, color, width in (
        (1.0, "-", BASELINE, 1.0),
        (lo, (0, (4, 3)), INK_MUTED, 1.0),
        (hi, (0, (4, 3)), INK_MUTED, 1.0),
        (mean_ratio, "-", INK_SECONDARY, 1.5),
    ):
        ax.axhline(y, xmax=line_end, linestyle=style, color=color, linewidth=width, zorder=1)
    ax.scatter(pair, ratio, s=30, color=SERIES, edgecolors=SURFACE, linewidths=1.2, zorder=3)

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_ylim(min(lo, float(ratio.min())) / 1.03, max(hi, float(ratio.max())) * 1.03)
    xmin, xmax = float(pair.min()), float(pair.max())
    pad = (xmax / xmin) ** 0.03
    # Data in the left `line_end` of the width, labels in the rest.
    left, right = math.log(xmin / pad), math.log(xmax * pad)
    ax.set_xlim(math.exp(left), math.exp(left + (right - left) / line_end))

    for y, text, color in (
        (hi, f"bound {hi:.2f}", INK_MUTED),
        (lo, f"bound {lo:.2f}", INK_MUTED),
        (mean_ratio, f"mean {mean_ratio:.3f}", INK_SECONDARY),
    ):
        ax.text(
            line_end + 0.015, y, text, color=color, fontsize=8, va="center", ha="left",
            transform=ax.get_yaxis_transform(),
        )  # fmt: skip

    ax.set_title(title, loc="left", fontsize=10, color=INK_PRIMARY)
    ax.set_yticks(sorted({lo, 1.0, hi}))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.2f}".rstrip("0").rstrip(".")))
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.grid(True, which="major", color=GRID, linewidth=0.6, zorder=0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASELINE)
    ax.tick_params(colors=INK_MUTED, labelsize=8)


def ratio_agreement_figure(margins: Margins, table: dict[str, list[Row]], out: Path) -> Path:
    rows = _ratio_rows(margins, table)
    fig, axes = plt.subplots(2, 2, figsize=(9, 7), facecolor=SURFACE)
    for ax, (name, title) in zip(axes.flat, ENDPOINTS.items(), strict=True):
        m = margins[name]
        _panel(ax, title, rows[name], math.exp(m.value), m.floor_offset)
    for ax in axes[1]:
        ax.set_xlabel("pair geometric mean (log scale)", fontsize=9, color=INK_SECONDARY)
    for ax in axes[:, 0]:
        ax.set_ylabel("cloudpipe / fMRIPrep (log scale)", fontsize=9, color=INK_SECONDARY)
    n = len(next(iter(rows.values())))
    fig.suptitle(
        f"Session ratios against pair magnitude, {n} sessions", x=0.06, ha="left",
        fontsize=11, color=INK_PRIMARY,
    )  # fmt: skip
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(out, dpi=200, facecolor=SURFACE)
    plt.close(fig)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--values", type=Path, required=True)
    p.add_argument("--margins", type=Path, default=DEFAULT_MARGINS)
    p.add_argument("--out", type=Path, required=True, help="PNG or PDF, by suffix")
    args = p.parse_args(argv)
    print(ratio_agreement_figure(load_margins(args.margins), read_table(args.values), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
