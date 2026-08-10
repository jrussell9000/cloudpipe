#!/usr/bin/env python3
"""Validate a co-packing probe BEFORE any of its timings are believed (#164 Phase B).

`scripts/manifests/t1w-to-mni-affine-threads-packing.yaml` runs trios of pods that are
supposed to land on ONE node so they contend for its four cores. Nothing forces that:
a required `podAffinity` would deadlock the first pod of a trio (it has nothing to
attract to yet) and anti-affinity repels rather than attracts, so Karpenter is free to
spread a trio across two nodes. A trio that did not share a node measured no
contention at all, and its numbers look perfectly reasonable — which is the danger.

This checks the two things that make the experiment valid:

  1. Every co-packed scope has all three of its arms on ONE node.
  2. Those arms actually OVERLAPPED in time. Three pods on one node that ran
     sequentially contend no more than three pods on three nodes. Argo starts the
     steps together, but a staggered image pull or artifact download can serialise
     them in practice, and the timings would then silently understate contention.

Exits non-zero if either fails, so it can gate an analysis script.

Usage:
    python scripts/investigations/fused_ops_ab/check_copacking.py /tmp/affine-packing/
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

MARKER = re.compile(r'ITK_THREADS=(\d+) SCOPE=(\S+) subj=(\S+) ses=(\S+) node=(\S+)')
FIRST_STAGE = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO Stage 1')
LAST_LINE = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO Done\.')

# Scopes whose arms are SUPPOSED to share a node. Anything else is a solo reference.
COPACKED_PREFIX = 'pack'


def load(logdir: Path) -> list[dict]:
    out = []
    for p in sorted(logdir.glob('*.log')):
        txt = p.read_text(errors='replace')
        m = MARKER.search(txt)
        if not m:
            continue
        start = FIRST_STAGE.search(txt)
        end = LAST_LINE.search(txt)
        out.append(
            {
                'itk': int(m.group(1)),
                'scope': m.group(2),
                'subj': m.group(3),
                'node': m.group(5),
                'start': datetime.strptime(start.group(1), '%Y-%m-%d %H:%M:%S') if start else None,
                'end': datetime.strptime(end.group(1), '%Y-%m-%d %H:%M:%S') if end else None,
                'log': p.name,
            }
        )
    return out


def _check_overlap(arms: list[dict]) -> bool:
    """Did every pair of arms in this trio actually run at the same time?

    Co-residency is not contention. Argo starts the steps of a parallel group
    together, but a staggered image pull or artifact download can serialise them on
    the node, and the timings would then understate contention while looking fine.
    Reports the WORST pair, since one non-overlapping pair is enough to invalidate.
    """
    spans = [(a['start'], a['end'], a['subj']) for a in arms if a['start'] and a['end']]
    if len(spans) < len(arms):
        print('    FAIL: an arm is missing Stage 1 or Done. — cannot confirm overlap')
        return False

    worst = None
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            s1, e1, n1 = spans[i]
            s2, e2, n2 = spans[j]
            ov = (min(e1, e2) - max(s1, s2)).total_seconds()
            if worst is None or ov < worst[0]:
                worst = (ov, n1, n2)

    if worst is None:
        return True
    if worst[0] <= 0:
        print(f'    FAIL: {worst[1]} and {worst[2]} did not overlap in time')
        return False
    print(f'    OK: minimum pairwise overlap {worst[0]:.0f}s')
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('logdir', type=Path)
    args = ap.parse_args()

    rows = load(args.logdir)
    if not rows:
        print(f'No probe logs with a node= marker in {args.logdir}', file=sys.stderr)
        return 1

    by_scope: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_scope[r['scope']].append(r)

    ok = True
    for scope in sorted(by_scope):
        arms = by_scope[scope]
        nodes = {a['node'] for a in arms}
        copacked = scope.startswith(COPACKED_PREFIX)
        label = 'co-packed' if copacked else 'solo'
        print(f'\n{scope} ({label}, itk={arms[0]["itk"]}, n={len(arms)})')
        for a in arms:
            span = (
                f'{a["start"]:%H:%M:%S}-{a["end"]:%H:%M:%S}' if a['start'] and a['end'] else 'n/a'
            )
            print(f'    {a["subj"]:<16} node={a["node"]:<45} {span}')

        if not copacked:
            continue

        if len(nodes) != 1:
            print(f'    FAIL: {len(nodes)} distinct nodes — these arms never contended')
            ok = False
        else:
            print('    OK: single node')

        ok = _check_overlap(arms) and ok

    print(
        '\nVALID — co-packed rows are safe to read'
        if ok
        else '\nINVALID — re-run; do not read the co-packed timings'
    )
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
