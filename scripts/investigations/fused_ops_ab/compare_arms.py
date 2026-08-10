#!/usr/bin/env python3
"""Paired comparison of the fused_ops A/B arms (scripts/manifests/t1w-to-mni-fused-ops-ab.yaml).

Reads the two arms' RegistrationQC records out of

    <root>/ffo-False/<subj>_<ses>_reg_qc.json     control  (torch interpolator + 'cc')
    <root>/ffo-True/<subj>_<ses>_reg_qc.json      treatment (fused kernels + 'fusedcc')

and reports, per metric, the PAIRED difference (treatment - control) rather than two
independent distributions. Paired is the whole point: session-to-session variance in
lncc (0.31-0.84 in production) is an order of magnitude larger than any plausible
kernel effect, so an unpaired comparison of 30 vs 30 could not resolve it.

The decision this feeds is not "are the means equal" but "does any session change
verdict". A metric can shift measurably and still be adopt-safe if no session crosses
a gate; conversely a single boundary session flipping pass -> fail is disqualifying
even with an otherwise invisible mean shift. Both are reported, and pair coverage is
reported explicitly so a lost pod cannot silently shrink the sample.

Usage:
    aws s3 sync s3://<YOUR_S3_BUCKET>/scratch/fused-ops-ab/run1/ /tmp/ffo-ab/
    python scripts/investigations/fused_ops_ab/compare_arms.py /tmp/ffo-ab/

    # the three-arm timing probe (#165) writes arm-off/ arm-exact/ arm-approx/,
    # and there are two comparisons worth making from it:
    aws s3 sync s3://<YOUR_S3_BUCKET>/scratch/fused-ops-timing/run1/ /tmp/ffo-timing/
    ... compare_arms.py /tmp/ffo-timing/ --control arm-off --treatment arm-exact
    ... compare_arms.py /tmp/ffo-timing/ --control arm-off --treatment arm-approx
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from pathlib import Path

# Defaults match t1w-to-mni-fused-ops-ab.yaml's output keys. Overridable because the
# timing probe's three arms (#165) cannot be named by a boolean any more.
CONTROL_DIR = 'ffo-False'
TREATMENT_DIR = 'ffo-True'

# Metrics worth a paired diff. lncc first (the headline alignment score); the
# Jacobian and inverse-consistency families next, because "different gradients"
# would show up as warp regularity changing even if the similarity score did not.
METRICS = [
    'lncc',
    'mask_dice',
    'jac_det_frac_negative',
    'log_jac_p01',
    'log_jac_p99',
    'log_jac_frac_beyond_1p5',
    'log_jac_frac_beyond_3',
    'ice_mean_mm',
    'ice_p95_mm',
    'centroid_displacement_mm',
]


def load_arm(d: Path) -> dict[tuple[str, str], dict]:
    out = {}
    for p in sorted(d.glob('*_reg_qc.json')):
        rec = json.loads(p.read_text())
        out[(rec['subject'], rec['session'])] = rec
    return out


def fmt(x: float | None, width: int = 9, prec: int = 5) -> str:
    return f'{x:>{width}.{prec}f}' if x is not None else f'{"n/a":>{width}}'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('root', type=Path, help='Directory containing the two arm subdirectories')
    ap.add_argument(
        '--control', default=CONTROL_DIR, help=f'control subdir (default {CONTROL_DIR})'
    )
    ap.add_argument(
        '--treatment', default=TREATMENT_DIR, help=f'treatment subdir (default {TREATMENT_DIR})'
    )
    args = ap.parse_args()

    ctl = load_arm(args.root / args.control)
    trt = load_arm(args.root / args.treatment)
    print(f'control: {args.control}   treatment: {args.treatment}')

    keys = sorted(set(ctl) & set(trt))
    only_ctl = sorted(set(ctl) - set(trt))
    only_trt = sorted(set(trt) - set(ctl))

    print(f'pairs: {len(keys)}   control-only: {len(only_ctl)}   treatment-only: {len(only_trt)}')
    for label, missing in (('control-only', only_ctl), ('treatment-only', only_trt)):
        for k in missing:
            # An unpaired record is NOT a datapoint — it means one arm's pod never
            # produced a measurement (spot reclaim, or USE_FFO asserted unavailable).
            print(f'  UNPAIRED ({label}): {k[0]} {k[1]}')
    if not keys:
        print('No paired sessions — nothing to compare.')
        return 1

    # ── Verdict changes: the actual adoption gate ────────────────────────────────
    print('\n=== verdict changes (the adoption gate) ===')
    flips = [k for k in keys if ctl[k].get('verdict') != trt[k].get('verdict')]
    if flips:
        for k in flips:
            print(
                f'  FLIP {k[0]} {k[1]}: {ctl[k]["verdict"]} -> {trt[k]["verdict"]}   '
                f'lncc {ctl[k]["lncc"]:.5f} -> {trt[k]["lncc"]:.5f}'
            )
    else:
        print(f'  none — all {len(keys)} paired sessions keep their verdict')

    from collections import Counter

    print(f'  control verdicts:   {dict(Counter(ctl[k].get("verdict") for k in keys))}')
    print(f'  treatment verdicts: {dict(Counter(trt[k].get("verdict") for k in keys))}')

    # ── Paired per-metric differences ────────────────────────────────────────────
    print('\n=== paired differences (treatment - control) ===')
    hdr = f'{"metric":<26}{"n":>4}{"median ctl":>12}{"median trt":>12}{"med diff":>12}{"max |diff|":>12}'
    print(hdr)
    print('-' * len(hdr))
    for m in METRICS:
        pairs = [
            (ctl[k][m], trt[k][m])
            for k in keys
            if ctl[k].get(m) is not None and trt[k].get(m) is not None
        ]
        if not pairs:
            continue
        diffs = [t - c for c, t in pairs]
        print(
            f'{m:<26}{len(pairs):>4}'
            f'{st.median(c for c, _ in pairs):>12.5f}'
            f'{st.median(t for _, t in pairs):>12.5f}'
            f'{st.median(diffs):>+12.5f}'
            f'{max(abs(d) for d in diffs):>12.5f}'
        )

    # ── Per-session lncc detail, ordered by control lncc ─────────────────────────
    # Ordered by the control score so the boundary and fail sessions — where a shift
    # can change a verdict — read at the top rather than being buried.
    print('\n=== per-session lncc (ordered by control) ===')
    print(f'{"subject":<16}{"session":<9}{"ctl":>9}{"trt":>9}{"diff":>10}  verdicts')
    for k in sorted(keys, key=lambda k: ctl[k]['lncc']):
        c, t = ctl[k]['lncc'], trt[k]['lncc']
        print(
            f'{k[0]:<16}{k[1]:<9}{fmt(c)}{fmt(t)}{t - c:>+10.5f}  '
            f'{ctl[k].get("verdict")}/{trt[k].get("verdict")}'
        )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
