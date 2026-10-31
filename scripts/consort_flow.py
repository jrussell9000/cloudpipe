"""CONSORT-style flow of ABCD v7 imaging data through cloudpipe_minproc.

Starts at ABCD's own imaging manifest and follows every session through the
pipeline's stored derivatives, splitting into a resting-state and an n-back arm
after the shared anatomical stages. Read-only on S3.

  collect  ABCD tables + S3 derivatives + registration QC -> data/consort/{sessions,runs}.parquet
  render   those tables -> data/consort/consort_counts.csv and consort_flow.{dot,svg,pdf}

Every existence test is the pipeline's own (src/inventory.py):

  anatomy         derivatives/fastsurfer/{subj}/{ses}/_complete.json
  t1w -> MNI      derivatives/registration/{subj}/{ses}/t1w_to_mni/{prefix}_desc-t1w2mni_affine.mat
  BOLD -> T1w     derivatives/registration/{subj}/{ses}/bold_to_t1w_{task}_{run}/{prefix}_desc-bold2t1w_itk.txt
  MNI BOLD        the run listed in derivatives/func/{subj}/{ses}/_complete.json
                  (or a legacy {prefix}_space-MNI152NLin2009cAsym_bold.tar.gz)
  fsLR32k         derivatives/func_surf/{subj}/{ses}/fsLR32k/{prefix}_space-fsLR32k_bold.dtseries.nii

A registration gate that rejects deletes its output, so the transform's absence
is the outcome and the RegistrationQC verdict is only the reason. A BOLD run
counts as entering the pipeline once it has a bold_to_t1w QC record, a
transform, or a published MNI BOLD: the staged mmps_mproc/ input is deleted
after a workflow succeeds, so nothing else in S3 says which runs ABCD shipped.
Runs of a session that never reached BOLD -> T1w (no anatomy, or no t1w -> MNI
transform) are therefore not counted; such sessions are excluded whole, earlier.

The parquet tables carry participant IDs and live under data/ (gitignored, DUA).
The rendered counts and figure are aggregate only.

Usage:
  pixi run -e ops python scripts/consort_flow.py collect --data-bucket "$CLOUDPIPE_BUCKET" --metrics-bucket "$CLOUDPIPE_METRICS_BUCKET"
  pixi run -e ops python scripts/consort_flow.py render
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path
from threading import local

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "consort"
PHENOTYPE = "tabulated/abcd/rawdata/phenotype"
TASKS = {"rest": "task-rest", "nback": "task-nback"}
INDICATOR = {
    "t1": "mr_y_qc__incl__smri__t1_indicator",
    "rest": "mr_y_qc__incl__rsfmri_indicator",
    "nback": "mr_y_qc__incl__tfmri__nback_indicator",
}
MNI_BOLD = "_space-MNI152NLin2009cAsym_bold"
DTSERIES = "_space-fsLR32k_bold.dtseries.nii"

_tls = local()


def _s3(region: str):
    """One client per thread (see images/shared/fs_derivatives.py::_client, #268)."""
    if not hasattr(_tls, "s3"):
        _tls.s3 = boto3.client("s3", region_name=region, config=Config(max_pool_connections=64))
    return _tls.s3


def _keys(s3, bucket: str, prefix: str) -> list[str]:
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out += [o["Key"] for o in page.get("Contents", [])]
    return out


def _json(s3, bucket: str, key: str) -> dict | None:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def _exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------


def abcd_sessions(s3, bucket: str):
    """ABCD's imaging manifest joined to its Recommended Image Inclusion flags."""
    import pandas as pd

    def table(name):
        body = s3.get_object(Bucket=bucket, Key=f"{PHENOTYPE}/{name}.tsv")["Body"].read()
        return pd.read_csv(io.BytesIO(body), sep="\t", dtype=str)

    info = table("mr_y_adm__info")[["participant_id", "session_id"]]
    incl = table("mr_y_qc__incl")[["participant_id", "session_id", *INDICATOR.values()]]
    df = info.merge(incl, on=["participant_id", "session_id"], how="left")
    df = df.rename(
        columns={"participant_id": "subject", "session_id": "session"}
        | {col: f"abcd_{name}" for name, col in INDICATOR.items()}
    )
    for name in INDICATOR:
        df[f"abcd_{name}"] = df[f"abcd_{name}"].eq("1")
    return df


def registration_verdicts(s3, bucket: str) -> dict[tuple, str]:
    """The latest RegistrationQC verdict per unit, compacted Parquet plus raw JSON.

    Raw is read only for the days not yet compacted, newest-wins on completed_at.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    cols = ["subject", "session", "registration_type", "task", "run", "completed_at", "verdict"]
    latest: dict[tuple, tuple[str, str]] = {}

    def keep(r):
        unit = (
            r["subject"],
            r["session"],
            r["registration_type"],
            r.get("task") or "",
            r.get("run") or "",
        )
        at = r.get("completed_at") or ""
        if unit not in latest or at > latest[unit][0]:
            latest[unit] = (at, r.get("verdict") or "")

    files = [
        k for k in _keys(s3, bucket, "metrics/compacted/registration/") if k.endswith(".parquet")
    ]
    for k in files:
        t = pq.read_table(pa.BufferReader(s3.get_object(Bucket=bucket, Key=k)["Body"].read()))
        for r in t.select([c for c in cols if c in t.column_names]).to_pylist():
            keep(r)
    compacted_days = {k.split("/")[3] for k in files}
    raw_days = {
        p["Prefix"].split("/")[2]
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix="metrics/registration/", Delimiter="/"
        )
        for p in page.get("CommonPrefixes", [])
    }
    pending = sorted(raw_days - compacted_days)
    raw_keys = [k for d in pending for k in _keys(s3, bucket, f"metrics/registration/{d}/")]
    with ThreadPoolExecutor(32) as pool:
        for doc in pool.map(lambda k: _json(s3, bucket, k), raw_keys):
            if doc:
                keep(doc)
    print(
        f"registration QC: {len(latest)} units ({len(files)} compacted files, "
        f"{len(raw_keys)} raw records from {len(pending)} uncompacted day(s))",
        flush=True,
    )
    return {u: v for u, (_, v) in latest.items()}


def session_state(region: str, bucket: str, subj: str, ses: str) -> dict:
    """What S3 holds for one session, by the pipeline's own existence tests."""
    s3 = _s3(region)
    prefix = f"{subj}_{ses}"
    reg = _keys(s3, bucket, f"derivatives/registration/{subj}/{ses}/")
    b2t = set()
    for k in reg:
        name = k.rsplit("/", 1)[-1]
        if name.endswith("_desc-bold2t1w_itk.txt"):
            task, run = name.removeprefix(f"{prefix}_").split("_")[:2]
            b2t.add((task, run))
    marker = _json(s3, bucket, f"derivatives/func/{subj}/{ses}/_complete.json")
    if marker and marker.get("kind") == "func-session":
        func = {tuple(rid.split("_", 1)) for rid in marker.get("runs") or {}}
        legacy = False
    else:
        func = {
            tuple(k.rsplit("/", 1)[-1].removeprefix(f"{prefix}_").split("_")[:2])
            for k in _keys(s3, bucket, f"derivatives/func/{subj}/{ses}/")
            if k.endswith(f"{MNI_BOLD}.tar.gz")
        }
        legacy = bool(func)
    surf = {
        tuple(k.rsplit("/", 1)[-1].removeprefix(f"{prefix}_").split("_")[:2])
        for k in _keys(s3, bucket, f"derivatives/func_surf/{subj}/{ses}/fsLR32k/")
        if k.endswith(DTSERIES)
    }
    return {
        "subject": subj,
        "session": ses,
        "fastsurfer": _exists(s3, bucket, f"derivatives/fastsurfer/{subj}/{ses}/_complete.json"),
        "t1w2mni": any(k.endswith(f"{prefix}_desc-t1w2mni_affine.mat") for k in reg),
        "func_marker": bool(marker),
        "func_legacy": legacy,
        "b2t": sorted(b2t),
        "func": sorted(func),
        "surf": sorted(surf),
    }


def collect(args) -> int:
    import pandas as pd

    s3 = boto3.client("s3", region_name=args.region)
    abcd = abcd_sessions(s3, args.data_bucket)
    print(
        f"ABCD manifest: {abcd['subject'].nunique()} participants, {len(abcd)} sessions", flush=True
    )
    verdicts = registration_verdicts(s3, args.metrics_bucket)

    units = list(abcd[["subject", "session"]].itertuples(index=False, name=None))
    states = []
    with ThreadPoolExecutor(args.workers) as pool:
        futures = pool.map(lambda u: session_state(args.region, args.data_bucket, *u), units)
        for i, st in enumerate(futures, 1):
            states.append(st)
            if i % 2000 == 0 or i == len(units):
                print(f"  sessions scanned: {i}/{len(units)}", flush=True)

    sessions = abcd.merge(
        pd.DataFrame(states).drop(columns=["b2t", "func", "surf"]), on=["subject", "session"]
    )
    sessions["t1w2mni_verdict"] = [
        verdicts.get((s, ses, "t1w_to_mni", "", ""), "") for s, ses in units
    ]

    # One row per (session, task, run) the pipeline knows about, with each stage's outcome.
    by_session = defaultdict(dict)
    for (subj, ses, rtype, task, run), verdict in verdicts.items():
        if rtype == "bold_to_t1w" and task in TASKS.values():
            by_session[(subj, ses)][(task, run)] = verdict
    rows = []
    for st in states:
        key = (st["subject"], st["session"])
        b2t, func, surf = (set(map(tuple, st[k])) for k in ("b2t", "func", "surf"))
        qc = by_session.get(key, {})
        for task, run in sorted(set(qc) | b2t | func):
            if task not in TASKS.values():
                continue
            rows.append(
                {
                    "subject": key[0],
                    "session": key[1],
                    "task": task,
                    "run": run,
                    "b2t_verdict": qc.get((task, run), ""),
                    "b2t": (task, run) in b2t,
                    "func": (task, run) in func,
                    "surf": (task, run) in surf,
                }
            )
    runs = pd.DataFrame(rows)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sessions.to_parquet(OUT_DIR / "sessions.parquet", index=False)
    runs.to_parquet(OUT_DIR / "runs.parquet", index=False)
    stray = {(s, ses) for (s, ses, *_) in verdicts} - set(units)
    print(f"wrote {len(sessions)} sessions, {len(runs)} runs -> {OUT_DIR}")
    print(f"registration QC units for sessions outside the ABCD manifest: {len(stray)} session(s)")
    return 0


# ---------------------------------------------------------------------------
# source: what ABCD actually shipped, for the sessions with no anatomy
# ---------------------------------------------------------------------------


def _inventory(s3, bucket: str, run: dict) -> list | None:
    """The session list a workflow's subject-data-inventory pod printed, or None.

    Names are reused, so only a log written inside this run's own window counts.
    """
    from datetime import datetime, timedelta

    def at(v):
        return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None

    start, end = at(run.get("started_at")), at(run.get("finished_at"))
    if not start:
        return None
    end = (end or start) + timedelta(hours=1)
    wf = run["workflow_name"]
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"logs/{wf}/"):
        for o in page.get("Contents", []):
            if "subject-data-inventory" in o["Key"] and start <= o["LastModified"] <= end:
                text = s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read().decode()
                for line in text.splitlines():
                    if line.strip().startswith("[{"):
                        return json.loads(line)
    return None


NO_BOLD_ERROR = "no rest/nback BOLD run in any session"


def _refused_for_no_bold(s3, bucket: str, run: dict) -> bool:
    """True if this run's verify-staged-input pod refused the subject for having no BOLD.

    Such a subject never reaches inventory, so it has no inventory log to read;
    the ingress contract (docs/data-ingress.md) stops it one step earlier.
    """
    from datetime import datetime, timedelta

    def at(v):
        return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None

    start, end = at(run.get("started_at")), at(run.get("finished_at"))
    if not start:
        return False
    end = (end or start) + timedelta(hours=1)
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"logs/{run['workflow_name']}/"
    ):
        for o in page.get("Contents", []):
            if "verify-staged-input" in o["Key"] and start <= o["LastModified"] <= end:
                text = s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read().decode()
                if NO_BOLD_ERROR in text:
                    return True
    return False


def _session_source(found: dict, row) -> str:
    """One session's source reason, from what `source` found for its subject."""
    if row["fastsurfer"]:
        return ""
    inv = found.get(row["subject"])
    if inv is None:
        return "no_record"
    if inv == "no_bold":
        return "no_bold"
    if row["session"] not in inv:
        return "not_distributed"
    return "t1w" if inv[row["session"]] else "no_t1w"


def source(args) -> int:
    """Mark each session without anatomy: not distributed, no T1w, no BOLD, or T1w present.

    The subject-data-inventory pod lists only the sessions the transfer brought
    from ABCD, each with `t1w_available`. Read from the subject's newest
    WorkflowRun whose inventory log survives. A subject the ingress contract
    refused for having no BOLD at all never ran inventory; its newest run's
    verify-staged-input log says so, and every session of it is `no_bold`.
    """
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    s3 = boto3.client("s3", region_name=args.region)
    sessions = pd.read_parquet(OUT_DIR / "sessions.parquet")
    wanted = set(sessions.loc[~sessions["fastsurfer"], "subject"])
    runs_by_subject = defaultdict(list)
    files = [
        k
        for k in _keys(s3, args.metrics_bucket, "metrics/compacted/workflow_runs/")
        if k.endswith(".parquet")
    ]
    for k in files:
        t = pq.read_table(
            pa.BufferReader(s3.get_object(Bucket=args.metrics_bucket, Key=k)["Body"].read())
        )
        for r in t.select(["subject", "workflow_name", "started_at", "finished_at"]).to_pylist():
            if r["subject"] in wanted:
                runs_by_subject[r["subject"]].append(r)
    # Days the nightly compactor has not reached yet live only as raw JSON. A
    # subject flushed and rerun today has ONLY such records, and reading the
    # compacted table alone misfiles it as "no workflow record".
    compacted_days = {k.split("/")[3] for k in files}
    raw_days = {
        p["Prefix"].split("/")[2]
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=args.metrics_bucket, Prefix="metrics/workflow-runs/", Delimiter="/"
        )
        for p in page.get("CommonPrefixes", [])
    }
    raw_keys = [
        k
        for d in sorted(raw_days - compacted_days)
        for k in _keys(s3, args.metrics_bucket, f"metrics/workflow-runs/{d}/")
    ]
    with ThreadPoolExecutor(32) as pool:
        for doc in pool.map(lambda k: _json(s3, args.metrics_bucket, k), raw_keys):
            if doc and doc.get("subject") in wanted:
                runs_by_subject[doc["subject"]].append(doc)
    print(
        f"workflow runs: {len(files)} compacted files + {len(raw_keys)} raw records "
        f"from {len(raw_days - compacted_days)} uncompacted day(s)",
        flush=True,
    )

    def one(subj):
        c = _s3(args.region)
        runs = sorted(
            runs_by_subject.get(subj, []), key=lambda r: r["started_at"] or "", reverse=True
        )
        if runs and _refused_for_no_bold(c, args.data_bucket, runs[0]):
            return subj, "no_bold"
        for run in runs:
            inv = _inventory(c, args.data_bucket, run)
            if inv is not None:
                return subj, {s["session"]: bool(s.get("t1w_available")) for s in inv}
        return subj, None

    with ThreadPoolExecutor(args.workers) as pool:
        found = dict(pool.map(one, sorted(wanted)))

    sessions["source"] = sessions.apply(lambda row: _session_source(found, row), axis=1)
    sessions.to_parquet(OUT_DIR / "sessions.parquet", index=False)
    print(sessions.loc[~sessions["fastsurfer"], "source"].value_counts().to_string())
    return 0


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def _n(df, runs: bool = False) -> dict:
    out = {
        "subjects": df["subject"].nunique(),
        "sessions": len(df[["subject", "session"]].drop_duplicates()),
    }
    if runs:
        out["runs"] = len(df)
    return out


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}" + ("" if n == 1 else "s")


def _fmt(c: dict, kind: str) -> str:
    """A stage counts who remains; an exclusion counts what it removed, and from whom."""
    if kind == "excluded":
        what = _count(c["runs"], "run") if "runs" in c else _count(c["sessions"], "session")
        return f"{what} from {_count(c['subjects'], 'participant')}"
    parts = [_count(c["subjects"], "participant"), _count(c["sessions"], "session")]
    if "runs" in c:
        parts.append(_count(c["runs"], "run"))
    return "\\n".join(parts)


def compute(sessions, runs) -> list[dict]:
    """Every box in the flow, in drawing order, as {id, arm, kind, label, counts}."""
    import pandas as pd

    boxes = []

    def box(bid, arm, kind, label, df, is_runs=False):
        boxes.append({"id": bid, "arm": arm, "kind": kind, "label": label, **_n(df, is_runs)})
        return df

    s = box("enrolled", "", "stage", "ABCD v7 imaging manifest (mr_y_adm__info)", sessions)
    lost = s[~s["fastsurfer"]]
    src = lost["source"] if "source" in lost else pd.Series("no_record", index=lost.index)
    box(
        "x_anat_absent",
        "",
        "excluded",
        "Session imaging not distributed by ABCD",
        lost[src == "not_distributed"],
    )
    box("x_anat_no_t1w", "", "excluded", "No T1w in the distributed session", lost[src == "no_t1w"])
    box(
        "x_anat_no_bold",
        "",
        "excluded",
        "No rest or n-back BOLD distributed for the participant; not processed",
        lost[src == "no_bold"],
    )
    box(
        "x_anat_failed",
        "",
        "excluded",
        "T1w distributed; FastSurfer produced no anatomy",
        lost[src == "t1w"],
    )
    box(
        "x_anat_unknown",
        "",
        "excluded",
        "No anatomy; no workflow record of the source",
        lost[src == "no_record"],
    )
    s = box("anat", "", "stage", "FastSurfer longitudinal anatomy complete", s[s["fastsurfer"]])
    lost = s[~s["t1w2mni"]]
    box(
        "x_mni_qc",
        "",
        "excluded",
        "T1w → MNI rejected by QC gate",
        lost[lost["t1w2mni_verdict"] == "fail"],
    )
    box(
        "x_mni_other",
        "",
        "excluded",
        "T1w → MNI not produced (no QC rejection)",
        lost[lost["t1w2mni_verdict"] != "fail"],
    )
    s = box("mni", "", "stage", "T1w → MNI registration passed QC", s[s["t1w2mni"]])

    keep = s[["subject", "session"]]
    for arm, task in TASKS.items():
        title = {"rest": "Resting state", "nback": "N-back"}[arm]
        r = runs[runs["task"] == task].merge(keep, on=["subject", "session"])
        no_bold = keep.merge(
            r[["subject", "session"]].drop_duplicates(), how="left", indicator=True
        )
        box(
            f"{arm}_x_none",
            arm,
            "excluded",
            f"No {title.lower()} BOLD run reached BOLD → T1w",
            no_bold[no_bold["_merge"] == "left_only"],
        )
        r = box(f"{arm}_runs", arm, "stage", f"{title}: BOLD runs entering BOLD → T1w", r, True)
        lost = r[~r["b2t"]]
        box(
            f"{arm}_x_b2t_qc",
            arm,
            "excluded",
            "BOLD → T1w rejected by QC gate",
            lost[lost["b2t_verdict"] == "fail"],
            True,
        )
        box(
            f"{arm}_x_b2t_other",
            arm,
            "excluded",
            "BOLD → T1w not produced (no QC rejection)",
            lost[lost["b2t_verdict"] != "fail"],
            True,
        )
        r = box(f"{arm}_b2t", arm, "stage", "BOLD → T1w registration passed QC", r[r["b2t"]], True)
        box(
            f"{arm}_x_func",
            arm,
            "excluded",
            "Functional preprocessing produced no MNI BOLD",
            r[~r["func"]],
            True,
        )
        r = box(
            f"{arm}_func", arm, "stage", "MNI-space BOLD + confounds published", r[r["func"]], True
        )
        box(f"{arm}_x_surf", arm, "excluded", "No fsLR32k dtseries", r[~r["surf"]], True)
        r = box(
            f"{arm}_surf",
            arm,
            "stage",
            "fsLR32k grayordinate dtseries published",
            r[r["surf"]],
            True,
        )
        rec = r.merge(sessions[["subject", "session", f"abcd_{arm}"]], on=["subject", "session"])
        box(
            f"{arm}_x_abcd",
            arm,
            "excluded",
            f"ABCD {title.lower()} not recommended (mr_y_qc__incl)",
            rec[~rec[f"abcd_{arm}"]],
            True,
        )
        box(
            f"{arm}_final",
            arm,
            "stage",
            f"Analyzable: ABCD-recommended {title.lower()} runs",
            rec[rec[f"abcd_{arm}"]],
            True,
        )
    return boxes


# Each step's exclusion reasons, merged into one CONSORT side box beside the arrow.
EXCLUSIONS = {
    "enrolled": [
        "x_anat_absent",
        "x_anat_no_t1w",
        "x_anat_no_bold",
        "x_anat_failed",
        "x_anat_unknown",
    ],
    "anat": ["x_mni_qc", "x_mni_other"],
} | {
    step: reasons
    for arm in TASKS
    for step, reasons in {
        f"hub_{arm}": [f"{arm}_x_none"],
        f"{arm}_runs": [f"{arm}_x_b2t_qc", f"{arm}_x_b2t_other"],
        f"{arm}_b2t": [f"{arm}_x_func"],
        f"{arm}_func": [f"{arm}_x_surf"],
        f"{arm}_surf": [f"{arm}_x_abcd"],
    }.items()
}


def to_dot(boxes: list[dict]) -> str:
    """Graphviz source: a shared column, then the two arms, one side box per step.

    A reason that removed nothing is left out of its box, and a step whose
    reasons all removed nothing gets no box; the counts CSV keeps every one.
    """
    by_id = {b["id"]: b for b in boxes}
    lines = [
        "digraph consort {",
        '  graph [rankdir=TB, nodesep=0.3, ranksep=0.3, fontname="Helvetica", splines=ortho, newrank=true];',
        '  node  [shape=box, fontname="Helvetica", fontsize=10, margin="0.15,0.08"];',
        "  edge  [arrowsize=0.6];",
    ]

    def counts(b):
        return {k: b[k] for k in ("subjects", "sessions", "runs") if k in b and b[k] == b[k]}

    for b in boxes:
        if b["kind"] == "stage":
            lines.append(
                f'  {b["id"]} [label="{b["label"]}\\n{_fmt(counts(b), "stage")}", width=3.4];'
            )

    def step(prev, nxt, key):
        """prev -> point -> nxt, with EXCLUSIONS[key] merged into one box off the point."""
        reasons = [r for r in (by_id[x] for x in EXCLUSIONS.get(key, [])) if r["sessions"]]
        hub_id = f"p_{key}"
        lines.append(f'  {hub_id} [shape=point, width=0.01, label=""];')
        lines.append(f"  {prev} -> {hub_id} [arrowhead=none];")
        lines.append(f"  {hub_id} -> {nxt};")
        if not reasons:
            return
        body = "".join(f"- {r['label']}: {_fmt(counts(r), 'excluded')}\\l" for r in reasons)
        xid = f"x_{key}"
        lines.append(f'  {xid} [label="Excluded\\l{body}", style=filled, fillcolor="#f2f2f2"];')
        lines.append(f"  {{ rank=same; {hub_id}; {xid} }}")
        lines.append(f"  {hub_id} -> {xid};")

    step("enrolled", "anat", "enrolled")
    step("anat", "mni", "anat")
    for arm in TASKS:
        step("mni", f"{arm}_runs", f"hub_{arm}")
        chain = [f"{arm}_runs", f"{arm}_b2t", f"{arm}_func", f"{arm}_surf", f"{arm}_final"]
        for prev, nxt in pairwise(chain):
            step(prev, nxt, prev)
    lines.append("}")
    return "\n".join(lines) + "\n"


def render(args) -> int:
    import pandas as pd

    sessions = pd.read_parquet(OUT_DIR / "sessions.parquet")
    runs = pd.read_parquet(OUT_DIR / "runs.parquet")
    boxes = compute(sessions, runs)
    pd.DataFrame(boxes).to_csv(OUT_DIR / "consort_counts.csv", index=False)
    dot = OUT_DIR / "consort_flow.dot"
    dot.write_text(to_dot(boxes))
    for fmt in ("svg", "pdf"):
        subprocess.run(
            ["dot", f"-T{fmt}", str(dot), "-o", str(OUT_DIR / f"consort_flow.{fmt}")], check=True
        )
    for b in boxes:
        counts = ", ".join(
            f"{k} {b[k]:,}" for k in ("subjects", "sessions", "runs") if k in b and b[k] == b[k]
        )
        print(f"{b['arm'] or '-':<6} {b['kind']:<9} {b['label']}: {counts}")
    print(f"\nwrote {OUT_DIR}/consort_counts.csv and consort_flow.{{dot,svg,pdf}}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect", help="read ABCD tables and S3 into data/consort/*.parquet")
    c.add_argument("--data-bucket", required=True)
    c.add_argument("--metrics-bucket", required=True)
    c.add_argument("--region", help="default: the AWS profile's")
    c.add_argument("--workers", type=int, default=48)
    s = sub.add_parser("source", help="mark what ABCD shipped for each session with no anatomy")
    s.add_argument("--data-bucket", required=True)
    s.add_argument("--metrics-bucket", required=True)
    s.add_argument("--region", help="default: the AWS profile's")
    s.add_argument("--workers", type=int, default=32)
    sub.add_parser("render", help="counts CSV and the flow figure from data/consort/*.parquet")
    args = p.parse_args()
    return {"collect": collect, "source": source, "render": render}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
