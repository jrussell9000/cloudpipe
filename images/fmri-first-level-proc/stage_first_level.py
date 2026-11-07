"""stage_first_level.py — deploy fmri-first-level-proc on one func session (OpenSpec
change first-level-direct-inputs, D12).

Cloudpipe stops at derivatives: everything that modifies data for first-level lives
in the fmri-first-level-proc repo, behind its `fmri-first-level-abcd` CLI. This runner
only connects that CLI to the bucket.

  list-sessions  print the subject's complete func sessions, as the JSON list the
                 template fans out over
  run            download one session from derivatives/func/, call the CLI on the
                 local copy, publish its outputs with `_complete.json` last (ADR 017)

Exit codes: 0 published (some analyses may be recorded `failed` or skipped); 1 a
session failure, nothing published; 75 an S3 error (EX_TEMPFAIL, in the template's
retry expression).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fs_derivatives import publish

MARKER = "_complete.json"
FUNC_PREFIX = "derivatives/func"
OUT_PREFIX = "derivatives/first_levels"
SPACE = "space-MNI152NLin2009cAsym"
SPACE_RES1 = f"{SPACE}_res-1"
# Rest's WM and CSF regressors come from FastSurfer's 1 mm label masks, as the
# orchestrator's do (fmri-first-level-proc 4.0.0+cp.2).
TISSUES = ("WM", "CSF")
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
    The tissue masks are needed only where the session has rest runs."""
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
    if any(r.startswith("task-rest_") for r in rids):
        names += [f"{subj}_{ses}_{SPACE_RES1}_label-{t}_mask.nii.gz" for t in TISSUES]
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


def list_sessions(s3, bucket: str, subj: str) -> list[dict]:
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
            sessions = list_sessions(s3, args.bucket, args.subj)
            text = json.dumps(sessions)
            if args.out:
                args.out.write_text(text)
            print(text)
            log("sessions", subj=args.subj, sessions=sessions)
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
