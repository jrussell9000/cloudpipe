#!/usr/bin/env python3
"""tissue_confounds_backfill.py: add `csf` and `white_matter` to one shard's published TSVs.

Published confounds TSVs predate the two columns (add-tissue-confound-columns). This
pod computes them from the published MNI BOLD, brain mask and session tissue maps,
with the same module the func session pod uses (images/shared/confounds_tissue.py),
so a backfilled TSV carries the bytes a fresh run would. Then it patches the run's
FuncQC record in place to schema 1.5 (D7, operator 2026-11-08). Runs in the afni
image, in-cluster.

The plan (scripts/backfill_tissue_confounds.py plan) is a JSON document in the data
bucket:

  config/tissue-backfill/{label}/sessions.json
  {"label": ..., "sessions": [{"subject": "sub-X", "session": "ses-00A",
                               "records": {"task-rest_run-01": "metrics/func-preproc/dt=.../..._qc.json"}}]}

This pod takes every `--shards`-th session from `--shard` on. A session's runs are the
ones its func `_complete.json` lists with a confounds TSV, BOLD and brain mask (the
marker is the only existence test, ADR 017). The maps are used only if the marker's
`session_files` lists them; otherwise the columns are n/a (`no-tissue-maps`).

Per run, one outcome line (`[tissue-backfill] {json}`, and appended to
config/tissue-backfill/{label}/{mode}/shard-NNNN.jsonl), with `tsv` and `record` parts:

  apply     tsv: rewritten (original first to .../originals/{subj}/{ses}/{name}, then
                 a PUT conditional on the ETag read) | already (both columns present;
                 nothing written) | raced | misplaced | bad-tsv | missing
            record: patched (IfMatch on its ETag, schema 1.5) | already (1.5, same
                 fields) | raced | missing | skipped (the TSV was not done)
  verify    tsv: present | absent | misplaced | bad-tsv | missing, and record: current
            | stale | missing | inconsistent (its status disagrees with the columns).
            Reads no BOLD; writes nothing.
  recompute the full computation on the published files, compared with the TSV's
            columns: equal | differs | absent. Writes nothing.
  restore   tsv: restored (only where the TSV differs from its backup in nothing but
            the two columns; conditional) | skipped. record: restored (back to 1.4
            without the three fields, conditional) | skipped.

The TSV goes first and the record last, so a 1.5 record means its TSV is done. A pod
preempted between the two leaves the TSV `present` and the record 1.4. A retry reads
the TSV as `already` and patches the record; the counts need only the masks, not the
BOLD. The func marker is never rewritten: it records names, not ETags
(images/shared/func_derivatives.py), and first-level's staleness test is the marker's
own ETag. An S3 error other than a lost precondition exits 75 (EX_TEMPFAIL) for a
retry. A retried shard resumes after the sessions an earlier attempt of the same
workflow finished.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import confounds_tissue as ct  # noqa: E402

MARKER = "_complete.json"
TSV_SUFFIX = "_desc-confounds_timeseries.tsv"
BOLD_SUFFIX = f"_{ct.SPACE}_bold.nii.gz"
MASK_SUFFIX = f"_{ct.SPACE}_brainmask.nii.gz"
TEMPFAIL = 75
MODES = ("apply", "verify", "recompute", "restore")
SCHEMA = "1.5"
PREVIOUS_SCHEMA = "1.4"
QC_FIELDS = ("wm_mask_voxels", "csf_mask_voxels", "tissue_confounds")
ABSENT = ("NoSuchKey", "404")
LOST_RACE = ("PreconditionFailed", "412", "ConditionalRequestConflict", "409")


def log(event: dict) -> None:
    print(f"[tissue-backfill] {json.dumps(event)}", flush=True)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def label_root(label: str) -> str:
    return f"config/tissue-backfill/{label}"


def sessions_key(label: str) -> str:
    return f"{label_root(label)}/sessions.json"


def outcomes_key(label: str, mode: str, shard: int) -> str:
    return f"{label_root(label)}/{mode}/shard-{shard:04d}.jsonl"


def backup_key(label: str, subj: str, ses: str, name: str) -> str:
    return f"{label_root(label)}/originals/{subj}/{ses}/{name}"


def label_name(subj: str, ses: str, tissue: str) -> str:
    return f"{subj}_{ses}_{ct.SPACE}_res-1_label-{tissue}_mask.nii.gz"


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def get(s3, bucket: str, key: str) -> tuple[bytes, str] | None:
    """(body, ETag), or None if there is no such object."""
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if _code(exc) in ABSENT:
            return None
        raise
    return obj["Body"].read(), obj["ETag"]


def conditional_put(s3, bucket: str, key: str, body: bytes, etag: str) -> dict | None:
    """put_object(IfMatch=etag); None if the object changed since that ETag was read."""
    try:
        return s3.put_object(Bucket=bucket, Key=key, Body=body, IfMatch=etag)
    except ClientError as exc:
        if _code(exc) in LOST_RACE:
            return None
        raise


def download(s3, bucket: str, key: str, path: Path) -> Path:
    s3.download_file(bucket, key, str(path))
    return path


def runs_of(marker: dict, subj: str, ses: str) -> list[str]:
    """Run ids the marker lists with a TSV, an MNI BOLD and a brain mask."""
    out = []
    for run_id, files in sorted((marker.get("runs") or {}).items()):
        stem = f"{subj}_{ses}_{run_id}"
        if {f"{stem}{TSV_SUFFIX}", f"{stem}{BOLD_SUFFIX}", f"{stem}{MASK_SUFFIX}"} <= set(files):
            out.append(run_id)
    return out


def patched(record: dict, fields: dict) -> dict:
    """The record at 1.5 with the three fields, key order kept (new keys before the
    runtime block, as func_iqm emits them, when absent)."""
    if all(k in record for k in QC_FIELDS):
        return {**record, **fields, "schema_version": SCHEMA}
    out = {}
    for key, value in record.items():
        if key == "stage_timings_s":
            out.update(fields)
        out[key] = value
    out.update({k: v for k, v in fields.items() if k not in out})
    out["schema_version"] = SCHEMA
    return out


def unpatched(record: dict) -> dict:
    return {
        **{k: v for k, v in record.items() if k not in QC_FIELDS},
        "schema_version": PREVIOUS_SCHEMA,
    }


class Session:
    """One session's shared inputs: its marker, its maps, and a scratch directory."""

    def __init__(self, s3, args, label: str, entry: dict):
        self.s3, self.args, self.label = s3, args, label
        self.subj, self.ses = entry["subject"], entry["session"]
        self.records = entry.get("records") or {}
        self.prefix = f"derivatives/func/{self.subj}/{self.ses}"
        self.work = Path(args.workdir) / f"{self.subj}_{self.ses}"
        self.marker = None
        self.labels = None
        self.labels_error = ""

    def load(self) -> bool:
        found = get(self.s3, self.args.data_bucket, f"{self.prefix}/{MARKER}")
        marker = json.loads(found[0]) if found else None
        if not isinstance(marker, dict) or marker.get("kind") != "func-session":
            return False
        self.marker = marker
        return True

    def need_labels(self) -> None:
        """Download and resample the maps once per session, on first need."""
        if self.labels is not None or self.labels_error or not self.has_maps():
            return
        self.work.mkdir(parents=True, exist_ok=True)
        try:
            self.labels = {
                c: ct.load_label(
                    download(
                        self.s3, self.args.data_bucket,
                        f"{self.prefix}/{label_name(self.subj, self.ses, t)}",
                        self.work / label_name(self.subj, self.ses, t),
                    )
                )
                for c, t in ct.COLUMNS
            }  # fmt: skip
        except (BotoCoreError, ClientError):
            raise
        except Exception as exc:  # noqa: BLE001 -- every run's columns are then `error`
            traceback.print_exc()
            self.labels_error = f"{type(exc).__name__}: {exc}"

    def has_maps(self) -> bool:
        listed = set(self.marker.get("session_files") or [])
        return all(label_name(self.subj, self.ses, t) in listed for _, t in ct.COLUMNS)

    def fetch(self, run_id: str, suffix: str) -> Path:
        name = f"{self.subj}_{self.ses}_{run_id}{suffix}"
        self.work.mkdir(parents=True, exist_ok=True)
        return download(self.s3, self.args.data_bucket, f"{self.prefix}/{name}", self.work / name)

    def columns(self, run_id: str, n_frames: int, *, with_bold: bool) -> ct.RunColumns:
        """The run's columns (with_bold) or only its masks' counts and status."""
        if not self.has_maps():
            return ct.unavailable(n_frames, ct.NO_MAPS)
        self.need_labels()
        if self.labels_error:
            return ct.unavailable(n_frames, ct.ERROR, self.labels_error)
        brain_path = self.fetch(run_id, MASK_SUFFIX)
        try:
            if with_bold:
                bold = self.fetch(run_id, BOLD_SUFFIX)
                try:
                    return ct.compute(bold, brain_path, self.labels)
                finally:
                    bold.unlink(missing_ok=True)
            brain = ct.load_brainmask(brain_path)
            cols = ct.RunColumns(n_frames=n_frames, status=ct.OK)
            for column, _ in ct.COLUMNS:
                cols.voxels[column] = int((self.labels[column] & brain).sum())
                if cols.voxels[column] == 0:
                    cols.status = ct.EMPTY
            return cols
        except (BotoCoreError, ClientError):
            raise
        except Exception as exc:  # noqa: BLE001 -- recorded as `error`, n/a columns
            traceback.print_exc()
            return ct.unavailable(n_frames, ct.ERROR, f"{type(exc).__name__}: {exc}")
        finally:
            brain_path.unlink(missing_ok=True)

    def cleanup(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    # ------------------------------------------------------------------ modes

    def tsv_key(self, run_id: str) -> tuple[str, str]:
        name = f"{self.subj}_{self.ses}_{run_id}{TSV_SUFFIX}"
        return name, f"{self.prefix}/{name}"

    def apply(self, run_id: str) -> dict:
        name, key = self.tsv_key(run_id)
        found = get(self.s3, self.args.data_bucket, key)
        if found is None:
            return {"tsv": "missing", "record": "skipped"}
        body, etag = found
        try:
            text = body.decode()
            st = ct.state(text)
        except (UnicodeDecodeError, ct.ConfoundsFormatError) as exc:
            return {"tsv": "bad-tsv", "record": "skipped", "error": str(exc)[:200]}
        if st == "misplaced":
            return {"tsv": "misplaced", "record": "skipped", "etag": etag}
        out: dict = {}
        if st == "present":
            # Done by an earlier attempt or a fresh func run: only the record may be left.
            cols = self.columns(run_id, ct.n_frames(text), with_bold=False)
            if cols.status != ct.ERROR:
                cols.status = self._status_from_text(text, cols)
            out["tsv"] = "already"
        else:
            cols = self.columns(run_id, ct.n_frames(text), with_bold=True)
            try:
                new_text = ct.add_columns(text, cols)
            except ct.ConfoundsFormatError as exc:
                return {"tsv": "bad-tsv", "record": "skipped", "error": str(exc)[:200]}
            backup = backup_key(self.label, self.subj, self.ses, name)
            # The original first: the data bucket keeps no versions, so this is the undo.
            self.s3.put_object(Bucket=self.args.data_bucket, Key=backup, Body=body)
            resp = conditional_put(self.s3, self.args.data_bucket, key, new_text.encode(), etag)
            if resp is None:
                return {"tsv": "raced", "record": "skipped", "old_etag": etag}
            out.update(tsv="rewritten", old_etag=etag, new_etag=resp.get("ETag"), backup_key=backup)
        out.update(cols.qc_fields())
        if cols.error:
            out["error"] = cols.error[:300]
        out["record"] = self.patch_record(run_id, cols.qc_fields())
        return out

    def _status_from_text(self, text: str, cols: ct.RunColumns) -> str:
        """For a TSV already done: the status its columns show, given what the masks say."""
        shown = ct.status_of(text)
        if shown == "unavailable":
            return ct.NO_MAPS if not self.has_maps() else ct.ERROR
        return shown

    def patch_record(self, run_id: str, fields: dict) -> str:
        key = self.records.get(run_id)
        found = get(self.s3, self.args.metrics_bucket, key) if key else None
        if found is None:
            return "missing"
        record, etag = json.loads(found[0]), found[1]
        if record.get("schema_version") == SCHEMA and all(
            record.get(k) == v for k, v in fields.items()
        ):
            return "already"
        body = json.dumps(patched(record, fields)).encode()
        resp = conditional_put(self.s3, self.args.metrics_bucket, key, body, etag)
        return "patched" if resp is not None else "raced"

    def verify(self, run_id: str) -> dict:
        _, key = self.tsv_key(run_id)
        found = get(self.s3, self.args.data_bucket, key)
        if found is None:
            return {"tsv": "missing"}
        try:
            text = found[0].decode()
            st = ct.state(text)
        except (UnicodeDecodeError, ct.ConfoundsFormatError) as exc:
            return {"tsv": "bad-tsv", "error": str(exc)[:200]}
        out = {"tsv": st}
        rkey = self.records.get(run_id)
        rec = get(self.s3, self.args.metrics_bucket, rkey) if rkey else None
        if rec is None:
            out["record"] = "missing"
            return out
        record = json.loads(rec[0])
        if record.get("schema_version") != SCHEMA or st != "present":
            out["record"] = "stale" if record.get("schema_version") != SCHEMA else "inconsistent"
            return out
        shown = ct.status_of(text)
        status = record.get("tissue_confounds")
        consistent = (
            (shown == ct.OK and status == ct.OK)
            or (shown == ct.EMPTY and status == ct.EMPTY)
            or (shown == "unavailable" and status in (ct.NO_MAPS, ct.ERROR, ct.EMPTY))
        )
        out["record"] = "current" if consistent else "inconsistent"
        out["tissue_confounds"] = status
        return out

    def recompute(self, run_id: str) -> dict:
        _, key = self.tsv_key(run_id)
        found = get(self.s3, self.args.data_bucket, key)
        if found is None:
            return {"tsv": "missing"}
        text = found[0].decode()
        if ct.state(text) != "present":
            return {"tsv": "absent"}
        cols = self.columns(run_id, ct.n_frames(text), with_bold=True)
        equal = ct.add_columns(ct.remove_columns(text), cols) == text
        return {"tsv": "equal" if equal else "differs", **cols.qc_fields()}

    def restore(self, run_id: str) -> dict:
        name, key = self.tsv_key(run_id)
        found = get(self.s3, self.args.data_bucket, key)
        original = get(
            self.s3, self.args.data_bucket, backup_key(self.label, self.subj, self.ses, name)
        )
        if found is None or original is None:
            return {"tsv": "skipped", "record": "skipped", "reason": "no TSV or no backup"}
        text, before = found[0].decode(), original[0].decode()
        ours = ct.state(text) == "present" and ct.remove_columns(text) == before
        out = {"tsv": "skipped", "record": "skipped"}
        if ours:
            resp = conditional_put(self.s3, self.args.data_bucket, key, original[0], found[1])
            out["tsv"] = "restored" if resp is not None else "raced"
        if out["tsv"] == "restored" or (not ours and text == before):
            rkey = self.records.get(run_id)
            rec = get(self.s3, self.args.metrics_bucket, rkey) if rkey else None
            if rec is not None and json.loads(rec[0]).get("schema_version") == SCHEMA:
                body = json.dumps(unpatched(json.loads(rec[0]))).encode()
                resp = conditional_put(self.s3, self.args.metrics_bucket, rkey, body, rec[1])
                out["record"] = "restored" if resp is not None else "raced"
        return out


def backfill_session(s3, args, label: str, entry: dict) -> list[dict]:
    """One outcome per run (or one `no-marker`)."""
    session = Session(s3, args, label, entry)
    try:
        if not session.load():
            return [{"status": "no-marker"}]
        lines = []
        for run_id in runs_of(session.marker, session.subj, session.ses):
            result = getattr(session, args.mode)(run_id)
            lines.append(
                {"run": run_id, "marker_written_at": session.marker.get("written_at"), **result}
            )
        return lines
    finally:
        session.cleanup()


def finished_sessions(lines: list[str], workflow_uid: str) -> set[tuple[str, str]]:
    """Sessions an earlier attempt of the workflow `workflow_uid` finished."""
    if not workflow_uid:
        return set()
    done = set()
    for raw in lines:
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if line.get("workflow_uid") == workflow_uid:
            done.add((line["subject"], line["session"]))
    return done


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--sessions-key", required=True)
    p.add_argument("--shard", type=int, required=True)
    p.add_argument("--shards", type=int, required=True)
    p.add_argument("--mode", choices=MODES, required=True)
    p.add_argument("--data-bucket", required=True)
    p.add_argument("--metrics-bucket", required=True)
    p.add_argument("--region", default=os.environ.get("AWS_REGION"))
    p.add_argument("--workdir", default="/scratch/tissue-backfill")
    p.add_argument("--workflow-uid", default=os.environ.get("WORKFLOW_UID", ""))
    args = p.parse_args(argv)
    if not 0 <= args.shard < args.shards:
        raise SystemExit(f"--shard {args.shard} is outside 0..{args.shards - 1}")
    s3 = boto3.client("s3", region_name=args.region)
    try:
        found = get(s3, args.data_bucket, args.sessions_key)
        doc = json.loads(found[0]) if found else None
        if not doc:
            raise SystemExit(f"no session list at s3://{args.data_bucket}/{args.sessions_key}")
        mine = doc["sessions"][args.shard :: args.shards]
        key = outcomes_key(doc["label"], args.mode, args.shard)
        log({"shard": args.shard, "of": args.shards, "mode": args.mode, "sessions": len(mine)})
        earlier = get(s3, args.data_bucket, key)
        lines = earlier[0].decode().splitlines() if earlier else []
        done = finished_sessions(lines, args.workflow_uid)
        if done:
            log({"shard": args.shard, "resumed": True, "sessions_already_done": len(done)})
        counts: dict[str, int] = {}
        for entry in mine:
            subj, ses = entry["subject"], entry["session"]
            if (subj, ses) in done:
                continue
            for result in backfill_session(s3, args, doc["label"], entry):
                line = {"subject": subj, "session": ses, "at": now_iso(), **result}
                if args.workflow_uid:
                    line["workflow_uid"] = args.workflow_uid
                log(line)
                lines.append(json.dumps(line))
                tag = result.get("status") or f"{result.get('tsv')}/{result.get('record', '-')}"
                counts[tag] = counts.get(tag, 0) + 1
            s3.put_object(Bucket=args.data_bucket, Key=key, Body=("\n".join(lines) + "\n").encode())
        log({"shard": args.shard, "mode": args.mode, "done": counts})
    except (BotoCoreError, ClientError) as exc:
        print(f"[tissue-backfill] S3 error, exiting {TEMPFAIL} for a retry: {exc}", flush=True)
        return TEMPFAIL
    return 0


if __name__ == "__main__":
    sys.exit(main())
