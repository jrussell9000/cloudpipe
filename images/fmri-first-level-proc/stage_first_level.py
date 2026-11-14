"""stage_first_level.py — deploy fmri-first-level-proc on one func session (OpenSpec
change first-level-direct-inputs, D12).

Cloudpipe stops at derivatives: everything that modifies data for first-level lives
in the fmri-first-level-proc repo, behind its `fmri-first-level-abcd` CLI. This runner
only connects that CLI to the bucket.

  list-sessions  print the subject's complete func sessions, as the JSON list the
                 template fans out over; with FIRST_LEVEL_MISSING_ONLY=true, only those
                 with no first-level _complete.json yet
  run            download one session from derivatives/func/, call the CLI on the
                 local copy, publish its outputs with `_complete.json` last (ADR 017)

Exit codes: 0 published (some analyses may be recorded `failed` or skipped); 1 a
session failure, nothing published; 75 an S3 error (EX_TEMPFAIL, in the template's
retry expression).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import anat_qc_rejects
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fs_derivatives import publish

MARKER = "_complete.json"
FUNC_PREFIX = "derivatives/func"
OUT_PREFIX = "derivatives/first_levels"
# A session-scoped reprocess (scripts/reprocess_t1w_mni.py, #1105) flushes only the
# sessions it replaced, and the subject's workflow must not rerun the rest. An ENV
# VAR, not a flag, on purpose: the template sets it before CI pins the image that
# reads it, and an older image ignores an env var but would reject an unknown flag.
MISSING_ONLY_ENV = "FIRST_LEVEL_MISSING_ONLY"
SPACE = "space-MNI152NLin2009cAsym"
# Built by fmri-first-level-abcd-validity, keyed by subject ID, so private to the bucket.
# Only first-level reads it, so config_objects' deployment preflight rightly doesn't.
RUN_VALIDITY_KEY = "config/first_level_run_validity.csv"
# The atlas ships with fmri-first-level-proc (4.0.0+cp.4), which uses it by default, so the
# runner passes no --atlas.
TEMPFAIL = 75
SCHEMA_VERSION = 1
CGROUP_MEMORY_PEAK = Path("/sys/fs/cgroup/memory.peak")
CGROUP_MEMORY_STAT = Path("/sys/fs/cgroup/memory.stat")
ANON_SAMPLE_SECONDS = 1.0


class SessionError(RuntimeError):
    """The session cannot be run; nothing is published."""


def log(event: str, **fields) -> None:
    print(f"[first-level] {json.dumps({'event': event, **fields}, default=str)}", flush=True)


def run_stager(cmd: list[str]) -> int:
    """Run fmri-first-level-abcd; its exit code. Output streams to the pod log.
    Module-level so tests can replace it."""
    return subprocess.run(cmd).returncode


def afni_version() -> str | None:
    path = Path("/opt/afni/AFNI_version.txt")
    return path.read_text().splitlines()[0] if path.is_file() else None


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def memory_peak_bytes() -> int | None:
    """The container's high-water mark, every AFNI child included. None outside a
    cgroup v2 container. It includes page cache, so it reads the limit on most sessions
    and cannot size the request alone (design D16)."""
    try:
        return int(CGROUP_MEMORY_PEAK.read_text().strip())
    except (OSError, ValueError):
        return None


def anon_bytes() -> int | None:
    """The container's current `anon`: memory programs allocated, without page cache."""
    try:
        for line in CGROUP_MEMORY_STAT.read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "anon":
                return int(value)
    except (OSError, ValueError):
        pass
    return None


class AnonPeak:
    """Sample `anon` on a daemon thread and keep the maximum (design D16). The kernel
    keeps no peak for it. A spike shorter than the interval can be missed, so size the
    request with a margin. `peak` is None outside a cgroup v2 container."""

    def __init__(self, interval: float = ANON_SAMPLE_SECONDS):
        self.interval = interval
        self.peak: int | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _read(self) -> None:
        value = anon_bytes()
        if value is not None and (self.peak is None or value > self.peak):
            self.peak = value

    def _sample(self) -> None:
        while not self._stop.wait(self.interval):
            self._read()

    def __enter__(self) -> AnonPeak:
        self._read()
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join()
        self._read()


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def read_marker(s3, bucket: str, subj: str, ses: str) -> tuple[bytes, str]:
    try:
        obj = s3.get_object(Bucket=bucket, Key=f"{FUNC_PREFIX}/{subj}/{ses}/{MARKER}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            raise SessionError(f"no func {MARKER} for {subj} {ses}") from exc
        raise
    return obj["Body"].read(), obj["ETag"].strip('"')


def session_objects(marker: dict, subj: str, ses: str) -> list[str]:
    """The names to download, chosen by the func marker's run list and nothing else.
    No tissue masks: since fork 4.0.0+cp.10, rest's WM and CSF regressors are the
    confounds TSV's `white_matter` and `csf` columns, which func preproc computes from
    those masks (add-tissue-confound-columns); a TSV without them skips rest as
    `no-tissue-confounds`."""
    names = []
    rids = sorted(marker.get("runs") or {})
    for rid in rids:
        stem = f"{subj}_{ses}_{rid}"
        names += [
            f"{stem}_{SPACE}_bold.nii.gz",
            f"{stem}_{SPACE}_brainmask.nii.gz",
            f"{stem}_desc-confounds_timeseries.tsv",
        ]
        if rid.startswith("task-nback_"):
            names.append(f"{stem}_events.tsv")
    return names


def download_session(s3, bucket: str, subj: str, ses: str, raw: Path) -> str:
    """Copy the session into `raw`; returns the func marker's ETag. An S3 error
    propagates: it is retryable (exit 75), not a bad session."""
    body, etag = read_marker(s3, bucket, subj, ses)
    raw.mkdir(parents=True, exist_ok=True)
    (raw / MARKER).write_bytes(body)
    for name in session_objects(json.loads(body), subj, ses):
        s3.download_file(bucket, f"{FUNC_PREFIX}/{subj}/{ses}/{name}", str(raw / name))
    return etag


def download_run_validity(s3, bucket: str, dest: Path) -> None:
    try:
        s3.download_file(bucket, RUN_VALIDITY_KEY, str(dest))
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            raise SessionError(
                f"no s3://{bucket}/{RUN_VALIDITY_KEY}: build it with fmri-first-level-abcd-validity"
            ) from exc
        raise


def run_session(s3, bucket: str, subj: str, ses: str, work: Path) -> tuple[Path, dict]:
    """Download, stage and run; returns (output tree, the marker's `extra`)."""
    raw, out_root, result = work / "raw", work / "out", work / "result.json"
    etag = download_session(s3, bucket, subj, ses, raw)
    validity = work / "first_level_run_validity.csv"
    download_run_validity(s3, bucket, validity)
    cmd = [
        "fmri-first-level-abcd",
        "--subj", subj,
        "--ses", ses,
        "--input-dir", str(raw),
        "--workdir", str(work),
        "--out-dir", str(out_root),
        "--result", str(result),
        "--run-validity", str(validity),
    ]  # fmt: skip
    with AnonPeak() as anon:
        code = run_stager(cmd)
    if code != 0:
        raise SessionError(f"fmri-first-level-abcd exited {code}")
    return out_root, {
        "kind": "first-level-session",
        "first_level_schema_version": SCHEMA_VERSION,
        "afni_version": afni_version(),
        "input_func_etag": etag,
        "memory_peak_bytes": memory_peak_bytes(),  # read after the analyses, before publish
        "memory_anon_peak_bytes": anon.peak,
        **json.loads(result.read_text()),
    }


# ---------------------------------------------------------------------------
# list-sessions
# ---------------------------------------------------------------------------


def _has_marker(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise
    return True


def list_sessions(s3, bucket: str, subj: str, missing_only: bool = False) -> list[dict]:
    """The subject's complete func sessions; with `missing_only`, only the ones that
    have no first-level `_complete.json` (a half-published tree counts as missing).

    A session on the anatomical-QC reject list (#1205) is left out: its cortical
    surfaces collapsed, so its func output is not fitted. A missing list fails the subject
    rather than fitting every session."""
    try:
        rejected = anat_qc_rejects.load(s3, bucket)
    except anat_qc_rejects.MissingList as exc:
        raise SessionError(str(exc)) from exc
    sessions = []
    prefix = f"{FUNC_PREFIX}/{subj}/"
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
    for cp in resp.get("CommonPrefixes", []):
        ses = cp["Prefix"][len(prefix) :].rstrip("/")
        try:
            s3.head_object(Bucket=bucket, Key=f"{prefix}{ses}/{MARKER}")
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                continue  # half-published: skipped, never inferred from a listing
            raise
        if (subj, ses) in rejected:
            log("anat-qc-rejected", subj=subj, ses=ses, list=anat_qc_rejects.KEY)
            continue
        if missing_only and _has_marker(s3, bucket, f"{OUT_PREFIX}/{subj}/{ses}/{MARKER}"):
            continue
        sessions.append({"ses": ses})
    return sessions


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list-sessions")
    ls.add_argument("--subj", required=True)
    ls.add_argument("--bucket", required=True)
    ls.add_argument("--out", type=Path, help="also write the JSON list here (Argo output)")
    rn = sub.add_parser("run")
    rn.add_argument("--subj", required=True)
    rn.add_argument("--ses", required=True)
    rn.add_argument("--bucket", required=True)
    rn.add_argument("--workdir", type=Path, default=Path("/scratch/first-level"))
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # The subjects file and older submissions carry bare IDs; S3 keys use sub-.
    args.subj = args.subj if args.subj.startswith("sub-") else f"sub-{args.subj}"
    s3 = boto3.client("s3")

    try:
        if args.cmd == "list-sessions":
            missing_only = os.environ.get(MISSING_ONLY_ENV, "false").strip().lower() == "true"
            sessions = list_sessions(s3, args.bucket, args.subj, missing_only=missing_only)
            text = json.dumps(sessions)
            if args.out:
                args.out.write_text(text)
            print(text)
            log("sessions", subj=args.subj, sessions=sessions, missing_only=missing_only)
            return 0

        out_root, extra = run_session(s3, args.bucket, args.subj, args.ses, args.workdir)
        publish(
            out_root,
            args.bucket,
            f"{OUT_PREFIX}/{args.subj}/{args.ses}",
            source_step="first-level",
            extra=extra,
        )
        log("published", subj=args.subj, ses=args.ses, analyses=extra["analyses"])
        return 0
    except (ClientError, BotoCoreError) as exc:
        log("s3-error", error=f"{type(exc).__name__}: {exc}")
        return TEMPFAIL
    except SessionError as exc:
        log("session-failed", error=str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
