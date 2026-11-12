"""One fMRIPrep reference-arm session for the equivalence benchmark.

openspec/changes/add-fmriprep-equivalence-benchmark, tasks 4.3-4.10. The
argo/workflows/fmriprep_bench template runs this once per session: fMRIPrep as a
user runs it, with its own cross-sectional FreeSurfer 7.3.2 recon-all (design
D19). The earlier `shared-anatomy` mode, which handed fMRIPrep cloudpipe's
FastSurfer tree, was retired on 2026-10-28: fMRIPrep's report can never confirm
such a handover, because FastSurfer does not write every recon-all stage's
outputs.

Everything a retried pod must find again lives under --work, a volume that
outlives the pod (D8): the BIDS input, fMRIPrep's working directory and its
output (with its FreeSurfer reconstruction under sourcedata/freesurfer). The input is copied there once and never rewritten,
because nipype keys its cache on file timestamps (fMRIPrep leaves `hash_method`
at nipype's default), so re-copying a downloaded input would invalidate every
node and a restart would recompute the session instead of resuming it.

Standard library only, apart from nipype (already in the image) for the per-node
timings, read after fMRIPrep exits.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/cloudpipe")

FMRIPREP_VERSION = "25.2.5"
MODES = ("default",)
# What cloudpipe processes, and so all fMRIPrep is given (spec: "the same objects
# cloudpipe's own run consumes"). T2w is staged by the transfer but left out: no
# cloudpipe stage reads it, and fMRIPrep's recon-all would use it for pial
# refinement in default mode.
TASKS = ("rest", "nback")
STAGED_MARKER = ".staged"
# out/ is renamed to this once the run passes its gates; the template uploads it.
PUBLISH_DIR = "publish"
COMPLETE_MARKER = "_bench_complete.json"
#: fMRIPrep exited 0 but its report cannot be read: the run did not finish.
EXIT_REPORT_INVALID = 1


class BenchError(RuntimeError):
    """The session cannot be run as configured; the message says why."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def nss_frames(nss_csv: Path, subject: str, session: str) -> int:
    """The session's protocol count of non-steady-state frames.

    From config/nss_volumes.csv, the same table cloudpipe's inventory reads, so
    both arms drop the same frames rather than fMRIPrep detecting its own (spec).
    Keyed as src/inventory.py's load_nss_volumes keys it: `subject_id`, `session`.
    """
    bare = subject.removeprefix("sub-")
    with open(nss_csv, newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("subject_id") or "").removeprefix("sub-") == bare and row.get(
                "session"
            ) == session:
                value = (row.get("nss_frames") or "").strip()
                if not value.isdigit() or int(value) < 1:
                    raise BenchError(
                        f"nss_volumes.csv: {subject}/{session} has nss_frames={value!r}"
                    )
                return int(value)
    raise BenchError(f"nss_volumes.csv has no row for {subject}/{session}")


def bids_inputs(staged: Path, subject: str, session: str) -> list[Path]:
    """The files of one staged session fMRIPrep is given, relative to `staged`.

    `staged` is mmps_mproc/{subject}/{session}/ as downloaded: anat/ and func/.
    The session's run-01 T1w (the one FastSurfer consumed), and every rest and
    n-back BOLD with its sidecar. Motion and events files are not BIDS raw data
    and fMRIPrep reads neither.
    """
    prefix = f"{subject}_{session}"
    t1w = staged / "anat" / f"{prefix}_run-01_T1w.nii.gz"
    if not t1w.is_file():
        raise BenchError(f"no {t1w.relative_to(staged)} in the staged session")
    files = [t1w, t1w.with_name(t1w.name.replace(".nii.gz", ".json"))]

    bold = sorted(
        p
        for p in (staged / "func").glob(f"{prefix}_task-*_run-*_bold.nii.gz")
        if re.search(r"_task-(" + "|".join(TASKS) + r")_", p.name)
    )
    if not bold:
        raise BenchError(f"no rest or n-back BOLD under {staged / 'func'}")
    for image in bold:
        sidecar = image.with_name(image.name.replace(".nii.gz", ".json"))
        if not sidecar.is_file():
            raise BenchError(f"{image.name} has no JSON sidecar")
        files += [image, sidecar]
    return [f.relative_to(staged) for f in files if f.is_file()]


def stage_bids(staged: Path, bids: Path, subject: str, session: str) -> None:
    """Assemble a one-session BIDS dataset under `bids`, once.

    One session, so fMRIPrep processes exactly the session whose FastSurfer
    tree is staged (see fs_subject_id for the name it looks that tree up by).
    """
    if (bids / STAGED_MARKER).exists():
        print(f"BIDS input already staged at {bids}; leaving it untouched", flush=True)
        return
    if bids.exists():
        shutil.rmtree(bids)  # a copy cut short by the previous pod
    session_dir = bids / subject / session
    for rel in bids_inputs(staged, subject, session):
        dest = session_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged / rel, dest)
    (bids / "dataset_description.json").write_text(
        json.dumps(
            {
                "Name": "cloudpipe fMRIPrep equivalence benchmark",
                "BIDSVersion": "1.9.0",
                "DatasetType": "raw",
            },
            indent=2,
        )
    )
    (bids / STAGED_MARKER).write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def fmriprep_command(
    *,
    mode: str,
    bids: Path,
    out: Path,
    work: Path,
    subject: str,
    license_file: Path,
    nss: int,
    random_seed: int,
    nprocs: int,
    mem_mb: int,
) -> list[str]:
    """The configured command line (spec, "Reference run configured for the comparison").

    fMRIPrep as a user runs it, with its own anatomy (design D19): no
    `--fs-subjects-dir`. Goodvoxels projection and MSM are left at fMRIPrep's
    defaults (off and on), and so is `--level full`.
    """
    if mode not in MODES:
        raise BenchError(f"mode {mode!r} is not one of {MODES}")
    cmd = [
        "fmriprep",
        str(bids),
        str(out),
        "participant",
        "--participant-label",
        subject.removeprefix("sub-"),
        "-w",
        str(work),
        "--output-spaces",
        # cloudpipe's grid: its functional preprocessing resamples onto
        # TemplateFlow's res-02 brain (2 mm, 97x115x97), stored LPS. res-native
        # would be 2.4 mm, a grid neither arm could be compared on unresampled.
        "MNI152NLin2009cAsym:res-2",
        "func",
        "--cifti-output",
        "91k",
        "--ignore",
        "fieldmaps",
        "slicetiming",
        "--skull-strip-fixed-seed",
        "--random-seed",
        str(random_seed),
        "--dummy-scans",
        str(nss),
        "--fs-license-file",
        str(license_file),
        "--nprocs",
        str(nprocs),
        "--omp-nthreads",
        str(nprocs),
        "--mem-mb",
        str(mem_mb),
        "--resource-monitor",
        "--skip-bids-validation",
        "--notrack",
    ]
    return cmd


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


def cgroup_memory_peak_bytes() -> int | None:
    """The container's peak memory (cgroup v2 memory.peak), not RSS."""
    try:
        return int(Path("/sys/fs/cgroup/memory.peak").read_text().strip())
    except (OSError, ValueError):
        return None


def tree_bytes(root: Path) -> int:
    """Bytes under `root`, counting each file once and not following symlinks."""
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            total += st.st_size
    return total


def node_timings(work: Path) -> list[dict]:
    """Per-node runtime from the nipype results in fMRIPrep's working directory.

    One row per executed node: its path in the workflow graph, start, duration,
    and with --resource-monitor its peak memory. Read after the run, so a restart
    shows which nodes reran (D8's measurement) by their start times.
    """
    from nipype.utils.filemanip import loadpkl

    rows = []
    for result in work.rglob("result_*.pklz"):
        try:
            res = loadpkl(result)
        except Exception as exc:  # noqa: BLE001 - one unreadable result is not fatal
            print(f"  unreadable {result}: {type(exc).__name__}: {exc}", flush=True)
            continue
        rt = getattr(res, "runtime", None)
        if isinstance(rt, list):  # a MapNode's result holds one runtime per item
            rt = rt[0] if rt else None
        if rt is None:
            continue
        rows.append(
            {
                "node": str(result.parent.relative_to(work)),
                "start": getattr(rt, "startTime", None),
                "duration_s": getattr(rt, "duration", None),
                "mem_peak_gb": getattr(rt, "mem_peak_gb", None),
                "cpu_percent": getattr(rt, "cpu_percent", None),
            }
        )
    return sorted(rows, key=lambda r: r["start"] or "")


# ---------------------------------------------------------------------------
# Running fMRIPrep
# ---------------------------------------------------------------------------

#: How long fMRIPrep may keep running after it writes a crash file. It runs with
#: nipype's stop_on_first_crash, so a crash should end the run within seconds.
#: The first smoke run (2026-10-28) crashed in its anatomical workflow, and the
#: exception raised inside the process pool's callback left fMRIPrep alive and
#: idle for 8.5 hours with nothing more to do.
CRASH_GRACE_S = 600
POLL_S = 30
EXIT_HUNG_AFTER_CRASH = 70  # EX_SOFTWARE: not in any retry expression


def new_crash_files(out: Path, since: float) -> list[Path]:
    """nipype crash files written under `out` after `since` (this attempt's start).

    An earlier attempt's crash file stays in the log directory on the volume,
    and must not end a run that has resumed past it.
    """
    if not out.exists():
        return []
    return sorted(p for p in out.rglob("crash-*.txt") if p.stat().st_mtime >= since)


def run_fmriprep(
    cmd: list[str], out: Path, *, grace_s: float = CRASH_GRACE_S, poll_s: float = POLL_S
) -> tuple[int, Path | None]:
    """Run fMRIPrep; return (exit code, the crash file if it hung after crashing).

    A run that writes a crash file and is still alive `grace_s` later is killed
    and reported with EXIT_HUNG_AFTER_CRASH, rather than holding its node until
    the workflow's deadline.
    """
    since = time.time()
    proc = subprocess.Popen(cmd)
    crashed_at: float | None = None
    while True:
        try:
            return proc.wait(timeout=poll_s), None
        except subprocess.TimeoutExpired:
            pass
        crashes = new_crash_files(out, since)
        if not crashes:
            continue
        crashed_at = crashed_at or time.time()
        if time.time() - crashed_at < grace_s:
            continue
        print(
            f"fMRIPrep wrote {crashes[0]} and is still running {grace_s:.0f} s later; killing it",
            file=sys.stderr,
            flush=True,
        )
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return EXIT_HUNG_AFTER_CRASH, crashes[0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    from step_outcome import write_outcome

    step = f"fmriprep-{args.mode}"
    root = Path(args.work)
    bids, out, wd = root / "bids", root / "out", root / "wd"
    publish = root / PUBLISH_DIR
    args.outcomes.mkdir(parents=True, exist_ok=True)
    args.report_dir.mkdir(parents=True, exist_ok=True)

    def outcome(status: str, reason: str = "", exit_code: object = None) -> None:
        write_outcome(
            args.outcomes,
            workflow_name=args.workflow_name,
            workflow_uid=args.workflow_uid,
            subject=args.subject,
            session=args.session,
            step=step,
            task="na",
            run="na",
            status=status,
            failure_reason=reason,
            exit_code=exit_code,
            pipeline="fmriprep",
        )

    started = time.time()
    report: dict = {
        "subject": args.subject,
        "session": args.session,
        "mode": args.mode,
        "fmriprep_version": FMRIPREP_VERSION,
        "image": args.image,
        "node_name": os.environ.get("NODE_NAME", ""),
        "pod_name": os.environ.get("POD_NAME", ""),
        "workflow_name": args.workflow_name,
        "workflow_uid": args.workflow_uid,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
    }
    if (publish / COMPLETE_MARKER).is_file():
        # A retry after the run had already passed its gates (the wait container
        # died during upload, say). Running fMRIPrep again would only add cost.
        print(f"{publish} is already complete; re-recording the outcome only", flush=True)
        outcome("succeeded")
        return 0
    try:
        nss = nss_frames(args.nss_csv, args.subject, args.session)
        stage_bids(args.staged, bids, args.subject, args.session)
        cmd = fmriprep_command(
            mode=args.mode,
            bids=bids,
            out=out,
            work=wd,
            subject=args.subject,
            license_file=args.license,
            nss=nss,
            random_seed=args.random_seed,
            nprocs=args.nprocs,
            mem_mb=args.mem_mb,
        )
    except BenchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        outcome("failed", f"missing input: {exc}")
        return 66

    report["nss_frames"] = nss
    report["command"] = cmd
    # A restart resumes from here: the working directory already holds the
    # finished nodes, which nipype skips. Counted per attempt in the report.
    report["resumed"] = wd.exists() and any(wd.iterdir())
    print("Running: " + " ".join(cmd), flush=True)
    t0 = time.time()
    rc, hung_crash = run_fmriprep(cmd, out)
    report["fmriprep_wall_s"] = round(time.time() - t0, 1)
    report["fmriprep_exit_code"] = rc
    report["hung_after_crash"] = str(hung_crash) if hung_crash else None
    report["memory_peak_bytes"] = cgroup_memory_peak_bytes()
    report["work_bytes"] = tree_bytes(wd) if wd.exists() else 0
    report["out_bytes"] = tree_bytes(out) if out.exists() else 0
    try:
        report["node_timings"] = node_timings(wd)
    except Exception as exc:  # noqa: BLE001 - timings are a measurement, not the run
        report["node_timings_error"] = f"{type(exc).__name__}: {exc}"

    status, reason, code = "succeeded", "", 0
    if hung_crash is not None:
        status, reason, code = "failed", f"fmriprep crashed and hung: {hung_crash}", rc
    elif rc != 0:
        status, reason, code = "failed", f"fmriprep exited {rc}", rc
    else:
        from provenance import ReportParseError, read_provenance

        # Provenance only: the report's `FreeSurfer reconstruction:` line is
        # recorded, not gated on (design D19). An unreadable report still fails
        # the run, since fMRIPrep writes both reportlets for every session it
        # completes.
        try:
            prov = read_provenance(
                out,
                args.subject,
                configured_command_line=" ".join(cmd),
                configured_version=FMRIPREP_VERSION,
            )
            report["freesurfer_source"] = prov.summary.freesurfer_source
            report["output_spaces"] = list(prov.summary.output_spaces)
            report["n_functional_series"] = prov.summary.n_functional_series
            report["deviations"] = [str(d) for d in prov.deviations]
        except ReportParseError as exc:
            status, reason, code = "failed", f"RuntimeError: {exc}", EXIT_REPORT_INVALID

    report["status"] = status
    report["failure_reason"] = reason
    report["total_wall_s"] = round(time.time() - started, 1)
    attempt = os.environ.get("POD_NAME", "pod")
    report_json = json.dumps(report, indent=1, default=str)
    (
        args.report_dir / f"{args.subject}_{args.session}_{args.mode}_{attempt}_bench.json"
    ).write_text(report_json)
    if status == "succeeded":
        # Last: only a run that passed every gate is published, and the marker
        # inside it is written after the move (the template uploads publish/).
        out.rename(publish)
        (publish / COMPLETE_MARKER).write_text(report_json)
    outcome(status, reason, code or None)
    if reason:
        print(f"ERROR: {reason}", file=sys.stderr, flush=True)
    return code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--subject", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--mode", required=True, choices=MODES)
    p.add_argument(
        "--staged", type=Path, required=True, help="mmps_mproc/{subj}/{ses}/ as downloaded"
    )
    p.add_argument("--nss-csv", type=Path, required=True)
    p.add_argument("--license", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True, help="the volume that outlives the pod")
    p.add_argument("--outcomes", type=Path, required=True)
    p.add_argument("--report-dir", type=Path, required=True)
    p.add_argument("--random-seed", type=int, required=True)
    p.add_argument("--nprocs", type=int, required=True)
    p.add_argument("--mem-mb", type=int, required=True)
    p.add_argument("--workflow-name", required=True)
    p.add_argument("--workflow-uid", required=True)
    p.add_argument("--image", default="", help="the image reference, recorded with every figure")
    return p


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
