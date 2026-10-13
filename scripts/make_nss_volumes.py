#!/usr/bin/env python3
"""Build `nss_volumes.csv` from your own ABCD scanner table.

Every functional run starts with non-steady-state (NSS) volumes, and how many the
pipeline drops depends on the scanner, which the images themselves do not record.
The BOLD sidecars carry no manufacturer or software version, and the timing
parameters do not separate the cases (checked 2026-10-11). So the count comes from
ABCD's tabulated MR administrative data, which is under the data use agreement.
Each deployment builds its own table from its own ABCD access; the reference
deployment's copy is keyed by subject ID and is never published.

The rule is acquisition protocol (Hagler et al. 2019, and the footnote quoted in
`scripts/computeNumNSS.ipynb`). It held without exception across the reference
deployment's 33,910 session rows:

| Scanner | Software | NSS frames |
|---|---|---|
| GE | DV25 | 5 |
| GE | DV26 and later | 16 |
| Philips | any | 8 |
| Siemens | any | 8 |

A session the rule does not cover is an error, not a default: a wrong count shifts
every confound and every output frame of every run in that session, silently. The
error reports how many sessions fall in each uncovered case and never names one.

Input is ABCD v7's `mr_y_adm__info` table: `<base>.tsv` and its data dictionary
`<base>.json`, whose `Levels` map the coded manufacturer to its name.

    pixi run python scripts/make_nss_volumes.py --mr-info /path/to/mr_y_adm__info --out nss_volumes.csv
    aws s3 cp nss_volumes.csv s3://<data bucket>/config/nss_volumes.csv
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import sys
from pathlib import Path

MANUFACTURER = "mr_y_adm__info__dev_manufact"
SOFTWARE = "mr_y_adm__info__dev__sftw_ver"

# What src/inventory.py reads is subject_id, session, nss_frames. The leading
# unnamed index column and the two descriptive columns match the reference
# deployment's table, so the two are interchangeable.
HEADER = ["", "subject_id", "session", "nss_frames", "scanner_make", "GEmajDVver"]

_GE_VERSION = re.compile(r"[DMR][VRX](\d{2})")


class Uncovered(Exception):
    """Some sessions match no rule. The message carries counts, never identifiers."""


def nss_frames(manufacturer: str, software: str) -> tuple[int, str] | None:
    """(frames, GE major version or "") for one session, or None if the rule does not cover it."""
    if manufacturer in ("Siemens", "Philips"):
        return 8, ""
    if manufacturer == "GE":
        match = _GE_VERSION.search(software or "")
        if match is None:
            return None
        major = int(match.group(1))
        if major == 25:
            return 5, str(major)
        if major > 25:
            return 16, str(major)
    return None


def _levels(dictionary: dict, column: str) -> dict[str, str]:
    return {str(k): str(v) for k, v in (dictionary.get(column, {}).get("Levels") or {}).items()}


def _decode(value: str, levels: dict[str, str]) -> str:
    """A coded value to its label, as the notebook did: "1.0" and "1" are the same code."""
    value = (value or "").strip()
    try:
        return levels.get(str(int(float(value))), value)
    except ValueError:
        return levels.get(value, value)


def build(tsv: Path, dictionary: dict) -> list[list[str]]:
    levels = _levels(dictionary, MANUFACTURER)
    rows: list[list[str]] = []
    uncovered: collections.Counter[tuple[str, str]] = collections.Counter()
    with tsv.open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle, delimiter="\t"):
            make = _decode(record.get(MANUFACTURER, ""), levels)
            software = (record.get(SOFTWARE) or "").strip()
            result = nss_frames(make, software)
            if result is None:
                uncovered[(make or "<blank>", software or "<blank>")] += 1
                continue
            frames, ge_major = result
            rows.append(
                [
                    str(len(rows)),
                    record["participant_id"],
                    record["session_id"],
                    str(frames),
                    make,
                    ge_major,
                ]
            )
    if uncovered:
        cases = "; ".join(
            f"{n} session(s) with manufacturer={m!r}, software={s!r}"
            for (m, s), n in sorted(uncovered.items())
        )
        raise Uncovered(
            f"{sum(uncovered.values())} session(s) match no NSS rule: {cases}. Decide their count from the "
            "scanner documentation and extend nss_frames() in this script; do not default them."
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--mr-info", required=True, type=Path, help="path to mr_y_adm__info, without .tsv/.json"
    )
    parser.add_argument("--out", type=Path, default=Path("nss_volumes.csv"))
    args = parser.parse_args(argv)

    base = args.mr_info
    tsv, dictionary_path = base.with_name(base.name + ".tsv"), base.with_name(base.name + ".json")
    for path in (tsv, dictionary_path):
        if not path.is_file():
            print(f"error: {path} not found", file=sys.stderr)
            return 1
    try:
        rows = build(tsv, json.loads(dictionary_path.read_text(encoding="utf-8")))
    except Uncovered as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(rows)
    counts = collections.Counter(row[3] for row in rows)
    print(
        f"wrote {len(rows)} sessions to {args.out}: "
        + ", ".join(f"{n} with {k} NSS frames" for k, n in sorted(counts.items()))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
