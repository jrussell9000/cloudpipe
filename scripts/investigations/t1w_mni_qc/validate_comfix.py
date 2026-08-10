#!/usr/bin/env python
"""validate_comfix.py — offline validation of the COM-init + composite-QC fix.

--emit-list : print `subject ses` for all fails + a 5 pass / 5 warn regression sample.
--summarize : join before/after metric CSVs, flag regressions and unresolved fails.
"""

import argparse
import csv


def emit_list(qc_table: str) -> None:
    rows = list(csv.DictReader(open(qc_table)))
    fails = [r for r in rows if r["verdict"] == "fail"]

    def sample(v, n):
        seen, out = set(), []
        for r in sorted(rows, key=lambda r: r["subject"]):
            if r["verdict"] == v and r["subject"] not in seen:
                seen.add(r["subject"])
                out.append(r)
            if len(out) == n:
                break
        return out

    for r in fails + sample("pass", 5) + sample("warn", 5):
        print(f'{r["subject"]} {r["session"]}')


def summarize(before_csv: str, after_csv: str) -> None:
    before = {(r["subject"], r["session"]): r for r in csv.DictReader(open(before_csv))}
    after = {(r["subject"], r["session"]): r for r in csv.DictReader(open(after_csv))}
    regressions, unresolved = [], []
    for key, a in after.items():
        b = before.get(key, {})
        if b.get("verdict") == "pass" and a["verdict"] != "pass":
            regressions.append((key, a["verdict"]))
        if b.get("verdict") == "fail" and a["verdict"] == "fail":
            unresolved.append(key)
    print(
        f"sessions: {len(after)}  regressions: {len(regressions)}  unresolved fails: {len(unresolved)}"
    )
    for k, v in regressions:
        print(f"  REGRESSION {k} -> {v}")
    for k in unresolved:
        print(f"  UNRESOLVED {k}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--qc-table", default="docs/investigations/artifacts/qc_table.csv")
    p.add_argument("--emit-list", action="store_true")
    p.add_argument("--summarize", nargs=2, metavar=("BEFORE", "AFTER"))
    a = p.parse_args()
    if a.emit_list:
        emit_list(a.qc_table)
    elif a.summarize:
        summarize(*a.summarize)
    else:
        p.error("choose --emit-list or --summarize")
