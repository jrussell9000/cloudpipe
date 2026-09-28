#!/usr/bin/env python3
"""Summarize a t1w-to-mni RANDOM-rescue validation run against the tranche-1 probe.

Reads the `probe_`-stamped records written by
`scripts/manifests/t1w-to-mni-rescue-validation.yaml` (arms: `ladder`, `seed43`,
`seed44`) and, for baselines, the tranche-1 rescue-sizing probe (arms: `control`,
`random-itk1`, `random-itk4`, `random-itk8`), which ran on the same 30 sessions.

It answers the four things the validation run exists for, and then uses the fact that
every step here is supposed to be DETERMINISTIC to cross-check the runs against each
other — agreement that should hold exactly, so any break is a finding:

  * ladder `none_lncc`          == tranche-1 `control` lncc   (NONE is deterministic)
  * ladder accepted @ ticket 1  == tranche-1 `random-itk1`     (seed 42 @ 1 thread IS itk1)
  * ladder accepted @ ticket 2  == this run's `seed43` arm      (same code, seed, card)
  * ladder accepted @ ticket 3  == this run's `seed44` arm
  * ladder total failure        =>  seed43 AND seed44 both fail too

Matches are judged at SyN's same-card run-to-run spread (~1e-3 lncc), and only when both
records ran on the same GPU model — the card shifts lncc (A10G->T4 ~-0.053 on failed
sessions), so a cross-card comparison says nothing.

EXPECT A FEW LNCC GAPS ABOVE THE TOLERANCE, AND READ THE VERDICT-FLIP LINE INSTEAD. The
affine is bit-exact at one thread, but the final lncc also passes through GPU SyN, whose
reductions are not deterministic. On run1 (30 sessions, all T4) identical runs differed by
a median 1.8e-4, yet two rugged sessions reached 0.013 / 0.023 — the same sessions that
already disagreed between production and the probes. That residual predates the rescue
and hits NONE too. What would matter is a SYSTEMATIC offset across many sessions (a code
difference), or a pass/fail verdict flipping between two runs of the same ticket.

Usage:
    aws s3 sync s3://<YOUR_S3_BUCKET>/scratch/rescue-validation/run1/ /tmp/rv/
    aws s3 sync s3://<YOUR_S3_BUCKET>/scratch/rescue-sizing/tranche1/ /tmp/t1/
    python3 scripts/investigations/summarize_rescue_validation.py /tmp/rv /tmp/t1
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

BANDS = ("catastrophic", "mid", "near-gate")
# SyN's run-to-run spread on one card model (fireants-fused-ops, n=4): ~1e-3 lncc.
SAME_CARD_TOL = 2e-3
# Probe's per-arm recovery on these 30 sessions — the efficacy baselines.
BASELINE = {"random-itk1": (25, 30), "random-itk4": (26, 30), "random-itk8": (27, 29)}
PROVENANCE_FIELDS = (
    "sampling_strategy",
    "rescue_ticket",
    "sampling_seed",
    "itk_threads",
    "attempts_run",
    "none_lncc",
    "none_jac_det_frac_negative",
)

Records = dict[tuple[str, str], dict[str, dict]]


def load(d: Path) -> Records:
    out: Records = defaultdict(dict)
    for f in sorted(d.glob("*.json")):
        r = json.loads(f.read_text())
        if "probe_arm" in r:
            out[(r["subject"], r["session"])][r["probe_arm"]] = r
    return out


def passed(r: dict | None) -> bool:
    return bool(r) and r.get("verdict") == "pass"


def same_card(a: dict, b: dict) -> bool:
    return a.get("probe_gpu_name") == b.get("probe_gpu_name")


def check(label: str, pairs: list[tuple[str, float, float, bool]]) -> None:
    """pairs: (session, value_here, value_baseline, comparable)."""
    comparable = [p for p in pairs if p[3]]
    bad = [p for p in comparable if abs(p[1] - p[2]) > SAME_CARD_TOL]
    skipped = len(pairs) - len(comparable)
    verdict = "OK" if not bad else f"BROKEN on {len(bad)}"
    print(
        f"  {label:44} {len(comparable):>3} compared  {verdict}"
        + (f"  ({skipped} skipped: different GPU)" if skipped else "")
    )
    for s, a, b, _ in bad[:8]:
        print(f"      {s:24} {a:.4f} vs {b:.4f}  (delta {a - b:+.4f})")


def report_efficacy(rv: Records, t1: Records) -> None:
    """1. Each seed run on its own, against the thread arms it replaces."""
    n = len(rv)
    print("1. SEED EFFICACY — each ticket run on its own, regardless of outcome")
    print(f"   {'arm':14} {'recovered':>10}   baseline (tranche-1 thread arms)")
    for arm, base in (("seed43", "random-itk4"), ("seed44", "random-itk8")):
        rows = [a[arm] for a in rv.values() if arm in a]
        bk, bn = BASELINE[base]
        print(f"   {arm:14} {sum(map(passed, rows)):>4}/{len(rows):<5}   {base} {bk}/{bn}")
    k42 = sum(passed(t1[s].get("random-itk1")) for s in rv)
    print(f"   {'seed42':14} {k42:>4}/{n:<5}   = tranche-1 random-itk1 (seed 42 @ 1 thread)")

    # The pods are unpinned (gpu-nodepool's minValues forbids a single-type pin), and the
    # card shifts lncc, so a pass that only holds on one card is not the same evidence as
    # the all-T4 tranche-1 baseline. Break the rates down by card before trusting them.
    print("   by GPU (baseline tranche 1 was all Tesla T4):")
    for arm in ("seed43", "seed44"):
        by_gpu: dict[str, list[bool]] = defaultdict(list)
        for a in rv.values():
            if arm in a:
                by_gpu[a[arm].get("probe_gpu_name", "?")].append(passed(a[arm]))
        print(
            f"     {arm:8} "
            + "   ".join(f"{g}: {sum(v)}/{len(v)}" for g, v in sorted(by_gpu.items()))
        )

    print("   by band:")
    for band in BANDS:
        ss = [s for s, a in rv.items() if next(iter(a.values())).get("probe_band") == band]
        parts = [f"seed42 {sum(passed(t1[s].get('random-itk1')) for s in ss)}/{len(ss)}"]
        parts += [
            f"{arm} {sum(passed(rv[s].get(arm)) for s in ss)}/{len(ss)}"
            for arm in ("seed43", "seed44")
        ]
        print(f"     {band:13} " + "   ".join(parts))

    # Projected ladder yield from the INDEPENDENT arms, in ladder order.
    print("   projected ladder yield (independent arms, in order):")
    got: set = set()
    for label, src, arm in (
        ("42", t1, "random-itk1"),
        ("+43", rv, "seed43"),
        ("+44", rv, "seed44"),
    ):
        got |= {s for s in rv if passed(src[s].get(arm))}
        print(f"     after seed {label:4} {len(got):>3}/{n}")


def ladder_problems(lad: dict) -> list[str]:
    """Provenance and ladder invariants that must hold on every ladder record."""
    probs = []
    for s, r in lad.items():
        if r.get("schema_version") != "2.7":
            probs.append(f"{s}: schema_version {r.get('schema_version')!r}")
        missing = [f for f in PROVENANCE_FIELDS if f not in r]
        if missing:
            probs.append(f"{s}: missing {missing}")
        t, a = r.get("rescue_ticket"), r.get("attempts_run")
        if passed(r) and t and a != t + 1:
            probs.append(f"{s}: accepted ticket {t} but attempts_run={a} (must stop at first pass)")
        if r.get("sampling_strategy") == "RANDOM" and r.get("itk_threads") != 1:
            probs.append(f"{s}: RANDOM at itk_threads={r.get('itk_threads')}")
    return probs


def report_ladder(rv: Records) -> dict:
    """2. The new script as production runs it."""
    print("\n2. LADDER END TO END — the new script as production runs it")
    lad = {s: a["ladder"] for s, a in rv.items() if "ladder" in a}
    outcome: dict[str, int] = defaultdict(int)
    for r in lad.values():
        if not passed(r):
            outcome["every attempt failed"] += 1
        elif r.get("rescue_ticket") == 0:
            outcome["NONE passed"] += 1
        else:
            outcome[f"rescued @ ticket {r.get('rescue_ticket')}"] += 1
    for k, v in sorted(outcome.items()):
        print(f"   {k:24} {v:>3}")
    print(f"   recovered {sum(map(passed, lad.values()))}/{len(lad)}")
    probs = ladder_problems(lad)
    print("   provenance / ladder invariants:", "all hold" if not probs else f"{len(probs)} BROKEN")
    for p in probs[:10]:
        print(f"     {p}")
    return lad


def report_vram(lad: dict) -> None:
    """3. Up to four SyN runs share one pod on a time-sliced T4."""
    print("\n3. VRAM ACROSS ATTEMPTS (per-process peak, MiB)")
    peaks = [(r.get("probe_vram_peaks_mib", []), r.get("probe_oom")) for r in lad.values()]
    by_n: dict[int, list[int]] = defaultdict(list)
    for pk, _ in peaks:
        if pk:
            by_n[len(pk)].append(max(pk))
    for k in sorted(by_n):
        print(f"   {k} attempt(s): {len(by_n[k]):>3} pods   max peak {max(by_n[k]):>5}")
    growth = sum(1 for pk, _ in peaks if len(pk) > 1 and max(pk) - pk[0] > 256)
    print(f"   peak grew >256 MiB across attempts in one pod: {growth}  (a leak would show here)")
    print(f"   OOM seen in any ladder pod log: {sum(bool(o) for _, o in peaks)}")


def report_determinism(rv: Records, t1: Records, lad: dict) -> None:
    """4. Cross-checks that must agree exactly if every step is deterministic."""
    print(f"\n4. DETERMINISM CROSS-CHECKS (tolerance {SAME_CARD_TOL} lncc, same GPU only)")

    def pairs(field: str, base: Records, arm: str, ticket: int | None) -> list:
        return [
            (f"{s[0]}/{s[1]}", r[field], base[s][arm]["lncc"], same_card(r, base[s][arm]))
            for s, r in lad.items()
            if (ticket is None or r.get("rescue_ticket") == ticket) and arm in base.get(s, {})
        ]

    check("ladder none_lncc == tranche-1 control", pairs("none_lncc", t1, "control", None))
    check("ladder ticket 1 == tranche-1 random-itk1 (seed 42)", pairs("lncc", t1, "random-itk1", 1))
    check("ladder ticket 2 == this run's seed43", pairs("lncc", rv, "seed43", 2))
    check("ladder ticket 3 == this run's seed44", pairs("lncc", rv, "seed44", 3))

    contradictions = [
        s
        for s, r in lad.items()
        if not passed(r) and (passed(rv[s].get("seed43")) or passed(rv[s].get("seed44")))
    ]
    result = (
        "none — OK" if not contradictions else f"{len(contradictions)} BROKEN: {contradictions}"
    )
    print(f"  {'ladder total-fail yet a seed arm passed':44} {result}")

    # The check that matters operationally: the same ticket, run twice, must reach the same
    # pass/fail. lncc gaps from GPU SyN nondeterminism are tolerable; a flip is not.
    flips = []
    for s, r in lad.items():
        ctl, itk1 = t1.get(s, {}).get("control"), t1.get(s, {}).get("random-itk1")
        none_failed_here = r.get("rescue_ticket") != 0 or not passed(r)
        if ctl and same_card(r, ctl) and passed(ctl) == none_failed_here:
            flips.append(f"{s} NONE")
        if itk1 and same_card(r, itk1) and none_failed_here:
            if passed(itk1) != (r.get("rescue_ticket") == 1):
                flips.append(f"{s} seed-42")
    result = "none — OK" if not flips else f"{len(flips)} FLIPPED: {flips}"
    print(f"  {'verdict flips vs tranche 1 (same ticket, card)':44} {result}")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    rv, t1 = load(Path(sys.argv[1])), load(Path(sys.argv[2]))
    print(
        f"validation: {sum(len(a) for a in rv.values())} records / {len(rv)} sessions; "
        f"tranche-1 baseline: {len(t1)} sessions"
    )
    gpus = sorted({r.get("probe_gpu_name") for a in rv.values() for r in a.values()})
    print(f"GPU models in this run: {gpus}\n")

    report_efficacy(rv, t1)
    lad = report_ladder(rv)
    report_vram(lad)
    report_determinism(rv, t1, lad)
    return 0


if __name__ == "__main__":
    sys.exit(main())
