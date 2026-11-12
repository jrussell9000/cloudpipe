"""Efficiency: time and compute per session, cloudpipe against fMRIPrep (methods 8.1).

Descriptive, no margin. Everything here is a pure function over plain tables;
the only I/O is the small CLI at the bottom, which reads local files the caller
assembled (pod cost rows, step timings, archived Nipype logs) and writes CSVs.
Nothing here calls AWS.

The plan, pre-specified 2026-11-07 before the settled costs were read, fixes:

- **Four measures per session, in total and per stage.** Time to result
  (running time; cloudpipe along its critical path, parallel steps overlapping);
  resources (CPU core-hours, GPU-hours, memory GB-hours, never summed) and cost
  at on-demand prices; utilisation (used over requested); throughput (sessions
  per node-hour and per dollar, derived from the resources).
- **Per-session ratios**, fMRIPrep's value over cloudpipe's for the same
  session, summarised as median, IQR and 5th-95th percentile.
- **cloudpipe's anatomy in full and as 1/k**, k the subject's anatomical
  session count in that workflow.

Revised 2026-11-09, after the settled costs were read (disclosed in methods 8.1):

- **fMRIPrep's best case is dropped.** It charged fMRIPrep only for the cores it
  used while keeping the run time of the cores it reserved: no configuration
  fMRIPrep supports does both. Utilisation stays, as a measured result.
- **The cohort projection is dropped.** Its calendar time on a fixed node budget
  was set by the budget's CPU/GPU split, a choice of ours, not a measurement.
- **Three cost views** (`COST_VIEWS`), each fMRIPrep's column over cloudpipe's:
  - *as operated* (the headline): cloudpipe on spot at the spot price it paid,
    its reclaimed earlier attempts included; fMRIPrep on on-demand, as run;
  - *fMRIPrep on spot* (a projection, labelled): fMRIPrep's spot price scaled by
    the expected reclaim losses, with and without a persistent working volume,
    at the measured reclaim rate and its monthly range;
  - *both on-demand*: both at on-demand list price, which isolates compute
    efficiency from the pricing of capacity.
  The per-row operated rate and the projection come from the caller's assembly.

The fixed mappings live below as data: cloudpipe step -> stage, the fMRIPrep
Nipype workflow path -> stage, the fMRIPrep nodes that redo work ABCD's minimal
processing already applied, and the nodes cloudpipe performs by another method.

Cost at on-demand prices, per pod cost row (a pod has one row per report date)::

    node_h     = max(cpu_core_hours / vCPU,
                     ram_gb_hours   / memory GiB,
                     gpu card-hours / GPUs)       # the node-hours the pod held
    on_demand  = node_h * node_ondemand_usd_per_hour + pv + network

`node_h` is the pod's dominant share of its node times its hours: the part of
the node no other pod could use. It is also what the throughput measure counts
as node-hours. The node's specification comes from `INSTANCE_SPECS`, its list
price from the row. GPU card-hours are the cluster's GPU-hours (one time slice
per pod) over the slices per card in force that day (`gpu_slices`), so a
time-sliced pod is charged its share of the card.

This replaces, for per-pod use, the repository's batch-level counterfactual
(`spot_savings`: compute cost x on-demand rate / effective rate). Per pod the
effective rate is unreliable in these records: the multiplier it implies runs
from 0.13 to 200 within one instance type, because a node's asset record is
joined by hostname and the cluster reuses private IPs. The list price and the
pod's own resource-hours do not depend on that join.

The same IP reuse can give a pod the wrong instance type. A row whose recorded
type is not one the step's pool provisions, or could not hold the pod
(`attribution_problem`), is unpriced and its pod's cost is NaN: never
estimated. Core-hours and GB-hours are the pod's own and stay. Every row is
also marked settled or not (`scrape_age_days >= 3`).
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ANATOMY = "anatomy"
T1W_TO_MNI = "t1w-to-mni"
BOLD_TO_T1W = "bold-to-t1w"
BOLD_PROCESSING = "bold-processing"
STAGES: tuple[str, ...] = (ANATOMY, T1W_TO_MNI, BOLD_TO_T1W, BOLD_PROCESSING)
OTHER = "other/report"
UNMAPPED = "unmapped"
# fMRIPrep's pod runs, but no Nipype node is in flight: container start, BIDS
# indexing, graph construction, and the scheduler's gaps between nodes. Kept as
# its own line so the per-stage split always reconciles to the pod total.
NO_NODE = "no-node-running"

SETTLED_SCRAPE_AGE_DAYS = 3

#: Cost partitions that never reached SETTLED_SCRAPE_AGE_DAYS but whose USAGE is
#: verified final, so their rows count as settled here. Only usage enters this
#: reader's prices (usage x the instance's list price), so only usage needs to be
#: final. Never re-scrape them to settle them: Kubecost now prices pre-2026-09-27
#: history at ~2x the values settled at the time (#1060, methods section 8).
USAGE_FINAL_PARTITIONS = {
    # Stuck at scrape_age_days 1. A read-only re-read on 2026-11-07 matched all
    # 31,033 stored pods on cpu_core_hours, ram_gb_hours, gpu_hours and runtime
    # (median ratio 1.000 each); only total_cost_usd moved (x0.50, the repricing).
    "2026-09-03",
}

# --------------------------------------------------------------------------
# cloudpipe: step -> stage, and the DAG the critical path runs over
# --------------------------------------------------------------------------

# Step names as the per-pod cost records label them (`cloudpipe.io/step`).
CLOUDPIPE_STEP_STAGE: Mapping[str, str] = {
    "template-build": ANATOMY,  # FastSurfer template segmentation, GPU
    "template-parcellation": ANATOMY,  # FastSurfer template surfaces, CPU
    "long-segmentation": ANATOMY,  # FastSurfer longitudinal segmentation, GPU
    "long-parcellation": ANATOMY,  # FastSurfer longitudinal surfaces, CPU
    "t1w-to-mni": T1W_TO_MNI,  # fireANTs, GPU
    "bold-to-t1w": BOLD_TO_T1W,  # SynthMorph rigid
    "bold-preprocessing": BOLD_PROCESSING,  # functional preprocessing (preproc.py/AFNI)
    "surface-resample": BOLD_PROCESSING,  # grayordinates
}
# Excluded from every figure (methods 8): no fMRIPrep counterpart (subregions,
# fsqc), or not compute on the session (transfer, bookkeeping, recorders).
CLOUDPIPE_EXCLUDED_STEPS: Mapping[str, str] = {
    "segment-gems": "subregion segmentation: no fMRIPrep counterpart",
    "segment-dl": "subregion segmentation: no fMRIPrep counterpart",
    "fsqc-metrics": "anatomical QC: no fMRIPrep counterpart",
    "globus-transfer": "input transfer: operating context, not compute",
    "https-transfer": "input transfer: operating context, not compute",
    "delete-globus-input": "transfer bookkeeping",
    "start-globus-instance": "transfer bookkeeping",
    "subject-data-inventory": "bookkeeping",
    "record-step-outcome": "bookkeeping",
    "workflow-run-metrics": "bookkeeping",
    "workflow-start-marker": "bookkeeping",
    "workflow-verdict": "bookkeeping",
    "published-sessions": "bookkeeping",
}

# Each step's prerequisites in the master workflow's DAG
# (cloudpipe-long-master-workflow-template.yaml): the template build feeds the
# template parcellation and the longitudinal segmentation; the longitudinal
# parcellation waits on both; the session-level registration (t1w-to-mni and
# bold-to-t1w side by side) waits on the anatomy; functional preprocessing waits
# on registration; surface resampling on functional preprocessing.
CLOUDPIPE_DAG: Mapping[str, tuple[str, ...]] = {
    "template-build": (),
    "template-parcellation": ("template-build",),
    "long-segmentation": ("template-build",),
    "long-parcellation": ("long-segmentation", "template-parcellation"),
    "t1w-to-mni": ("long-parcellation",),
    "bold-to-t1w": ("long-parcellation",),
    "bold-preprocessing": ("t1w-to-mni", "bold-to-t1w"),
    "surface-resample": ("bold-preprocessing",),
}

# --------------------------------------------------------------------------
# fMRIPrep: Nipype workflow path -> stage, and the attribution tags
# --------------------------------------------------------------------------
# Paths are the node's working-directory location with the subject, session and
# run tokens removed (`normalise_node_path`): `anat_fit_wf.register_template_wf.
# registration`, `bold_wf.bold_fit_wf.bold_hmc_wf.mcflirt`. Short names repeat
# across workflows (`ds_bold`, `val_bold`), so nothing maps on them. The longest
# matching prefix wins; a path no rule matches is UNMAPPED and reported.
# Taken from fMRIPrep 25.2.5's own logs (the 100 measured sessions).

FMRIPREP_STAGE_RULES: tuple[tuple[str, str], ...] = (
    # --- anatomy: FreeSurfer recon-all with fMRIPrep's anatomical workflow
    ("anat_fit_wf", ANATOMY),
    ("surface_derivatives_wf", ANATOMY),
    ("curv_wf", ANATOMY),
    ("hcp_morphometrics_wf", ANATOMY),
    ("morph_grayords_wf", ANATOMY),
    # --- T1w to MNI: ANTs SyN, and the anatomical outputs it resamples to MNI
    ("anat_fit_wf.register_template_wf", T1W_TO_MNI),
    ("ds_std_volumes_wf", T1W_TO_MNI),
    # --- BOLD to T1w: bbregister, with its BOLD reference
    ("bold_wf.bold_fit_wf.bold_reg_wf", BOLD_TO_T1W),
    ("bold_wf.bold_fit_wf.enhance_and_skullstrip_bold_wf", BOLD_TO_T1W),
    ("bold_wf.bold_fit_wf.ds_coreg_boldref_wf", BOLD_TO_T1W),
    ("bold_wf.bold_fit_wf.ds_boldreg_wf", BOLD_TO_T1W),
    ("bold_wf.bold_fit_wf.ds_boldmask_wf", BOLD_TO_T1W),
    # --- BOLD processing: reference, motion, resampling, confounds, CIFTI
    ("bold_wf", BOLD_PROCESSING),
    # MSM-sulc is fMRIPrep's route to fsLR; cloudpipe's counterpart
    # (sphere.reg + the HCP fsLR-deformed sphere) lives in surface resampling.
    ("anat_fit_wf.msm_sulc_wf", BOLD_PROCESSING),
    # The rest of the fsLR route, likewise cloudpipe's surface resampling:
    # sphere.reg projected onto the fsLR sphere, the surfaces resampled to it,
    # and the MNI6 template the CIFTI subcortical block is sampled on.
    ("anat_fit_wf.fsLR_reg_wf", BOLD_PROCESSING),
    ("resample_surfaces_wf", BOLD_PROCESSING),
    ("select_MNI6_tpl", BOLD_PROCESSING),
    # --- reports and bookkeeping
    ("bold_wf.carpetplot_wf", OTHER),
    ("bold_wf.bold_fit_wf.func_fit_reports_wf", OTHER),
    ("anat_fit_wf.anat_reports_wf", OTHER),
    ("bidssrc", OTHER),
    ("bids_info", OTHER),
    ("about", OTHER),
    ("summary", OTHER),
    ("ds_report_summary", OTHER),
    ("ds_report_about", OTHER),
    ("source_anatomical", OTHER),
    ("create_fs_id", OTHER),
)

# Work ABCD's minimal processing already applied (methods 2: head motion,
# B0 distortion, gradient nonlinearity, between-scan motion), which fMRIPrep
# repeats. B0 correction and slice timing are switched off (`--ignore fieldmaps
# slicetiming`) and fMRIPrep has no gradient-nonlinearity step, so neither has a
# node here. Between-scan motion is not repeated as such: each run is
# registered to the T1w independently, which is BOLD-to-T1w work both pipelines do.
REDUNDANT_UPSTREAM: tuple[tuple[str, str], ...] = (
    (
        "bold_wf.bold_fit_wf.bold_hmc_wf",
        "head-motion estimation (mcflirt, its ITK conversion and normalisation) "
        "on BOLD ABCD has already motion-corrected",
    ),
    (
        "bold_wf.bold_fit_wf.ds_hmc_wf",
        "writes the head-motion transforms that bold_hmc_wf re-estimated",
    ),
)

# A step cloudpipe performs by another method. Tagged for the gain attribution
# only; it keeps its stage, and it is never redundant.
METHOD_DIFFERENCE: tuple[tuple[str, str], ...] = (
    (
        "anat_fit_wf.msm_sulc_wf",
        "MSM-sulc surface registration to fsLR; cloudpipe uses FreeSurfer's "
        "sphere.reg and the HCP fsLR-deformed sphere instead (methods 3.1). "
        "A method choice: MSM improves areal alignment (Robinson et al., 2014)",
    ),
)

REDUNDANT = "redundant_upstream"
METHOD = "method_difference"


def _prefix_match(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + ".")


def _longest(path: str, rules: Iterable[tuple[str, str]]) -> tuple[str, str] | None:
    best: tuple[str, str] | None = None
    for prefix, value in rules:
        if _prefix_match(path, prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, value)
    return best


def node_stage(path: str) -> str:
    """The stage of a normalised Nipype node path; UNMAPPED when no rule matches."""
    hit = _longest(path, FMRIPREP_STAGE_RULES)
    return hit[1] if hit else UNMAPPED


def node_tag(path: str) -> str | None:
    """REDUNDANT, METHOD, or None. A node cannot carry both; method wins."""
    if _longest(path, METHOD_DIFFERENCE):
        return METHOD
    if _longest(path, REDUNDANT_UPSTREAM):
        return REDUNDANT
    return None


# --------------------------------------------------------------------------
# Nipype log parsing
# --------------------------------------------------------------------------

_STAMP = re.compile(r"^(\d{6}-\d\d:\d\d:\d\d),(\d+) nipype\.")
_SETUP = re.compile(r'\[Node\] Setting-up "([^"]+)" in "([^"]+)"')
_EXEC = re.compile(r'\[Node\] Executing "([^"]+)"')
_CACHED = re.compile(r'\[Node\] Cached "([^"]+)"')
_FINISHED = re.compile(r'\[Node\] Finished "([^"]+)", elapsed time ([0-9.eE+-]+)s')
AMBIGUITY_S = 2.0
_BOLD_RUN = re.compile(r"^bold_(?:ses_[^/.]+_)?task_[^/.]+_wf$")


def normalise_node_path(workdir: str) -> str:
    """A node's working directory -> its workflow path, free of subject and run tokens.

    `/work/wd/fmriprep_25_2_wf/sub_X_ses_Y_wf/bold_ses_Y_task_rest_run_01_wf/
    bold_std_wf/_in_tuple_MNI.res2/resample` -> `bold_wf.bold_std_wf.resample`.
    MapNode children (`mapflow/_name0`) fold into their MapNode, and iterable
    directories (`_hemi_L`, `_in_tuple_...`) are dropped.
    """
    parts = workdir.rstrip("/").split("/")
    try:
        start = next(i for i, p in enumerate(parts) if re.fullmatch(r"fmriprep_\d+_\d+_wf", p)) + 1
    except StopIteration:
        start = 0
    out: list[str] = []
    rest = parts[start:]
    for i, p in enumerate(rest):
        if not p:
            continue
        if i == 0 and re.fullmatch(r"sub_.+_wf", p):
            continue
        if p == "mapflow" or p.startswith("_"):
            continue
        out.append("bold_wf" if _BOLD_RUN.fullmatch(p) else p)
    return ".".join(out)


def _short(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _pop_finished(q: list[tuple[str, datetime | None]], began: datetime | None) -> tuple[str, bool]:
    """Take from `q` (executing nodes of one short name) the one whose Executing
    stamp is nearest `began`; ambiguous if another candidate of a different
    stage or tag is within AMBIGUITY_S of as near."""

    def gap(c: tuple[str, datetime | None]) -> float:
        return abs((c[1] - began).total_seconds()) if c[1] and began else math.inf

    order = sorted(range(len(q)), key=lambda i: gap(q[i]))
    path = q[order[0]][0]
    klass = (node_stage(path), node_tag(path))
    nearest = gap(q[order[0]])
    amb = any(
        (node_stage(q[i][0]), node_tag(q[i][0])) != klass and gap(q[i]) - nearest <= AMBIGUITY_S
        for i in order[1:]
    )
    q.pop(order[0])
    return path, amb


@dataclass
class ParsedLog:
    nodes: pd.DataFrame  # path, start, end, elapsed_s, ambiguous
    n_runs: int
    ambiguous: int  # Finished lines whose in-flight candidates differ in stage or tag
    unpaired: int  # Finished lines with no node set up under that name
    first_stamp: datetime | None
    last_stamp: datetime | None
    succeeded: bool


def parse_nipype_log(text: str) -> ParsedLog:
    """Executed nodes from an fMRIPrep log: workflow path, start, end, elapsed.

    The `Finished` line carries only the short name (`init_aff` runs in both
    the T1w brain extraction and the BOLD reference workflow), so it is paired
    with the in-flight node of that name whose `Executing` stamp is nearest the
    finish stamp minus the elapsed time. When a second candidate in another
    stage or tag lies within `AMBIGUITY_S` of that the pairing is counted as
    ambiguous rather than resolved silently. Cached nodes do no work and are
    not rows.
    """
    pending: dict[str, deque[str]] = defaultdict(deque)
    executing: dict[str, list[tuple[str, datetime | None]]] = defaultdict(list)
    rows: list[dict] = []
    ambiguous = unpaired = 0
    runs: set[str] = set()
    stamp: datetime | None = None
    first = last = None
    for line in text.splitlines():
        m = _STAMP.match(line)
        if m:
            stamp = datetime.strptime(m.group(1), "%y%m%d-%H:%M:%S").replace(
                microsecond=int(m.group(2).ljust(6, "0")[:6])
            )
            first = first or stamp
            last = stamp
            continue
        if "[Node]" not in line:
            continue
        if m := _SETUP.search(line):
            name, wd = m.groups()
            runs.update(p for p in wd.split("/") if _BOLD_RUN.fullmatch(p))
            pending[_short(name)].append(normalise_node_path(wd))
        elif m := _EXEC.search(line):
            q = pending[m.group(1)]
            if q:
                executing[m.group(1)].append((q.popleft(), stamp))
        elif (m := _CACHED.search(line)) and pending[m.group(1)]:
            pending[m.group(1)].popleft()
        elif m := _FINISHED.search(line):
            name, elapsed = m.group(1), float(m.group(2))
            q = executing[name]
            if not q:
                unpaired += 1
                continue
            began = stamp - pd.Timedelta(seconds=elapsed) if stamp else None
            path, amb = _pop_finished(q, began)
            ambiguous += amb
            rows.append(
                {"path": path, "end": stamp, "start": began, "elapsed_s": elapsed, "ambiguous": amb}
            )
    nodes = pd.DataFrame(rows, columns=["path", "start", "end", "elapsed_s", "ambiguous"])
    return ParsedLog(
        nodes=nodes,
        n_runs=len(runs),
        ambiguous=ambiguous,
        unpaired=unpaired,
        first_stamp=first,
        last_stamp=last,
        succeeded="fMRIPrep finished successfully" in text,
    )


def classify_nodes(nodes: pd.DataFrame) -> pd.DataFrame:
    """Add `stage` and `tag` columns. Unmatched paths get UNMAPPED, never dropped."""
    out = nodes.copy()
    out["stage"] = out["path"].map(node_stage)
    out["tag"] = out["path"].map(node_tag)
    return out


def unmapped_nodes(nodes: pd.DataFrame) -> pd.DataFrame:
    """The UNMAPPED paths with their count and node-seconds, largest first."""
    c = classify_nodes(nodes)
    u = c[c.stage == UNMAPPED]
    return (
        u.groupby("path")
        .agg(n=("path", "size"), node_s=("elapsed_s", "sum"))
        .sort_values("node_s", ascending=False)
        .reset_index()
    )


# --------------------------------------------------------------------------
# Interval arithmetic
# --------------------------------------------------------------------------


def union_seconds(intervals: Iterable[tuple[float, float]]) -> float:
    """Length of the union of [start, end) intervals: parallel work overlaps, not adds."""
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted((s, e) for s, e in intervals if e > s):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def concurrency_shares(intervals: Sequence[tuple[float, float]]) -> list[float]:
    """Each interval's share of wall time, splitting every instant equally among
    the intervals in flight. The shares sum to the union length."""
    events: list[tuple[float, int, int]] = []
    for i, (s, e) in enumerate(intervals):
        if e > s:
            events.append((s, 1, i))
            events.append((e, -1, i))
    events.sort(key=lambda x: (x[0], x[1]))
    share = [0.0] * len(intervals)
    active: set[int] = set()
    prev = None
    for t, kind, i in events:
        if prev is not None and active and t > prev:
            dt = (t - prev) / len(active)
            for j in active:
                share[j] += dt
        if kind == 1:
            active.add(i)
        else:
            active.discard(i)
        prev = t
    return share


def critical_path(
    durations: Mapping[str, float], dag: Mapping[str, Sequence[str]]
) -> tuple[float, list[str]]:
    """Longest path through `dag` weighted by `durations` (seconds).

    Steps absent from `durations` are treated as not part of this session's run
    and are skipped; their dependents still wait on their own prerequisites.
    """
    finish: dict[str, float] = {}
    via: dict[str, str | None] = {}

    def visit(step: str) -> float:
        if step in finish:
            return finish[step]
        best, arg = 0.0, None
        for dep in dag.get(step, ()):
            f = visit(dep)
            if f > best or arg is None and f == best and dep in durations:
                best, arg = f, dep
        finish[step] = best + durations.get(step, 0.0)
        via[step] = arg
        return finish[step]

    for s in dag:
        visit(s)
    end = max(finish, key=finish.get) if finish else None
    path: list[str] = []
    while end is not None:
        if end in durations:
            path.append(end)
        end = via.get(end)
    return (max(finish.values()) if finish else 0.0), path[::-1]


# --------------------------------------------------------------------------
# Per-pod cost aggregation
# --------------------------------------------------------------------------

# vCPU, memory GiB and GPUs (AWS's published specifications) for every instance
# type the measured pods were recorded on. A type not listed is unpriced, not guessed.
INSTANCE_SPECS: Mapping[str, tuple[int, int, int]] = {
    "c5.2xlarge": (8, 16, 0),
    "c5.4xlarge": (16, 32, 0),
    "c5a.2xlarge": (8, 16, 0),
    "c5a.4xlarge": (16, 32, 0),
    "c5d.2xlarge": (8, 16, 0),
    "c5d.4xlarge": (16, 32, 0),
    "c6a.2xlarge": (8, 16, 0),
    "c6a.4xlarge": (16, 32, 0),
    "c6i.2xlarge": (8, 16, 0),
    "c6i.4xlarge": (16, 32, 0),
    "c6id.2xlarge": (8, 16, 0),
    "c6id.4xlarge": (16, 32, 0),
    "c7a.2xlarge": (8, 16, 0),
    "c7a.4xlarge": (16, 32, 0),
    "c7i.2xlarge": (8, 16, 0),
    "c7i.4xlarge": (16, 32, 0),
    "c7i-flex.2xlarge": (8, 16, 0),
    "c7i-flex.4xlarge": (16, 32, 0),
    "c8i.2xlarge": (8, 16, 0),
    "c8i.4xlarge": (16, 32, 0),
    "c8i-flex.2xlarge": (8, 16, 0),
    "c8i-flex.4xlarge": (16, 32, 0),
    "c8id.4xlarge": (16, 32, 0),
    "m5.2xlarge": (8, 32, 0),
    "m5.4xlarge": (16, 64, 0),
    "m6i.2xlarge": (8, 32, 0),
    "m6i.4xlarge": (16, 64, 0),
    "m6idn.4xlarge": (16, 64, 0),
    "m7a.2xlarge": (8, 32, 0),
    "m7a.4xlarge": (16, 64, 0),
    "m7i.2xlarge": (8, 32, 0),
    "m7i.4xlarge": (16, 64, 0),
    "m7i-flex.2xlarge": (8, 32, 0),
    "m7i-flex.4xlarge": (16, 64, 0),
    "m7gd.xlarge": (4, 16, 0),
    "m8i.2xlarge": (8, 32, 0),
    "m8i.4xlarge": (16, 64, 0),
    "m8i-flex.2xlarge": (8, 32, 0),
    "m8i-flex.4xlarge": (16, 64, 0),
    "m8id.4xlarge": (16, 64, 0),
    "m8ib.4xlarge": (16, 64, 0),
    "m8idb.4xlarge": (16, 64, 0),
    "t3.medium": (2, 4, 0),
    "t3.large": (2, 8, 0),
    "t3.xlarge": (4, 16, 0),
    "t3.2xlarge": (8, 32, 0),
    "t3a.medium": (2, 4, 0),
    "t3a.large": (2, 8, 0),
    "t3a.xlarge": (4, 16, 0),
    "t3a.2xlarge": (8, 32, 0),
    "g4dn.xlarge": (4, 16, 1),
    "g4dn.2xlarge": (8, 32, 1),
    "g5.xlarge": (4, 16, 1),
    "g5.2xlarge": (8, 32, 1),
    "g6.xlarge": (4, 16, 1),
    "g6.2xlarge": (8, 32, 1),
    "g6e.xlarge": (4, 32, 1),
    "g6e.2xlarge": (8, 64, 1),
}

# Instance families each step's node pool provisions (the templates' nodepool
# selectors; docs/infrastructure.md). A GPU pod must be on a `g` node whatever
# its step; CPU steps not listed run on cpu-heavy-nodepool's c and m families.
CPU_STEP_FAMILIES: Mapping[str, tuple[str, ...]] = {
    "surface-resample": ("t",),  # cpu-light-nodepool
    "fmriprep-default": ("c", "m"),  # fmriprep-bench-nodepool: c/m 4xlarge
}
DEFAULT_CPU_FAMILIES = ("c", "m")  # cpu-heavy-nodepool
FIT_TOLERANCE = 1.02

# GPU time slices per physical card, by report date, from the git history of the
# device-plugin profile and NodeOverlays: 2 from 2026-07-20 (#32), 3 from
# 2026-08-01 (#109); the dense 2xlarge types moved to their own pool at 5 on
# 2026-09-14 (#392) and were cut to 4 on 2026-09-15 (#396). On a changeover day
# the count in force is not known, so such a row is unpriced, not guessed.
DENSE_GPU_TYPES = frozenset({"g5.2xlarge", "g6.2xlarge", "g6e.2xlarge"})
_SLICES_ALL = (("2026-07-20", 2), ("2026-08-01", 3))
_SLICES_DENSE = (("2026-09-14", 5), ("2026-09-15", 4))
_CHANGEOVER_ALL = frozenset(d for d, _ in _SLICES_ALL)
_CHANGEOVER_DENSE = frozenset(d for d, _ in _SLICES_DENSE)


def gpu_slices(instance_type: str, date: str) -> int | None:
    """Time slices per card on `instance_type` on report date `date` (YYYY-MM-DD),
    or None on a changeover day."""
    if date in _CHANGEOVER_ALL or (instance_type in DENSE_GPU_TYPES and date in _CHANGEOVER_DENSE):
        return None
    n = 1
    for since, k in _SLICES_ALL:
        if date >= since:
            n = k
    if instance_type in DENSE_GPU_TYPES:
        for since, k in _SLICES_DENSE:
            if date >= since:
                n = k
    return n


def attribution_problem(
    step: str,
    instance_type: str,
    cores: float,
    ram_gib: float,
    gpu: bool,
    families: Mapping[str, tuple[str, ...]] = CPU_STEP_FAMILIES,
) -> str | None:
    """Why a cost row's recorded node cannot be the pod's, or None if it can.

    `cores` and `ram_gib` are the pod's charged rates (hours over its runtime hours).
    """
    spec = INSTANCE_SPECS.get(instance_type or "")
    if spec is None:
        return "no-instance-type" if not instance_type else "type-not-in-INSTANCE_SPECS"
    vcpu, mem, gpus = spec
    if gpu:
        if not gpus:
            return "gpu-pod-on-cpu-node"
    elif instance_type[:1] not in families.get(step, DEFAULT_CPU_FAMILIES):
        return "family-not-in-steps-pool"
    if cores > vcpu * FIT_TOLERANCE or ram_gib > mem * FIT_TOLERANCE:
        return "pod-larger-than-node"
    return None


def _row_pricing(
    row, families: Mapping[str, tuple[str, ...]] = CPU_STEP_FAMILIES
) -> tuple[str | None, float, float]:
    """(problem, node_h, gpu card-hours) for one cost row.

    `families` maps a CPU step to the instance families its nodepool allows; the
    default is this study's. The reprocess audit passes the whole pipeline's
    (src/audit/report.py), because its bookkeeping steps run on cpu-light (t).
    """
    h = row.runtime_minutes / 60.0
    gpu = row.gpu_hours > 0
    nan_card = math.nan if gpu else 0.0
    if h <= 0:
        return None, 0.0, 0.0
    prob = attribution_problem(
        row.step,
        row.node_instance_type,
        row.cpu_core_hours / h,
        row.ram_gb_hours / h,
        gpu,
        families=families,
    )
    slices = gpu_slices(row.node_instance_type, row.date) if gpu and prob is None else 1
    if prob is None and slices is None:
        prob = "gpu-slice-changeover-day"
    if prob is None and not (row.node_ondemand_usd_per_hour and row.node_ondemand_usd_per_hour > 0):
        prob = "no-on-demand-rate"
    if prob is not None:
        return prob, math.nan, nan_card
    vcpu, mem, gpus = INSTANCE_SPECS[row.node_instance_type]
    card = row.gpu_hours / slices if gpu else 0.0
    node_h = max(row.cpu_core_hours / vcpu, row.ram_gb_hours / mem, card / gpus if gpus else 0.0)
    return None, node_h, card


def aggregate_pod_costs(
    rows: pd.DataFrame, families: Mapping[str, tuple[str, ...]] = CPU_STEP_FAMILIES
) -> pd.DataFrame:
    """Per-pod-per-date cost rows -> one row per (workflow_name, pod).

    Sums cpu_core_h, ram_gb_h, operated_usd (cost as operated) and
    runtime_minutes, and adds:

    - `cpu_core_h_used`, `ram_gb_h_used`: charged hours x efficiency, the
      efficiency capped at 1 (the cluster charges max(request, usage));
    - `gpu_slice_h` (the cluster's GPU-hours) and `gpu_h` (card-hours);
    - `node_h` and `ondemand_usd` (module docstring);
    - `asop_usd`, cost as operated: the same node-hours at the row's
      `node_operated_usd_per_hour` (the spot price paid on spot, the list rate
      on on-demand), supplied by the caller; NaN where the caller supplies none;
    - `priced` (every row's node attribution holds), `problem` (the first
      reason one did not), `settled` (every row at scrape_age_days >= 3, or in
      a USAGE_FINAL_PARTITIONS date).

    One unpriced row makes the pod's node-hours and costs NaN: no partial pods.
    """
    r = rows.copy()
    r["node_instance_type"] = r["node_instance_type"].fillna("")
    priced = [_row_pricing(row, families) for row in r.itertuples(index=False)]
    r["_problem"] = [p[0] for p in priced]
    r["_node_h"] = [p[1] for p in priced]
    r["_card_h"] = [p[2] for p in priced]
    od = r["node_ondemand_usd_per_hour"].astype(float)
    pvnet = r["pv_cost_usd"] + r["network_cost_usd"]
    r["_od"] = r["_node_h"] * od + pvnet
    operated = (
        r["node_operated_usd_per_hour"].astype(float)
        if "node_operated_usd_per_hour" in r
        else pd.Series(math.nan, index=r.index)
    )
    r["_asop"] = r["_node_h"] * operated + pvnet
    r["_cpu_used"] = r["cpu_core_hours"] * r["cpu_efficiency"].clip(0, 1)
    r["_ram_used"] = r["ram_gb_hours"] * r["ram_efficiency"].clip(0, 1)
    r["_settled"] = (r["scrape_age_days"] >= SETTLED_SCRAPE_AGE_DAYS) | (
        r["date"].astype(str).isin(USAGE_FINAL_PARTITIONS) if "date" in r else False
    )
    g = r.groupby(["workflow_name", "pod"], sort=False)

    def strict(col: str) -> pd.Series:
        return g[col].apply(lambda s: s.sum() if s.notna().all() else np.nan)

    out = pd.DataFrame(
        {
            "cpu_core_h": g["cpu_core_hours"].sum(),
            "ram_gb_h": g["ram_gb_hours"].sum(),
            "gpu_slice_h": g["gpu_hours"].sum(),
            "operated_usd": g["total_cost_usd"].sum(),
            "runtime_minutes": g["runtime_minutes"].sum(),
            "cpu_core_h_used": g["_cpu_used"].sum(),
            "ram_gb_h_used": g["_ram_used"].sum(),
            "gpu_h": strict("_card_h"),
            "node_h": strict("_node_h"),
            "ondemand_usd": strict("_od"),
            "asop_usd": strict("_asop"),
            "priced": g["_problem"].apply(lambda s: s.isna().all()),
            "problem": g["_problem"].apply(
                lambda s: next((x for x in s if isinstance(x, str)), None)
            ),
            "settled": g["_settled"].all(),
            "n_rows": g.size(),
            "instance_types": g["node_instance_type"].apply(
                lambda s: ",".join(sorted({x for x in s if x}))
            ),
            "capacity_types": g["node_capacity_type"].apply(
                lambda s: ",".join(sorted({x for x in s if x}))
            ),
        }
    )
    return out.reset_index()


# --------------------------------------------------------------------------
# Per-session measures
# --------------------------------------------------------------------------

RESOURCE_COLS = (
    "cpu_core_h",
    "cpu_core_h_used",
    "ram_gb_h",
    "ram_gb_h_used",
    "gpu_h",
    "ondemand_usd",
    "asop_usd",
    "node_h",
)


def _resources(p: pd.DataFrame, scale: float = 1.0) -> dict[str, float]:
    """Sum a pod table's resources. A NaN in any pod propagates: no partial totals."""
    d = {}
    for c in RESOURCE_COLS:
        v = p[c].astype(float)
        d[c] = float(v.sum() * scale) if v.notna().all() else math.nan
    gpu = p["gpu_pod"].astype(bool)
    for cls, mask in (("gpu", gpu), ("cpu", ~gpu)):
        v = p.loc[mask, "node_h"].astype(float)
        d[f"node_h_{cls}"] = float(v.sum() * scale) if v.notna().all() else math.nan
        v = p.loc[mask, "cpu_core_h"].astype(float)
        d[f"cpu_core_h_on_{cls}_nodes"] = float(v.sum() * scale) if v.notna().all() else math.nan
    return d


def _utilisation(d: Mapping[str, float]) -> dict[str, float]:
    return {
        "cpu_util": d["cpu_core_h_used"] / d["cpu_core_h"] if d["cpu_core_h"] > 0 else math.nan,
        "ram_util": d["ram_gb_h_used"] / d["ram_gb_h"] if d["ram_gb_h"] > 0 else math.nan,
    }


def _throughput(d: Mapping[str, float]) -> dict[str, float]:
    out = {}
    for cls in ("cpu", "gpu"):
        nh = d[f"node_h_{cls}"]
        out[f"sessions_per_{cls}_node_h"] = 1.0 / nh if nh and nh > 0 else math.nan
    return out


def cloudpipe_session(
    pods: pd.DataFrame, k: int, anatomy_scale: str = "full"
) -> dict[str, float | str | bool]:
    """One session's cloudpipe measures from its final-attempt pods (one row per pod).

    `pods` columns: step, running_s, gpu_pod, settled, has_cost, and RESOURCE_COLS;
    optionally `attempt` ("final" or "prior"). A prior attempt (one a spot reclaim
    or a failure ended, then retried) adds only to `asop_usd`, the cost as
    operated, and is reported as `retry_usd`: the outputs came from the final
    attempts, but the retries were paid for.
    `anatomy_scale` "full" charges the session the whole anatomy; "1/k" charges
    1/k of its resources and cost. Time to result is never divided: the session
    waits for the whole anatomy either way.
    """
    if anatomy_scale not in ("full", "1/k"):
        raise ValueError(anatomy_scale)
    prior = pods[pods.attempt == "prior"] if "attempt" in pods else pods.iloc[0:0]
    pods = pods[pods.attempt != "prior"] if "attempt" in pods else pods
    excluded = set(pods.step) & set(CLOUDPIPE_EXCLUDED_STEPS)
    if excluded:
        raise ValueError(f"steps excluded from every figure (methods 8): {sorted(excluded)}")
    unknown = set(pods.step) - set(CLOUDPIPE_STEP_STAGE)
    if unknown:
        raise ValueError(f"steps with no stage: {sorted(unknown)}")
    p = pods.assign(stage=pods.step.map(CLOUDPIPE_STEP_STAGE))
    out: dict[str, float | str | bool] = {"anatomy_scale": anatomy_scale, "k": k}
    dur = p.groupby("step").running_s.sum().to_dict()
    total, path = critical_path(dur, CLOUDPIPE_DAG)
    out["time_h"] = total / 3600
    out["critical_path"] = ">".join(path)
    scale_anat = 1.0 / k if anatomy_scale == "1/k" else 1.0
    stage_res = {}
    for st in STAGES:
        g = p[p.stage == st]
        sub_dag = {
            s: tuple(x for x in deps if CLOUDPIPE_STEP_STAGE[x] == st)
            for s, deps in CLOUDPIPE_DAG.items()
            if CLOUDPIPE_STEP_STAGE[s] == st
        }
        out[f"{st}:time_h"] = (
            critical_path(g.groupby("step").running_s.sum().to_dict(), sub_dag)[0] / 3600
        )
        scale = scale_anat if st == ANATOMY else 1.0
        r = _resources(g, scale)
        pr = prior[prior.step.map(CLOUDPIPE_STEP_STAGE) == st]["asop_usd"].astype(float)
        r["retry_usd"] = float(pr.sum() * scale) if pr.notna().all() else math.nan
        r["asop_usd"] = r["asop_usd"] + r["retry_usd"]
        stage_res[st] = r
        for c, v in r.items():
            out[f"{st}:{c}"] = v
        for c, v in _utilisation(r).items():
            out[f"{st}:{c}"] = v
        out[f"{st}:on_gpu"] = bool(g.gpu_pod.any())
    tot = {c: sum(stage_res[s][c] for s in STAGES) for c in stage_res[STAGES[0]]}
    out.update(tot)
    out.update(_utilisation(tot))
    out.update(_throughput(tot))
    out["missing_steps"] = ",".join(s for s in CLOUDPIPE_STEP_STAGE if s not in set(p.step))
    out["resources_complete"] = bool(
        not out["missing_steps"]
        and p.has_cost.all()
        and not math.isnan(tot["cpu_core_h"])
        and not math.isnan(tot["gpu_h"])
    )
    out["cost_complete"] = bool(out["resources_complete"] and not math.isnan(tot["ondemand_usd"]))
    out["n_prior_attempts"] = len(prior)
    out["settled"] = bool(p.settled.all())
    return out


def fmriprep_session(
    pod: Mapping[str, float], nodes: pd.DataFrame
) -> dict[str, float | str | bool]:
    """One session's fMRIPrep measures from its pod and its executed Nipype nodes.

    `pod`: running_s, settled, has_cost and RESOURCE_COLS (gpu_pod False).
    `nodes`: path, start, end, elapsed_s (`parse_nipype_log`).

    Per stage, time is the union of the stage's node intervals. Resources are
    one pod's, so they are attributed to stages by occupancy: each instant of
    the pod's running time goes to the nodes then in flight, split equally;
    instants with none go to NO_NODE. Utilisation is the pod's and has no
    per-stage value.

    Any `spot_*` key in `pod` (fMRIPrep's spot projection, from the caller's
    assembly) is passed through unchanged. fMRIPrep ran on on-demand capacity,
    so its `asop_usd` is its on-demand cost as run.
    """
    c = classify_nodes(nodes)
    t0 = c.start.min()
    iv = [
        ((s - t0).total_seconds(), (e - t0).total_seconds())
        for s, e in zip(c.start, c.end, strict=True)
    ]
    c = c.assign(attributed_s=concurrency_shares(iv), s0=[a for a, _ in iv], s1=[b for _, b in iv])
    running = float(pod["running_s"])
    busy = union_seconds(iv)
    res = {k: float(pod[k]) for k in RESOURCE_COLS}
    u = res["cpu_core_h_used"] / res["cpu_core_h"] if res["cpu_core_h"] > 0 else math.nan
    tot = dict(res)
    tot["gpu_h"] = 0.0
    tot["retry_usd"] = 0.0
    tot["node_h_cpu"], tot["node_h_gpu"] = res["node_h"], 0.0
    tot["cpu_core_h_on_cpu_nodes"], tot["cpu_core_h_on_gpu_nodes"] = res["cpu_core_h"], 0.0
    out: dict[str, float | str | bool] = {"time_h": running / 3600}
    out.update(tot)
    out.update(_utilisation(tot))
    out.update(_throughput(tot))
    out.update({k: float(v) for k, v in dict(pod).items() if str(k).startswith("spot_")})
    # occupancy shares over the pod's running time; the remainder is NO_NODE
    denom = max(running, busy)
    shares = c.groupby("stage").attributed_s.sum().to_dict()
    lines = [*STAGES, OTHER, UNMAPPED]
    for st in lines:
        g = c[c.stage == st]
        out[f"{st}:time_h"] = union_seconds(zip(g.s0, g.s1, strict=True)) / 3600
        frac = shares.get(st, 0.0) / denom if denom > 0 else math.nan
        for k in RESOURCE_COLS:
            out[f"{st}:{k}"] = tot[k] * frac
        out[f"{st}:gpu_h"] = 0.0
        out[f"{st}:node_h_cpu"] = tot["node_h_cpu"] * frac
        out[f"{st}:node_h_gpu"] = 0.0
        out[f"{st}:share"] = frac
        out[f"{st}:node_s"] = float(g.elapsed_s.sum())
        out[f"{st}:on_gpu"] = False
    out[f"{NO_NODE}:share"] = (denom - busy) / denom if denom > 0 else math.nan
    all_node_s = float(c.elapsed_s.sum())
    for tag in (REDUNDANT, METHOD):
        g = c[c.tag == tag]
        out[f"{tag}:share"] = float(g.attributed_s.sum()) / denom if denom > 0 else math.nan
        out[f"{tag}:node_time_share"] = (
            float(g.elapsed_s.sum()) / all_node_s if all_node_s > 0 else math.nan
        )
        out[f"{tag}:ondemand_usd"] = tot["ondemand_usd"] * out[f"{tag}:share"]
        out[f"{tag}:cpu_core_h"] = tot["cpu_core_h"] * out[f"{tag}:share"]
    out["mean_nodes_in_flight"] = all_node_s / busy if busy > 0 else math.nan
    out["n_unmapped_nodes"] = int((c.stage == UNMAPPED).sum())
    out["n_ambiguous_nodes"] = int(c.ambiguous.sum()) if "ambiguous" in c else 0
    out["cpu_util_as_run"] = u
    out["resources_complete"] = bool(pod["has_cost"] and not math.isnan(res["cpu_core_h"]))
    out["cost_complete"] = bool(out["resources_complete"] and not math.isnan(res["ondemand_usd"]))
    out["settled"] = bool(pod["settled"])
    return out


# --------------------------------------------------------------------------
# Ratios and summaries
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Spread:
    n: int
    median: float
    q25: float
    q75: float
    p05: float
    p95: float

    @classmethod
    def of(cls, values: Iterable[float]) -> Spread:
        a = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
        if not len(a):
            return cls(0, *([math.nan] * 5))
        q = np.percentile(a, [50, 25, 75, 5, 95])
        return cls(len(a), *map(float, q))


def paired_ratios(fp: pd.DataFrame, cp: pd.DataFrame, measures: Sequence[str]) -> pd.DataFrame:
    """fMRIPrep's value over cloudpipe's, per session, joined on the `session` column.

    A session present in only one arm yields no ratio. A zero or missing
    denominator yields NaN, never inf.
    """
    j = fp.set_index("session")[list(measures)].join(
        cp.set_index("session")[list(measures)], how="inner", lsuffix="__fp", rsuffix="__cp"
    )
    out = pd.DataFrame(index=j.index)
    for m in measures:
        num, den = j[f"{m}__fp"].astype(float), j[f"{m}__cp"].astype(float)
        out[m] = (num / den.where(den > 0)).where(num.notna())
    return out.reset_index()


def summarise(ratios: pd.DataFrame, measures: Sequence[str]) -> dict[str, Spread]:
    return {m: Spread.of(ratios[m]) for m in measures}


# --------------------------------------------------------------------------
# Cost views
# --------------------------------------------------------------------------

# Each view: (fMRIPrep's column, cloudpipe's column, what it is). Every ratio is
# fMRIPrep's value over cloudpipe's for the same session.
COST_VIEWS: Mapping[str, tuple[str, str, str]] = {
    "as_operated": (
        "asop_usd",
        "asop_usd",
        "as operated: cloudpipe on spot at the spot price paid, reclaimed attempts "
        "included; fMRIPrep on on-demand, as run",
    ),
    **{
        f"fmriprep_spot_{setup}_{lvl}": (
            f"spot_{setup}_usd_{lvl}",
            "asop_usd",
            f"PROJECTION: fMRIPrep on spot ({'persistent working volume' if setup == 'persistent' else 'node-local working directory'}), "
            f"reclaim rate {lvl}; cloudpipe as operated",
        )
        for setup in ("persistent", "ephemeral")
        for lvl in ("mid", "low", "high")
    },
    "both_on_demand": (
        "ondemand_usd",
        "ondemand_usd",
        "both at on-demand list price: compute efficiency without the pricing of capacity",
    ),
}


def paired_view_ratios(
    fp: pd.DataFrame, cp: pd.DataFrame, views: Mapping[str, tuple[str, str, str]] = COST_VIEWS
) -> pd.DataFrame:
    """One column per view: fMRIPrep's view column over cloudpipe's, per session.

    A view whose fMRIPrep column is absent (no projection supplied) is skipped.
    """
    present = {v: (f, c) for v, (f, c, _) in views.items() if f in fp and c in cp}
    a = fp.set_index("session").add_suffix("__fp")
    b = cp.set_index("session").add_suffix("__cp")
    j = a.join(b, how="inner")
    out = pd.DataFrame(index=j.index)
    for view, (f, c) in present.items():
        num, den = j[f"{f}__fp"].astype(float), j[f"{c}__cp"].astype(float)
        out[view] = (num / den.where(den > 0)).where(num.notna())
    return out.reset_index()


# --------------------------------------------------------------------------
# CLI: read the assembled tables, write the per-session and summary files
# --------------------------------------------------------------------------

UNSETTLED_BANNER = "UNSETTLED — not for publication"
# Ratio measures other than cost (cost has its views). GPU-hours have no ratio:
# fMRIPrep uses none, so the figure is reported per arm instead.
HEADLINE = (
    "time_h",
    "cpu_core_h",
    "ram_gb_h",
    "cpu_util",
    "ram_util",
    "node_h_cpu",
    "sessions_per_cpu_node_h",
)
STAGE_MEASURES = ("time_h", "cpu_core_h", "ram_gb_h", "ondemand_usd", "asop_usd")
VALUE_MEASURES = (
    *HEADLINE,
    "asop_usd",
    "retry_usd",
    "ondemand_usd",
    "gpu_h",
    "node_h_gpu",
    "cpu_core_h_on_gpu_nodes",
)


def _spread_line(name: str, s: Spread) -> str:
    return f"  {name:34s} n={s.n:3d} median {s.median:9.3f} IQR {s.q25:9.3f}-{s.q75:9.3f} 5-95th {s.p05:9.3f}-{s.p95:9.3f}"


def build_tables(
    cp_pods: pd.DataFrame, fp_pods: pd.DataFrame, fp_nodes: pd.DataFrame
) -> dict[str, pd.DataFrame]:
    """The three per-session tables: cloudpipe in full and 1/k, and fMRIPrep as run."""
    k_by = cp_pods.groupby("session").k.first()
    cp_rows: dict[str, list[dict]] = {"full": [], "1/k": []}
    for ses, g in cp_pods.groupby("session"):
        for scale, rows in cp_rows.items():
            rows.append({"session": ses, **cloudpipe_session(g, int(k_by[ses]), scale)})
    nodes_by = dict(tuple(fp_nodes.groupby("session")))
    fp_rows = [
        {"session": pod.session, **fmriprep_session(pod, nodes_by[pod.session])}
        for _, pod in fp_pods[fp_pods.session.isin(nodes_by)].iterrows()
    ]
    return {
        "cloudpipe_full": pd.DataFrame(cp_rows["full"]),
        "cloudpipe_1k": pd.DataFrame(cp_rows["1/k"]),
        "fmriprep": pd.DataFrame(fp_rows),
    }


def _value_lines(tables: Mapping[str, pd.DataFrame], stage_measures: Sequence[str]) -> list[str]:
    lines = [
        "# Per-session values, each arm. A missing value (no cost record, an unpriced pod) drops the session from that measure only."
    ]
    spot_cols = sorted({c for t in tables.values() for c in t.columns if c.startswith("spot_")})
    for name, t in tables.items():
        lines.append(
            f"## {name}: {len(t)} sessions; resources complete {int(t.resources_complete.sum())}, cost complete {int(t.cost_complete.sum())}"
        )
        lines += [
            _spread_line(m, Spread.of(t[m].astype(float)))
            for m in (*VALUE_MEASURES, *spot_cols, *stage_measures)
            if m in t
        ]
        lines.append("")
    return lines


def _attribution_lines(tables: Mapping[str, pd.DataFrame]) -> list[str]:
    fp_t, cp_t = tables["fmriprep"], tables["cloudpipe_full"]
    cols = (
        f"{REDUNDANT}:share",
        f"{REDUNDANT}:node_time_share",
        f"{METHOD}:share",
        f"{METHOD}:node_time_share",
        f"{NO_NODE}:share",
        "mean_nodes_in_flight",
        "cpu_util",
        "ram_util",
    )
    lines = [f"## fMRIPrep attribution ({len(fp_t)} sessions; shares of the pod's running time)"]
    lines += [_spread_line(c, Spread.of(fp_t[c])) for c in cols]
    lines.append(
        "  stages on GPU (cloudpipe, share of sessions): "
        + ", ".join(f"{st} {cp_t[f'{st}:on_gpu'].mean():.2f}" for st in STAGES)
    )
    return [*lines, ""]


def _run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tables = build_tables(
        pd.read_parquet(args.cloudpipe_pods),
        pd.read_parquet(args.fmriprep_pods),
        pd.read_parquet(args.fmriprep_nodes),
    )
    settled = all(t.settled.all() for t in tables.values())
    banner = "" if settled and not args.force_unsettled else UNSETTLED_BANNER
    stage_measures = [f"{s}:{m}" for s in STAGES for m in STAGE_MEASURES]
    measures = [*HEADLINE, *stage_measures]
    lines = [f"# {banner}" if banner else "# efficiency (methods 8.1)", ""]
    if banner:
        unsettled = {n: int((~t.settled).sum()) for n, t in tables.items()}
        lines += [f"sessions with an unsettled cost row: {unsettled}", ""]

    def write(df: pd.DataFrame, name: str) -> None:
        with open(out / name, "w") as fh:
            fh.write(f"# {banner}\n" if banner else "")
            df.to_csv(fh, index=False)

    for name, t in tables.items():
        t.insert(1, "status", banner or "settled")
        write(t, f"per_session_{name}.csv")
    lines += _value_lines(tables, stage_measures)
    for cp_name in ("cloudpipe_full", "cloudpipe_1k"):
        lines.append(f"# fMRIPrep / {cp_name}, per session")
        r = paired_ratios(tables["fmriprep"], tables[cp_name], measures)
        write(r, f"ratios_fmriprep_over_{cp_name}.csv")
        lines += [_spread_line(m, s) for m, s in summarise(r, measures).items()]
        v = paired_view_ratios(tables["fmriprep"], tables[cp_name])
        write(v, f"cost_views_fmriprep_over_{cp_name}.csv")
        lines.append("  cost views:")
        for view in v.columns.drop("session"):
            lines.append(_spread_line(view, Spread.of(v[view])))
            lines.append(f"      {COST_VIEWS[view][2]}")
        lines.append("")
    lines += _attribution_lines(tables)
    (out / "summary.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--cloudpipe-pods",
        required=True,
        help="parquet: one row per session x pod (final attempts; prior attempts with attempt=prior)",
    )
    ap.add_argument("--fmriprep-pods", required=True, help="parquet: one row per session (its pod)")
    ap.add_argument(
        "--fmriprep-nodes",
        required=True,
        help="parquet: one row per session x executed Nipype node",
    )
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--force-unsettled",
        action="store_true",
        help="banner the output even if every row is settled",
    )
    return _run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
