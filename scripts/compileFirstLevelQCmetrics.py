#!/usr/bin/env python3
"""
Extract per-run QC metrics from fmri-first-level-proc CloudWatch log output.

Each output row corresponds to one fMRI run.  Preprocessing QC fields
(tSNR, Dice, motion rotation check, non-steady-state TR counts) are populated
from the Step 5-8 log block.  Rest-run censoring fields come from the
rest_conn analysis.  Nback concat-level censoring fields are fanned out to
each individual nback run row.

Usage
-----
  # read from a CloudWatch Logs CSV export
  python compileFirstLevelQCmetrics.py --csv log-events-viewer-result.csv

  # pull live from CloudWatch — explicit stream names
  python compileFirstLevelQCmetrics.py \\
      --stream abcdv6/default/<id1> [abcdv6/default/<id2> ...] \\
      [--log-group /aws/batch/job] [--region <YOUR_AWS_REGION>]

  # pull live from CloudWatch — all streams in the log group
  python compileFirstLevelQCmetrics.py --all-streams \\
      [--log-group /aws/batch/job] [--region <YOUR_AWS_REGION>]

  # filter streams by name prefix and/or minimum last-event time
  python compileFirstLevelQCmetrics.py --all-streams \\
      --prefix abcdv6/default/ \\
      --after "2026-04-30 18:00"

  # override output path (default: first_level_qc_metrics.csv)
  python compileFirstLevelQCmetrics.py --csv ... --output my_qc.csv
"""

import argparse
import csv
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import boto3
import pandas as pd
from botocore.config import Config

_BOTO_CONFIG = Config(retries={'max_attempts': 10, 'mode': 'adaptive'})

# ── regex patterns ────────────────────────────────────────────────────────────

session_re = re.compile(r'PROCESSING SESSION: (sub-\S+) / (ses-\S+)')
mask_step_re = re.compile(r'Step 5: Applying brain mask for (ses-\S+)_task-(\w+)_run-(\d+)')
nonss_re = re.compile(r'Detected (\d+) non-steady-state TR')
tsnr_re = re.compile(r'Median brain tSNR = ([\d.]+)')
dice_re = re.compile(r'Registration quality \(Dice\): ([\d.]+)')
trim_re = re.compile(r'Removed (\d+) initial TR\(s\):.*\((\d+) TRs remaining\)')
rot_pass_re = re.compile(r'Rotation unit check: PASSED.*max abs rotation = ([\d.]+)')
rot_ambig_re = re.compile(r'Rotation unit check AMBIGUOUS.*max\(abs\(rotation\)\) = ([\d.]+)')
rest_censor_re = re.compile(
    r'(sub-\w+)_(ses-\w+)_rest_run(\d+)_censor\.1D: '
    r'(\d+) of (\d+) TRs censored \(([\d.]+)%\)'
)
nback_censor_re = re.compile(
    r'(sub-\w+)_(ses-\w+)_nback_concat_censor\.1D: '
    r'(\d+) of (\d+) TRs censored \(([\d.]+)%\)'
)
dof_re = re.compile(r'Estimated DOF: (\d+) \(uncensored TRs: (\d+), regressors: (\d+)\)')

# ── parser ────────────────────────────────────────────────────────────────────


def parse_messages(messages):  # noqa: C901 — see note below
    # C901 (complexity 21 > 12) is suppressed rather than refactored. This is a
    # flat sequence of independent regex branches over AFNI log lines, one per
    # message shape; the complexity is the number of message shapes, not nesting
    # depth, and splitting it would only scatter the branches across helpers that
    # all share the same parser state. Revisit if this grows test coverage.
    records = {}  # (sub, ses, task, run) → metric dict
    nback_concat = {}  # (sub, ses) → concat censor/DOF dict
    nback_seen = set()  # tracks captured (sub, ses) censor and (sub, ses, 'dof')

    current_sub = None
    current_key = None  # (sub, ses, task, run) of the run currently being parsed
    pending_nonss = None  # non-steady-state count seen before Step 5 line
    last_censor = None  # most recent censor line, for pairing with next DOF

    for msg in messages:
        # Session boundary
        m = session_re.search(msg)
        if m:
            current_sub = m.group(1)
            current_key = None

        # Non-steady-state count (always appears on the line just before Step 5)
        m = nonss_re.search(msg)
        if m:
            pending_nonss = int(m.group(1))

        # Step 5 — start of a new run's preprocessing block; sets current_key
        m = mask_step_re.search(msg)
        if m:
            ses_id = m.group(1)  # e.g. "ses-02A"
            task = m.group(2)  # e.g. "nback" or "rest"
            run = int(m.group(3))
            current_key = (current_sub, ses_id, task, run)
            rec = records.setdefault(
                current_key,
                {
                    'subject': current_sub,
                    'session': ses_id,
                    'task': task,
                    'run': run,
                },
            )
            if pending_nonss is not None:
                rec['n_nonss_detected'] = pending_nonss
                pending_nonss = None

        # Preprocessing QC — only valid while current_key is set
        m = tsnr_re.search(msg)
        if m and current_key:
            records[current_key]['tsnr_median'] = float(m.group(1))

        m = dice_re.search(msg)
        if m and current_key:
            records[current_key]['dice_registration'] = float(m.group(1))

        m = trim_re.search(msg)
        if m and current_key:
            records[current_key]['n_nonss_removed'] = int(m.group(1))
            records[current_key]['n_trs_remaining'] = int(m.group(2))

        m = rot_pass_re.search(msg)
        if m and current_key:
            records[current_key]['rotation_check'] = 'PASSED'
            records[current_key]['rotation_max_abs'] = float(m.group(1))

        m = rot_ambig_re.search(msg)
        if m and current_key:
            records[current_key]['rotation_check'] = 'AMBIGUOUS'
            records[current_key]['rotation_max_abs'] = float(m.group(1))

        # Rest censor line — self-identifies sub/ses/run from filename
        m = rest_censor_re.search(msg)
        if m:
            sub, ses_str, run_str, n_cens, n_tot, pct = m.groups()
            run = int(run_str)
            key = (sub, ses_str, 'rest', run)
            rec = records.setdefault(
                key,
                {
                    'subject': sub,
                    'session': ses_str,
                    'task': 'rest',
                    'run': run,
                },
            )
            rec.update(
                {
                    'n_censored': int(n_cens),
                    'n_trs_total': int(n_tot),
                    'pct_censored': float(pct),
                }
            )
            last_censor = {'type': 'rest', 'key': key}

        # Nback concat censor line — stored only on first occurrence
        # (nback_act and nback_conn both emit it with identical values)
        m = nback_censor_re.search(msg)
        if m:
            sub, ses_str, n_cens, n_tot, pct = m.groups()
            nback_key = (sub, ses_str)
            if nback_key not in nback_seen:
                nback_concat[nback_key] = {
                    'n_censored_concat': int(n_cens),
                    'n_trs_total_concat': int(n_tot),
                    'pct_censored_concat': float(pct),
                }
                nback_seen.add(nback_key)
            last_censor = {'type': 'nback', 'nback_key': nback_key}

        # DOF — applies to the most recent censor line
        m = dof_re.search(msg)
        if m and last_censor:
            dof, uncensored, regressors = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if last_censor['type'] == 'rest':
                records[last_censor['key']].update(
                    {
                        'n_uncensored': uncensored,
                        'dof': dof,
                        'n_regressors': regressors,
                    }
                )
            elif last_censor['type'] == 'nback':
                nback_key = last_censor['nback_key']
                dof_marker = (*nback_key, 'dof')
                if dof_marker not in nback_seen:
                    nback_concat.setdefault(nback_key, {}).update(
                        {
                            'n_uncensored_concat': uncensored,
                            'dof_concat': dof,
                            'n_regressors_concat': regressors,
                        }
                    )
                    nback_seen.add(dof_marker)
            last_censor = None

    # Fan nback concat censor/DOF out to each individual nback run record
    for (sub, ses, task, _run), rec in records.items():
        if task == 'nback':
            nback_key = (sub, ses)
            if nback_key in nback_concat:
                rec.update(nback_concat[nback_key])

    return records


# ── input sources ─────────────────────────────────────────────────────────────


def iter_csv_messages(csv_path):
    with open(csv_path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            yield row['message']


def get_streams(log_group, region='<YOUR_AWS_REGION>', prefix=None, after_ms=None):
    """Return stream names from log_group, optionally filtered by prefix and/or minimum last-event time.

    AWS constraint: orderBy=LastEventTime cannot be combined with logStreamNamePrefix.
    When prefix is given we list by name and filter client-side; otherwise we list by
    descending last-event time and short-circuit as soon as we fall below after_ms.
    """
    client = boto3.client('logs', region_name=region)
    paginator = client.get_paginator('describe_log_streams')

    kwargs = {'logGroupName': log_group}
    if prefix:
        kwargs['logStreamNamePrefix'] = prefix
        # orderBy is locked to LogStreamName when a prefix is given
    else:
        kwargs['orderBy'] = 'LastEventTime'
        kwargs['descending'] = True

    streams = []
    for page in paginator.paginate(**kwargs):
        for stream in page.get('logStreams', []):
            last_ts = stream.get('lastEventTimestamp', 0)
            if after_ms is not None and last_ts < after_ms:
                if not prefix:
                    # Ordered by time desc — everything from here will also be older
                    return streams
                continue  # prefix mode: must scan all pages, skip old streams
            streams.append(stream['logStreamName'])

    return streams


def parse_after(value):
    """Parse an ISO-8601-ish date or datetime string and return milliseconds since epoch (UTC)."""
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Cannot parse '{value}' as a date/time. "
        "Use ISO format, e.g.  2026-04-30  or  '2026-04-30 18:00'"
    )


def _fetch_stream(log_group, stream, region, start_time_ms=None):
    """Fetch all messages from one stream. Returns (stream_name, [messages])."""
    client = boto3.client('logs', region_name=region, config=_BOTO_CONFIG)
    paginator = client.get_paginator('filter_log_events')
    kwargs = {'logGroupName': log_group, 'logStreamNames': [stream]}
    if start_time_ms is not None:
        kwargs['startTime'] = start_time_ms
    msgs = []
    try:
        for page in paginator.paginate(**kwargs):
            msgs.extend(e.get('message', '') for e in page.get('events', []))
    except client.exceptions.ResourceNotFoundException as e:
        print(f'  WARNING: {stream}: {e}', file=sys.stderr)
    return stream, msgs


def fetch_and_parse_parallel(log_group, stream_names, region, max_workers=10, start_time_ms=None):
    """Fetch streams concurrently, parse each independently, merge results."""
    all_records = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_fetch_stream, log_group, s, region, start_time_ms): s for s in stream_names
        }
        for future in as_completed(futures):
            stream, msgs = future.result()
            if msgs:
                records = parse_messages(iter(msgs))
                n = len(records)
                print(f'  {stream}  →  {n} run(s)')
                all_records.update(records)
            else:
                print(f'  {stream}  →  skipped (empty or not found)')
    return all_records


# ── main ──────────────────────────────────────────────────────────────────────


OUTPUT_COLUMNS = [
    'subject',
    'session',
    'task',
    'run',
    'n_nonss_detected',
    'n_nonss_removed',
    'n_trs_remaining',
    'tsnr_median',
    'dice_registration',
    'rotation_check',
    'rotation_max_abs',
    # rest per-run censoring
    'n_censored',
    'n_trs_total',
    'pct_censored',
    'n_uncensored',
    'dof',
    'n_regressors',
    # nback concat-level censoring (fanned out to each run)
    'n_censored_concat',
    'n_trs_total_concat',
    'pct_censored_concat',
    'n_uncensored_concat',
    'dof_concat',
    'n_regressors_concat',
]


def main():
    ap = argparse.ArgumentParser(
        description='Extract per-run QC metrics from fmri-first-level-proc logs.'
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument(
        '--csv',
        metavar='PATH',
        help='Local CloudWatch Logs CSV export (columns: timestamp,message,logStreamName)',
    )
    src.add_argument(
        '--stream',
        metavar='NAME',
        nargs='+',
        help='One or more CloudWatch log stream names to fetch live',
    )
    src.add_argument(
        '--all-streams',
        action='store_true',
        help='Fetch all streams in the log group (combine with --prefix / --after to narrow)',
    )
    ap.add_argument(
        '--log-group',
        default='/aws/batch/fmri-first-level-proc',
        metavar='NAME',
        help='CloudWatch log group (default: /aws/batch/fmri-first-level-proc)',
    )
    ap.add_argument('--region', default='<YOUR_AWS_REGION>', help='AWS region (default: <YOUR_AWS_REGION>)')
    ap.add_argument(
        '--prefix',
        metavar='PREFIX',
        help='Only include streams whose names start with PREFIX (CloudWatch modes only)',
    )
    ap.add_argument(
        '--after',
        metavar='DATETIME',
        type=parse_after,
        help='Only include streams with events after this date/time, e.g. "2026-04-30" or '
        '"2026-04-30 18:00" (UTC, CloudWatch modes only)',
    )
    ap.add_argument(
        '--workers',
        type=int,
        default=4,
        metavar='N',
        help='Parallel fetch threads for CloudWatch modes (default: 4; '
        'FilterLogEvents is capped at 5 TPS account-wide)',
    )
    ap.add_argument(
        '--output',
        default='first_level_qc_metrics.csv',
        metavar='PATH',
        help='Output CSV path (default: first_level_qc_metrics.csv)',
    )
    args = ap.parse_args()

    if args.csv:
        records = parse_messages(iter_csv_messages(args.csv))
    else:
        if args.stream:
            streams = args.stream
        else:  # --all-streams
            streams = get_streams(
                args.log_group, args.region, prefix=args.prefix, after_ms=args.after
            )
            if not streams:
                print('No matching streams found.', file=sys.stderr)
                sys.exit(1)
            print(f'Found {len(streams)} stream(s) to process.')
        records = fetch_and_parse_parallel(
            args.log_group,
            streams,
            args.region,
            max_workers=args.workers,
            start_time_ms=args.after,
        )

    if not records:
        print(
            'No records extracted — check that the log file contains Step 5 or censor lines.',
            file=sys.stderr,
        )
        sys.exit(1)

    df = pd.DataFrame(list(records.values()), columns=OUTPUT_COLUMNS)
    df.sort_values(['subject', 'session', 'task', 'run'], inplace=True, ignore_index=True)
    df.to_csv(args.output, index=False)
    print(f'Wrote {len(df)} rows to {args.output}')


if __name__ == '__main__':
    main()
