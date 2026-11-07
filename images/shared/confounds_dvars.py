"""The confounds TSV's `dvars_mean1000` column: DVARS on a scanner-independent scale.

`dvars` is in the input BOLD's own intensity units, and ABCD's minimally processed
input varies about 60-fold in intensity between scanners (#1016). This column is

    dvars_mean1000[t] = dvars[t] * 1000 / mean(global_signal)

the mean taken over every frame of the run, non-steady-state frames included,
exactly as `dvars` and `global_signal` themselves span every frame. Row 0 is n/a,
as `dvars` is. A mean that is not positive and finite makes the whole column n/a.

Why the mean of `global_signal`: it is the per-frame mean over the same brain
voxels DVARS differences, so its mean over frames is the run's mean brain
intensity, and it is in every confounds TSV ever published. The native BOLD is
deleted after every func workflow, so a column derived from anything else could
not be backfilled without re-ingesting the cohort. Both writers compute it from
the TSV's own text, so a TSV written by preproc.py and one backfilled later carry
the same bytes:

- preproc.py serialises the table without this column, then add_column();
- the dvars-backfill pod (src/dvars_backfill.py) calls add_column() on the TSV
  it reads from S3.

Relationship to fMRIPrep's `dvars`: that is nipype ComputeDVARS's non-standardised
DVARS with intensity_normalization=1000, which scales by the in-mask MEDIAN over
all voxels and frames, not the mean, and drops zero-variance voxels first
(remove_zerovariance=True). This column keeps every brain-mask voxel, as `dvars`
does. In the equivalence benchmark, cloudpipe's `dvars` x 1000 / the run's mean
intensity (there taken from the MNI-space BOLD, a close stand-in for this
column) ran at 0.975 of fMRIPrep's `dvars`, r = 0.989 per run: the same scale to
a few percent, not identical.

A retired column: `dvars_median1000` (#1027) scaled by the in-mask median of the
native BOLD, which no TSV-only computation can reproduce for past runs, so it
was replaced by this one before the backfill. TSVs written by the image that
carried it have it right after `dvars`; replace_retired() swaps it for this
column, and state() reports such a TSV as `retired`, never `current`.

Text in, text out, standard library only: the backfill runs in the python image,
which has no numpy or pandas. Every other byte of the TSV is left as it was, so
remove_column() restores the original exactly.
"""

from __future__ import annotations

import math

COLUMN = "dvars_mean1000"
#: The column the new one is inserted after, and its numerator.
AFTER = "dvars"
#: The column whose mean over frames is the denominator.
SOURCE = "global_signal"
SCALE = 1000.0
NA = "n/a"
#: Earlier versions of this column (see the module docstring). Replaced, never kept.
RETIRED = ("dvars_median1000",)


class ConfoundsFormatError(ValueError):
    """The text is not a confounds TSV this module can add the column to."""


def _float(field: str) -> float:
    return math.nan if field == NA else float(field)


def _format(value: float) -> str:
    # repr is the shortest string that round-trips, as pandas writes a float64.
    return NA if math.isnan(value) else repr(value)


def _table(text: str) -> tuple[list[list[str]], bool]:
    """(rows of fields, header first; whether the text ended with a newline)."""
    if not text:
        raise ConfoundsFormatError("empty TSV")
    trailing = text.endswith("\n")
    lines = (text[:-1] if trailing else text).split("\n")
    rows = [line.split("\t") for line in lines]
    width = len(rows[0])
    ragged = [i for i, row in enumerate(rows) if len(row) != width]
    if ragged:
        raise ConfoundsFormatError(f"row {ragged[0]} has a different field count than the header")
    return rows, trailing


def _text(rows: list[list[str]], trailing: bool) -> str:
    return "\n".join("\t".join(row) for row in rows) + ("\n" if trailing else "")


def run_mean(global_signal: list[str]) -> float:
    """The mean of the global_signal fields, NaN if there are none or any is n/a."""
    values = [_float(f) for f in global_signal]
    if not values or any(math.isnan(v) for v in values):
        return math.nan
    return math.fsum(values) / len(values)


def column_fields(dvars: list[str], global_signal: list[str]) -> tuple[list[str], float]:
    """(the column's fields, one per frame; the denominator, the run's mean global signal)."""
    mean = run_mean(global_signal)
    if not (math.isfinite(mean) and mean > 0):
        return [NA] * len(dvars), mean
    return [_format(_float(d) * SCALE / mean) for d in dvars], mean


def _columns(header: list[str]) -> tuple[int, int]:
    missing = [c for c in (AFTER, SOURCE) if c not in header]
    if missing:
        raise ConfoundsFormatError(f"no {'/'.join(missing)} column")
    return header.index(AFTER), header.index(SOURCE)


def expected(text: str) -> tuple[list[str], float]:
    """The column this TSV should carry, computed from its own dvars and global_signal."""
    rows, _ = _table(text)
    i_dvars, i_gs = _columns(rows[0])
    body = rows[1:]
    return column_fields([r[i_dvars] for r in body], [r[i_gs] for r in body])


def add_column(text: str) -> tuple[str, float]:
    """(the TSV with the column inserted right after `dvars`; the denominator).

    Raises ConfoundsFormatError if the column is already there, or the table lacks
    `dvars` or `global_signal`, or a row is ragged.
    """
    rows, trailing = _table(text)
    if COLUMN in rows[0]:
        raise ConfoundsFormatError(f"{COLUMN} is already a column")
    fields, mean = expected(text)
    at = rows[0].index(AFTER) + 1
    rows[0].insert(at, COLUMN)
    for row, field in zip(rows[1:], fields, strict=True):
        row.insert(at, field)
    return _text(rows, trailing), mean


def state(text: str) -> str:
    """`absent`; `retired` (it carries a RETIRED column); `current` (this column, right
    after dvars, equal to expected()); or `mismatch`."""
    rows, _ = _table(text)
    header = rows[0]
    _columns(header)
    if set(RETIRED) & set(header):
        return "retired"
    if COLUMN not in header:
        return "absent"
    at = header.index(COLUMN)
    if at == 0 or header[at - 1] != AFTER:
        return "mismatch"
    fields, _ = expected(remove_column(text))
    return "current" if [r[at] for r in rows[1:]] == fields else "mismatch"


def _drop(text: str, names: set[str]) -> str:
    rows, trailing = _table(text)
    keep = [i for i, name in enumerate(rows[0]) if name not in names]
    return _text([[row[i] for i in keep] for row in rows], trailing)


def replace_retired(text: str) -> tuple[str, float, list[str]]:
    """(the TSV with every RETIRED column, and any stale copy of this one, swapped for
    this column right after `dvars`; the denominator; the names dropped)."""
    rows, _ = _table(text)
    dropped = [c for c in rows[0] if c in RETIRED or c == COLUMN]
    out, mean = add_column(_drop(text, set(dropped)))
    return out, mean, dropped


def without_dvars_columns(text: str) -> str:
    """The TSV less this column and every RETIRED one: what any version of the
    column was added to. Two TSVs equal here differ only in that column."""
    return _drop(text, {COLUMN, *RETIRED})


def remove_column(text: str) -> str:
    """The TSV without the column: the exact original bytes add_column() was given."""
    rows, _ = _table(text)
    if COLUMN not in rows[0]:
        raise ConfoundsFormatError(f"no {COLUMN} column")
    return _drop(text, {COLUMN})
