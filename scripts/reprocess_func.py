"""
reprocess_func.py — func-scoped flush, launch, verify and reconcile for a
reprocess that replaces only the functional derivatives (plan 017).

Three subcommands:

  launch     preflight, flush one subject list's func derivatives and func
             metrics, then submit the list to the queue manager. Flush and
             submit are ONE command on purpose: a flush followed by a submission
             that never happened leaves subjects with nothing for days.
  verify     read-only; after a slice's workflows are terminal, check that every
             flushed run came back with its derivative, exactly one QC record, a
             clean read-back in the func pod's log, and that no anatomical step ran.
  reconcile  cohort-wide; delete every func metric that does not describe a stored
             derivative or its producing workflow, then purge superseded
             registration and fsqc records.

Every destructive subcommand is a dry run unless --write, and writes a JSONL audit
naming every key it deleted or rewrote. Subject IDs come from files only; nothing
here names a real subject, path or count.

Usage:
  pixi run -e ops python scripts/reprocess_func.py launch --subjects <csv> \\
      --data-bucket "$CLOUDPIPE_BUCKET" --metrics-bucket "$CLOUDPIPE_METRICS_BUCKET" \\
      --batch-label func-reprocess-NN [--write]
  pixi run -e ops python scripts/reprocess_func.py verify --subjects <csv> \\
      --data-bucket ... --metrics-bucket ... --batch-label func-reprocess-NN
  pixi run -e ops python scripts/reprocess_func.py reconcile --all \\
      --data-bucket ... --metrics-bucket ... [--write]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
from botocore.client import BaseClient

REPO = Path(__file__).resolve().parent.parent
# src/metrics/ modules import each other flatly, so the package directory itself
# has to be on the path (as in prep_test_batch.py).
sys.path.insert(0, str(REPO / "src" / "metrics"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compactor import UnattributableRows, drop_rows  # noqa: E402
from prep_test_batch import (  # noqa: E402
    COMPACTED_PREFIX,
    WORKFLOW_RUN_PREFIX,
    _strip_dt_partition,
    compacted_tables,
    flush_compacted,
    list_keys,
    workflow_subject_from_run_key,
)

# The two derivative trees a func reprocess replaces. Everything else under
# derivatives/ (FastSurfer, subregions, registration) is correct and is what lets
# the resubmission skip straight to func-preproc.
FUNC_DERIV_PREFIXES = ["derivatives/func/{subj}/", "derivatives/func_surf/{subj}/"]
# Per-scan QC computed from the func derivatives, keyed {subject}_{session}_...
FUNC_QC_PREFIXES = ["metrics/func-preproc/", "metrics/surface-sample/"]
# step_outcomes steps whose rows describe the func derivatives (all eras deleted).
FUNC_STEPS = ("func-preproc", "surface-sample", "surface-resample")
# T1w is required: inventory sets t1w_available from the staged T1w and the
# session fan-out filters on it, so a BOLD-only transfer processes nothing.
SCAN_TYPES = '["T1w","rest","nback"]'
# A non-skipped row for any of these means the longitudinal anatomy reran, which
# moves the template under the kept registrations. fsqc-metrics is NOT here: it
# has no exists-gate and reruns on every workflow by design.
ANATOMY_STEPS = (
    "anatomical-phase",
    "fastsurfer-template",
    "fastsurfer-template-parc",
    "fastsurfer-long-seg",
    "fastsurfer-long-parc",
)
# Anatomy statuses that mean the step did NOT run. `omitted` is Argo's word for a
# DAG task never reached because an upstream task failed: wave 1's seven failed
# ingress workflows recorded `anatomical-phase=omitted` and read as ANATOMY,
# hiding them from the retry list (2026-10-22).
ANATOMY_NOT_RUN = ("skipped", "omitted")
# verify's ANATOMY threshold (plan 017 amendment 20): below it, list and continue.
ANATOMY_STOP_FRACTION = 0.01
# Every label this tool submits starts with this, so reconcile can find the
# workflows it created by WorkflowRun.batch_label.
BATCH_LABEL_PREFIX = "func-reprocess"
DEPLOYMENT = "cloudpipe-queue-manager/cloudpipe-queue-manager"
ARGO_NAMESPACE = "argo-workflows"

STEP_OUTCOME_PREFIX = "metrics/step-outcomes/"
STEP_OUTCOME_SUFFIX = "_outcome.json"
MANIFEST_PREFIX = "metrics/subject-manifests/"
MANIFEST_SUFFIX = "_manifest.json"
WORKFLOW_START_PREFIX = "metrics/workflow-starts/"
LOG_PREFIX = "logs/"
FUNC_POD_MARKER = "functional-preprocessing-session-template"
READBACK_OK = "read-back OK"
READBACK_ERROR = "MNIBoldReadbackError"
# The record versions a fresh run must carry, equal to the schemas.py defaults
# (tests/test_reprocess_func.py holds them together). FuncQC 1.3 / SurfaceSampleQC
# 1.1 since #859 (summaries exclude the NSS frames); a 1.2 / 1.0 record was
# written by an image from before it, so the run is redone, not accepted.
FUNC_QC_SCHEMA = "1.3"
SURFACE_QC_SCHEMA = "1.1"

# Producer window slack, the same rule data/census/cost_cleanup.py attributes
# terminal outputs by: an output belongs to the workflow whose
# [started_at - 2 min, finished_at + 2 min] contains its LastModified.
PRODUCER_SLACK = timedelta(minutes=2)

# Subject IDs are matched as whole key segments, never substrings; refusing
# anything outside this shape keeps a malformed ID from widening a match.
SUBJECT_RE = re.compile(r"^sub-[A-Za-z0-9]+$")
# A run's MNI BOLD, in either layout: the exploded `.nii.gz` the driver has
# published since ADR 017 was extended to func, or the legacy tarball every run
# held before. Both name exactly one object per run, so either marks the run.
MNI_BOLD_RE = re.compile(
    r"^derivatives/func/(sub-[^/]+)/(ses-[^/]+)/\1_\2_(task-[^_]+)_(run-[^_]+)"
    r"_space-MNI152NLin2009cAsym_bold\.(tar\.gz|nii\.gz)$"
)
# The session's completion marker (images/shared/func_derivatives.py). An
# exploded run counts only once the marker lists it.
FUNC_MARKER_RE = re.compile(r"^derivatives/func/(sub-[^/]+)/(ses-[^/]+)/_complete\.json$")
# ABCD's task timing, copied beside each fresh n-back BOLD by the func driver.
EVENTS_RE = re.compile(
    r"^derivatives/func/(sub-[^/]+)/(ses-[^/]+)/\1_\2_(task-[^_]+)_(run-[^_]+)_events\.tsv$"
)
# The tasks whose runs should carry an events file: ABCD ships one for every
# n-back run it releases, and none for rest. verify reports a missing one; it
# does not retry for it.
EVENTS_TASKS = ("task-nback",)
DTSERIES_RE = re.compile(
    r"^derivatives/func_surf/(sub-[^/]+)/(ses-[^/]+)/fsLR32k/\1_\2_(task-[^_]+)_(run-[^_]+)"
    r"_space-fsLR32k_bold\.dtseries\.nii$"
)
QC_BASENAME_RE = re.compile(
    r"^(sub-[^_]+)_(ses-[^_]+)_(task-[^_]+)_(run-[^_]+)_(?:surf_)?qc\.json$"
)
DT_IN_KEY_RE = re.compile(r"/dt=(\d{4}-\d{2}-\d{2})/")

DEFAULT_AUDIT_DIR = REPO / "data" / "reprocess"
LEDGER_NAME = "func_reprocess_ledger.jsonl"


# ---------------------------------------------------------------------------
# Key parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepKey:
    workflow_name: str
    workflow_uid: str
    step: str
    subject: str
    session: str
    task: str
    run: str


def parse_step_outcome_key(key: str) -> StepKey | None:
    """Parse a step-outcome key in either layout (None if it is not one).

    `{wf}__{uid}__{step}__{subj}__{ses}__{task}__{run}_outcome.json` since #668,
    `{wf}__{step}__{subj}__{ses}__{task}__{run}_outcome.json` before it. Argo
    names are DNS-1123 labels and UIDs are UUIDs, so neither contains `__`, and
    the part count alone tells the layouts apart.
    """
    if not key.startswith(STEP_OUTCOME_PREFIX) or not key.endswith(STEP_OUTCOME_SUFFIX):
        return None
    stem = _strip_dt_partition(key[len(STEP_OUTCOME_PREFIX) : -len(STEP_OUTCOME_SUFFIX)])
    parts = stem.split("__")
    if len(parts) == 6:
        parts.insert(1, "")
    elif len(parts) != 7:
        return None
    if not all(p for i, p in enumerate(parts) if i != 1):
        return None
    return StepKey(*parts)


def split_tag(tag: str) -> tuple[str, str]:
    """`{name}__{uid}` or bare `{name}` -> (name, uid)."""
    name, _, uid = tag.partition("__")
    return name, uid


def qc_unit(key: str) -> tuple[str, str, str, str] | None:
    """A per-scan QC key's (subject, session, task, run), or None."""
    m = QC_BASENAME_RE.match(key.rsplit("/", 1)[-1])
    return m.groups() if m else None  # type: ignore[return-value]


def manifest_tag_subject(key: str) -> tuple[str, str] | None:
    """`metrics/subject-manifests/dt=…/{tag}__{subj}_manifest.json` -> (tag, subj)."""
    if not key.startswith(MANIFEST_PREFIX) or not key.endswith(MANIFEST_SUFFIX):
        return None
    stem = _strip_dt_partition(key[len(MANIFEST_PREFIX) : -len(MANIFEST_SUFFIX)])
    tag, sep, subj = stem.rpartition("__")
    return (tag, subj) if sep and tag and subj else None


def workflow_start_tag(key: str) -> str | None:
    """`metrics/workflow-starts/dt=…/{tag}.json` -> tag."""
    if not key.startswith(WORKFLOW_START_PREFIX) or not key.endswith(".json"):
        return None
    return _strip_dt_partition(key[len(WORKFLOW_START_PREFIX) : -len(".json")]) or None


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def read_subject_csv(path: str) -> list[str]:
    """Subject IDs from a CSV whose header is `subject_id`; refuses malformed IDs."""
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or reader.fieldnames[0] != "subject_id":
            raise SystemExit(f"{path}: the first column header must be `subject_id`")
        subjects = [row["subject_id"].strip() for row in reader if row["subject_id"].strip()]
    bad = [s for s in subjects if not SUBJECT_RE.match(s)]
    if bad:
        raise SystemExit(f"{path}: {len(bad)} malformed subject ID(s), e.g. {bad[0]!r}")
    if len(set(subjects)) != len(subjects):
        raise SystemExit(f"{path}: duplicate subject IDs")
    if not subjects:
        raise SystemExit(f"{path}: no subjects")
    return subjects


def file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def list_objects(s3, bucket: str, prefix: str) -> list[dict]:
    """Every object under a prefix as {Key, LastModified}."""
    out: list[dict] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out.extend(
            {"Key": o["Key"], "LastModified": o["LastModified"]} for o in page.get("Contents", [])
        )
    return out


_PROCESS_S3 = None  # one boto3 client per listing worker process


def _process_init(region: str | None) -> None:
    global _PROCESS_S3
    _PROCESS_S3 = boto3.client("s3", region_name=region)


def _process_list(job: tuple[str, str, str]) -> tuple[str, list[str]]:
    bucket, prefix, part = job
    return prefix, list_keys(_PROCESS_S3, bucket, part)


def list_keys_parallel(s3, bucket: str, prefixes: list[str], workers: int) -> dict[str, list[str]]:
    """Every key under each prefix, as `list_keys` returns it, listed in parallel.

    One delimited listing per prefix finds its partitions (`dt=YYYY-MM-DD/`) and
    any key directly under it; each partition is then listed whole, all prefixes'
    partitions in one pool. The keys are the same set as a serial `list_keys`, so
    what a flush or verify sees is unchanged. Only the wall time changes: serially,
    func-preproc (180k keys), surface-sample (180k) and step-outcomes (893k) took
    123 + 122 + 370 s, ten minutes of every launch whatever the slice size.

    The partitions are listed in PROCESSES for a real boto3 client. The work is
    parsing ListObjectsV2's XML, which holds the GIL: 32 threads took 435 s of
    that 615 (2026-10-24), at 100% of one core. Any other client (the tests'
    in-memory fake) is listed with threads in this process.
    """

    def top(prefix: str) -> tuple[str, list[str], list[str]]:
        direct: list[str] = []
        parts: list[str] = []
        pages = s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix, Delimiter="/"
        )
        for page in pages:
            direct += [o["Key"] for o in page.get("Contents", [])]
            parts += [p["Prefix"] for p in page.get("CommonPrefixes", [])]
        return prefix, direct, parts

    with ThreadPoolExecutor(max_workers=workers) as pool:
        tops = list(pool.map(top, prefixes))
    out = {prefix: list(direct) for prefix, direct, _ in tops}
    jobs = [(bucket, prefix, part) for prefix, _, parts in tops for part in parts]
    if isinstance(s3, BaseClient) and jobs:
        procs = max(1, min(workers, os.cpu_count() or 1, len(jobs)))
        with ProcessPoolExecutor(
            max_workers=procs, initializer=_process_init, initargs=(s3.meta.region_name,)
        ) as pool:
            listed = list(pool.map(_process_list, jobs))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            listed = list(pool.map(lambda j: (j[1], list_keys(s3, j[0], j[2])), jobs))
    for prefix, keys in listed:
        out[prefix] += keys
    return {prefix: sorted(keys) for prefix, keys in out.items()}


def get_json(s3, bucket: str, key: str) -> dict | None:
    try:
        doc = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:
        return None
    return doc if isinstance(doc, dict) else None


def get_text(s3, bucket: str, key: str) -> str:
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8", "replace")


def object_exists(s3, bucket: str, key: str) -> bool:
    """List, not HEAD: a HEAD 403 and 404 look alike, a listing does not."""
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
    return any(o["Key"] == key for o in resp.get("Contents", []))


def parse_instant(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Audit:
    """Append-only JSONL: one line per planned or executed action."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields) -> None:
        with self.path.open("a") as fh:
            fh.write(json.dumps({"at": iso(now_utc()), "event": event, **fields}) + "\n")

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


class DeleteFailed(RuntimeError):
    pass


def delete_audited(s3, bucket: str, keys: list[str], audit: Audit, category: str) -> int:
    """Delete keys 1000 at a time, auditing each with its version IDs.

    Not prep_test_batch.delete_keys: that one runs Quiet and ignores the
    response's Errors, and this audit has to name every delete marker (the
    metrics bucket is versioned, so the marker is how a delete is undone).
    """
    done = 0
    for i in range(0, len(keys), 1000):
        chunk = keys[i : i + 1000]
        resp = s3.delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": False}
        )
        for d in resp.get("Deleted", []):
            audit.write(
                "deleted",
                category=category,
                bucket=bucket,
                key=d["Key"],
                version_id=d.get("VersionId"),
                delete_marker_version_id=d.get("DeleteMarkerVersionId"),
            )
            done += 1
        errors = resp.get("Errors", [])
        if errors:
            for e in errors:
                audit.write("delete_error", category=category, bucket=bucket, **e)
            raise DeleteFailed(f"{len(errors)} delete(s) failed in {category}, e.g. {errors[0]}")
    return done


def compacted_files(s3, bucket: str, table: str) -> list[str]:
    return [
        k for k in list_keys(s3, bucket, f"{COMPACTED_PREFIX}{table}/") if k.endswith(".parquet")
    ]


def read_parquet_rows(s3, bucket: str, key: str, columns: tuple[str, ...]) -> list[dict]:
    """Rows of one compacted file, as dicts over `columns` (absent column -> None)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pq.read_table(pa.BufferReader(s3.get_object(Bucket=bucket, Key=key)["Body"].read()))
    present = [c for c in columns if c in table.column_names]
    rows = table.select(present).to_pylist()
    return [{c: r.get(c) for c in columns} for r in rows]


def drop_compacted(
    s3,
    bucket: str,
    table: str,
    column: str | tuple[str, ...],
    values_for,
    write: bool,
    workers: int,
    complement: bool = False,
) -> tuple[int, list[str]]:
    """drop_rows over every compacted file of `table`. Returns (rows, failed keys).

    `values_for(key)` returns the value set for one file, so a caller that has to
    read a file to decide what to drop from it can; a constant set is
    `lambda _k: values`. A file without the columns is reported and left alone.
    """

    def one(key: str):
        try:
            values = values_for(key)
            if not values and not complement:
                return key, 0, None
            return (
                key,
                drop_rows(s3, bucket, key, column, values, write, complement=complement),
                None,
            )
        except Exception as exc:  # noqa: BLE001 — reported per file, never swallowed
            return key, 0, exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, compacted_files(s3, bucket, table)))
    rows, failed = 0, []
    for key, n, exc in results:
        if isinstance(exc, UnattributableRows):
            print(f"    [note] {key} left in place: {exc}")
        elif exc is not None:
            print(f"    [FAILED] {key}: {exc}")
            failed.append(key)
        rows += n
    return rows, failed


# ---------------------------------------------------------------------------
# External systems (module-level so tests can replace them)
# ---------------------------------------------------------------------------


def running_workflows() -> list[dict] | None:
    """Workflows Argo reports as running, or None when Argo cannot be read.

    None is not `[]`: a blind preflight must refuse, not pass.
    """
    try:
        proc = subprocess.run(
            ["argo", "-n", ARGO_NAMESPACE, "list", "--running", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    if not proc.stdout.strip() or proc.stdout.strip() == "null":
        return []
    try:
        workflows = json.loads(proc.stdout)
    except ValueError:
        return None
    return workflows if isinstance(workflows, list) else None


def workflow_subject(wf: dict) -> str | None:
    """The subject of an Argo workflow: its `subjectid` label, else its subjID parameter."""
    labels = (wf.get("metadata") or {}).get("labels") or {}
    if labels.get("subjectid"):
        return labels["subjectid"]
    for p in ((wf.get("spec") or {}).get("arguments") or {}).get("parameters", []):
        if p.get("name") == "subjID":
            return p.get("value")
    return None


_EKS_ARN = re.compile(r"^arn:aws[\w-]*:eks:([a-z0-9-]+):(\d{12}):cluster/([\w.-]+)$")


def _kubectl_view(jsonpath: str) -> str:
    result = subprocess.run(
        ["kubectl", "config", "view", "--minify", "-o", f"jsonpath={jsonpath}"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def kubectl_cluster() -> str:
    """kubectl's current-context cluster, as `kubectl config view --minify` names it."""
    return _kubectl_view("{.contexts[0].context.cluster}")


def kubectl_server() -> str:
    """The API server URL kubectl's current context actually connects to."""
    return _kubectl_view("{.clusters[0].cluster.server}")


def aws_account() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


def eks_endpoint(region: str, name: str) -> str:
    """The cluster's real API endpoint, as the AWS credentials' account reports it."""
    return boto3.client("eks", region_name=region).describe_cluster(name=name)["cluster"][
        "endpoint"
    ]


def _same_url(a: str, b: str) -> bool:
    return a.rstrip("/").lower() == b.rstrip("/").lower()


def require_same_account() -> str:
    """Refuse unless kubectl and the AWS credentials name the same cluster.

    The flush runs in the account the AWS credentials name; Argo, and the Prefect
    URL that cloudpipe-env.sh derives, follow kubectl's current context. On
    2026-10-14 a second account's cluster was the current context more than once,
    which would flush production and then submit (and run preflight) against a
    cluster that cannot read production's bucket. Copied from
    scripts/investigations/https-ingress-probe/_account_guard.py (#812), which is
    not published. Point kubectl at the right cluster in this shell instead of
    overriding this.

    Two checks, because the context's NAME is not where kubectl connects. On
    2026-10-23 a kubeconfig whose context named production's ARN held another
    cluster's server URL, and an ARN-only check passed it. So the server URL must
    also equal the endpoint that `eks describe-cluster` reports for that ARN.
    """
    cluster = kubectl_cluster()
    match = _EKS_ARN.match(cluster)
    if not match:
        raise SystemExit(
            f"refusing: kubectl's current context is not an EKS cluster ARN ({cluster!r})"
        )
    region, kube, name = match.groups()
    account = aws_account()
    if kube != account:
        raise SystemExit(
            f"refusing: kubectl targets account {kube} ({cluster}) but the AWS credentials are "
            f"for account {account}. Point kubectl at {account}'s cluster in this shell, e.g.\n"
            f"  aws eks update-kubeconfig --region {region} --name {name} "
            f"--kubeconfig ~/.kube/cloudpipe-{account}.config\n"
            f"  export KUBECONFIG=~/.kube/cloudpipe-{account}.config"
        )
    server = kubectl_server()
    try:
        endpoint = eks_endpoint(region, name)
    except Exception as exc:  # fail closed: an unverifiable target is not a verified one
        raise SystemExit(
            f"refusing: could not read {cluster}'s endpoint to check kubectl's server "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    if not _same_url(server, endpoint):
        raise SystemExit(
            f"refusing: kubectl's context is named {cluster}, but it connects to {server!r}, "
            f"and that cluster's endpoint is {endpoint}. The kubeconfig points at another "
            f"cluster under this name. Regenerate it:\n"
            f"  aws eks update-kubeconfig --region {region} --name {name} "
            f'--kubeconfig "${{KUBECONFIG:-$HOME/.kube/config}}"'
        )
    print(f"account {account}, cluster {cluster}, server {server}")
    return account


def check_prefect_target() -> str:
    """Refuse unless Prefect points at a real server, and the deployment resolves.

    Without PREFECT_API_URL the client silently starts a temporary local server and
    the flow run never reaches the cluster (2026-09-10, 2026-10-06). Checked before
    the flush, because after it a failed submission is the worst outcome.
    """
    url = os.environ.get("PREFECT_API_URL", "")
    if not url or "127.0.0.1" in url or "localhost" in url:
        raise SystemExit(
            "PREFECT_API_URL is unset or local — run under `pixi run -e ops` so the flow "
            "run reaches the cluster"
        )
    from prefect.client.orchestration import get_client

    with get_client(sync_client=True) as client:
        deployment = client.read_deployment_by_name(DEPLOYMENT)
    return str(deployment.id)


def create_flow_run(parameters: dict, idempotency_key: str):
    """Create the queue-manager flow run and return it without waiting."""
    from prefect.deployments import run_deployment

    return run_deployment(
        DEPLOYMENT,
        parameters=parameters,
        timeout=0,
        idempotency_key=idempotency_key,
        as_subflow=False,
    )


# ---------------------------------------------------------------------------
# Ledger: what each subject held before its FIRST flush, and when each flush ran
# ---------------------------------------------------------------------------


def load_ledger(path: Path) -> dict[str, dict]:
    """{subject: {"baseline": [MNI BOLD keys] | None, "flushed_at": [iso, ...]}}.

    The baseline event's field is still named `tarballs`, as in ledgers written
    before the exploded layout; the keys in it may be either layout's.
    """
    ledger: dict[str, dict] = defaultdict(lambda: {"baseline": None, "flushed_at": []})
    for ev in Audit(path).events():
        entry = ledger[ev["subject"]]
        if ev["event"] == "baseline" and entry["baseline"] is None:
            entry["baseline"] = ev["tarballs"]
        elif ev["event"] == "flushed":
            entry["flushed_at"].append(ev["at"])
    return dict(ledger)


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------


@dataclass
class FlushPlan:
    deriv: dict[str, list[dict]]  # subject -> objects under FUNC_DERIV_PREFIXES
    qc_raw: list[str]
    steps_raw: list[str]

    @property
    def deriv_keys(self) -> list[str]:
        return [o["Key"] for objs in self.deriv.values() for o in objs]

    def bold_keys(self, subject: str) -> list[str]:
        return [o["Key"] for o in self.deriv[subject] if MNI_BOLD_RE.match(o["Key"])]


def step_values(subjects) -> set[tuple[str, str]]:
    return {(s, step) for s in subjects for step in FUNC_STEPS}


def plan_flush(s3, data_bucket, metrics_bucket, subjects, workers) -> FlushPlan:
    subject_set = set(subjects)

    def deriv_of(subj):
        return subj, [
            o
            for t in FUNC_DERIV_PREFIXES
            for o in list_objects(s3, data_bucket, t.format(subj=subj))
        ]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        deriv = dict(pool.map(deriv_of, subjects))

    listed = list_keys_parallel(
        s3, metrics_bucket, [*FUNC_QC_PREFIXES, STEP_OUTCOME_PREFIX], workers
    )
    # The basename's first `_` field is the whole subject ID (IDs contain no `_`),
    # so this is an exact match — sub-AB never catches sub-ABC's records.
    qc_raw = [
        k
        for prefix in FUNC_QC_PREFIXES
        for k in listed[prefix]
        if k.rsplit("/", 1)[-1].split("_", 1)[0] in subject_set
    ]
    steps_raw = []
    for k in listed[STEP_OUTCOME_PREFIX]:
        parsed = parse_step_outcome_key(k)
        if parsed and parsed.step in FUNC_STEPS and parsed.subject in subject_set:
            steps_raw.append(k)
    return FlushPlan(deriv=deriv, qc_raw=qc_raw, steps_raw=steps_raw)


def preflight(s3, args, subjects: list[str]) -> list[str]:
    """Reasons to refuse the launch; empty when it may proceed."""
    problems = []
    running = running_workflows()
    if running is None:
        problems.append("could not list running Argo workflows (check cluster access)")
    else:
        busy = sorted({workflow_subject(wf) for wf in running} & set(subjects))
        if busy:
            problems.append(f"{len(busy)} subject(s) have a running workflow, e.g. {busy[0]}")

    def no_template(subj):
        key = f"derivatives/fastsurfer/{subj}/long-template/_complete.json"
        return None if object_exists(s3, args.data_bucket, key) else subj

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        missing = sorted(s for s in pool.map(no_template, subjects) if s)
    if missing:
        problems.append(
            f"{len(missing)} subject(s) have no long-template _complete.json, e.g. {missing[0]}"
        )
    return problems


def launch_gate(prior: list[dict], sha: str, skip_flush: bool) -> str:
    """What a launch may do given its label's audit: flush, submit, or refuse (why).

    One label is one flush and one submission. A completed flush with no
    submission is the stranded state; only --skip-flush on the same CSV leaves it.
    """
    if any(e["event"] == "submitted" for e in prior):
        return "already submitted; use a new label"
    flushed = [e for e in prior if e["event"] == "flush_complete"]
    if skip_flush:
        if any(e.get("csv_sha256") == sha for e in flushed):
            return "submit"
        return "--skip-flush needs a completed flush of this same CSV under this label"
    if flushed:
        return "already flushed but not submitted; re-run with --skip-flush to submit it"
    return "flush"


def no_func_problems(subjects, plan: FlushPlan, ledger) -> list[str]:
    """Subjects with nothing to reprocess. A resumed partial flush legitimately
    finds some already emptied; the ledger baseline proves they had func output."""
    no_func = sorted(
        s for s in subjects if not plan.bold_keys(s) and not (ledger.get(s) or {}).get("baseline")
    )
    return [f"{len(no_func)} subject(s) have no MNI BOLD, e.g. {no_func[0]}"] if no_func else []


def launch(s3, args) -> int:
    subjects = read_subject_csv(args.subjects)
    if not args.batch_label.startswith(BATCH_LABEL_PREFIX):
        raise SystemExit(f"--batch-label must start with {BATCH_LABEL_PREFIX!r}")
    audit_dir = Path(args.audit_dir)
    audit = Audit(audit_dir / f"{args.batch_label}_launch.jsonl")
    sha = file_sha256(args.subjects)
    require_same_account()

    gate = launch_gate(audit.events(), sha, args.skip_flush)
    if gate == "submit":
        if not args.write:
            print("Dry run: would submit without flushing (pass --write).")
            return 0
        return submit(s3, args, subjects, sha, audit)
    if gate != "flush":
        print(f"ERROR: {args.batch_label}: {gate} ({audit.path}).", file=sys.stderr)
        return 2

    print(f"=== func reprocess launch: {args.batch_label} ({len(subjects)} subjects) ===")
    if args.write:
        check_prefect_target()
    ledger_path = audit_dir / LEDGER_NAME
    ledger = load_ledger(ledger_path)
    problems = preflight(s3, args, subjects)
    plan = plan_flush(s3, args.data_bucket, args.metrics_bucket, subjects, args.workers)
    problems += no_func_problems(subjects, plan, ledger)
    if problems:
        for p in problems:
            print(f"REFUSED: {p}", file=sys.stderr)
        return 2

    tag = "write" if args.write else "dry-run"
    n_bold = sum(len(plan.bold_keys(s)) for s in subjects)
    print(f"  [{tag}] derivatives: {len(plan.deriv_keys)} objects ({n_bold} MNI BOLD runs)")
    print(f"  [{tag}] raw per-scan QC: {len(plan.qc_raw)} objects")
    print(f"  [{tag}] raw step-outcomes {'/'.join(FUNC_STEPS)}: {len(plan.steps_raw)} objects")
    if not args.write:
        qc_rows, step_rows, _ = flush_func_compacted(s3, args, subjects, write=False)
        audit.write(
            "dry_run",
            csv_sha256=sha,
            subjects=len(subjects),
            deriv=len(plan.deriv_keys),
            tarballs=n_bold,  # field name kept: older audits use it
            qc_raw=len(plan.qc_raw),
            steps_raw=len(plan.steps_raw),
            qc_compacted_rows=qc_rows,
            steps_compacted_rows=step_rows,
        )
        print(f"\nDry run. Audit: {audit.path}\nRe-run with --write to flush and submit.")
        return 0

    if not confirmed(args, len(plan.deriv_keys), len(subjects)):
        print("Aborted.")
        return 1
    # Again, now: the prompt can sit unanswered for hours, and another launch of
    # this label may have flushed and submitted meanwhile (2026-10-15: a 17-hour-old
    # prompt was still open beside the live run). This plan is stale by then.
    gate = launch_gate(audit.events(), sha, args.skip_flush)
    if gate != "flush":
        print(
            f"ERROR: {args.batch_label}: {gate} while awaiting confirmation; nothing deleted.",
            file=sys.stderr,
        )
        return 2
    if not flush(s3, args, subjects, plan, ledger, Audit(ledger_path), audit):
        return 1
    audit.write("flush_complete", csv_sha256=sha, subjects=len(subjects))
    return submit(s3, args, subjects, sha, audit)


def confirmed(args, n_deriv: int, n_subjects: int) -> bool:
    """The operator types the batch label; --yes skips it."""
    if args.yes:
        return True
    answer = input(
        f"\nIRREVERSIBLY delete {n_deriv} derivative objects from s3://{args.data_bucket} "
        f"(unversioned) and submit {n_subjects} subjects? Type the batch label to confirm: "
    )
    return answer.strip() == args.batch_label


def flush_func_compacted(s3, args, subjects, write: bool) -> tuple[int, int, list[str]]:
    """The batch's func rows out of compacted per-scan QC and step_outcomes."""
    qc_rows, failed = flush_compacted(
        s3,
        args.metrics_bucket,
        subjects,
        compacted_tables(FUNC_QC_PREFIXES),
        not write,
        args.workers,
    )
    tag = "write" if write else "dry-run"
    step_rows, failed2 = drop_compacted(
        s3,
        args.metrics_bucket,
        "step_outcomes",
        ("subject", "step"),
        lambda _k: step_values(subjects),
        write,
        args.workers,
    )
    print(f"  [{tag}] compacted step_outcomes: {step_rows} rows")
    return qc_rows, step_rows, failed + failed2


def flush(s3, args, subjects, plan: FlushPlan, ledger, ledger_audit: Audit, audit: Audit) -> bool:
    """Delete the plan, raw before compacted. True when every delete landed."""
    # Baseline BEFORE any delete, and only once per subject: a retry slice must be
    # verified against what the subject held before the campaign, not what a
    # failed first attempt left behind.
    for s in subjects:
        if not (ledger.get(s) or {}).get("baseline"):
            ledger_audit.write(
                "baseline", subject=s, tarballs=plan.bold_keys(s), batch_label=args.batch_label
            )
    try:
        d = delete_audited(s3, args.data_bucket, plan.deriv_keys, audit, "derivatives")
        q = delete_audited(s3, args.metrics_bucket, plan.qc_raw, audit, "qc_raw")
        st = delete_audited(s3, args.metrics_bucket, plan.steps_raw, audit, "steps_raw")
    except DeleteFailed as exc:
        print(f"ERROR: {exc}\nRe-run the same command to finish the flush.", file=sys.stderr)
        return False
    # Compacted after raw — see flush_compacted's docstring for why.
    qc_rows, step_rows, failed = flush_func_compacted(s3, args, subjects, write=True)
    audit.write("compacted", qc_rows=qc_rows, step_rows=step_rows, failed=failed)
    if failed:
        print(
            f"ERROR: {len(failed)} compacted file(s) could not be filtered. Re-run the same "
            "command to finish the flush; nothing was submitted.",
            file=sys.stderr,
        )
        return False
    for s in subjects:
        ledger_audit.write("flushed", subject=s, batch_label=args.batch_label)
    print(
        f"Flushed {d} derivative + {q + st} raw metric objects, "
        f"{qc_rows + step_rows} compacted rows."
    )
    return True


def submit(s3, args, subjects, sha: str, audit: Audit) -> int:
    key = f"config/func-reprocess/{args.batch_label}.csv"
    uri = f"s3://{args.data_bucket}/{key}"
    parameters = {
        "subjects_file": uri,
        "batch_label": args.batch_label,
        "globus_scan_types": SCAN_TYPES,
        "ingress_mode": args.ingress_mode,
    }
    try:
        check_prefect_target()
        s3.upload_file(args.subjects, args.data_bucket, key)
        flow_run = create_flow_run(parameters, idempotency_key=f"{args.batch_label}:{sha}")
    except BaseException as exc:  # noqa: BLE001 — any failure here strands a flushed slice
        recover = (
            f"pixi run -e ops python scripts/reprocess_func.py launch --subjects {args.subjects} "
            f'--data-bucket "{args.data_bucket}" --metrics-bucket "{args.metrics_bucket}" '
            f"--batch-label {args.batch_label} --ingress-mode {args.ingress_mode} "
            "--skip-flush --write"
        )
        audit.write("submit_failed", error=f"{type(exc).__name__}: {exc}")
        print(
            f"\nERROR: submission failed AFTER the flush ({type(exc).__name__}: {exc}).\n"
            f"These {len(subjects)} subjects now have no func output. Submit them with:\n"
            f"  {recover}",
            file=sys.stderr,
        )
        return 1
    audit.write(
        "submitted",
        flow_run_id=str(flow_run.id),
        flow_run_name=flow_run.name,
        uri=uri,
        ingress_mode=args.ingress_mode,
    )
    print(f"Submitted flow run {flow_run.name} ({flow_run.id}) for {uri}")
    return 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@dataclass
class SubjectResult:
    subject: str
    status: str  # OK | PENDING | RETRY | ANATOMY
    reasons: list[str]


@dataclass
class Evidence:
    """Everything verify gathered for one subject, all of it written after its flush
    except `baseline` (the runs it held before the campaign).

    `bold`/`dtseries` map unit -> LastModified; `qc_raw`/`surf_raw` map unit
    -> list of record dicts; the compacted counters count rows per unit.
    `exploded` holds the units whose BOLD is in the exploded layout, `published`
    the units a fresh session marker lists, and `tissue` the sessions whose fresh
    marker carries tissue maps.
    """

    flushed_at: datetime
    baseline: set = field(default_factory=set)
    bold: dict = field(default_factory=dict)
    exploded: set = field(default_factory=set)
    published: set = field(default_factory=set)
    tissue: set = field(default_factory=set)
    dtseries: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)
    qc_raw: dict = field(default_factory=lambda: defaultdict(list))
    surf_raw: dict = field(default_factory=lambda: defaultdict(list))
    qc_compacted: Counter = field(default_factory=Counter)
    surf_compacted: Counter = field(default_factory=Counter)
    workflows: set = field(default_factory=set)
    readback_ok: int = 0
    readback_errors: int = 0
    anatomy: list = field(default_factory=list)
    running: bool = False


def unit_of_bold(key: str):
    m = MNI_BOLD_RE.match(key)
    return m.groups()[:4] if m else None


def unit_of_events(key: str):
    m = EVENTS_RE.match(key)
    return m.groups() if m else None


def unit_of_dtseries(key: str):
    m = DTSERIES_RE.match(key)
    return m.groups() if m else None


def dates_since(start: datetime, end: datetime) -> list[str]:
    days = (end.date() - start.date()).days
    return [(start.date() + timedelta(days=i)).isoformat() for i in range(days + 1)]


def fresh_single(records: list[dict], after: datetime, schema_version: str | None) -> bool:
    """Exactly one record, completed after `after` (and at `schema_version`, if given)."""
    if len(records) != 1:
        return False
    done = parse_instant(records[0].get("completed_at"))
    if done is None or done <= after:
        return False
    return schema_version is None or records[0].get("schema_version") == schema_version


def raw_problems(ev: Evidence, units: set) -> list[str]:
    reasons = []
    missing = sorted(ev.baseline - {u for u, lm in ev.bold.items() if lm > ev.flushed_at})
    if missing:
        reasons.append(
            f"{len(missing)} run(s) have no new MNI BOLD, e.g. {'_'.join(missing[0][1:])}"
        )
    # An exploded run's objects exist before its marker does, so a run whose
    # upload died reads as present by listing alone. Only the marker says done.
    unlisted = sorted(u for u in units if u in ev.exploded and u not in ev.published)
    if unlisted:
        reasons.append(
            f"{len(unlisted)} run(s) are not in a fresh session _complete.json, "
            f"e.g. {'_'.join(unlisted[0][1:])}"
        )
    no_surf = [u for u in units if not (ev.dtseries.get(u) and ev.dtseries[u] > ev.flushed_at)]
    if no_surf:
        reasons.append(f"{len(no_surf)} run(s) have no new fsLR32k dtseries")
    # A record below the current version was written by an image from before #859.
    for name, raw, version in (
        ("func_preproc", ev.qc_raw, FUNC_QC_SCHEMA),
        ("surface_sample", ev.surf_raw, SURFACE_QC_SCHEMA),
    ):
        bad = [u for u in units if not fresh_single(raw.get(u, []), ev.flushed_at, version)]
        if bad:
            reasons.append(f"{len(bad)} run(s) lack exactly one fresh raw {name} record")
    if ev.readback_ok < len(units):
        reasons.append(f"{READBACK_OK} x{ev.readback_ok} in logs, expected {len(units)}")
    return reasons


def classify_subject(subject: str, ev: Evidence) -> SubjectResult:
    """One subject's verdict from everything verify gathered."""
    if ev.anatomy:
        return SubjectResult(
            subject, "ANATOMY", [f"non-skipped anatomical step: {', '.join(ev.anatomy)}"]
        )
    if ev.running:
        return SubjectResult(subject, "PENDING", ["workflow still running"])
    units = ev.baseline | {u for u, lm in ev.bold.items() if lm > ev.flushed_at}
    reasons = raw_problems(ev, units)
    if ev.readback_errors:
        reasons.insert(
            0, f"{ev.readback_errors} {READBACK_ERROR} in func-preproc logs — STOP all launches"
        )
    if reasons:
        return SubjectResult(subject, "RETRY", reasons)
    pending = []
    for name, counter in (("func_preproc", ev.qc_compacted), ("surface_sample", ev.surf_compacted)):
        dup = [u for u in units if counter[u] > 1]
        if dup:
            return SubjectResult(
                subject, "RETRY", [f"{len(dup)} run(s) have duplicate compacted {name} rows"]
            )
        lag = [u for u in units if counter[u] == 0]
        if lag:
            pending.append(f"{len(lag)} run(s) not yet in compacted {name}")
    # A note, never a RETRY: the timing is ABCD's file, copied as-is, and a
    # subject reprocessed before the driver kept it (the 2026-10-13 pilot) is
    # not worth a whole reprocess just to gain it.
    no_events = [
        u
        for u in units
        if u[2] in EVENTS_TASKS and not (ev.events.get(u) and ev.events[u] > ev.flushed_at)
    ]
    notes = [f"note: {len(no_events)} n-back run(s) have no _events.tsv"] if no_events else []
    # Also a note: the tissue maps are non-fatal by design (the BOLD is valid
    # without them), and only an exploded session's marker can say they landed.
    no_tissue = {u[:2] for u in units if u in ev.exploded} - ev.tissue
    if no_tissue:
        notes.append(f"note: {len(no_tissue)} session(s) have no tissue maps in their marker")
    return SubjectResult(subject, "PENDING" if pending else "OK", pending + notes)


def gather_derivatives(s3, args, evidence: dict[str, Evidence]) -> None:
    def deriv_of(subj):
        return [
            o
            for t in FUNC_DERIV_PREFIXES
            for o in list_objects(s3, args.data_bucket, t.format(subj=subj))
        ]

    markers = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for objs in pool.map(deriv_of, evidence):
            for o in objs:
                if m := MNI_BOLD_RE.match(o["Key"]):
                    u = m.groups()[:4]
                    evidence[u[0]].bold[u] = o["LastModified"]
                    if m.group(5) == "nii.gz":
                        evidence[u[0]].exploded.add(u)
                elif (m := FUNC_MARKER_RE.match(o["Key"])) and o["LastModified"] > (
                    evidence[m.group(1)].flushed_at
                ):
                    markers.append((m.group(1), m.group(2), o["Key"]))
                elif u := unit_of_events(o["Key"]):
                    evidence[u[0]].events[u] = o["LastModified"]
                elif u := unit_of_dtseries(o["Key"]):
                    evidence[u[0]].dtseries[u] = o["LastModified"]

        docs = list(pool.map(lambda m: get_json(s3, args.data_bucket, m[2]), markers))
    for (subj, ses, _), doc in zip(markers, docs, strict=True):
        record_marker(evidence[subj], subj, ses, doc)


def record_marker(ev: Evidence, subj: str, ses: str, doc: dict | None) -> None:
    """Fold one fresh session marker into the subject's evidence."""
    if not doc or doc.get("kind") != "func-session":
        return
    # A run ID is `{task}_{run}`, and a task label has no `_`.
    for rid in doc.get("runs") or {}:
        task, run = rid.split("_", 1)
        ev.published.add((subj, ses, task, run))
    if doc.get("session_files"):
        ev.tissue.add((subj, ses))


def gather_raw_qc(s3, args, evidence: dict[str, Evidence]) -> None:
    listed = list_keys_parallel(s3, args.metrics_bucket, FUNC_QC_PREFIXES, args.workers)
    for prefix, attr in zip(FUNC_QC_PREFIXES, ("qc_raw", "surf_raw"), strict=True):
        keys = [k for k in listed[prefix] if (u := qc_unit(k)) and u[0] in evidence]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            docs = list(pool.map(lambda k: get_json(s3, args.metrics_bucket, k), keys))
        for k, doc in zip(keys, docs, strict=True):
            u = qc_unit(k)
            getattr(evidence[u[0]], attr)[u].append(doc or {})


def gather_compacted(s3, args, evidence: dict[str, Evidence], earliest: datetime) -> None:
    cols = ("subject", "session", "task", "run", "completed_at")
    for table, attr in (("func_preproc", "qc_compacted"), ("surface_sample", "surf_compacted")):
        for key in compacted_files(s3, args.metrics_bucket, table):
            m = DT_IN_KEY_RE.search(key)
            if m and m.group(1) < earliest.date().isoformat():
                continue
            for r in read_parquet_rows(s3, args.metrics_bucket, key, cols):
                ev = evidence.get(r["subject"])
                done = parse_instant(r["completed_at"])
                if ev and done and done > ev.flushed_at:
                    getattr(ev, attr)[(r["subject"], r["session"], r["task"], r["run"])] += 1


def gather_steps(s3, args, evidence: dict[str, Evidence], earliest: datetime) -> None:
    """Which workflows ran each subject since its flush, and whether any ran anatomy."""
    anatomy_keys = []
    # Through tomorrow: a dt is the writer's UTC date, which can run ahead of ours.
    for dt in dates_since(earliest, now_utc() + timedelta(days=1)):
        for k in list_keys(s3, args.metrics_bucket, f"{STEP_OUTCOME_PREFIX}dt={dt}/"):
            p = parse_step_outcome_key(k)
            if p and p.subject in evidence:
                evidence[p.subject].workflows.add(p.workflow_name)
                if p.step in ANATOMY_STEPS:
                    anatomy_keys.append((p.subject, k))
    for subj, k in anatomy_keys:
        doc = get_json(s3, args.metrics_bucket, k) or {}
        at = parse_instant(doc.get("recorded_at"))
        if at and at > evidence[subj].flushed_at and doc.get("status") not in ANATOMY_NOT_RUN:
            evidence[subj].anatomy.append(f"{doc.get('step')}={doc.get('status')}")


def gather_readback(s3, args, evidence: dict[str, Evidence]) -> None:
    def one(subj):
        ev = evidence[subj]
        for wf in ev.workflows:
            for o in list_objects(s3, args.data_bucket, f"{LOG_PREFIX}{wf}/"):
                # Names are reused: only pods that ran after this subject's flush count.
                key = o["Key"]
                if (
                    FUNC_POD_MARKER in key
                    and key.endswith("/main.log")
                    and o["LastModified"] > ev.flushed_at
                ):
                    text = get_text(s3, args.data_bucket, key)
                    ev.readback_ok += text.count(READBACK_OK)
                    ev.readback_errors += text.count(READBACK_ERROR)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(one, evidence))


def verify(s3, args) -> int:
    subjects = read_subject_csv(args.subjects)
    require_same_account()
    audit_dir = Path(args.audit_dir)
    ledger = load_ledger(audit_dir / LEDGER_NAME)
    never = [s for s in subjects if not (ledger.get(s) or {}).get("flushed_at")]
    if never:
        raise SystemExit(f"{len(never)} subject(s) were never flushed by launch, e.g. {never[0]}")
    running = running_workflows()
    if running is None:
        raise SystemExit("could not list running Argo workflows; verify needs them")
    busy = {workflow_subject(wf) for wf in running}

    evidence = {
        s: Evidence(
            flushed_at=max(parse_instant(t) for t in ledger[s]["flushed_at"]),
            baseline={unit_of_bold(k) for k in ledger[s]["baseline"] or []} - {None},
            running=s in busy,
        )
        for s in subjects
    }
    earliest = min(ev.flushed_at for ev in evidence.values())
    gather_derivatives(s3, args, evidence)
    gather_raw_qc(s3, args, evidence)
    gather_compacted(s3, args, evidence, earliest)
    gather_steps(s3, args, evidence, earliest)
    gather_readback(s3, args, evidence)

    results = [classify_subject(s, evidence[s]) for s in subjects]
    for r in results:
        print(f"{r.status:<8} {r.subject}  {'; '.join(r.reasons)}")
    counts = Counter(r.status for r in results)
    print("\n" + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    retry = [r.subject for r in results if r.status == "RETRY"]
    if retry:
        retry_csv = audit_dir / f"{args.batch_label}_retry.csv"
        retry_csv.parent.mkdir(parents=True, exist_ok=True)
        retry_csv.write_text("subject_id\n" + "".join(f"{s}\n" for s in retry))
        print(f"Retry list: {retry_csv}")
    if any(ev.readback_errors for ev in evidence.values()):
        print(f"\nSTOP: {READBACK_ERROR} seen. Halt all launches.", file=sys.stderr)
    if counts["ANATOMY"]:
        # Plan 017 amendment 20: a few such subjects per wave are expected (a
        # session with a source T1w but no FastSurfer tree re-runs the whole
        # longitudinal phase). They go to the full-reprocess list; only a rate
        # above ANATOMY_STOP_FRACTION points at a systematic cause.
        anatomy = sorted(r.subject for r in results if r.status == "ANATOMY")
        share = len(anatomy) / len(results)
        verdict = (
            "STOP: halt the campaign and find the cause"
            if share > ANATOMY_STOP_FRACTION
            else "add them to func_reprocess_anatomy.csv and keep them out of every retry slice"
        )
        print(
            f"\nANATOMY: {len(anatomy)} subject(s), {share:.1%} of this slice "
            f"(stop above {ANATOMY_STOP_FRACTION:.0%}): {verdict}. {' '.join(anatomy)}",
            file=sys.stderr,
        )
    return 1 if counts["RETRY"] or counts["ANATOMY"] else 0


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    name: str
    uid: str
    subject: str
    started_at: datetime | None
    finished_at: datetime | None
    batch_label: str
    key: str


def load_runs(s3, bucket: str, workers: int) -> list[Run]:
    keys = [
        k for k in list_keys(s3, bucket, WORKFLOW_RUN_PREFIX) if workflow_subject_from_run_key(k)
    ]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        docs = list(pool.map(lambda k: get_json(s3, bucket, k), keys))
    runs = []
    for k, doc in zip(keys, docs, strict=True):
        tag, subj = workflow_subject_from_run_key(k)
        name, uid = split_tag(tag)
        doc = doc or {}
        runs.append(
            Run(
                name=doc.get("workflow_name") or name,
                uid=doc.get("workflow_uid") or uid,
                subject=doc.get("subject") or subj,
                started_at=parse_instant(doc.get("started_at")),
                finished_at=parse_instant(doc.get("finished_at")),
                batch_label=doc.get("batch_label") or "",
                key=k,
            )
        )
    return runs


def find_producers(
    runs: list[Run], bold_times: dict[str, list[datetime]]
) -> tuple[dict[str, set[tuple[str, str]]], set[str]]:
    """Per subject, the workflows whose window contains a stored MNI BOLD's LastModified.

    Returns (producers, unattributed subjects). A subject with a BOLD no window
    covers is unattributed, and callers leave its records alone rather than
    deleting the producer's by mistake.
    """
    by_subject: dict[str, list[Run]] = defaultdict(list)
    for r in runs:
        if r.started_at and r.finished_at:
            by_subject[r.subject].append(r)
    producers: dict[str, set[tuple[str, str]]] = defaultdict(set)
    unattributed = set()
    for subj, times in bold_times.items():
        for t in times:
            hits = [
                r
                for r in by_subject.get(subj, [])
                if r.started_at - PRODUCER_SLACK <= t <= r.finished_at + PRODUCER_SLACK
            ]
            if not hits:
                unattributed.add(subj)
            producers[subj].update((r.name, r.uid) for r in hits)
    return producers, unattributed


def is_producer(producers, subject: str, name: str, uid: str) -> bool:
    """A row with a UID must match name and UID; a pre-UID row can only match by name."""
    mine = producers.get(subject, set())
    if uid:
        return (name, uid) in mine
    return name in {n for n, _ in mine}


def list_func_units(s3, data_bucket: str):
    """(MNI BOLD units, dtseries units, subject -> MNI BOLD LastModifieds)."""
    func_units, surf_units = set(), set()
    bold_times: dict[str, list[datetime]] = defaultdict(list)
    for o in list_objects(s3, data_bucket, "derivatives/func/"):
        if u := unit_of_bold(o["Key"]):
            func_units.add(u)
            bold_times[u[0]].append(o["LastModified"])
    for o in list_objects(s3, data_bucket, "derivatives/func_surf/"):
        if u := unit_of_dtseries(o["Key"]):
            surf_units.add(u)
    return func_units, surf_units, bold_times


@dataclass
class Attribution:
    """Who produced what, cohort-wide, and which reprocess workflows produced nothing."""

    producers: dict
    unattributed: set
    reprocess: dict  # (name, uid) -> Run, every func-reprocess* workflow
    idle: dict  # (name, uid) -> Run, the reprocess workflows that produced nothing

    @classmethod
    def build(cls, runs: list[Run], bold_times) -> Attribution:
        producers, unattributed = find_producers(runs, bold_times)
        reprocess = {
            (r.name, r.uid): r for r in runs if r.batch_label.startswith(BATCH_LABEL_PREFIX)
        }
        # Never idle on an unattributed subject: its producer is unknown, so any of
        # its workflows might be it.
        idle = {
            k: r
            for k, r in reprocess.items()
            if r.subject not in unattributed
            and not is_producer(producers, r.subject, r.name, r.uid)
        }
        return cls(producers, unattributed, reprocess, idle)

    def step_row_goes(self, name: str, uid: str, step: str, subject: str, status) -> bool | None:
        """True/False for a step row; None when the answer needs the row's status."""
        if (name, uid) in self.idle:
            return True
        if step in FUNC_STEPS:
            return subject not in self.unattributed and not is_producer(
                self.producers, subject, name, uid
            )
        if (name, uid) not in self.reprocess:
            return False
        return None if status is None else status == "skipped"


def plan_step_rows(s3, mb: str, workers: int, attr: Attribution) -> list[str]:
    """Raw step-outcome keys to delete under Attribution.step_row_goes."""
    drop, need_status = [], []
    for k in list_keys(s3, mb, STEP_OUTCOME_PREFIX):
        p = parse_step_outcome_key(k)
        if not p:
            continue
        verdict = attr.step_row_goes(p.workflow_name, p.workflow_uid, p.step, p.subject, None)
        if verdict:
            drop.append(k)
        elif verdict is None:
            need_status.append(k)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        docs = list(pool.map(lambda k: get_json(s3, mb, k), need_status))
    drop += [
        k for k, d in zip(need_status, docs, strict=True) if (d or {}).get("status") == "skipped"
    ]
    return drop


def plan_reconcile(s3, args, func_units, surf_units, attr: Attribution) -> dict[str, list[str]]:
    mb = args.metrics_bucket
    plans = {
        f"orphan {prefix}": [
            k for k in list_keys(s3, mb, prefix) if (u := qc_unit(k)) and u not in units
        ]
        for prefix, units in zip(FUNC_QC_PREFIXES, (func_units, surf_units), strict=True)
    }
    plans["step-outcomes"] = plan_step_rows(s3, mb, args.workers, attr)
    idle_tags = {f"{n}__{u}" if u else n for n, u in attr.idle}
    plans["workflow-runs, idle reprocess"] = [r.key for r in attr.idle.values()]
    plans["subject-manifests, idle reprocess"] = [
        k
        for k in list_keys(s3, mb, MANIFEST_PREFIX)
        if (ts := manifest_tag_subject(k)) and ts[0] in idle_tags
    ]
    plans["workflow-starts, idle reprocess"] = [
        k for k in list_keys(s3, mb, WORKFLOW_START_PREFIX) if workflow_start_tag(k) in idle_tags
    ]
    return plans


STEP_IDENTITY = ("workflow_name", "step", "subject", "session", "task", "run", "recorded_at")


def compacted_jobs(s3, mb: str, func_units, surf_units, attr: Attribution) -> list[tuple]:
    """(table, columns, values_for, complement) per compacted table reconcile filters.

    Per-scan QC keeps only units with a derivative (complement). step_outcomes is
    read file by file to select exactly the rows the raw rule selects; its identity
    leaves out workflow_uid, which older files do not carry.
    """

    def step_rows_to_drop(key: str) -> set[tuple]:
        drop = set()
        for r in read_parquet_rows(s3, mb, key, (*STEP_IDENTITY, "workflow_uid", "status")):
            goes = attr.step_row_goes(
                r["workflow_name"], r["workflow_uid"] or "", r["step"], r["subject"], r["status"]
            )
            if goes and all(r[c] is not None for c in STEP_IDENTITY):
                drop.add(tuple(str(r[c]) for c in STEP_IDENTITY))
        return drop

    unit = ("subject", "session", "task", "run")
    run_id = ("workflow_name", "workflow_uid", "subject")
    idle_ids = {(n, u, r.subject) for (n, u), r in attr.idle.items()}
    return [
        ("func_preproc", unit, lambda _k: func_units, True),
        ("surface_sample", unit, lambda _k: surf_units, True),
        ("step_outcomes", STEP_IDENTITY, step_rows_to_drop, False),
        ("workflow_runs", run_id, lambda _k: idle_ids, False),
        ("subject_manifests", run_id, lambda _k: idle_ids, False),
    ]


# Reprocess workflows re-attempt QC-rejected BOLD runs and held t1w->MNI sessions,
# and rerun fsqc unconditionally (it has no exists-gate); each lands under a new dt.
PURGES = (
    ("--table", "registration", "--registration-type", "bold_to_t1w"),
    ("--table", "registration", "--registration-type", "t1w_to_mni"),
    ("--table", "fsqc_qc"),
)


def run_purges(args) -> list[str]:
    failed = []
    for extra in PURGES:
        cmd = [
            sys.executable,
            str(REPO / "scripts" / "purge_superseded_metrics.py"),
            "--metrics-bucket",
            args.metrics_bucket,
            "--data-bucket",
            args.data_bucket,
            *extra,
            *(["--write"] if args.write else []),
        ]
        print(f"\n$ {' '.join(cmd)}")
        if subprocess.run(cmd, check=False).returncode != 0:
            failed.append(" ".join(extra))
    return failed


def reconcile(s3, args) -> int:
    audit = Audit(Path(args.audit_dir) / f"reconcile_{now_utc():%Y%m%dT%H%M%SZ}.jsonl")
    tag = "write" if args.write else "dry-run"

    print("Listing derivatives and workflow runs ...")
    func_units, surf_units, bold_times = list_func_units(s3, args.data_bucket)
    runs = load_runs(s3, args.metrics_bucket, args.workers)
    attr = Attribution.build(runs, bold_times)
    print(
        f"  {len(runs)} runs; {len(attr.reprocess)} from {BATCH_LABEL_PREFIX}*, "
        f"{len(attr.idle)} of those produced nothing"
    )
    if attr.unattributed:
        print(
            f"  [hold] {len(attr.unattributed)} subject(s) have an MNI BOLD no workflow window "
            "covers; their func step rows and workflows are left alone"
        )
        audit.write("unattributed", subjects=sorted(attr.unattributed))

    plans = plan_reconcile(s3, args, func_units, surf_units, attr)
    for name, keys in plans.items():
        print(f"  [{tag}] raw {name}: {len(keys)}")
        if not args.write:
            audit.write("planned", category=name, keys=keys)
    # Raw first, compacted after — the nightly compactor rebuilds from raw.
    if args.write:
        try:
            for name, keys in plans.items():
                delete_audited(s3, args.metrics_bucket, keys, audit, name)
        except DeleteFailed as exc:
            print(f"ERROR: {exc}\nRe-run reconcile; it is idempotent.", file=sys.stderr)
            return 1
    failed: list[str] = []
    for table, cols, values_for, complement in compacted_jobs(
        s3, args.metrics_bucket, func_units, surf_units, attr
    ):
        n, f = drop_compacted(
            s3, args.metrics_bucket, table, cols, values_for, args.write, args.workers, complement
        )
        failed += f
        print(f"  [{tag}] compacted {table}: {n} rows")
        audit.write("compacted", table=table, rows=n, failed=f, write=args.write)
    failed += run_purges(args)

    print(f"\nAudit: {audit.path}")
    if failed:
        print(f"\n{len(failed)} step(s) failed (listed above); re-run reconcile.", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, subjects: bool):
        # Buckets are required, never defaulted — the same stance as prep_test_batch.py.
        sp.add_argument("--data-bucket", required=True)
        sp.add_argument("--metrics-bucket", required=True)
        if subjects:
            sp.add_argument("--subjects", required=True, help="CSV with a `subject_id` header.")
        sp.add_argument("--region")
        sp.add_argument("--workers", type=int, default=32)
        sp.add_argument("--audit-dir", default=str(DEFAULT_AUDIT_DIR))

    sp = sub.add_parser("launch", help="flush one slice's func outputs and submit it")
    common(sp, subjects=True)
    sp.add_argument("--batch-label", required=True)
    # Required, never defaulted: https is the fast path, globus the rollback, and a
    # launch should say which it is rather than inherit the queue manager's default.
    sp.add_argument("--ingress-mode", required=True, choices=("https", "globus"))
    sp.add_argument("--write", action="store_true", help="Delete and submit (default: dry run).")
    sp.add_argument("--yes", action="store_true", help="Skip the typed confirmation.")
    sp.add_argument(
        "--skip-flush",
        action="store_true",
        help="Only to recover a submission that failed after a completed flush.",
    )

    sp = sub.add_parser("verify", help="check a terminal slice (read-only)")
    common(sp, subjects=True)
    sp.add_argument("--batch-label", required=True)

    sp = sub.add_parser("reconcile", help="delete func metrics that describe nothing stored")
    common(sp, subjects=False)
    sp.add_argument("--all", action="store_true", required=True, help="Cohort-wide; required.")
    sp.add_argument("--write", action="store_true", help="Apply (default: dry run).")
    return p


def main(argv: list[str] | None = None, s3=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.data_bucket.strip() or not args.metrics_bucket.strip():
        # "$CLOUDPIPE_BUCKET" outside `pixi shell -e ops` expands to nothing.
        raise SystemExit(
            "--data-bucket and --metrics-bucket must not be empty (run in pixi shell -e ops)"
        )
    s3 = s3 or boto3.client("s3", region_name=args.region)
    return {"launch": launch, "verify": verify, "reconcile": reconcile}[args.command](s3, args)


if __name__ == "__main__":
    sys.exit(main())
