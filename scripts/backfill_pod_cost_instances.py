#!/usr/bin/env python3
"""Restore the instance and node rates of pod-cost rows phase A left unknown (#1060, phase B).

Why this exists
---------------
Phase A (scripts/backfill_pod_cost_rates.py) set the node-rate columns of every
pod-cost row on a hostname more than one EC2 instance held that day to unknown.
Kubecost's daily rollups merge those instances, so they cannot say which one a
pod ran on. Two other records can, and neither is Kubecost's daily data:

- **Kubecost's hourly asset files** (s3://cloudpipe-finops/federated/cloudpipe/
  etl/bingen/assets/1h/, back to 2026-04-16). One file per hour, and in it one
  Node asset per instance, keyed by instance ID, with that instance's own
  start, end, type and capacity labels. The hourly data keeps reused hostnames
  apart (2026-11-06: 91/91 pods). The files are OpenCost "bingen" binary;
  scripts/kubecost_bingen_decode/ decodes them to JSON lines.
- **The hourly Argo node snapshots** (s3://cloudpipe-metrics/snapshots/
  argo-nodes/, from 2026-08-12). One row per pod attempt with its host and its
  started/finished times, which pod-cost rows do not carry.

A row's instance is **proven** when exactly one instance held the row's
hostname at any time during the pod's running window that day, the hourly data
covers every hour of that window, and the instance's own records agree on one
type and one capacity type. Anything else stays unknown, with its reason.

An hour with no asset file at all (16 between 2026-08-12 and 08-19) does not
block the proof when one instance is on record on both sides of it
(`proven_bridged`): an EC2 instance keeps its private IP, and so its hostname,
for its whole life, so no other instance can have held it in between.

What a proven row gets
----------------------
  node_capacity_type          <- the instance's (Karpenter label, else Kubecost's preemptible flag)
  node_ondemand_usd_per_hour  <- the on-demand list price of the instance's type
  node_instance_type          <- the instance's type, where Kubecost's label named another
  node_effective_usd_per_hour    stays NULL

The effective (reconciled) rate exists only in the daily rollups, which merged
the instances, so it cannot be restored exactly. node_instance_type is corrected
rather than refused (the forward scraper refuses a type mismatch,
kubecost_scraper.resolve_node): Kubecost joins that label onto the pod by
hostname, so on a reused hostname it can name the other instance. Here the
instance is proven by the pod's own times, not by Kubecost's join, so the label
is the value that is wrong. Each correction is counted and audited.

Which rows
----------
Only rows that say no instance is proven, in one of two exact forms, on a
(date, workflow, pod, hostname) in the plan:
- schema 1.1, capacity "" and both rates NULL: what phase A wrote;
- schema 1.2 with no node_instance_id, capacity "" and both rates NULL: what the
  fixed scraper wrote for 2026-11-05 and 11-06, whose day+1 reads were taken by
  the old code and so left nothing to carry into the settled read. These rows
  also get node_instance_id.
A 1.2 row that names an instance is the scraper's own proof and is never
touched. Cost and usage columns are never touched.

One more form gets its on-demand list price and nothing else (`ondemand_only`):
a row that names a type and a capacity but has no on-demand rate, because the
old scraper's price lookup failed at the time. Its hostname had one instance that
day (phase A left it alone), and the price is filled only where the same proof
finds the row's own type and capacity; otherwise `row_disagrees`, left alone.

Safety
------
Phase A's rules and machinery (its jsonl/Parquet round-trip checks, conditional
writes on the ETag read, VersionId audit and rollback):
- **Two steps.** `plan` reads local copies and writes a plan file; `apply` is a
  dry run unless given `--write`.
- **Every rewrite is checked before it is written:** only the four columns
  above may change, and only on planned rows; a raw object's other lines must
  re-serialise byte-identically; a Parquet object must round-trip losslessly.
- **Rollback:** `rollback FILE` restores each object's previous version, where
  the object is still the version this wrote.
- **Idempotent:** a patched row is no longer in phase A's state.

Usage
-----
    # inputs, read-only:
    aws s3 sync s3://cloudpipe-finops/federated/cloudpipe/etl/bingen/assets/1h/ <assets-dir>
    (cd scripts/kubecost_bingen_decode && go build -o kcdecode .) && ./kcdecode <assets-dir> > nodes_1h.jsonl
    aws s3 sync s3://cloudpipe-metrics/snapshots/argo-nodes/ <snapshots-dir>
    aws s3 sync s3://cloudpipe-metrics/metrics/compacted/pod_costs/ <pod-costs-dir>
    # optional, and what makes most blackout rows provable (one JSON line per record):
    #   launches.jsonl       CloudTrail CreateFleet: provider_id, instance_type, lifecycle, launched, subnet_id
    #   ends.jsonl           CloudTrail TerminateInstances + BidEvictedEvent: provider_id, terminated
    #   registrations.jsonl  EKS audit log Node creates: hostname, provider_id, registered
    #   subnets.json         the cluster VPC's subnets: [{subnet_id, cidr}]

    pixi run python scripts/backfill_pod_cost_instances.py plan --nodes nodes_1h.jsonl \\
        --snapshots <snapshots-dir> --pod-costs <pod-costs-dir> --from 2026-08-12 --to 2026-11-05 \\
        --launches launches.jsonl --terminations ends.jsonl --registrations registrations.jsonl \\
        --subnets subnets.json --out plan.json
    pixi run python scripts/backfill_pod_cost_instances.py apply plan.json --bucket cloudpipe-metrics
    pixi run python scripts/backfill_pod_cost_instances.py apply plan.json --bucket cloudpipe-metrics --write
    pixi run python scripts/backfill_pod_cost_instances.py rollback backfill-pod-cost-instances-<ts>.jsonl

The plan names workflows and pods, which can carry subject IDs: keep it outside
the repository.
"""

from __future__ import annotations

import argparse
import bisect
import copy
import glob
import gzip
import ipaddress
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backfill_pod_cost_rates import (  # noqa: E402
    COMPACTED_PREFIX,
    RAW_PREFIX,
    PatchRefused,
    _list,
    _parquet_body,
    _parquet_rows,
    jsonl_body,
    jsonl_rows,
    rollback,
)

log = logging.getLogger("backfill_pod_cost_instances")

HOUR = timedelta(hours=1)
# How far back from a pod's finish its instance must still be up. Kubecost's asset
# windows are minute-resolution, and a reclaimed node's asset can end a moment
# before Argo records the pod's end.
FINISH_MARGIN = timedelta(minutes=2)

# Why a row was or was not proven.
PROVEN = "proven"
# Proven across hours with no asset file: the same instance is on record on both
# sides of the gap. An EC2 instance keeps its private IP, and so its hostname,
# for its whole life, so no other instance can have held it in between.
PROVEN_BRIDGED = "proven_bridged"
# The instance is one of several, all launched (per CloudTrail) with the same type
# and capacity: the price is proven, the instance is not named.
PROVEN_SHARED = "proven_shared"
# Exactly one instance that held the hostname was alive (CloudTrail launch, and
# termination or later records) when the pod finished. An IP has one holder at a
# time, so no other instance, recorded or not, can have held it then.
PROVEN_TENURE = "proven_tenure"
PROVEN_REASONS = frozenset({PROVEN, PROVEN_BRIDGED, PROVEN_SHARED, PROVEN_TENURE})
NO_POD_TIMING = "no_pod_timing"  # no Argo snapshot of the pod
HOST_MISMATCH = "host_mismatch"  # the snapshot puts the pod on another hostname
NO_WINDOW = "no_window"  # the pod's running window does not touch the row's date
NO_COVERAGE = "no_coverage"  # an hour of the window has no asset file, and no instance bridges it
NO_ASSET = "no_asset"  # no instance held the hostname during the window
AMBIGUOUS = "ambiguous"  # more than one did
INCONSISTENT_ASSET = "inconsistent_asset"  # the instance's records disagree on its type or capacity
NO_PRICE = "no_price"  # its type has no on-demand list price
# A row that already names an instance and a capacity, only its list price missing,
# whose proven instance is of another type or capacity. Left alone and counted.
ROW_DISAGREES = "row_disagrees"

# What a planned row gets.
FULL = "full"  # capacity, on-demand rate, type (and, on 1.2, the instance ID)
ONDEMAND_ONLY = "ondemand_only"  # the on-demand list price, nothing else
# A row an earlier phase-B run wrote, whose instance EC2's launch record now
# contradicts: type, capacity and on-demand rate, only if still as written.
CORRECT = "correct"

# How close an instance's record must come to a gap's edge to bridge it. Hourly
# pieces end and begin exactly on the hour; a minute allows for rounding.
BRIDGE_MARGIN = timedelta(minutes=1)

# The columns this may change.
PATCH_COLUMNS = (
    "node_capacity_type",
    "node_ondemand_usd_per_hour",
    "node_instance_type",
    "node_effective_usd_per_hour",
    "node_instance_id",  # schema 1.2 rows only
)


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Instances, from the decoded hourly assets
# ---------------------------------------------------------------------------


@dataclass
class Instance:
    provider_id: str
    hostnames: set[str] = field(default_factory=set)
    windows: list[tuple[datetime, datetime]] = field(default_factory=list)
    types: set[str] = field(default_factory=set)
    capacities: set[str] = field(default_factory=set)
    # EC2's own record of the launch (CloudTrail CreateFleet): (type, capacity).
    launch: tuple[str, str] | None = None
    # When EC2 launched it, and when TerminateInstances was called on it (CloudTrail).
    launched: datetime | None = None
    terminated: datetime | None = None
    # The launch of the next instance to hold any of its hostnames: an IP has one
    # holder at a time, so this one had released it by then.
    next_launch: datetime | None = None

    def alive_at(self, t: datetime) -> bool | None:
        """Held its IP at t? True/False when the record says, None when it cannot.

        Launched at or before t, and either not terminated before t or still on
        record in the hourly data at or after t. TerminateInstances is called
        before the IP is released, so its time is a lower bound on the release.
        """
        if self.launched is None:
            return None
        if self.launched > t:
            return False
        if self.terminated is not None:
            return self.terminated >= t
        if self.next_launch is not None and self.next_launch <= t:
            return False  # the hostname's next holder had launched: this one had let it go
        if any(e >= t for _, e in self.windows):
            return True
        return None

    @property
    def instance_type(self) -> str | None:
        """The launch record's type, else the hourly records' one type, else None.

        Kubecost's hourly records can disagree within one instance (its labels and
        nodeType are joined by hostname inside an hour too); the launch cannot.
        """
        if self.launch:
            return self.launch[0]
        return next(iter(self.types)) if len(self.types) == 1 else None

    @property
    def capacity_type(self) -> str | None:
        if self.launch:
            return self.launch[1]
        return next(iter(self.capacities)) if len(self.capacities) == 1 else None

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return any(s < end and start < e for s, e in self.windows)

    def bridges(self, gap_start: datetime, gap_end: datetime) -> bool:
        """On record up to the gap's start and again from its end: it held the IP throughout."""
        before = any(s < gap_start and e >= gap_start - BRIDGE_MARGIN for s, e in self.windows)
        after = any(s <= gap_end + BRIDGE_MARGIN and e > gap_end for s, e in self.windows)
        return before and after


def capacity_of(row: dict[str, Any]) -> str:
    """The forward scraper's rule (_capacity_type): Karpenter's label, else preemptible.

    The decoder's capacity label falls back to the EKS managed-nodegroup label,
    whose ON_DEMAND is the same fact as preemptible == 0.
    """
    label = row.get("capacity_type_label") or ""
    if label in ("spot", "on-demand"):
        return label
    return "spot" if float(row.get("preemptible") or 0) > 0 else "on-demand"


def build_instances(rows) -> tuple[dict[str, Instance], dict[str, list[Instance]], set[datetime]]:
    """(by instance ID, by hostname, the hour starts that have a file) from decoded asset rows."""
    by_id: dict[str, Instance] = {}
    hours: set[datetime] = set()
    for r in rows:
        hours.add(datetime.fromtimestamp(int(r["file"].split("-")[0]), tz=timezone.utc))
        pid, name = r.get("provider_id") or "", r.get("name") or ""
        start, end = _ts(r.get("start")), _ts(r.get("end"))
        if not pid or not name or start is None or end is None:
            continue
        inst = by_id.setdefault(pid, Instance(pid))
        inst.hostnames.add(name)
        inst.windows.append((start, end))
        for t in (r.get("node_type"), r.get("instance_type_label")):
            if t:
                inst.types.add(t)
        inst.capacities.add(capacity_of(r))
    by_host: dict[str, list[Instance]] = defaultdict(list)
    for inst in by_id.values():
        for h in inst.hostnames:
            by_host[h].append(inst)
    return by_id, dict(by_host), hours


LIFECYCLES = {"spot": "spot", "on-demand": "on-demand", "ondemand": "on-demand"}


def load_launches(rows) -> dict[str, tuple[str, str]]:
    """instance ID -> (type, capacity) from archived CloudTrail CreateFleet launches.

    `rows` are {"provider_id", "instance_type", "lifecycle"} dicts. An ID launched
    twice with different facts (it cannot be) is dropped rather than guessed.
    """
    out: dict[str, tuple[str, str]] = {}
    bad: set[str] = set()
    for r in rows:
        pid, itype = r.get("provider_id"), r.get("instance_type")
        cap = LIFECYCLES.get((r.get("lifecycle") or "").lower())
        if not pid or not itype or not cap:
            continue
        if pid in out and out[pid] != (itype, cap):
            bad.add(pid)
        out[pid] = (itype, cap)
    for pid in bad:
        del out[pid]
    return out


def hostname_ip(name: str):
    """ip-10-0-32-160.<region>.compute.internal -> IPv4Address('10.0.32.160'), else None."""
    m = re.match(r"ip-(\d+)-(\d+)-(\d+)-(\d+)\.", name or "")
    return ipaddress.ip_address(".".join(m.groups())) if m else None


def phantom_launches(launch_rows, recorded: set[str], subnets: dict[str, str] | None):
    """(time, network) of launches of instances the hourly data never recorded.

    With `subnets` (the cluster VPC's subnet ID -> CIDR), a launch into any other
    subnet is outside the cluster's address space (another VPC can reuse the same
    CIDR) and is dropped; without it, every such launch is kept, with no network.
    """
    out = []
    for r in launch_rows:
        pid, t = r.get("provider_id"), _ts(r.get("launched"))
        if not pid or pid in recorded or t is None:
            continue
        if subnets is None:
            out.append((t, None))
        elif r.get("subnet_id") in subnets:
            out.append((t, ipaddress.ip_network(subnets[r["subnet_id"]])))
        elif not r.get("subnet_id"):
            out.append((t, None))  # unknown subnet: could be anywhere
    return out


def attach_registrations(
    by_id: dict[str, Instance],
    by_host: dict[str, list[Instance]],
    registrations,
    launches: dict[str, tuple[str, str]],
) -> int:
    """Add each node registration (EKS audit log: hostname -> instance ID) to the index.

    The registration is Kubernetes' own record that the instance held that hostname,
    so an instance the hourly data never recorded still becomes a candidate (typed
    by its launch record), and the tenure rule can name it. Returns how many
    instances were new.
    """
    new = 0
    for r in registrations:
        pid, host = r.get("provider_id") or "", r.get("hostname") or ""
        if not pid or not host:
            continue
        inst = by_id.get(pid)
        if inst is None:
            inst = by_id[pid] = Instance(pid, launch=launches.get(pid))
            new += 1
        if host not in inst.hostnames:
            inst.hostnames.add(host)
            by_host.setdefault(host, []).append(inst)
    return new


def attach_tenures(by_id: dict[str, Instance], launch_rows, termination_rows) -> None:
    """Each instance's CloudTrail launch time and earliest TerminateInstances call."""
    for r in launch_rows:
        inst, t = by_id.get(r.get("provider_id") or ""), _ts(r.get("launched"))
        if inst is not None and t is not None:
            inst.launched = t if inst.launched is None else min(inst.launched, t)
    for r in termination_rows:
        inst, t = by_id.get(r.get("provider_id") or ""), _ts(r.get("terminated"))
        if inst is not None and t is not None:
            inst.terminated = t if inst.terminated is None else min(inst.terminated, t)
    # Each hostname's holders in launch order: one had released it by the next's launch.
    by_host: dict[str, list[Instance]] = defaultdict(list)
    for inst in by_id.values():
        for h in inst.hostnames:
            by_host[h].append(inst)
    for holders in by_host.values():
        ordered = sorted((i for i in holders if i.launched is not None), key=lambda i: i.launched)
        for prev, nxt in pairwise(ordered):
            if prev.next_launch is None or nxt.launched < prev.next_launch:
                prev.next_launch = nxt.launched


def attach_launches(by_id: dict[str, Instance], launches: dict[str, tuple[str, str]]) -> Counter:
    """Give each instance its launch record; tally how the hourly records compared."""
    tally: Counter[str] = Counter()
    for pid, inst in by_id.items():
        launch = launches.get(pid)
        if launch is None:
            tally["no_launch_record"] += 1
            continue
        hourly = (inst.instance_type, inst.capacity_type)  # before the launch is attached
        inst.launch = launch
        if hourly == launch:
            tally["hourly_agrees"] += 1
        elif hourly[0] is None or hourly[1] is None:
            tally["hourly_inconsistent_resolved"] += 1
        else:
            tally["hourly_consistent_but_wrong"] += 1
    return tally


# ---------------------------------------------------------------------------
# Pod running windows, from the Argo node snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PodTiming:
    host: str
    started: datetime | None
    finished: datetime | None
    seen: datetime  # the newest snapshot that recorded it


def pod_key(workflow: str, pod_or_node_id: str) -> tuple[str, str] | None:
    """(workflow, trailing numeric ID): a pod name and its Argo node ID end in the same number."""
    m = re.search(r"-(\d+)$", pod_or_node_id or "")
    return (workflow, m.group(1)) if workflow and m else None


def load_pod_timings(paths) -> dict[tuple[str, str], PodTiming]:
    """Every pod attempt in the snapshots, each from the newest snapshot that recorded it."""
    out: dict[tuple[str, str], PodTiming] = {}
    for path in paths:
        with gzip.open(path, "rt") as fh:
            for line in fh:
                rec = json.loads(line)
                seen = _ts(rec.get("snapshot_at"))
                for p in rec.get("pods") or []:
                    key = pod_key(rec.get("workflow", ""), p.get("node_id", ""))
                    if key is None or not p.get("host_node"):
                        continue
                    prev = out.get(key)
                    if prev is not None and seen is not None and prev.seen >= seen:
                        continue
                    out[key] = PodTiming(
                        p["host_node"], _ts(p.get("started_at")), _ts(p.get("finished_at")), seen
                    )
    return out


# ---------------------------------------------------------------------------
# The proof
# ---------------------------------------------------------------------------


def _gaps(start: datetime, end: datetime, hours: set[datetime]) -> list[tuple[datetime, datetime]]:
    """Runs of hours in [start, end) with no asset file at all."""
    gaps: list[list[datetime]] = []
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour < end:
        if hour not in hours:
            if gaps and gaps[-1][1] == hour:
                gaps[-1][1] = hour + HOUR
            else:
                gaps.append([hour, hour + HOUR])
        hour += HOUR
    return [(s, e) for s, e in gaps]


@dataclass(frozen=True)
class DataEdges:
    """Every record's start and end, sorted: where Kubecost's data actually stops and resumes.

    Kubecost does not resume on the hour after an outage. After 04:00-09:00Z on
    2026-08-18 the first records start at 09:11:56, so every instance looks absent
    for those minutes too. A gap's real extent runs from the last record anywhere
    before it to the first record anywhere after it.
    """

    starts: tuple[datetime, ...]
    ends: tuple[datetime, ...]
    # (launch time, its subnet's network or None) of instances that never appear
    # in the hourly data (CloudTrail), sorted by time.
    phantoms: tuple[tuple[datetime, Any], ...] = ()

    @classmethod
    def of(cls, instances, phantoms=()) -> DataEdges:
        windows = [w for i in instances for w in i.windows]
        return cls(
            tuple(sorted(s for s, _ in windows)),
            tuple(sorted(e for _, e in windows)),
            tuple(sorted(phantoms, key=lambda p: p[0])),
        )

    def phantom_launch_in(self, start: datetime, end: datetime, ip: Any = None) -> bool:
        """Could a never-recorded instance launched in [start, end] have held `ip`?

        One whose subnet is known holds an IP only inside that subnet; one whose
        subnet is unknown, or a question with no IP, could hold any.
        """
        i = bisect.bisect_left(self.phantoms, (start - BRIDGE_MARGIN,), key=lambda p: (p[0],))
        for t, network in self.phantoms[i:]:
            if t > end + BRIDGE_MARGIN:
                return False
            if network is None or ip is None or ip in network:
                return True
        return False

    def dark(self, gap_start: datetime, gap_end: datetime) -> tuple[datetime, datetime]:
        """The gap widened to the data's own edges."""
        i = bisect.bisect_right(self.ends, gap_start + BRIDGE_MARGIN)
        before = self.ends[i - 1] if i else gap_start
        j = bisect.bisect_left(self.starts, gap_end - BRIDGE_MARGIN)
        after = self.starts[j] if j < len(self.starts) else gap_end
        return min(before, gap_start), max(after, gap_end)


def _bridging(candidates: list[Instance], gaps) -> set[str] | None:
    """The instances that bridge each gap, or None if any gap has not exactly one."""
    bridged: set[str] = set()
    for gap_start, gap_end in gaps:
        bridging = [i for i in candidates if i.bridges(gap_start, gap_end)]
        if len(bridging) != 1:
            return None
        bridged.add(bridging[0].provider_id)
    return bridged


def _window(day: date, timing: PodTiming) -> tuple[datetime, datetime]:
    """The pod's window, clipped to the row's day."""
    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = min(timing.finished or timing.seen, day_start + timedelta(days=1))
    return max(timing.started, day_start), end


def resolve(
    day: date,
    node: str,
    timing: PodTiming | None,
    by_host: dict[str, list[Instance]],
    hours: set[datetime],
    edges: DataEdges | None = None,
) -> tuple[Instance | None, str]:
    """The instance a pod-cost row's pod ran on, or None and the reason.

    `edges`, when given, widens each missing-hour gap to where the data really
    stops and resumes; without it a gap is its missing hours exactly.
    """
    if timing is None or timing.started is None:
        return None, NO_POD_TIMING
    if timing.host != node:
        return None, HOST_MISMATCH
    start, end = _window(day, timing)
    if end <= start:
        return None, NO_WINDOW
    gaps = _gaps(start, end, hours)
    if edges is not None:
        gaps = [edges.dark(s, e) for s, e in gaps]
    candidates = by_host.get(node, [])
    # A gap is safe only when one instance held the hostname on both sides of it.
    bridged = _bridging(candidates, gaps)
    if bridged is None:
        return None, NO_COVERAGE

    def up(inst: Instance, s: datetime, e: datetime) -> bool:
        """On record in [s, e), counting the gaps it bridges."""
        if inst.overlaps(s, e):
            return True
        return inst.provider_id in bridged and any(gs < e and s < ge for gs, ge in gaps)

    held = [i for i in candidates if up(i, start, end)]
    if not held:
        return None, NO_ASSET
    if len(held) > 1:
        # Argo's started_at counts Pending, which can begin before the pod's own
        # instance exists and so overlap the instance that held the IP before it.
        # A pod runs on one instance for its whole running life, and it was running
        # at the end of its window (its finish, or the day's end), so the instance
        # holding the hostname then is the one; exactly one may.
        probe = end - FINISH_MARGIN
        at_end = [i for i in held if up(i, probe, end)]
        if len(at_end) != 1:
            return None, AMBIGUOUS
        held = at_end
    inst = held[0]
    if inst.instance_type is None or inst.capacity_type is None:
        return None, INCONSISTENT_ASSET
    return inst, PROVEN_BRIDGED if inst.provider_id in bridged else PROVEN


def _by_tenure(host_instances: list[Instance], t: datetime, lo: datetime) -> Instance | None:
    """The one instance that held the hostname at t, by its launch and termination, else None.

    An IP has one holder at a time, so if exactly one instance known to have held
    this hostname was alive at t, no other, recorded or not, held it then. Refused
    if any instance on the hostname that was around from `lo` on has no launch
    record and no later records, since it cannot be ruled in or out.
    """
    alive = [i for i in host_instances if i.alive_at(t) is True]
    # Status unknown and plausibly around: on record from lo on, or known only from
    # its node registration (no hourly records at all), which says nothing of its end.
    unknown = [
        i
        for i in host_instances
        if i.alive_at(t) is None and (not i.windows or any(e >= lo for _, e in i.windows))
    ]
    if len(alive) != 1 or unknown:
        return None
    inst = alive[0]
    return inst if inst.instance_type is not None and inst.capacity_type is not None else None


def _shared_facts(instances) -> Instance | None:
    """One stand-in for instances that all have the same known type and capacity, else None."""
    facts = {(i.instance_type, i.capacity_type) for i in instances}
    if not instances or len(facts) != 1:
        return None
    itype, cap = next(iter(facts))
    if itype is None or cap is None:
        return None
    return Instance("", launch=(itype, cap))


def resolve_price(
    day: date,
    node: str,
    timing: PodTiming | None,
    by_host: dict[str, list[Instance]],
    hours: set[datetime],
    edges: DataEdges | None = None,
) -> tuple[Instance | None, str]:
    """`resolve`, or, where it cannot name the instance, the price every candidate shares.

    Two cases leave the instance unknown but its type and capacity fixed:
    - AMBIGUOUS: the pod ran on one of the instances up at its finish;
    - NO_COVERAGE: the pod ran on an instance on record next to or inside the
      window, or on one that launched inside an unrecorded gap and was never on
      record at all. The last is ruled out only when CloudTrail shows no launch,
      cluster-wide, of a never-recorded instance inside any of the gaps.
    If every candidate has the same type and capacity per its launch record, the
    row's price is proven without naming the instance (PROVEN_SHARED; no ID).
    """
    inst, reason = resolve(day, node, timing, by_host, hours, edges)
    if reason not in (AMBIGUOUS, NO_COVERAGE, NO_ASSET) or edges is None or timing is None:
        return inst, reason
    start, end = _window(day, timing)
    gaps = [edges.dark(s, e) for s, e in _gaps(start, end, hours)]
    lo = min([start] + [s for s, _ in gaps]) - BRIDGE_MARGIN
    hi = max([end] + [e for _, e in gaps]) + BRIDGE_MARGIN
    # NO_ASSET too: an instance with no hourly records in the window (a blackout
    # Kubecost never filled) can still be named by its tenure of the hostname.
    tenure = _by_tenure(by_host.get(node, []), max(start, end - FINISH_MARGIN), lo)
    if tenure is not None:
        return tenure, PROVEN_TENURE
    if reason == NO_ASSET:
        return None, reason
    ip = hostname_ip(node)
    if any(edges.phantom_launch_in(s, e, ip) for s, e in gaps):
        return None, reason
    candidates = [i for i in by_host.get(node, []) if i.overlaps(lo, hi)]
    if any(i.launch is None for i in candidates):  # only EC2's own record is trusted here
        return None, reason
    shared = _shared_facts(candidates)
    return (shared, PROVEN_SHARED) if shared else (None, reason)


def in_phase_a_state(row: dict[str, Any]) -> bool:
    """A row whose instance nothing has proven, in one of the two exact forms that say so.

    - Schema 1.1, capacity unknown, both rates NULL: what phase A wrote.
    - Schema 1.2 with no node_instance_id, capacity unknown, both rates NULL: what
      the fixed scraper writes when it cannot prove the instance and has no day+1
      record to carry (dt=2026-11-05 and 11-06, whose day+1 reads predate 1.2).

    A 1.2 row that names an instance is the scraper's proof and is never touched.
    """
    schema = row.get("schema_version")
    if schema == "1.2":
        if row.get("node_instance_id"):
            return False
    elif schema != "1.1":
        return False
    return (
        (row.get("node_capacity_type") or "") == ""
        and row.get("node_effective_usd_per_hour") is None
        and row.get("node_ondemand_usd_per_hour") is None
    )


def only_price_missing(row: dict[str, Any]) -> bool:
    """A row that names a type and a capacity but has no on-demand list price.

    Phase A left these alone, because their hostname had one instance that day. The
    old scraper simply failed to price the type at the time (ec2_pricing returns
    None on any lookup failure). Only the price is filled, and only where the
    row's own type and capacity are what the proof finds.
    """
    return (
        row.get("schema_version") in ("1.1", "1.2")
        and bool(row.get("node_instance_type"))
        and bool(row.get("node_capacity_type"))
        and row.get("node_ondemand_usd_per_hour") is None
    )


def written_by_phase_b(row: dict[str, Any]) -> bool:
    """Schema 1.1 with a type, capacity and on-demand rate but no effective rate.

    That is what phase B writes (the effective rate stays NULL). The old scraper
    wrote an effective rate wherever it wrote the others.
    """
    return (
        row.get("schema_version") == "1.1"
        and bool(row.get("node_instance_type"))
        and bool(row.get("node_capacity_type"))
        and row.get("node_ondemand_usd_per_hour") is not None
        and row.get("node_effective_usd_per_hour") is None
    )


def _mode(row: dict[str, Any]) -> str | None:
    """What a plan may do to this row, or None to leave it out."""
    if in_phase_a_state(row):
        return FULL
    if only_price_missing(row):
        return ONDEMAND_ONLY
    if written_by_phase_b(row):
        return CORRECT
    return None


def build_plan(
    nodes_path: Path,
    snapshot_paths,
    pod_cost_paths,
    start: date,
    end: date,
    price,
    launch_rows: list[dict] | None = None,
    subnets: dict[str, str] | None = None,
    termination_rows: list[dict] | None = None,
    registrations: list[dict] | None = None,
) -> dict:
    """Prove each phase-A row's instance. `price(instance_type)` -> on-demand USD/h or None.

    `launch_rows` (archived CloudTrail CreateFleet launches) make EC2's own launch
    record the authority on an instance's type and capacity, and allow a price
    shared by every candidate instance (PROVEN_SHARED). `subnets` (the cluster
    VPC's subnet ID -> CIDR) narrows which never-recorded launches could have held
    a given IP. `termination_rows` (archived TerminateInstances calls and spot
    reclaims), with the launches, give each instance's tenure of its IP
    (PROVEN_TENURE). `registrations` (EKS audit log node creates: hostname ->
    instance ID) add instances the hourly data never recorded.
    """
    with nodes_path.open() as fh:
        by_id, by_host, hours = build_instances(json.loads(line) for line in fh)
    launch_tally: Counter[str] = Counter()
    phantoms: list = []
    if launch_rows:
        launch_tally = attach_launches(by_id, load_launches(launch_rows))
        if registrations:
            launch_tally["registered_only"] = attach_registrations(
                by_id, by_host, registrations, load_launches(launch_rows)
            )
        # Phantoms: launches neither the hourly data nor a node registration accounts for.
        phantoms = phantom_launches(launch_rows, set(by_id), subnets)
        attach_tenures(by_id, launch_rows, termination_rows or [])
    edges = DataEdges.of(by_id.values(), phantoms)
    timings = load_pod_timings(snapshot_paths)
    import pyarrow.parquet as pq

    days: dict[str, dict[str, dict]] = defaultdict(dict)
    reasons: Counter[str] = Counter()
    relabelled = 0
    for path in pod_cost_paths:
        for row in (
            pq.ParquetFile(path).read().to_pylist()
        ):  # no hive partitioning: dt= is in the rows
            day = date.fromisoformat(str(row["date"]))
            if not (start <= day <= end):
                continue
            mode = _mode(row)
            if mode is None:
                continue
            key = pod_key(row.get("workflow_name", ""), row.get("pod", ""))
            inst, reason = resolve_price(
                day,
                row.get("node") or "",
                timings.get(key) if key else None,
                by_host,
                hours,
                edges,
            )
            entry: dict[str, Any] = {"node": row.get("node"), "reason": reason, "mode": mode}
            if (
                inst is not None
                and mode == ONDEMAND_ONLY
                and (
                    inst.instance_type != row.get("node_instance_type")
                    or inst.capacity_type != row.get("node_capacity_type")
                )
            ):
                inst, reason = None, ROW_DISAGREES
                entry["reason"] = reason
            if mode == CORRECT:
                # Only a row the proof now contradicts is planned; the rest stand as written.
                if inst is None or (inst.instance_type, inst.capacity_type) == (
                    row.get("node_instance_type"),
                    row.get("node_capacity_type"),
                ):
                    reasons[f"{mode}:{'agrees' if inst is not None else 'unproven'}"] += 1
                    continue
                entry.update(
                    old_type=row.get("node_instance_type"),
                    old_capacity=row.get("node_capacity_type"),
                    old_ondemand=row.get("node_ondemand_usd_per_hour"),
                )
            if inst is not None:
                rate = price(inst.instance_type)
                if rate is None:
                    reason = entry["reason"] = NO_PRICE
                else:
                    entry.update(
                        instance_id=inst.provider_id,
                        instance_type=inst.instance_type,
                        capacity_type=inst.capacity_type,
                        ondemand_usd_per_hour=rate,
                        labelled_type=row.get("node_instance_type") or "",
                    )
                    relabelled += (
                        bool(entry["labelled_type"])
                        and entry["labelled_type"] != inst.instance_type
                    )
            reasons[f"{mode}:{reason}"] += 1
            days[day.isoformat()][f"{row['workflow_name']}\t{row['pod']}"] = entry
    return {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "reasons": dict(sorted(reasons.items())),
        "relabelled": relabelled,
        "launch_records": dict(sorted(launch_tally.items())),
        "days": {d: days[d] for d in sorted(days)},
    }


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _without_patch_columns(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in PATCH_COLUMNS}


def patch_rows(
    rows: list[dict[str, Any]], day_plan: dict[str, dict]
) -> tuple[list[dict[str, Any]], int]:
    """A patched copy of an object's rows, and how many were patched.

    Refuses if anything but the node columns changed, if the effective rate changed,
    or if an on-demand-only row changed anything but its on-demand rate.
    """
    patched = copy.deepcopy(rows)
    n = 0
    for row in patched:
        entry = day_plan.get(f"{row.get('workflow_name')}\t{row.get('pod')}")
        if not _patchable(row, entry):
            continue
        if row.get("node") != entry["node"]:
            raise PatchRefused(
                f"row for {row.get('pod')} is on {row.get('node')}, the plan says {entry['node']}"
            )
        row["node_ondemand_usd_per_hour"] = entry["ondemand_usd_per_hour"]
        if entry.get("mode", FULL) in (FULL, CORRECT):
            row["node_capacity_type"] = entry["capacity_type"]
            row["node_instance_type"] = entry["instance_type"]
            # 1.1 has no such column, never add one; a shared-price proof names no instance.
            if row.get("schema_version") == "1.2" and entry.get("instance_id"):
                row["node_instance_id"] = entry["instance_id"]
        n += 1
    _check_patch(patched, rows)
    return patched, n


def _patchable(row: dict[str, Any], entry: dict | None) -> bool:
    """The plan proves this row, and the row is still in the state the plan saw."""
    if not entry or entry.get("reason") not in PROVEN_REASONS:
        return False
    mode = entry.get("mode", FULL)
    if mode == FULL:
        return in_phase_a_state(row)
    if mode == CORRECT:
        return written_by_phase_b(row) and (
            row.get("node_instance_type"),
            row.get("node_capacity_type"),
            row.get("node_ondemand_usd_per_hour"),
        ) == (entry["old_type"], entry["old_capacity"], entry["old_ondemand"])
    return (
        only_price_missing(row)
        and row.get("node_instance_type") == entry["instance_type"]
        and row.get("node_capacity_type") == entry["capacity_type"]
    )


def _check_patch(patched: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    """Refuse a patch that changed anything it may not."""
    if [_without_patch_columns(r) for r in patched] != [_without_patch_columns(r) for r in rows]:
        raise PatchRefused("patch changed something other than the node columns")
    for r, o in zip(patched, rows, strict=True):
        if r.get("node_effective_usd_per_hour") != o.get("node_effective_usd_per_hour"):
            raise PatchRefused("patch changed an effective rate")
        if r != o and only_price_missing(o):
            changed = {k for k in r if r.get(k) != o.get(k)}
            if changed != {"node_ondemand_usd_per_hour"}:
                raise PatchRefused(f"an on-demand-only row changed {sorted(changed)}")


def patch_object(s3, bucket: str, key: str, day_plan: dict[str, dict], write: bool):
    """Read, patch and (with write) conditionally rewrite one object."""
    obj = s3.get_object(Bucket=bucket, Key=key)
    body, etag, prev_version = obj["Body"].read(), obj["ETag"], obj.get("VersionId")
    if key.endswith(".parquet"):
        schema, rows = _parquet_rows(body)
        patched, n = patch_rows(rows, day_plan)
        new_body = _parquet_body(schema, patched) if n else body
        content_type = "application/octet-stream"
    else:
        patched, n = patch_rows(jsonl_rows(body), day_plan)
        new_body = jsonl_body(patched)
        content_type = "application/json"
    if not n or not write:
        return n, None
    resp = s3.put_object(
        Bucket=bucket, Key=key, Body=new_body, ContentType=content_type, IfMatch=etag
    )
    return n, {
        "bucket": bucket,
        "key": key,
        "patched": n,
        "prev_version": prev_version,
        "new_version": resp.get("VersionId"),
    }


def apply(s3, plan: dict[str, Any], bucket: str, write: bool, audit_path: Path | None) -> int:
    failures = 0
    totals = {"objects": 0, "rows": 0}
    audit = audit_path.open("a") if audit_path else None
    try:
        for dt, day_plan in sorted(plan["days"].items()):
            if not any(e.get("reason") in PROVEN_REASONS for e in day_plan.values()):
                continue
            keys = [o["Key"] for o in _list(s3, bucket, f"{RAW_PREFIX}dt={dt}/")]
            keys += [o["Key"] for o in _list(s3, bucket, f"{COMPACTED_PREFIX}dt={dt}/") if o["Key"].endswith(".parquet")]  # fmt: skip
            day_rows = {"raw": 0, "compacted": 0}
            for key in keys:  # raw first
                try:
                    n, entry = patch_object(s3, bucket, key, day_plan, write)
                except PatchRefused as exc:
                    failures += 1
                    log.error("SKIPPED %s: %s", key, exc)
                    continue
                except Exception as exc:  # e.g. 412 PreconditionFailed: changed underneath
                    failures += 1
                    log.error("FAILED %s: %s", key, exc)
                    continue
                if n:
                    totals["objects"] += 1
                    totals["rows"] += n
                    day_rows["compacted" if key.endswith(".parquet") else "raw"] += n
                if entry and audit:
                    audit.write(json.dumps(entry) + "\n")
                    audit.flush()
            log.info("dt=%s rows raw=%d compacted=%d %s", dt, day_rows["raw"], day_rows["compacted"],
                     "rewritten" if write else "(dry run)")  # fmt: skip
            if day_rows["raw"] != day_rows["compacted"]:
                log.warning("dt=%s: raw and compacted patched different row counts (%d vs %d)",
                            dt, day_rows["raw"], day_rows["compacted"])  # fmt: skip
    finally:
        if audit:
            audit.close()
    print(
        f"\n{'Patched' if write else 'Would patch'} {totals['rows']} row(s) in {totals['objects']} object(s)"
    )
    return failures


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument(
        "--nodes", type=Path, required=True, help="decoded hourly Node assets (JSON lines)"
    )
    p.add_argument(
        "--snapshots", type=Path, required=True, help="local copy of snapshots/argo-nodes/"
    )
    p.add_argument(
        "--pod-costs", type=Path, required=True, help="local copy of metrics/compacted/pod_costs/"
    )
    p.add_argument("--from", dest="start", type=date.fromisoformat, default=date(2026, 8, 12))
    p.add_argument("--to", dest="end", type=date.fromisoformat, default=date(2026, 11, 4))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--launches",
        type=Path,
        help="archived CloudTrail CreateFleet launches (JSON lines: provider_id, instance_type, "
        "lifecycle, launched, subnet_id); makes EC2's launch record the authority on type "
        "and capacity",
    )
    p.add_argument(
        "--subnets",
        type=Path,
        help="the cluster VPC's subnets (JSON list of {subnet_id, cidr}), from "
        "`aws ec2 describe-subnets --filters Name=vpc-id,Values=<cluster VPC>`",
    )
    p.add_argument(
        "--terminations",
        type=Path,
        help="archived CloudTrail TerminateInstances calls (JSON lines: provider_id, "
        "terminated) and spot reclaims (BidEvictedEvent, same shape); with --launches, "
        "each instance's tenure of its IP",
    )
    p.add_argument(
        "--registrations",
        type=Path,
        help="node registrations from the EKS audit log (JSON lines: hostname, provider_id, "
        "registered); with --launches, instances the hourly data never recorded",
    )
    a = sub.add_parser("apply")
    a.add_argument("plan", type=Path)
    a.add_argument("--bucket", required=True)
    a.add_argument("--write", action="store_true")
    r = sub.add_parser("rollback")
    r.add_argument("audit", type=Path)
    for s in (p, a, r):
        # Unset: the deployment's own region (ec2_pricing / the AWS profile), as in phase A.
        s.add_argument("--region", default=None)
    for s in (a, r):
        s.add_argument("--profile", default=None)
    args = ap.parse_args()

    if args.cmd == "plan":
        from metrics.ec2_pricing import ondemand_hourly_usd

        snaps = sorted(glob.glob(str(args.snapshots / "dt=*" / "argo-nodes-*.jsonl.gz")))
        pcs = sorted(
            glob.glob(str(args.pod_costs / "dt=*" / "schema_version=1.[12]" / "*.parquet"))
        )
        launch_rows = None
        if args.launches:
            text = args.launches.read_text()
            launch_rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        subnets = None
        if args.subnets:
            subnets = {s["subnet_id"]: s["cidr"] for s in json.loads(args.subnets.read_text())}
        terminations = None
        if args.terminations:
            text = args.terminations.read_text()
            terminations = [json.loads(line) for line in text.splitlines() if line.strip()]
        registrations = None
        if args.registrations:
            text = args.registrations.read_text()
            registrations = [json.loads(line) for line in text.splitlines() if line.strip()]
        plan = build_plan(args.nodes, snaps, pcs, args.start, args.end,
                          lambda t: ondemand_hourly_usd(t, region=args.region), launch_rows,
                          subnets, terminations, registrations)  # fmt: skip
        args.out.write_text(json.dumps(plan, indent=1) + "\n")
        print(f"wrote {args.out}: {plan['reasons']}, relabelled {plan['relabelled']}, "
              f"launch records {plan['launch_records']}")  # fmt: skip
        return 0

    import boto3

    s3 = boto3.Session(profile_name=args.profile).client("s3", region_name=args.region)
    if args.cmd == "apply":
        plan = json.loads(args.plan.read_text())
        audit = None
        if args.write:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            audit = Path(f"backfill-pod-cost-instances-{stamp}.jsonl")
            print(f"audit: {audit}")
        return 1 if apply(s3, plan, args.bucket, args.write, audit) else 0
    return 1 if rollback(s3, args.audit) else 0


if __name__ == "__main__":
    sys.exit(main())
