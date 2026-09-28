#!/usr/bin/env python3
"""Summarize a t1w-to-mni rescue-sizing probe run.

Reads the `probe_`-stamped RegistrationQC records written by
`scripts/manifests/t1w-to-mni-rescue-sizing-probe.yaml` and answers the three
questions that probe exists to settle:

  Q1  RECOVERY RATE per severity band — of the sessions that failed the gate in
      production, what fraction can any RANDOM arm rescue?
  Q2  TICKET COUNT — the marginal yield of attempts 2 and 3. This is the
      parameter that sizes a production rescue pass: if arm 1 alone recovers
      everything, the pass is a single retry.
  Q3  CONTROL OFFSET — does the harness reproduce the recorded production lncc,
      broken out BY GPU MODEL. The n=9 probe (pinned g4dn.xlarge) saw every
      control land up to 0.073 BELOW production, all in the same direction. On
      tranche 1 Karpenter put every pod on a T4, so this breakdown could not settle
      it; archived production pod logs did (7 of the 10 outliers were A10G runs).

A "recovery" is verdict == 'pass', never an lncc improvement. fused_ops raised
lncc every single time (+0.078 +/- 0.002) while folding the warp into a
jac_det_frac_negative FAIL, so lncc alone cannot certify a rescue — which is why
arms that improved lncc but still failed are listed with the gate that stopped them.

Usage:
    aws s3 sync s3://<YOUR_S3_BUCKET>/scratch/rescue-sizing/tranche1/ /tmp/rescue/
    python3 scripts/investigations/summarize_rescue_sizing.py /tmp/rescue/
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

BANDS = ("catastrophic", "mid", "near-gate")
# The t1w-to-mni gate, from images/shared/registration_qc.py::_T1W_MNI_THRESHOLDS.
# Used only to CLASSIFY why an arm failed; the verdict itself is always read
# from the record, never recomputed here.
LNCC_BOUND = 0.65
JAC_BOUND = 0.005
# Order matters: this is the sequence a rescue pass would try, so the cumulative
# recovery curve below reads as "yield after N attempts".
RANDOM_ARMS = ("random-itk1", "random-itk4", "random-itk8")

Sessions = dict[tuple[str, str], dict[str, dict]]


def load(d: Path) -> list[dict]:
    recs = []
    for f in sorted(d.glob("*.json")):
        try:
            r = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            print(f"  !! unreadable {f.name}: {e}", file=sys.stderr)
            continue
        if "probe_arm" not in r:
            print(f"  !! no probe_arm stamp in {f.name} — skipping", file=sys.stderr)
            continue
        r["_file"] = f.name
        recs.append(r)
    return recs


def recovered(arms: dict, subset=RANDOM_ARMS) -> bool:
    return any(arms.get(x, {}).get("verdict") == "pass" for x in subset)


def warn_on_passing_controls(sess: Sessions) -> None:
    """A passing control would invalidate the premise: production failed these."""
    bad = [s for s, a in sess.items() if a.get("control", {}).get("verdict") == "pass"]
    if bad:
        print(
            f"!! CONTROL ARM PASSED on {len(bad)} session(s) — production recorded these as fail."
        )
        examples = ", ".join(f"{x[0]}/{x[1]}" for x in bad[:5])
        print(f"!! Investigate before trusting any arm result. Examples: {examples}\n")


def report_recovery_rate(sess: Sessions) -> None:
    print("Q1  RECOVERY RATE (session recovered = any RANDOM arm reached verdict=pass)")
    print(f"    {'band':14} {'n':>4} {'recovered':>10} {'rate':>7}")
    total_n = total_rec = 0
    for band in BANDS:
        rows = [a for a in sess.values() if next(iter(a.values())).get("probe_band") == band]
        if not rows:
            continue
        rec = sum(map(recovered, rows))
        total_n, total_rec = total_n + len(rows), total_rec + rec
        print(f"    {band:14} {len(rows):>4} {rec:>10} {rec / len(rows):>6.0%}")
    if total_n:
        print(f"    {'ALL':14} {total_n:>4} {total_rec:>10} {total_rec / total_n:>6.0%}\n")


def report_ticket_count(sess: Sessions) -> None:
    print("Q2  TICKET COUNT — cumulative recovery as a rescue pass adds attempts")
    print(f"    {'after trying':42} {'recovered':>10} {'marginal':>9}")
    prev = 0
    for i in range(1, len(RANDOM_ARMS) + 1):
        subset = RANDOM_ARMS[:i]
        rec = sum(recovered(a, subset) for a in sess.values())
        print(f"    {' + '.join(subset):42} {rec:>10} {rec - prev:>+9}")
        prev = rec
    print()


def report_improved_but_failed(sess: Sessions) -> None:
    """Arms that beat the control's lncc yet failed — CLASSIFIED, never assumed.

    An earlier version asserted these were "usually" folding failures (the fused_ops
    trap). On tranche 1 all four were the opposite — lncc short of the bound with
    jac_neg healthy. So the reason is computed per arm-run against the gate.
    """
    reasons: dict[str, int] = defaultdict(int)
    for a in sess.values():
        ctl = a.get("control")
        for x in RANDOM_ARMS if ctl else ():
            r = a.get(x)
            if not (r and r.get("verdict") == "fail" and r.get("lncc", 0) > ctl.get("lncc", 0)):
                continue
            why = []
            if r.get("lncc", 0) < LNCC_BOUND:
                why.append("lncc short of bound")
            if r.get("jac_det_frac_negative", 0) > JAC_BOUND:
                why.append("warp FOLDED (fused_ops trap)")
            reasons[" + ".join(why) or "another gate"] += 1
    print(f"    improved lncc yet did NOT pass: {sum(reasons.values())} arm-run(s)")
    for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"        {n:>3}  {why}")
    print()


def report_control_offset(sess: Sessions) -> None:
    print("Q3  CONTROL OFFSET vs recorded production lncc, BY GPU MODEL")
    print(f"    {'gpu':22} {'n':>4} {'mean delta':>11} {'median':>9} {'max |d|':>9}")
    by_gpu: dict[str, list[float]] = defaultdict(list)
    for a in sess.values():
        c = a.get("control")
        if c and c.get("probe_recorded_lncc") is not None:
            by_gpu[c.get("probe_gpu_name", "unknown")].append(
                c.get("lncc", 0.0) - c["probe_recorded_lncc"]
            )
    for gpu, ds in sorted(by_gpu.items()):
        print(
            f"    {gpu:22} {len(ds):>4} {statistics.fmean(ds):>+11.4f} "
            f"{statistics.median(ds):>+9.4f} {max(abs(x) for x in ds):>9.4f}"
        )
    if not by_gpu:
        print("    (no control arm in this run — expected for a RANDOM-only tranche)")
    print(
        "\n    A near-zero mean means the harness reproduces production on that card.\n"
        "    This breaks the offset down by the card the PROBE ran on; production's own\n"
        "    card is not in the record — read it from the archived pod log's nvidia-smi\n"
        "    lines (s3://<YOUR_S3_BUCKET>/logs/<workflow>/<pod>/main.log) to compare like with like."
    )


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    d = Path(sys.argv[1])
    recs = load(d)
    if not recs:
        print(f"no stamped records under {d}")
        return 1

    sess: Sessions = defaultdict(dict)  # (subject, session) -> arm -> record
    for r in recs:
        sess[(r["subject"], r["session"])][r["probe_arm"]] = r
    print(f"{len(recs)} records / {len(sess)} sessions from {d}\n")

    warn_on_passing_controls(sess)
    report_recovery_rate(sess)
    report_ticket_count(sess)
    report_improved_but_failed(sess)
    report_control_offset(sess)
    return 0


if __name__ == "__main__":
    sys.exit(main())
