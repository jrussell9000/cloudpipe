"""
reprocess_t1w_mni.py — SESSION-scoped re-registration (T1w->MNI) and functional
reprocess for the sessions whose affine the talairach cross-check gate rejects (#1105).

A new T1w->MNI transform invalidates everything resampled through it: the session's MNI
BOLD, MNI tissue maps and grayordinates, and the first levels fitted to them. So this
flushes, per listed SESSION:

  derivatives/registration/{subj}/{ses}/t1w_to_mni/   inventory re-registers the session
  derivatives/func/{subj}/{ses}/                      func-preproc reruns it
  derivatives/func_surf/{subj}/{ses}/                 surface-resample reruns it
  derivatives/first_levels/{subj}/{ses}/              stale; the first-level pass (run
                                                      with missing_only) regenerates it

plus that session's raw and compacted t1w_to_mni registration, func-preproc,
surface-sample and tissue-masks records and its t1w-to-mni / func step outcomes. Every
other session of the subject is untouched: the pipeline's exists-gates are per session,
so the resubmitted subject re-registers and reprocesses only what was flushed. That is
the difference from reprocess_func.py, which flushes whole subjects; everything this
needs that is not session-specific is that script's own, imported (the audited delete,
the compacted-row filter, preflight, the launch gate, Prefect, verify's evidence).

Two subcommands, both refusing anything not in the session CSV:

  launch   preflight, flush one slice's sessions, then submit their subjects to the
           queue manager. ONE command, as in reprocess_func.py: a flush whose
           submission never happened strands sessions with nothing.
  verify   read-only; per session, a fresh schema-2.8 T1w->MNI registration that passed
           with the talairach check on, and reprocess_func's own per-run evidence.

reprocess_func.py `reconcile` stays the cohort-wide cleanup: this tool's labels start
with its prefix, so it attributes these workflows as reprocess runs.

Usage (CSV header: subject_id,session_id):
  pixi run -e ops python scripts/reprocess_t1w_mni.py launch --sessions <csv> \\
      --data-bucket "$CLOUDPIPE_BUCKET" --metrics-bucket "$CLOUDPIPE_METRICS_BUCKET" \\
      --batch-label func-reprocess-t1w-NN [--write]
  pixi run -e ops python scripts/reprocess_t1w_mni.py verify --sessions <csv> \\
      --data-bucket ... --metrics-bucket ... --batch-label func-reprocess-t1w-NN
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import reprocess_func as rf  # noqa: E402

LABEL_PREFIX = "func-reprocess-t1w"
assert LABEL_PREFIX.startswith(rf.BATCH_LABEL_PREFIX)  # reconcile attributes by this prefix
LEDGER_NAME = "t1w_mni_reprocess_ledger.jsonl"
T1W_STEP = "t1w-to-mni"
STEPS = (T1W_STEP, *rf.FUNC_STEPS)
SESSION_DERIV_PREFIXES = (
    "derivatives/registration/{subj}/{ses}/t1w_to_mni/",
    "derivatives/func/{subj}/{ses}/",
    "derivatives/func_surf/{subj}/{ses}/",
    "derivatives/first_levels/{subj}/{ses}/",
)
REG_PREFIX = "metrics/registration/"
REG_SUFFIX = "_t1w_to_mni_reg_qc.json"
REG_TYPE = "t1w_to_mni"
# Compacted tables keyed (subject, session[, ...]); the per-scan ones are dropped whole
# for the session, every task and run.
SESSION_TABLES = ("func_preproc", "surface_sample", "tissue_masks")
SESSION_RE = re.compile(r"^ses-[A-Za-z0-9]+$")
# The deployed WorkflowTemplate must carry the gate, or re-registering is pointless:
# NONE sampling is deterministic and would reproduce exactly the displaced affine.
REG_TEMPLATE = "registration"
GATE_FLAG = "--talairach"
REG_SCHEMA = "2.8"
TAL_KEY = "derivatives/fastsurfer/{subj}/{ses}/mri/transforms/talairach.xfm.lta"

Unit = tuple[str, str]  # (subject, session)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def read_session_csv(path: str) -> list[Unit]:
    """(subject, session) pairs from a CSV headed `subject_id,session_id`; refuses bad rows."""
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        if (reader.fieldnames or [])[:2] != ["subject_id", "session_id"]:
            raise SystemExit(f"{path}: the header must start `subject_id,session_id`")
        units = [
            (r["subject_id"].strip(), r["session_id"].strip())
            for r in reader
            if r["subject_id"].strip()
        ]
    bad = [u for u in units if not (rf.SUBJECT_RE.match(u[0]) and SESSION_RE.match(u[1]))]
    if bad:
        raise SystemExit(f"{path}: {len(bad)} malformed row(s), e.g. {bad[0]!r}")
    if len(set(units)) != len(units):
        raise SystemExit(f"{path}: duplicate sessions")
    if not units:
        raise SystemExit(f"{path}: no sessions")
    return units


def subjects_of(units: list[Unit]) -> list[str]:
    return sorted({s for s, _ in units})


def metric_unit(key: str) -> Unit | None:
    """`.../{subj}_{ses}_...` -> (subj, ses). IDs contain no `_`, so this is exact."""
    parts = key.rsplit("/", 1)[-1].split("_", 2)
    return (parts[0], parts[1]) if len(parts) >= 2 else None


# ---------------------------------------------------------------------------
# External systems (module-level so tests can replace them)
# ---------------------------------------------------------------------------


def live_template_has_gate() -> bool | None:
    """True when the deployed registration WorkflowTemplate passes --talairach.

    None when it cannot be read. ArgoCD owns the template, so this is the version
    workflows submitted now will actually get.
    """
    try:
        out = subprocess.run(
            [
                "kubectl",
                "-n",
                rf.ARGO_NAMESPACE,
                "get",
                "workflowtemplate",
                REG_TEMPLATE,
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    return GATE_FLAG in out


# ---------------------------------------------------------------------------
# Ledger: per session, what it held before the campaign, and when it was flushed
# ---------------------------------------------------------------------------


def load_ledger(path: Path) -> dict[Unit, dict]:
    """{(subj, ses): {"baseline": [MNI BOLD keys] | None, "flushed_at": [iso, ...]}}."""
    ledger: dict[Unit, dict] = defaultdict(lambda: {"baseline": None, "flushed_at": []})
    for ev in rf.Audit(path).events():
        entry = ledger[(ev["subject"], ev["session"])]
        if ev["event"] == "baseline" and entry["baseline"] is None:
            entry["baseline"] = ev["bold"]
        elif ev["event"] == "flushed":
            entry["flushed_at"].append(ev["at"])
    return dict(ledger)


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------


@dataclass
class SessionPlan:
    deriv: dict[Unit, list[dict]]
    qc_raw: list[str]
    reg_raw: list[str]
    steps_raw: list[str]

    @property
    def deriv_keys(self) -> list[str]:
        return [o["Key"] for objs in self.deriv.values() for o in objs]

    def bold_keys(self, unit: Unit) -> list[str]:
        return [o["Key"] for o in self.deriv[unit] if rf.MNI_BOLD_RE.match(o["Key"])]

    def has_affine(self, unit: Unit) -> bool:
        return any(o["Key"].endswith("_desc-t1w2mni_affine.mat") for o in self.deriv[unit])


def plan_flush(s3, data_bucket: str, metrics_bucket: str, units: list[Unit], workers: int):
    wanted = set(units)

    def deriv_of(unit: Unit):
        subj, ses = unit
        return unit, [
            o
            for t in SESSION_DERIV_PREFIXES
            for o in rf.list_objects(s3, data_bucket, t.format(subj=subj, ses=ses))
        ]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        deriv = dict(rf.progress(pool.map(deriv_of, units), "derivatives", len(units), "ses"))

    listed = rf.list_keys_parallel(
        s3,
        metrics_bucket,
        [*rf.FLUSHED_QC_PREFIXES, REG_PREFIX, rf.STEP_OUTCOME_PREFIX],
        workers,
        "metrics listing",
    )
    qc_raw = [k for p in rf.FLUSHED_QC_PREFIXES for k in listed[p] if metric_unit(k) in wanted]
    # Only the T1w->MNI record: bold_to_t1w is independent of MNI and is kept.
    reg_raw = [k for k in listed[REG_PREFIX] if k.endswith(REG_SUFFIX) and metric_unit(k) in wanted]
    steps_raw = []
    for k in listed[rf.STEP_OUTCOME_PREFIX]:
        p = rf.parse_step_outcome_key(k)
        if p and p.step in STEPS and (p.subject, p.session) in wanted:
            steps_raw.append(k)
    return SessionPlan(deriv=deriv, qc_raw=qc_raw, reg_raw=reg_raw, steps_raw=steps_raw)


def preflight(s3, args, units: list[Unit]) -> list[str]:
    """Reasons to refuse; empty when the launch may proceed."""
    problems = rf.preflight(s3, args, subjects_of(units))
    gate = live_template_has_gate()
    if gate is None:
        problems.append(f"could not read the deployed {REG_TEMPLATE} WorkflowTemplate")
    elif not gate:
        problems.append(
            f"the deployed {REG_TEMPLATE} WorkflowTemplate does not pass {GATE_FLAG}: "
            "re-registering now would reproduce the same affine (#1107 not live)"
        )

    def no_tal(unit: Unit):
        key = TAL_KEY.format(subj=unit[0], ses=unit[1])
        return None if rf.object_exists(s3, args.data_bucket, key) else unit

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        missing = sorted(u for u in pool.map(no_tal, units) if u)
    if missing:
        problems.append(
            f"{len(missing)} session(s) have no talairach.xfm.lta, so the gate cannot run "
            f"on them, e.g. {missing[0]}"
        )
    return problems


def no_registration_problems(units: list[Unit], plan: SessionPlan, ledger) -> list[str]:
    """Sessions with no T1w->MNI registration to replace (a resumed flush is excused)."""
    none = sorted(
        u for u in units if not plan.has_affine(u) and not ledger.get(u, {}).get("flushed_at")
    )
    return (
        [f"{len(none)} session(s) have no T1w->MNI affine to replace, e.g. {none[0]}"]
        if none
        else []
    )


def flush_compacted(s3, args, units: list[Unit], write: bool) -> tuple[int, list[str]]:
    """The listed sessions' rows out of every compacted table this flush touched."""
    pairs = set(units)
    jobs = [(t, ("subject", "session"), pairs) for t in SESSION_TABLES]
    jobs.append(
        (
            "registration",
            ("subject", "session", "registration_type"),
            {(s, ses, REG_TYPE) for s, ses in units},
        )
    )
    jobs.append(
        (
            "step_outcomes",
            ("subject", "session", "step"),
            {(s, ses, st) for s, ses in units for st in STEPS},
        )
    )
    rows, failed = 0, []
    tag = "write" if write else "dry-run"
    for table, cols, values in jobs:
        n, f = rf.drop_compacted(
            s3, args.metrics_bucket, table, cols, lambda _k, v=values: v, write, args.workers
        )
        print(f"  [{tag}] compacted {table}: {n} rows")
        rows += n
        failed += f
    return rows, failed


def flush(s3, args, units, plan: SessionPlan, ledger, ledger_audit, audit) -> bool:
    """Delete the plan, raw before compacted. True when every delete landed."""
    for u in units:  # baseline BEFORE any delete, once per session
        if ledger.get(u, {}).get("baseline") is None:
            ledger_audit.write(
                "baseline",
                subject=u[0],
                session=u[1],
                bold=plan.bold_keys(u),
                batch_label=args.batch_label,
            )
    try:
        d = rf.delete_audited(s3, args.data_bucket, plan.deriv_keys, audit, "derivatives")
        q = rf.delete_audited(s3, args.metrics_bucket, plan.qc_raw, audit, "qc_raw")
        r = rf.delete_audited(s3, args.metrics_bucket, plan.reg_raw, audit, "registration_raw")
        st = rf.delete_audited(s3, args.metrics_bucket, plan.steps_raw, audit, "steps_raw")
    except rf.DeleteFailed as exc:
        print(f"ERROR: {exc}\nRe-run the same command to finish the flush.", file=sys.stderr)
        return False
    rows, failed = flush_compacted(s3, args, units, write=True)
    audit.write("compacted", rows=rows, failed=failed)
    if failed:
        print(
            f"ERROR: {len(failed)} compacted file(s) could not be filtered. Re-run the same "
            "command to finish the flush; nothing was submitted.",
            file=sys.stderr,
        )
        return False
    for u in units:
        ledger_audit.write("flushed", subject=u[0], session=u[1], batch_label=args.batch_label)
    print(f"Flushed {d} derivative + {q + r + st} raw metric objects, {rows} compacted rows.")
    return True


def submit(s3, args, units: list[Unit], sha: str, audit) -> int:
    """Upload the slice's SUBJECT list and start the queue manager on it."""
    subjects = subjects_of(units)
    subj_csv = Path(args.audit_dir) / f"{args.batch_label}_subjects.csv"
    subj_csv.write_text("subject_id\n" + "".join(f"{s}\n" for s in subjects))
    key = f"config/func-reprocess/{args.batch_label}.csv"
    uri = f"s3://{args.data_bucket}/{key}"
    parameters = {
        "subjects_file": uri,
        "batch_label": args.batch_label,
        "globus_scan_types": rf.SCAN_TYPES,
        "ingress_mode": args.ingress_mode,
    }
    try:
        rf.check_prefect_target()
        s3.upload_file(str(subj_csv), args.data_bucket, key)
        flow_run = rf.create_flow_run(parameters, idempotency_key=f"{args.batch_label}:{sha}")
    except BaseException as exc:  # noqa: BLE001 — any failure here strands a flushed slice
        recover = (
            f"pixi run -e ops python scripts/reprocess_t1w_mni.py launch --sessions {args.sessions} "
            f'--data-bucket "{args.data_bucket}" --metrics-bucket "{args.metrics_bucket}" '
            f"--batch-label {args.batch_label} --ingress-mode {args.ingress_mode} --skip-flush --write"
        )
        audit.write("submit_failed", error=f"{type(exc).__name__}: {exc}")
        print(
            f"\nERROR: submission failed AFTER the flush ({type(exc).__name__}: {exc}).\n"
            f"These {len(units)} sessions now have no registration or func output. Submit with:\n"
            f"  {recover}",
            file=sys.stderr,
        )
        return 1
    audit.write("submitted", flow_run_id=str(flow_run.id), flow_run_name=flow_run.name, uri=uri)
    print(
        f"Submitted flow run {flow_run.name} ({flow_run.id}) for {uri} ({len(subjects)} subjects)"
    )
    return 0


def launch(s3, args) -> int:
    units = read_session_csv(args.sessions)
    if not args.batch_label.startswith(LABEL_PREFIX):
        raise SystemExit(f"--batch-label must start with {LABEL_PREFIX!r}")
    audit_dir = Path(args.audit_dir)
    audit = rf.Audit(audit_dir / f"{args.batch_label}_launch.jsonl")
    sha = rf.file_sha256(args.sessions)
    rf.require_same_account()

    gate = rf.launch_gate(audit.events(), sha, args.skip_flush)
    if gate == "submit":
        if not args.write:
            print("Dry run: would submit without flushing (pass --write).")
            return 0
        return submit(s3, args, units, sha, audit)
    if gate != "flush":
        print(f"ERROR: {args.batch_label}: {gate} ({audit.path}).", file=sys.stderr)
        return 2

    subjects = subjects_of(units)
    print(
        f"=== T1w->MNI reprocess launch: {args.batch_label} ({len(units)} sessions, {len(subjects)} subjects) ==="
    )
    if args.write:
        rf.check_prefect_target()
    ledger_path = audit_dir / LEDGER_NAME
    ledger = load_ledger(ledger_path)
    problems = preflight(s3, args, units)
    plan = plan_flush(s3, args.data_bucket, args.metrics_bucket, units, args.workers)
    problems += no_registration_problems(units, plan, ledger)
    if problems:
        for p in problems:
            print(f"REFUSED: {p}", file=sys.stderr)
        return 2

    tag = "write" if args.write else "dry-run"
    n_bold = sum(len(plan.bold_keys(u)) for u in units)
    by_tree = Counter(k.split("/", 2)[1] for k in plan.deriv_keys)
    print(
        f"  [{tag}] derivatives: {len(plan.deriv_keys)} objects {dict(by_tree)} ({n_bold} MNI BOLD runs)"
    )
    print(
        f"  [{tag}] raw per-scan/tissue QC: {len(plan.qc_raw)}; raw t1w_to_mni: {len(plan.reg_raw)}"
    )
    print(f"  [{tag}] raw step-outcomes {'/'.join(STEPS)}: {len(plan.steps_raw)}")
    if not args.write:
        rows, _ = flush_compacted(s3, args, units, write=False)
        audit.write(
            "dry_run",
            csv_sha256=sha,
            sessions=len(units),
            subjects=len(subjects),
            deriv=len(plan.deriv_keys),
            bold=n_bold,
            qc_raw=len(plan.qc_raw),
            reg_raw=len(plan.reg_raw),
            steps_raw=len(plan.steps_raw),
            compacted_rows=rows,
        )
        print(f"\nDry run. Audit: {audit.path}\nRe-run with --write to flush and submit.")
        return 0

    if not rf.confirmed(args, len(plan.deriv_keys), len(subjects)):
        print("Aborted.")
        return 1
    gate = rf.launch_gate(audit.events(), sha, args.skip_flush)  # the prompt may have sat for hours
    if gate != "flush":
        print(
            f"ERROR: {args.batch_label}: {gate} while awaiting confirmation; nothing deleted.",
            file=sys.stderr,
        )
        return 2
    if not flush(s3, args, units, plan, ledger, rf.Audit(ledger_path), audit):
        return 1
    audit.write("flush_complete", csv_sha256=sha, sessions=len(units))
    return submit(s3, args, units, sha, audit)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def registration_problems(
    s3, args, units: list[Unit], flushed: dict[Unit, object], reg_docs
) -> dict:
    """Per session: (status, reasons) for the T1w->MNI half. status RETRY | REGFAIL | ''."""
    out = {}

    def affine_lm(unit: Unit):
        subj, ses = unit
        objs = rf.list_objects(
            s3, args.data_bucket, f"derivatives/registration/{subj}/{ses}/t1w_to_mni/"
        )
        lm = [o["LastModified"] for o in objs if o["Key"].endswith("_desc-t1w2mni_affine.mat")]
        return unit, (lm[0] if lm else None)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        affines = dict(pool.map(affine_lm, units))
    for u in units:
        fresh = [
            d
            for d in reg_docs.get(u, [])
            if (t := rf.parse_instant(d.get("completed_at"))) and t > flushed[u]
        ]
        reasons = []
        if len(fresh) != 1:
            reasons.append(f"{len(fresh)} fresh raw t1w_to_mni record(s), expected 1")
        elif fresh[0].get("verdict") == "fail":
            out[u] = ("REGFAIL", ["every attempt failed the gate (rerunning reproduces it)"])
            continue
        else:
            doc = fresh[0]
            if doc.get("schema_version") != REG_SCHEMA:
                reasons.append(
                    f"t1w_to_mni schema {doc.get('schema_version')!r}, expected {REG_SCHEMA}"
                )
            if "affine_tal_dev_mm" not in doc:
                reasons.append("t1w_to_mni record has no affine_tal_dev_mm: the gate did not run")
        if not (affines[u] and affines[u] > flushed[u]):
            reasons.append("no new T1w->MNI affine")
        out[u] = ("RETRY", reasons) if reasons else ("", [])
    return out


def combine(
    func: rf.SubjectResult, reg: tuple[str, list[str]], running: bool
) -> tuple[str, list[str]]:
    """One session's verdict from its subject's func verdict and its own registration's.

    ANATOMY first (the template moved under every kept registration), then a running
    workflow (nothing is final yet); then a registration problem, which explains any
    func problem after it (no transform, no func), so its reasons come first.
    """
    rstatus, rreasons = reg
    if func.status == "ANATOMY":
        return func.status, func.reasons
    if running:
        return "PENDING", ["workflow still running"]
    if rstatus:
        return rstatus, rreasons + ([] if func.status == "OK" else func.reasons)
    return func.status, func.reasons


def gather_registration(s3, args, units: list[Unit]) -> dict[Unit, list[dict]]:
    wanted = set(units)
    listed = rf.list_keys_parallel(
        s3, args.metrics_bucket, [REG_PREFIX], args.workers, "registration listing"
    )
    keys = [k for k in listed[REG_PREFIX] if k.endswith(REG_SUFFIX) and metric_unit(k) in wanted]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        docs = list(pool.map(lambda k: rf.get_json(s3, args.metrics_bucket, k) or {}, keys))
    out: dict[Unit, list[dict]] = defaultdict(list)
    for k, d in zip(keys, docs, strict=True):
        out[metric_unit(k)].append(d)
    return out


def verify(s3, args) -> int:
    units = read_session_csv(args.sessions)
    rf.require_same_account()
    audit_dir = Path(args.audit_dir)
    ledger = load_ledger(audit_dir / LEDGER_NAME)
    never = [u for u in units if not ledger.get(u, {}).get("flushed_at")]
    if never:
        raise SystemExit(f"{len(never)} session(s) were never flushed by launch, e.g. {never[0]}")
    running = rf.running_workflows()
    if running is None:
        raise SystemExit("could not list running Argo workflows; verify needs them")
    busy = {rf.workflow_subject(wf) for wf in running}

    flushed = {u: max(rf.parse_instant(t) for t in ledger[u]["flushed_at"]) for u in units}
    by_subject: dict[str, list[Unit]] = defaultdict(list)
    for u in units:
        by_subject[u[0]].append(u)
    # reprocess_func's evidence is per RUN; a baseline holding only the listed sessions'
    # runs makes its verdict exactly theirs (runs of other sessions are never fresh).
    evidence = {
        s: rf.Evidence(
            flushed_at=max(flushed[u] for u in us),
            baseline={rf.unit_of_bold(k) for u in us for k in ledger[u]["baseline"] or []} - {None},
            running=s in busy,
        )
        for s, us in by_subject.items()
    }
    earliest = min(flushed.values())
    rf.gather_derivatives(s3, args, evidence)
    rf.gather_raw_qc(s3, args, evidence)
    rf.gather_compacted(s3, args, evidence, earliest)
    rf.gather_steps(s3, args, evidence, earliest)
    rf.gather_readback(s3, args, evidence)
    func = {s: rf.classify_subject(s, ev) for s, ev in evidence.items()}
    reg = registration_problems(s3, args, units, flushed, gather_registration(s3, args, units))

    results = [(u, *combine(func[u[0]], reg[u], evidence[u[0]].running)) for u in units]
    for (subj, ses), status, reasons in results:
        print(f"{status:<8} {subj} {ses}  {'; '.join(reasons)}")
    counts = Counter(s for _, s, _ in results)
    print("\n" + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    # Rewritten on EVERY verify, empty or not: a list left by an earlier verify would
    # otherwise be read as this one's.
    audit_dir.mkdir(parents=True, exist_ok=True)
    for status, name in (("RETRY", "retry"), ("REGFAIL", "regfail"), ("OK", "first_level_ready")):
        rows = [u for u, s, _ in results if s == status]
        p = audit_dir / f"{args.batch_label}_{name}.csv"
        p.write_text("subject_id,session_id\n" + "".join(f"{a},{b}\n" for a, b in rows))
        print(f"{status} ({len(rows)}): {p}")
    if any(ev.readback_errors for ev in evidence.values()):
        print(f"\nSTOP: {rf.READBACK_ERROR} seen. Halt all launches.", file=sys.stderr)
    return 1 if counts["RETRY"] or counts["ANATOMY"] or counts["REGFAIL"] else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("launch", "verify"):
        sp = sub.add_parser(name)
        sp.add_argument("--sessions", required=True, help="CSV headed `subject_id,session_id`.")
        sp.add_argument("--data-bucket", required=True)
        sp.add_argument("--metrics-bucket", required=True)
        sp.add_argument("--batch-label", required=True)
        sp.add_argument("--region")
        sp.add_argument("--workers", type=int, default=32)
        sp.add_argument("--audit-dir", default=str(rf.DEFAULT_AUDIT_DIR))
    lp = sub.choices["launch"]
    lp.add_argument("--ingress-mode", default="https", choices=("https", "presynced"))
    lp.add_argument("--write", action="store_true", help="Delete and submit (default: dry run).")
    lp.add_argument("--yes", action="store_true", help="Skip the typed confirmation.")
    lp.add_argument(
        "--skip-flush", action="store_true", help="Submit a slice already flushed under this label."
    )
    return p


def main(argv: list[str] | None = None, s3=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.data_bucket or not args.metrics_bucket:
        raise SystemExit(
            "--data-bucket and --metrics-bucket must not be empty (run in pixi shell -e ops)"
        )
    if s3 is None:
        import boto3

        s3 = boto3.client("s3", region_name=args.region)
    return {"launch": launch, "verify": verify}[args.cmd](s3, args)


if __name__ == "__main__":
    sys.exit(main())
