#!/usr/bin/env python3
"""Select the stratified session sample for the fused_ops A/B (handoffs/fireants-fused-ops.md).

Reads t1w_to_mni RegistrationQC records from a metrics-corpus partition and emits
the sample as a JSON list of {subject, session, lncc, verdict} ready to paste into
the withItems block of scripts/manifests/t1w-to-mni-fused-ops-ab.yaml.

The sample is deliberately NOT random. It is:

  * every 'fail' session  — the sharpest test there is. A fail means the SyN
    optimizer landed in a bad basin; the question fused kernels raise is whether
    different gradients land in the SAME basin. Since #138 those failures are
    deterministic rather than self-healing on retry, so they are reproducible
    experimental units, not flakes.
  * the N_BOUNDARY lowest passes — a distribution shift only changes a verdict
    near the gate, so that is where power is worth spending.
  * N_SPREAD sessions spaced by lncc quantile over the rest, so the bulk of the
    distribution is represented too.

Usage:
    aws s3 cp --recursive --exclude '*' --include '*t1w_to_mni*' \
        s3://cloudpipe-metrics/metrics/registration/dt=2026-08-04/ /tmp/base/
    python scripts/investigations/select_ffo_ab_sample.py /tmp/base/ -o /tmp/ab_sample.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

N_BOUNDARY = 5
N_SPREAD = 20


def load(records_dir: Path) -> list[dict]:
    rows = []
    for path in sorted(records_dir.glob('*.json')):
        rec = json.loads(path.read_text())
        if rec.get('registration_type') != 't1w_to_mni':
            continue
        if rec.get('lncc') is None:
            continue
        rows.append(
            {
                'subject': rec['subject'],
                'session': rec['session'],
                'lncc': round(float(rec['lncc']), 5),
                'verdict': rec.get('verdict', ''),
            }
        )
    return rows


def select(rows: list[dict]) -> list[dict]:
    rows = sorted(rows, key=lambda r: r['lncc'])
    fails = [r for r in rows if r['verdict'] == 'fail']
    rest = [r for r in rows if r['verdict'] != 'fail']

    boundary = rest[:N_BOUNDARY]
    remaining = rest[N_BOUNDARY:]

    # Even spacing by RANK, not by lncc value: the pass distribution is tight
    # (0.68-0.84) and left-skewed, so equal-width lncc bins would put almost
    # every session in one bin.
    if remaining and N_SPREAD:
        step = len(remaining) / N_SPREAD
        spread = [remaining[min(int(i * step), len(remaining) - 1)] for i in range(N_SPREAD)]
    else:
        spread = []

    # Dedupe while preserving order — a spaced pick can collide with a boundary
    # pick when `remaining` is short.
    out, seen = [], set()
    for r in fails + boundary + spread:
        key = (r['subject'], r['session'])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('records_dir', type=Path, help='Directory of *_t1w_to_mni_reg_qc.json records')
    p.add_argument('-o', '--out', type=Path, required=True, help='Output JSON path')
    args = p.parse_args()

    rows = load(args.records_dir)
    sample = select(rows)
    args.out.write_text(json.dumps(sample, indent=2))

    print(f'corpus: {len(rows)} sessions, {len({r["subject"] for r in rows})} subjects')
    print(f'sample: {len(sample)} sessions -> {args.out}')
    for r in sample:
        print(f'  {r["subject"]:<16} {r["session"]:<8} lncc={r["lncc"]:.5f}  {r["verdict"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
