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

The recorded software version is occasionally wrong, so on GE the run's length has the
last word: an n-back run of 367 frames means 5 NSS frames, and one of 378 means 16.
Those lengths leave the same 362 frames as every other scanner. Checked 2026-11-02: 11
of the 33,910 session rows disagreed with their version, and the func data of the 10
that have any shows the transient their length predicts (a frame 0 about 50-57% above
steady state at 378 frames, about 1% at 367). Those are mixed sessions (2026-11-06):
rest and n-back were scanned on different days, and their rest runs have the other
length. So no row is right for every run of them, and the pipeline resolves the
count per run from its own length (`images/shared/nss.py`). This table only has to
mark a GE session as GE (5 or 16). ABCD's `rep_count` holds other values too,
on runs that are truly short and on full-length runs it miscounts, so only these two
lengths are read. Without a usable length, the version decides.

These counts flag non-steady-state frames. ABCD's own task analyses have since moved to
discarding one frame fewer on GE (4 and 15; ABCD 6.0 release notes), and task events
record that in their `dummy` row. Flagging one extra steady frame does no harm, so this
table keeps Hagler's counts.

A session the rule does not cover is an error, not a default: a wrong count shifts
every confound and every output frame of every run in that session, silently. The
error reports how many sessions fall in each uncovered case and never names one.

Inputs are ABCD v7's `mr_y_adm__info` table, `<base>.tsv` and its data dictionary
`<base>.json`, whose `Levels` map the coded manufacturer and software version to their
names, and its `mr_y_qc__raw__tfmri__nback.tsv`.

    pixi run python scripts/make_nss_volumes.py --mr-info /path/to/mr_y_adm__info \\
        --mr-qc-nback /path/to/mr_y_qc__raw__tfmri__nback.tsv --out nss_volumes.csv
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
_REP_COUNT = re.compile(r"^mr_y_qc__raw__tfmri__nback__r\d+__rep_count$")
GE_FRAMES_BY_LENGTH = {367: 5, 378: 16}


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


def ge_frames_by_length(qc_tsv: Path) -> dict[tuple[str, str], int]:
    """{(subject, session): NSS frames} where an n-back run's length is one of GE's two."""
    out: dict[tuple[str, str], int] = {}
    conflicting = 0
    with qc_tsv.open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle, delimiter="\t"):
            found = set()
            for column, value in record.items():
                if column and _REP_COUNT.match(column) and (value or "").strip():
                    try:
                        found.add(GE_FRAMES_BY_LENGTH.get(int(float(value))))
                    except ValueError:
                        continue
            found.discard(None)
            if len(found) > 1:
                conflicting += 1
            elif found:
                out[(record["participant_id"], record["session_id"])] = found.pop()
    if conflicting:
        raise Uncovered(
            f"{conflicting} session(s) have n-back runs of both 367 and 378 frames, so their "
            "length cannot decide the count. Check the scanner documentation."
        )
    return out


def build(
    tsv: Path, dictionary: dict, by_length: dict[tuple[str, str], int]
) -> tuple[list[list[str]], int]:
    """(table rows, GE sessions whose run length overrode their software version)."""
    levels = _levels(dictionary, MANUFACTURER)
    software_levels = _levels(dictionary, SOFTWARE)
    rows: list[list[str]] = []
    overridden = 0
    uncovered: collections.Counter[tuple[str, str]] = collections.Counter()
    with tsv.open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle, delimiter="\t"):
            make = _decode(record.get(MANUFACTURER, ""), levels)
            software = _decode(record.get(SOFTWARE, ""), software_levels)
            result = nss_frames(make, software)
            if result is None:
                uncovered[(make or "<blank>", software or "<blank>")] += 1
                continue
            frames, ge_major = result
            if make == "GE":
                length_says = by_length.get(
                    (record["participant_id"], record["session_id"]), frames
                )
                overridden += length_says != frames
                frames = length_says
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
    return rows, overridden


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--mr-info", required=True, type=Path, help="path to mr_y_adm__info, without .tsv/.json"
    )
    parser.add_argument(
        "--mr-qc-nback", required=True, type=Path, help="path to mr_y_qc__raw__tfmri__nback.tsv"
    )
    parser.add_argument("--out", type=Path, default=Path("nss_volumes.csv"))
    args = parser.parse_args(argv)

    base = args.mr_info
    tsv, dictionary_path = base.with_name(base.name + ".tsv"), base.with_name(base.name + ".json")
    for path in (tsv, dictionary_path, args.mr_qc_nback):
        if not path.is_file():
            print(f"error: {path} not found", file=sys.stderr)
            return 1
    try:
        rows, overridden = build(
            tsv,
            json.loads(dictionary_path.read_text(encoding="utf-8")),
            ge_frames_by_length(args.mr_qc_nback),
        )
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
    if overridden:
        print(f"{overridden} GE session(s) took their count from run length, not software version")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
